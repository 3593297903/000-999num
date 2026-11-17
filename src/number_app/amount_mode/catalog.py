"""Small independent SQLite index; files and original anchors stay authoritative.

Callers hold the shared OS workspace lock for every write operation. Connections
are short lived and always closed. Existing invalid/foreign indices are never
recreated or upgraded implicitly.
"""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re
import sqlite3
import uuid

from .input_config import validate_request
from .protocol import (AuditContext, CHECKER_VERSION, MODE, FIXED_FILES, canonical_json,
                       digest, read_snapshot, strict_json)
from .spec import AmountBatchSpec
from .state import GENERATION_STATES, VERIFICATION_STATES, TRANSITIONS, require_transition

APPLICATION_ID = 0x414D544C  # AMTL; old NumberTool's index has a different identity.
DATABASE_VERSION = 1001
_SETTINGS = {"backup_root", "ui_page_size", "recent_export_root", "window_geometry"}
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)


def _now():
    return datetime.now().astimezone().isoformat()


def _batch_id(value):
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise ValueError("非法批次编号")
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value, re.ASCII) is None:
        raise ValueError("可信指纹必须为SHA-256")
    return value


_SCHEMA = """
CREATE TABLE batches (
 batch_id TEXT PRIMARY KEY,
 mode TEXT NOT NULL CHECK(mode='set_amount_small_v1'),
 created_at TEXT NOT NULL,
 software_version TEXT NOT NULL,
 set_count INTEGER NOT NULL CHECK(set_count BETWEEN 1 AND 300),
 raw_total INTEGER NOT NULL,
 per_code_expected_amount TEXT NOT NULL,
 total_amount TEXT NOT NULL,
 frozen_input_json TEXT NOT NULL,
 original_request_json TEXT NOT NULL,
 request_sha256 TEXT NOT NULL,
 manifest_sha256 TEXT,
 group_count INTEGER,
 batch_relpath TEXT NOT NULL CHECK(batch_relpath='staging/'||batch_id OR batch_relpath='batches/'||batch_id),
 provenance TEXT NOT NULL CHECK(provenance='local_anchored'),
 generation_status TEXT NOT NULL,
 latest_verification_status TEXT NOT NULL DEFAULT 'UNKNOWN',
 latest_audit_id TEXT,
 backup_status TEXT NOT NULL DEFAULT 'PENDING',
 backup_error TEXT,
 error TEXT,
 updated_at TEXT NOT NULL
);
CREATE TABLE batch_sets (
 batch_id TEXT NOT NULL REFERENCES batches(batch_id),
 set_no INTEGER NOT NULL,
 amount TEXT NOT NULL,
 PRIMARY KEY(batch_id,set_no)
);
CREATE TABLE audit_runs (
 audit_id TEXT PRIMARY KEY,
 batch_id TEXT NOT NULL REFERENCES batches(batch_id),
 checker_version TEXT NOT NULL,
 provenance TEXT NOT NULL,
 request_sha256 TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL,
 group_count INTEGER NOT NULL,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 summary_relpath TEXT,
 summary_sha256 TEXT,
 errors_json TEXT NOT NULL
);
CREATE TABLE audit_checks (
 audit_id TEXT NOT NULL REFERENCES audit_runs(audit_id),
 method TEXT NOT NULL CHECK(method IN ('A','B','C')),
 round INTEGER NOT NULL CHECK(round BETWEEN 1 AND 5),
 execution_id TEXT NOT NULL,
 pid INTEGER,
 process_created_filetime TEXT,
 started_at TEXT,
 ended_at TEXT,
 exit_code INTEGER,
 report_relpath TEXT,
 report_sha256 TEXT,
 PRIMARY KEY(audit_id,method,round),
 UNIQUE(execution_id)
);
CREATE TABLE artifacts (
 batch_id TEXT NOT NULL REFERENCES batches(batch_id),
 path TEXT NOT NULL,
 size INTEGER NOT NULL,
 sha256 TEXT NOT NULL,
 PRIMARY KEY(batch_id,path)
);
CREATE TABLE backup_runs (
 backup_id TEXT PRIMARY KEY,
 batch_id TEXT NOT NULL REFERENCES batches(batch_id),
 manifest_sha256 TEXT NOT NULL,
 audit_ids_json TEXT NOT NULL,
 package_path TEXT,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 error TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX batches_created ON batches(created_at DESC,batch_id DESC);
CREATE INDEX batches_status ON batches(generation_status,created_at DESC,batch_id DESC);
CREATE INDEX audit_batch ON audit_runs(batch_id,created_at DESC,audit_id);
CREATE INDEX backup_batch ON backup_runs(batch_id,manifest_sha256,status);

CREATE TRIGGER batches_insert_guard BEFORE INSERT ON batches BEGIN
 SELECT CASE WHEN amount_create_ok()!=1 THEN RAISE(ABORT,'trusted batch creation requires coordinator') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM batches WHERE batch_id=NEW.batch_id) THEN RAISE(ABORT,'batch identity already exists') END;
 SELECT CASE WHEN NEW.generation_status!='CREATED' OR NEW.manifest_sha256 IS NOT NULL OR NEW.group_count IS NOT NULL THEN RAISE(ABORT,'new batch cannot claim frozen/completed') END;
END;
CREATE TRIGGER batches_original_immutable BEFORE UPDATE ON batches BEGIN
 SELECT CASE WHEN NEW.batch_id IS NOT OLD.batch_id OR NEW.mode IS NOT OLD.mode
 OR NEW.created_at IS NOT OLD.created_at OR NEW.software_version IS NOT OLD.software_version
 OR NEW.set_count IS NOT OLD.set_count OR NEW.raw_total IS NOT OLD.raw_total
 OR NEW.per_code_expected_amount IS NOT OLD.per_code_expected_amount OR NEW.total_amount IS NOT OLD.total_amount
 OR NEW.frozen_input_json IS NOT OLD.frozen_input_json OR NEW.original_request_json IS NOT OLD.original_request_json
 OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.provenance IS NOT OLD.provenance
 THEN RAISE(ABORT,'original input and request fingerprint are immutable') END;
 SELECT CASE WHEN OLD.manifest_sha256 IS NOT NULL AND NEW.manifest_sha256 IS NOT OLD.manifest_sha256
 THEN RAISE(ABORT,'first manifest fingerprint is immutable') END;
 SELECT CASE WHEN OLD.group_count IS NOT NULL AND NEW.group_count IS NOT OLD.group_count
 THEN RAISE(ABORT,'first group count is immutable') END;
 SELECT CASE WHEN OLD.manifest_sha256 IS NULL AND NEW.manifest_sha256 IS NOT NULL AND amount_freeze_ok()!=1
 THEN RAISE(ABORT,'first freeze requires actual files') END;
 SELECT CASE WHEN OLD.group_count IS NULL AND NEW.group_count IS NOT NULL AND amount_freeze_ok()!=1
 THEN RAISE(ABORT,'first group count requires actual files') END;
 SELECT CASE WHEN NEW.generation_status='FROZEN' AND OLD.generation_status!='FROZEN' AND amount_freeze_ok()!=1
 THEN RAISE(ABORT,'frozen state requires first freeze') END;
 SELECT CASE WHEN NEW.generation_status='COMPLETED' AND OLD.generation_status!='COMPLETED' AND amount_complete_ok()!=1
 THEN RAISE(ABORT,'completion requires 15 actual reports and published files') END;
 SELECT CASE WHEN NEW.latest_verification_status='PASS' AND
 (OLD.latest_verification_status!='PASS' OR NEW.latest_audit_id IS NOT OLD.latest_audit_id) AND amount_verify_ok()!=1
 THEN RAISE(ABORT,'verification PASS requires actual reports') END;
 SELECT CASE WHEN amount_transition_ok(OLD.generation_status,NEW.generation_status)!=1
 THEN RAISE(ABORT,'illegal generation state transition') END;
END;
CREATE TRIGGER batches_delete_guard BEFORE DELETE ON batches BEGIN
 SELECT RAISE(ABORT,'batch evidence cannot be deleted');
END;
CREATE TRIGGER sets_insert_guard BEFORE INSERT ON batch_sets BEGIN
 SELECT CASE WHEN amount_create_ok()!=1 OR EXISTS(SELECT 1 FROM batch_sets WHERE batch_id=NEW.batch_id AND set_no=NEW.set_no)
 THEN RAISE(ABORT,'source input is immutable') END;
END;
CREATE TRIGGER sets_update_guard BEFORE UPDATE ON batch_sets BEGIN SELECT RAISE(ABORT,'source input is immutable'); END;
CREATE TRIGGER sets_delete_guard BEFORE DELETE ON batch_sets BEGIN SELECT RAISE(ABORT,'source input is immutable'); END;
CREATE TRIGGER artifacts_insert_guard BEFORE INSERT ON artifacts BEGIN
 SELECT CASE WHEN amount_freeze_ok()!=1 OR EXISTS(SELECT 1 FROM artifacts WHERE batch_id=NEW.batch_id AND path=NEW.path)
 THEN RAISE(ABORT,'artifacts require first verified freeze') END;
END;
CREATE TRIGGER artifacts_update_guard BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'artifact fingerprint is immutable'); END;
CREATE TRIGGER artifacts_delete_guard BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'artifact fingerprint is immutable'); END;
CREATE TRIGGER audit_insert_guard BEFORE INSERT ON audit_runs BEGIN
 SELECT CASE WHEN amount_audit_ok()!=1 OR EXISTS(SELECT 1 FROM audit_runs WHERE audit_id=NEW.audit_id)
 THEN RAISE(ABORT,'audit evidence requires coordinator') END;
END;
CREATE TRIGGER audit_update_guard BEFORE UPDATE ON audit_runs BEGIN SELECT RAISE(ABORT,'audit evidence is immutable'); END;
CREATE TRIGGER audit_delete_guard BEFORE DELETE ON audit_runs BEGIN SELECT RAISE(ABORT,'audit evidence is immutable'); END;
CREATE TRIGGER check_insert_guard BEFORE INSERT ON audit_checks BEGIN
 SELECT CASE WHEN amount_audit_ok()!=1 OR EXISTS(SELECT 1 FROM audit_checks WHERE audit_id=NEW.audit_id AND method=NEW.method AND round=NEW.round)
 THEN RAISE(ABORT,'audit evidence requires coordinator') END;
END;
CREATE TRIGGER check_update_guard BEFORE UPDATE ON audit_checks BEGIN SELECT RAISE(ABORT,'audit evidence is immutable'); END;
CREATE TRIGGER check_delete_guard BEFORE DELETE ON audit_checks BEGIN SELECT RAISE(ABORT,'audit evidence is immutable'); END;
"""
_TABLES = {"batches", "batch_sets", "audit_runs", "audit_checks", "artifacts", "backup_runs", "settings"}
_TRIGGERS = {
    "batches_insert_guard", "batches_original_immutable", "batches_delete_guard",
    "sets_insert_guard", "sets_update_guard", "sets_delete_guard",
    "artifacts_insert_guard", "artifacts_update_guard", "artifacts_delete_guard",
    "audit_insert_guard", "audit_update_guard", "audit_delete_guard",
    "check_insert_guard", "check_update_guard", "check_delete_guard",
}


class Catalog:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=False)
        self.db_path = self.root / "history.sqlite"
        if self.root.exists() and not self.root.is_dir():
            raise ValueError("数据根必须是目录")
        if self.db_path.exists():
            self._recognize()
        else:
            for name in ("batches", "staging"):
                region = self.root / name
                if region.exists() and (not region.is_dir() or next(region.iterdir(), None) is not None):
                    raise ValueError("索引缺失但已有批次/暂存文件；保留原文件，请从可信备份恢复索引")
            self.root.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents accidental opening/overwriting of a racing file.
            with self.db_path.open("xb"):
                pass
            connection = sqlite3.connect(self.db_path, isolation_level=None)
            try:
                self._configure(connection, set())
                connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA +
                                         f"\nPRAGMA application_id={APPLICATION_ID};\nPRAGMA user_version={DATABASE_VERSION};\nCOMMIT;")
            finally:
                connection.close()
            self._recognize()

    @staticmethod
    def _configure(connection, grants):
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        for key in ("create", "freeze", "complete", "verify", "audit"):
            connection.create_function("amount_" + key + "_ok", 0, lambda key=key: int(key in grants))
        connection.create_function("amount_transition_ok", 2,
                                   lambda old, new: int(old in TRANSITIONS and (old == new or new in TRANSITIONS[old])))

    def _recognize(self):
        if self.db_path.is_symlink() or self.db_path.is_junction() or not self.db_path.is_file() or self.db_path.stat().st_size == 0:
            raise ValueError("历史索引为空、链接或非文件；未改写原索引")
        connection = None
        try:
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("不是按套金额模式历史库，禁止写入")
            if connection.execute("PRAGMA user_version").fetchone()[0] != DATABASE_VERSION:
                raise ValueError("不支持的历史库版本，禁止写入")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("历史索引完整性检查失败，保留原文件")
            objects = connection.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','trigger')").fetchall()
            if {n for t, n in objects if t == "table"} != _TABLES or {n for t, n in objects if t == "trigger"} != _TRIGGERS:
                raise ValueError("历史索引结构不完整或未知，禁止写入")
        except sqlite3.DatabaseError as error:
            raise ValueError("历史索引损坏，未重建：" + str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def connection(self, *, write=False, _grants=()):
        """Short-lived connection; public writes receive no trusted-state grant."""
        connection = sqlite3.connect(self.db_path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, isolation_level=None)
        try:
            self._configure(connection, set(_grants))
            if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0] != DATABASE_VERSION):
                raise ValueError("历史库版本改变，已停止")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("PRAGMA query_only=ON")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def create(self, spec, request_bytes):
        if type(spec) is not AmountBatchSpec or type(request_bytes) is not bytes:
            raise ValueError("必须先冻结严格输入和请求字节")
        request = strict_json(request_bytes)
        if validate_request(request) != spec or canonical_json(request) != request_bytes:
            raise ValueError("原始请求必须与冻结输入及规范字节一致")
        bid = _batch_id(request["batch_id"])
        config = canonical_json({"set_count": spec.set_count, "amounts": [str(a) for a in spec.amounts]}).decode("utf-8")
        with self.connection(write=True, _grants=("create",)) as connection:
            connection.execute("""INSERT INTO batches
              (batch_id,mode,created_at,software_version,set_count,raw_total,per_code_expected_amount,total_amount,
               frozen_input_json,original_request_json,request_sha256,batch_relpath,provenance,generation_status,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (bid, MODE, request["created_at"], request["software_version"], spec.set_count,
                                spec.raw_total, str(spec.per_code_amount), str(spec.total_amount), config,
                                request_bytes.decode("utf-8"), digest(request_bytes), "staging/" + bid,
                                "local_anchored", "CREATED", _now()))
            connection.executemany("INSERT INTO batch_sets(batch_id,set_no,amount) VALUES (?,?,?)",
                                   ((bid, i, str(a)) for i, a in enumerate(spec.amounts, 1)))
        return self.get(bid)

    def get(self, batch_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_id=?", (_batch_id(batch_id),)).fetchone()
        return dict(row) if row is not None else None

    def get_by_path(self, path):
        target = Path(path).resolve(strict=False)
        if not target.is_relative_to(self.root):
            return None
        relative = target.relative_to(self.root).as_posix()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_relpath=?", (relative,)).fetchone()
        return dict(row) if row is not None else None

    def list_page(self, offset=0, limit=50, status=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("历史分页参数错误")
        where = ""
        args = []
        if status is not None:
            if status not in GENERATION_STATES:
                raise ValueError("未知历史状态")
            where = " WHERE generation_status=?"
            args.append(status)
        # Do not retrieve original request or source amounts for a history page.
        with self.connection() as connection:
            rows = connection.execute("""SELECT batch_id,created_at,set_count,raw_total,per_code_expected_amount,
             total_amount,group_count,generation_status,latest_verification_status,latest_audit_id,
             backup_status,error,backup_error FROM batches""" + where +
                                      " ORDER BY created_at DESC,batch_id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def count(self, status=None):
        with self.connection() as connection:
            if status is None:
                return connection.execute("SELECT count(*) FROM batches").fetchone()[0]
            if status not in GENERATION_STATES:
                raise ValueError("未知历史状态")
            return connection.execute("SELECT count(*) FROM batches WHERE generation_status=?", (status,)).fetchone()[0]

    def path(self, batch_id):
        row = self.get(batch_id)
        if row is None:
            raise KeyError("不存在的批次：" + batch_id)
        relative = row["batch_relpath"]
        bid = row["batch_id"]
        if relative not in ("staging/" + bid, "batches/" + bid):
            raise ValueError("批次位置必须为当前根下的受限相对路径")
        value = self.root / relative
        resolved = value.resolve(strict=False)
        if resolved != value or not resolved.is_relative_to(self.root):
            raise ValueError("批次位置包含链接或越界")
        return resolved

    def _original(self, batch_id):
        row = self.get(batch_id)
        if row is None:
            raise KeyError("不存在的批次：" + batch_id)
        raw = row["original_request_json"].encode("utf-8")
        if digest(raw) != row["request_sha256"]:
            raise ValueError("持久原始请求或可信指纹损坏")
        request = strict_json(raw)
        spec = validate_request(request)
        if canonical_json(request) != raw or request["batch_id"] != batch_id:
            raise ValueError("持久原始请求身份错误")
        expected = canonical_json({"set_count": spec.set_count, "amounts": [str(a) for a in spec.amounts]}).decode("utf-8")
        if (row["frozen_input_json"] != expected or row["set_count"] != spec.set_count
                or row["raw_total"] != spec.raw_total or row["per_code_expected_amount"] != str(spec.per_code_amount)
                or row["total_amount"] != str(spec.total_amount)):
            raise ValueError("可信输入摘要与原请求不一致")
        with self.connection() as connection:
            sources = connection.execute("SELECT set_no,amount FROM batch_sets WHERE batch_id=? ORDER BY set_no", (batch_id,)).fetchall()
        if [(r[0], r[1]) for r in sources] != [(i, str(a)) for i, a in enumerate(spec.amounts, 1)]:
            raise ValueError("来源金额登记与原始输入不一致")
        return row, spec, raw

    def context(self, batch_id, audit_id=None):
        row, spec, _ = self._original(batch_id)
        if row["manifest_sha256"] is None or row["group_count"] is None:
            raise ValueError("尚无首次可信冻结，不得从当前文件建立恢复基准")
        return AuditContext(MODE, batch_id, _batch_id(audit_id or uuid.uuid4().hex), CHECKER_VERSION, spec.amounts,
                            _hash(row["request_sha256"]), _hash(row["manifest_sha256"]), row["group_count"], row["provenance"])

    def freeze(self, batch_id, context):
        row, spec, raw = self._original(batch_id)
        if row["generation_status"] != "WRITING" or row["manifest_sha256"] is not None or row["group_count"] is not None:
            raise ValueError("仅允许WRITING批次首次冻结一次")
        if (context.batch_id != batch_id or context.frozen_set_amounts != spec.amounts
                or context.expected_request_sha256 != row["request_sha256"] or context.provenance != "local_anchored"):
            raise ValueError("首次冻结上下文与持久原始输入不一致")
        buffers = read_snapshot(self.path(batch_id), context)
        if buffers["request.json"] != raw:
            raise ValueError("候选请求不是持久原始字节")
        plan = strict_json(buffers["group_plan.json"])
        groups = plan.get("groups") if type(plan) is dict else None
        if (type(groups) is not list or type(plan.get("group_count")) is not int
                or plan["group_count"] != context.expected_group_count or len(groups) != context.expected_group_count):
            raise ValueError("首次冻结组数与实际计划不一致")
        sizes = []
        for i, entry in enumerate(groups, 1):
            if (type(entry) is not dict or type(entry.get("group")) is not int or entry["group"] != i
                    or type(entry.get("raw_count")) is not int or not 2 <= entry["raw_count"] <= 5):
                raise ValueError("首次冻结组计划不合法")
            sizes.append(entry["raw_count"])
        if sum(sizes) != spec.raw_total:
            raise ValueError("首次冻结组计划未完整分配")
        with self.connection(write=True, _grants=("freeze",)) as connection:
            changed = connection.execute("""UPDATE batches SET manifest_sha256=?,group_count=?,generation_status='FROZEN',updated_at=?
             WHERE batch_id=? AND manifest_sha256 IS NULL AND generation_status='WRITING'""",
                                         (context.expected_manifest_sha256, context.expected_group_count, _now(), batch_id)).rowcount
            if changed != 1:
                raise ValueError("首次冻结登记竞争或重复")
            connection.executemany("INSERT INTO artifacts(batch_id,path,size,sha256) VALUES (?,?,?,?)",
                                   ((batch_id, name, len(buffers[name]), digest(buffers[name])) for name in FIXED_FILES))
        return self.get(batch_id)

    def transition(self, batch_id, new_status, *, error=None, batch_relpath=None):
        row = self.get(batch_id)
        if row is None:
            raise KeyError(batch_id)
        require_transition(row["generation_status"], new_status)
        if new_status in ("FROZEN", "COMPLETED"):
            raise ValueError("冻结/完成必须通过对应的实际证据接口")
        if new_status in ("AUDITING", "COMMITTING") and row["manifest_sha256"] is None:
            raise ValueError("缺少首次可信冻结，不能审查或提交")
        relative = row["batch_relpath"] if batch_relpath is None else batch_relpath
        if relative not in ("staging/" + batch_id, "batches/" + batch_id):
            raise ValueError("批次相对路径错误")
        with self.connection(write=True) as connection:
            connection.execute("UPDATE batches SET generation_status=?,batch_relpath=?,error=?,updated_at=? WHERE batch_id=?",
                               (new_status, relative, error, _now(), batch_id))
        return self.get(batch_id)

    def _validated_session(self, batch_id, context, checks, summary_sha256=None):
        from .reports import validate_audit_session, validate_summary
        trusted = self.context(batch_id, context.audit_id)
        if trusted != context:
            raise ValueError("审查上下文与持久原始登记不一致")
        batch = self.path(batch_id)
        session_dir = batch / "audits" / context.audit_id
        valid, errors = validate_audit_session(batch, trusted, checks, session_dir)
        if not valid:
            raise ValueError("15项实际证据不完整：" + "; ".join(errors[:10]))
        if summary_sha256 is not None:
            _hash(summary_sha256)
            valid, errors = validate_summary(session_dir, context, checks, summary_sha256)
            if not valid:
                raise ValueError("审查汇总文件不正确：" + "; ".join(errors[:10]))
        return session_dir

    def record_audit(self, batch_id, session):
        context = session.context
        trusted = self.context(batch_id, context.audit_id)
        if context != trusted:
            raise ValueError("不能混入其他输入、指纹或批次的审查")
        status = session.status
        if status not in ("PASS", "FAIL", "ERROR", "TIMEOUT", "CANCELLED"):
            raise ValueError("未知审查结果")
        if status == "PASS":
            if session.checks_passed != 15 or session.summary_sha256 is None:
                raise ValueError("PASS必须有真实15项及最终汇总")
            self._validated_session(batch_id, context, session.checks, session.summary_sha256)
        summary_relative = "audits/" + context.audit_id + "/summary.json" if session.summary_sha256 else None
        checks = tuple(session.checks)
        pairs = set()
        for check in checks:
            pair = (check.get("method"), check.get("round"))
            if pair in pairs or pair[0] not in ("A", "B", "C") or type(pair[1]) is not int or not 1 <= pair[1] <= 5:
                raise ValueError("审查索引方法/轮次重复或错误")
            pairs.add(pair)
        with self.connection(write=True, _grants=("audit", "verify") if status == "PASS" else ("audit",)) as connection:
            connection.execute("""INSERT INTO audit_runs
             (audit_id,batch_id,checker_version,provenance,request_sha256,manifest_sha256,group_count,status,created_at,
              summary_relpath,summary_sha256,errors_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (context.audit_id, batch_id, CHECKER_VERSION, context.provenance,
                                context.expected_request_sha256, context.expected_manifest_sha256, context.expected_group_count,
                                status, _now(), summary_relative, session.summary_sha256,
                                canonical_json([str(e)[:2000] for e in session.errors[:10]]).decode("utf-8")))
            for check in checks:
                method, round_no = check["method"], check["round"]
                file = check.get("report_path")
                if file is not None:
                    expected = f"{method}_{round_no}.json"
                    if Path(file).name != expected or str(file) != expected:
                        raise ValueError("审查报告路径必须为本会话固定文件名")
                    file = "audits/" + context.audit_id + "/" + expected
                created = check.get("process_created_filetime")
                connection.execute("""INSERT INTO audit_checks(audit_id,method,round,execution_id,pid,process_created_filetime,
                 started_at,ended_at,exit_code,report_relpath,report_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                   (context.audit_id, method, round_no, check["execution_id"], check.get("pid"),
                                    str(created) if created is not None else None, check.get("started_at"), check.get("ended_at"),
                                    check.get("exit_code"), file, check.get("report_sha256")))
            # A new persisted session becomes the latest result. Insertion is
            # exclusive, so a previous PASS cannot be replayed after a failed or
            # cancelled newer review to regain current availability.
            connection.execute("UPDATE batches SET latest_verification_status=?,latest_audit_id=?,updated_at=? WHERE batch_id=?",
                               (status, context.audit_id, _now(), batch_id))
        return self.audit_record(context.audit_id)

    def audit_record(self, audit_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM audit_runs WHERE audit_id=?", (_batch_id(audit_id),)).fetchone()
            if row is None:
                raise KeyError(audit_id)
            checks = connection.execute("SELECT * FROM audit_checks WHERE audit_id=? ORDER BY round,method", (audit_id,)).fetchall()
        return {**dict(row), "checks": [dict(c) for c in checks]}

    def _checks_for_validation(self, record):
        result = []
        for stored in record["checks"]:
            entry = {key: stored[key] for key in ("method", "round", "execution_id", "pid", "started_at", "ended_at", "exit_code")}
            value = stored["process_created_filetime"]
            entry["process_created_filetime"] = int(value) if value is not None else None
            entry["report_path"] = Path(stored["report_relpath"]).name if stored["report_relpath"] else None
            entry["report_sha256"] = stored["report_sha256"]
            result.append(entry)
        return result

    def mark_verification(self, batch_id, status, audit_id=None, error=None):
        if status not in VERIFICATION_STATES:
            raise ValueError("未知最近复核状态")
        grants = ()
        if status == "PASS":
            row = self.get(batch_id)
            if row is None or row["latest_audit_id"] != audit_id or row["latest_verification_status"] not in ("PASS", "RUNNING"):
                raise ValueError("旧PASS会话不能替代最近未通过的复核；必须重新完整15次")
            record = self.audit_record(audit_id)
            if record["batch_id"] != batch_id or record["status"] != "PASS":
                raise ValueError("最近复核不能使用其他批次或失败记录")
            context = self.context(batch_id, audit_id)
            self._validated_session(batch_id, context, self._checks_for_validation(record), record["summary_sha256"])
            grants = ("verify",)
        with self.connection(write=True, _grants=grants) as connection:
            changed = connection.execute("UPDATE batches SET latest_verification_status=?,latest_audit_id=?,error=?,updated_at=? WHERE batch_id=?",
                                         (status, audit_id, error, _now(), batch_id)).rowcount
            if changed != 1:
                raise KeyError(batch_id)
        return self.get(batch_id)

    def complete(self, batch_id, session):
        row = self.get(batch_id)
        if row is None:
            raise KeyError(batch_id)
        if row["generation_status"] != "COMMITTING" or row["batch_relpath"] != "batches/" + batch_id:
            raise ValueError("正式目录发布后才允许完成登记")
        if session.status != "PASS" or session.checks_passed != 15 or session.summary_sha256 is None:
            raise ValueError("全部15项及最终汇总通过才允许完成")
        self._validated_session(batch_id, session.context, session.checks, session.summary_sha256)
        record = self.audit_record(session.context.audit_id)
        if record["status"] != "PASS" or record["batch_id"] != batch_id:
            raise ValueError("完成登记前必须持久保存本次审查索引")
        with self.connection(write=True, _grants=("complete", "verify")) as connection:
            changed = connection.execute("""UPDATE batches SET generation_status='COMPLETED',latest_verification_status='PASS',
             latest_audit_id=?,error=NULL,updated_at=? WHERE batch_id=? AND generation_status='COMMITTING'""",
                                         (session.context.audit_id, _now(), batch_id)).rowcount
            if changed != 1:
                raise ValueError("完成状态登记竞争")
        return self.get(batch_id)

    def result_summary(self, batch_id, audit_id=None):
        row = self.get(batch_id)
        if row is None:
            raise KeyError(batch_id)
        return {"status": row["generation_status"], "mode": MODE, "batch_id": batch_id,
                "batch_dir": str(self.path(batch_id)), "audit_id": audit_id or row["latest_audit_id"],
                "set_count": row["set_count"], "raw_total": row["raw_total"], "group_count": row["group_count"],
                "per_code_expected_amount": row["per_code_expected_amount"], "total_amount": row["total_amount"],
                "checks_passed": 15 if row["generation_status"] == "COMPLETED" and row["latest_verification_status"] == "PASS" else 0,
                "request_sha256": row["request_sha256"], "snapshot_digest": row["manifest_sha256"],
                "provenance": row["provenance"], "latest_verification_status": row["latest_verification_status"],
                "backup_status": row["backup_status"]}

    def get_orig_anchor(self, batch_id):
        row, spec, raw = self._original(batch_id)
        if row["manifest_sha256"] is None:
            raise ValueError("没有可信首次冻结锚点")
        return {"backup_version": 1, "mode": MODE, "batch_id": batch_id,
                "original_request": strict_json(raw), "frozen_set_amounts": [str(a) for a in spec.amounts],
                "expected_request_sha256": row["request_sha256"], "expected_manifest_sha256": row["manifest_sha256"],
                "expected_group_count": row["group_count"], "provenance": row["provenance"]}

    def export_catalog_record(self, batch_id):
        row = self.get(batch_id)
        if row is None:
            raise KeyError(batch_id)
        with self.connection() as connection:
            audits = connection.execute("SELECT audit_id FROM audit_runs WHERE batch_id=? ORDER BY created_at,audit_id", (batch_id,)).fetchall()
            artifacts = connection.execute("SELECT path,size,sha256 FROM artifacts WHERE batch_id=? ORDER BY path", (batch_id,)).fetchall()
        return {"backup_version": 1, "mode": MODE, "batch_id": batch_id,
                "batch_relpath": "batches/" + batch_id, "generation_status": row["generation_status"],
                "latest_verification_status": row["latest_verification_status"], "latest_audit_id": row["latest_audit_id"],
                "set_count": row["set_count"], "raw_total": row["raw_total"], "group_count": row["group_count"],
                "per_code_expected_amount": row["per_code_expected_amount"], "total_amount": row["total_amount"],
                "audits": [self.audit_record(a[0]) for a in audits], "artifacts": [dict(a) for a in artifacts]}

    def record_backup(self, batch_id, backup_id, *, manifest_sha256, audit_ids, package_path=None, status="COMPLETE", error=None):
        row = self.get(batch_id)
        if status not in ("COMPLETE", "FAILED") or manifest_sha256 != row["manifest_sha256"]:
            raise ValueError("备份身份或状态不正确")
        with self.connection(write=True) as connection:
            connection.execute("INSERT INTO backup_runs(backup_id,batch_id,manifest_sha256,audit_ids_json,package_path,status,created_at,error) VALUES (?,?,?,?,?,?,?,?)",
                               (_batch_id(backup_id), batch_id, _hash(manifest_sha256), canonical_json(list(audit_ids)).decode("utf-8"),
                                str(package_path) if package_path is not None else None, status, _now(), error))
            connection.execute("UPDATE batches SET backup_status=?,backup_error=?,updated_at=? WHERE batch_id=?",
                               (status, error, _now(), batch_id))

    def backup_coverage(self, batch_id):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM backup_runs WHERE batch_id=? ORDER BY created_at DESC,backup_id DESC", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    get_backup_coverage = backup_coverage

    def mark_backup_reused(self, batch_id, backup_id):
        row = self.get(batch_id)
        with self.connection(write=True) as connection:
            saved = connection.execute("SELECT status,manifest_sha256 FROM backup_runs WHERE batch_id=? AND backup_id=?",
                                       (batch_id, backup_id)).fetchone()
            if saved is None or saved['status'] != 'COMPLETE' or saved['manifest_sha256'] != row['manifest_sha256']:
                raise ValueError('不能复用没有可信完整账本的备份')
            connection.execute("UPDATE batches SET backup_status='COMPLETE',backup_error=NULL,updated_at=? WHERE batch_id=?", (_now(), batch_id))

    def list_audits(self, batch_id):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM audit_runs WHERE batch_id=? ORDER BY created_at,audit_id", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def set_setting(self, key, value):
        if key not in _SETTINGS or type(value) is not str or len(value) > 4096:
            raise ValueError("不支持的设置键或值，原始输入不能通过设置修改")
        with self.connection(write=True) as connection:
            connection.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_setting(self, key, default=None):
        if key not in _SETTINGS:
            raise ValueError("不支持的设置键")
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
