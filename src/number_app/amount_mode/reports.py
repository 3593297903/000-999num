"""Strict parent-side report gate; no checker business calculation is shared."""
import math
import re
from datetime import datetime
from pathlib import Path

from .protocol import (CHECKER_VERSION, COMMON_PROOFS, EXTRA_PROOFS, MODE,
                       canonical_json, digest, read_snapshot, strict_json)

REPORT_VERSION = 1
MAX_REPORT_BYTES = 65536


def expected_statistics(context):
    amounts = context.frozen_set_amounts
    return {"set_count": len(amounts), "raw_total": 1000 * len(amounts),
            "group_count": context.expected_group_count,
            "per_code_amount": str(sum(amounts)), "total_amount": str(1000 * sum(amounts))}


def _positive_integer(value):
    return type(value) is int and value > 0


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except ValueError:
        return False


def validate_report(report, context, execution):
    """Validate all required fields and exact types, never a PASS label alone."""
    errors = []
    if type(report) is not dict or type(execution) is not dict:
        return ["报告或进程证据不是对象"]
    method, round_no = execution.get("method"), execution.get("round")
    if type(method) is not str or method not in EXTRA_PROOFS or type(round_no) is not int or not 1 <= round_no <= 5:
        return ["方法或轮次非法"]
    identity = {
        "report_version": REPORT_VERSION, "mode": MODE, "batch_id": context.batch_id,
        "audit_id": context.audit_id, "checker_version": CHECKER_VERSION,
        "execution_id": execution.get("execution_id"), "method": method, "round": round_no,
        "provenance": context.provenance,
        "expected_request_sha256": context.expected_request_sha256,
        "expected_manifest_sha256": context.expected_manifest_sha256,
    }
    for key, expected in identity.items():
        value = report.get(key)
        if type(value) is not type(expected) or value != expected:
            errors.append("报告身份/版本/指纹不匹配：" + key)
    if context.mode != MODE or context.checker_version != CHECKER_VERSION:
        errors.append("可信上下文版本不受支持")
    if type(execution.get("execution_id")) is not str or re.fullmatch(r"[0-9a-f]{32}", execution["execution_id"]) is None:
        errors.append("执行编号非法")
    for key in ("pid", "process_created_filetime"):
        if (not _positive_integer(execution.get(key)) or not _positive_integer(report.get(key))
                or execution[key] != report[key]):
            errors.append("实际进程身份不匹配：" + key)
    if type(execution.get("exit_code")) is not int or execution["exit_code"] != 0:
        errors.append("子进程未正常退出")
    if report.get("status") != "PASS" or type(report.get("status")) is not str:
        errors.append("核查未通过")
    if type(report.get("errors")) is not list or report["errors"]:
        errors.append("错误列表非空或缺失")
    expected = expected_statistics(context)
    if type(report.get("expected")) is not dict:
        errors.append("缺少完整预期统计")
    else:
        for key, value in expected.items():
            if type(report["expected"].get(key)) is not type(value) or report["expected"][key] != value:
                errors.append("预期统计错误：" + key)
    actual = {"actual_raw_total": expected["raw_total"], "actual_group_count": expected["group_count"],
              "actual_total_amount": expected["total_amount"], "checked_codes": 1000,
              "checked_source_code_cells": expected["raw_total"], "files_read": 5}
    for key, value in actual.items():
        if type(report.get(key)) is not type(value) or report[key] != value:
            errors.append("实际统计错误、缺失或类型错误：" + key)
    proofs = report.get("proofs")
    if type(proofs) is not dict:
        errors.append("缺少证明项")
    else:
        for key in (*COMMON_PROOFS, *EXTRA_PROOFS[method]):
            if proofs.get(key) is not True:
                errors.append("必需证明项未完成：" + key)
    duration = report.get("elapsed_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        errors.append("实际耗时缺失或非法")
    if not _timestamp(report.get("started_at")) or not _timestamp(report.get("ended_at")):
        errors.append("进程执行时间不合法")
    return errors


def read_report_bytes(path):
    file = Path(path)
    if not file.is_file() or file.is_symlink() or file.is_junction():
        raise ValueError("报告缺失或为链接")
    with file.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("报告文件超限")
    return data, digest(data)


def parse_report_bytes(data):
    value = strict_json(data)
    if canonical_json(value) != data:
        raise ValueError("报告不是规范JSON")
    return value


def read_report(path):
    data, sha = read_report_bytes(path)
    return parse_report_bytes(data), sha


def validate_audit_session(batch_dir, context, checks, session_dir):
    """Re-read every actual report and current fixed files; return fail-closed."""
    errors = []
    try:
        read_snapshot(batch_dir, context)
        if type(checks) not in (list, tuple) or len(checks) != 15:
            return False, ["真实执行组合必须准确为15项"]
        root = Path(batch_dir).resolve(strict=True)
        session = Path(session_dir)
        if (session.is_symlink() or session.is_junction() or not session.is_dir()
                or session.resolve(strict=True) != root / "audits" / context.audit_id):
            return False, ["报告会话目录与当前审查不匹配"]
        pairs, execution_ids, process_instances = set(), set(), set()
        expected_names = {f"{method}_{round_no}.json" for round_no in range(1, 6) for method in "ABC"}
        actual_names = {p.name for p in session.iterdir()}
        if actual_names - {"summary.json", "summary.txt"} != expected_names:
            errors.append("实际原始报告文件集合不正确")
        for execution in checks:
            if type(execution) is not dict:
                errors.append("执行证据不是对象")
                continue
            method, round_no = execution.get("method"), execution.get("round")
            if type(method) is not str or method not in EXTRA_PROOFS or type(round_no) is not int or not 1 <= round_no <= 5:
                errors.append("方法轮次非法")
                continue
            pair = (method, round_no)
            if pair in pairs:
                errors.append("方法轮次重复")
            pairs.add(pair)
            execution_id = execution.get("execution_id")
            if type(execution_id) is not str or execution_id in execution_ids:
                errors.append("执行编号缺失或重复")
            else:
                execution_ids.add(execution_id)
            instance = (execution.get("pid"), execution.get("process_created_filetime"))
            if not all(_positive_integer(v) for v in instance) or instance in process_instances:
                errors.append("未证明每次都是新的进程实例")
            else:
                process_instances.add(instance)
            name = f"{method}_{round_no}.json"
            if execution.get("report_path") != name:
                errors.append("原始报告路径不匹配")
                continue
            try:
                report, sha = read_report(session / name)
                if type(execution.get("report_sha256")) is not str or execution["report_sha256"] != sha:
                    errors.append("原始报告在汇总前变化：" + name)
                errors.extend(name + "：" + error for error in validate_report(report, context, execution))
            except (ValueError, OSError, TypeError, KeyError) as error:
                errors.append(name + "：" + str(error))
        if pairs != {(method, round_no) for round_no in range(1, 6) for method in "ABC"}:
            errors.append("A/B/C各5遍的组合不齐全")
        read_snapshot(batch_dir, context)
        # Reconfirm the original report bytes after the potentially longer fixed
        # snapshot read, immediately before permitting a machine PASS summary.
        for execution in checks:
            if type(execution) is dict and execution.get("report_path") in expected_names:
                try:
                    _, final_sha = read_report(session / execution["report_path"])
                    if final_sha != execution.get("report_sha256"):
                        errors.append("原始报告在最终汇总前变化：" + execution["report_path"])
                except (OSError, ValueError, TypeError) as error:
                    errors.append("最终报告复读失败：" + str(error))
    except Exception as error:
        errors.append("会话/固定快照检查失败：" + str(error))
    return not errors, errors


def validate_summary(session_dir, context, checks, expected_sha256):
    """Bind a committed summary to its original bytes and execution index."""
    errors = []
    try:
        if type(expected_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            return False, ["缺少原汇总报告指纹"]
        summary, sha = read_report(Path(session_dir) / "summary.json")
        if sha != expected_sha256:
            errors.append("汇总报告指纹变化")
        if type(summary) is not dict:
            return False, ["汇总报告不是对象"]
        text, text_sha = read_report_bytes(Path(session_dir) / 'summary.txt')
        if not text or summary.get('summary_text_sha256') != text_sha:
            errors.append('文本汇总报告缺失或指纹变化')
        required = {"summary_version": 1, "mode": MODE, "batch_id": context.batch_id,
                    "audit_id": context.audit_id, "checker_version": CHECKER_VERSION,
                    "provenance": context.provenance,
                    "expected_request_sha256": context.expected_request_sha256,
                    "expected_manifest_sha256": context.expected_manifest_sha256,
                    "status": "PASS", "checks_passed": 15}
        for key, value in required.items():
            if type(summary.get(key)) is not type(value) or summary[key] != value:
                errors.append("汇总身份、状态或统计不正确：" + key)
        if type(summary.get("errors")) is not list or summary["errors"]:
            errors.append("汇总错误列表非空或缺失")
        if canonical_json(summary.get("checks")) != canonical_json(list(checks)) or len(checks) != 15:
            errors.append("汇总执行索引与原始进程证据不一致")
        if canonical_json(summary.get("expected")) != canonical_json(expected_statistics(context)):
            errors.append("汇总预期统计不正确")
        if not _timestamp(summary.get("started_at")) or not _timestamp(summary.get("ended_at")):
            errors.append("汇总时间不合法")
        duration = summary.get("elapsed_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            errors.append("汇总耗时不合法")
    except Exception as error:
        errors.append("汇总读取失败：" + str(error))
    return not errors, errors
