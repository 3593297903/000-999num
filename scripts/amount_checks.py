"""Recorded, isolated source/release checks for AmountTool review 1.1.

No test runs automatically at import. Each invocation records its exact command,
stdout, exit code, source/dependency/fixture hashes and optional exe fingerprints.
Legacy assertions are retained; only legacy output paths are relocated.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_QA = PROJECT / "qa/amount_mode/r17-ddb2bc"


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_fingerprints(project=PROJECT):
    project = Path(project)
    files = [*project.joinpath("src").rglob("*.py"), *project.joinpath("tests").rglob("*.py"),
             project / "scripts/amount_checks.py", project / "requirements-dev.lock", project / "pyproject.toml",
             *project.glob("按套金额随机小组_*.md"), project / "按套金额随机小组_验收样例.json"]
    return {path.relative_to(project).as_posix(): file_sha256(path) for path in sorted(set(files)) if path.is_file()}


def environment_evidence():
    names = ("PySide6", "pytest", "pytest-qt", "pyinstaller", "psutil")
    return {"python": sys.version, "python_executable": sys.executable, "platform": platform.platform(),
            "dependencies": {name: importlib.metadata.version(name) for name in names}}


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def compare_baseline(baseline_file, project=PROJECT):
    """Check only the original explicit file list; do not rescan old QA."""
    project = Path(project).resolve()
    value = json.loads(Path(baseline_file).read_text(encoding="utf-8"))
    changes = []
    for entry in value["files"]:
        relative = Path(entry["path"])
        target = project / relative
        if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(project):
            raise ValueError("基线包含越界路径")
        if not target.is_file() or target.is_symlink():
            changes.append({"path": entry["path"], "reason": "missing_or_link"})
        elif target.stat().st_size != entry["size"] or file_sha256(target) != entry["sha256"]:
            changes.append({"path": entry["path"], "reason": "bytes_changed"})
    return {"baseline_file": str(Path(baseline_file).resolve()), "files_checked": len(value["files"]),
            "status": "UNCHANGED" if not changes else "CHANGED", "changes": changes,
            "scope": "exact original baseline file list; old QA/venv/build cache not scanned"}


def record_command(command, run_dir, *, metadata=None, env=None, cwd=PROJECT):
    """Run once, stream stdout into evidence and preserve a final exit receipt."""
    run_dir = Path(run_dir)
    log = run_dir / "stdout.log"
    receipt = {"status": "RUNNING", "command": list(map(str, command)), "cwd": str(Path(cwd).resolve()),
               "started_at": datetime.now().astimezone().isoformat(), "metadata": metadata or {},
               "stdout_file": str(log), "exit_code": None}
    _json(run_dir / "run.json", receipt)
    before = time.monotonic()
    process = None
    try:
        process = subprocess.Popen(receipt["command"], cwd=cwd, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        receipt["pid"] = process.pid
        with log.open("w", encoding="utf-8", newline="\n") as stream:
            for line in process.stdout:
                stream.write(line)
                stream.flush()
                print(line, end="", flush=True)
        code = process.wait()
        receipt["status"] = "FINISHED"
    except BaseException as error:
        receipt["status"] = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "ERROR"
        receipt["error"] = type(error).__name__ + ": " + str(error)
        code = 130 if isinstance(error, KeyboardInterrupt) else 4
        if process is not None and process.poll() is None:
            import psutil
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(descendants):
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                process.kill()
                process.wait(timeout=10)
            except (psutil.Error, OSError, subprocess.TimeoutExpired):
                pass
    receipt.update(exit_code=code, ended_at=datetime.now().astimezone().isoformat(),
                   elapsed_seconds=time.monotonic() - before)
    if log.is_file():
        receipt["stdout_sha256"] = file_sha256(log)
    _json(run_dir / "run.json", receipt)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("source", "legacy-source", "release", "legacy-release", "baseline"))
    parser.add_argument("--qa-root", type=Path, default=DEFAULT_QA)
    parser.add_argument("--dist-dir", type=Path)
    args, extra = parser.parse_known_args(argv)
    qa = args.qa_root.resolve()
    boundary = (PROJECT / "qa/amount_mode").resolve()
    if qa == boundary or not qa.is_relative_to(boundary) or not qa.is_dir():
        parser.error("--qa-root必须为已经建立的本轮独立amount_mode QA目录")
    run = qa / "checks" / (args.stage + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    data_root = run
    if args.stage in ("legacy-source", "legacy-release"):
        for _ in range(8):
            data_root = qa / ("l" + uuid.uuid4().hex[:4])
            try:
                data_root.mkdir(exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            parser.error("无法为旧测试分配独立短QA目录")
    print("本次检查证据：" + str(run), flush=True)
    metadata = {"stage": args.stage, "qa_root": str(qa), "environment": environment_evidence(),
                "test_data_dir": str(data_root),
                "source_and_fixture_sha256": source_fingerprints(),
                "launcher_command": [sys.executable, str(Path(__file__).resolve()),
                                     *(argv if argv is not None else sys.argv[1:])],
                "run_kind": "collection_only" if any(value in extra for value in ("--collect-only", "--co")) else "test_execution"}
    if args.stage == "baseline":
        result = compare_baseline(qa / "baseline.json")
        result["metadata"] = metadata
        result["command"] = [sys.executable, str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])]
        result["exit_code"] = 0 if result["status"] == "UNCHANGED" else 1
        _json(run / "baseline-comparison.json", result)
        print(json.dumps({key: result[key] for key in ("status", "files_checked", "changes", "exit_code")}, ensure_ascii=False), flush=True)
        return result["exit_code"]
    if args.stage == "source":
        targets = [str(path.relative_to(PROJECT)) for path in sorted((PROJECT / "tests/amount_mode").glob("test_*.py"))
                   if not path.name.startswith(("test_release", "test_packaged", "test_soak"))]
    elif args.stage == "legacy-source":
        targets = ["tests/test_project.py", "tests/unit", "tests/integration"]
    elif args.stage == "release":
        targets = ["tests/amount_mode/test_release.py"]
        if not (PROJECT / targets[0]).is_file():
            parser.error("AmountTool发布测试尚未实现：" + targets[0])
    else:
        targets = ["tests/release"]
    hashes = {}
    if args.stage in ("release", "legacy-release"):
        prefix = "Amount" if args.stage == "release" else "Number"
        dist = (args.dist_dir or PROJECT / ("dist/" + prefix + "Tool")).resolve()
        for filename in (prefix + "Tool.exe", prefix + "Verify.exe"):
            exe = dist / filename
            if not exe.is_file():
                parser.error("缺少实际候选exe：" + str(exe))
            hashes[filename] = file_sha256(exe)
        metadata["dist_dir"] = str(dist)
        metadata["exe_sha256"] = hashes
        _json(run / "candidate-exes.json", {"dist_dir": str(dist), "sha256": hashes})
        if data_root != run:
            _json(data_root / "candidate-exes.json", {"dist_dir": str(dist), "sha256": hashes})
        targets += ["--dist-dir", str(dist)]
    command = [sys.executable, "-m", "pytest", "-q", *targets, "-p", "no:cacheprovider",
               "-p", "tests.amount_mode.legacy_isolation", "--confcutdir", str(PROJECT / "tests/amount_mode"),
               "--junitxml", str(run / "tests.xml"), *extra]
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="utf-8", AMOUNT_QA_ROOT=str(run), AMOUNT_TEST_RUN_DIR=str(run),
                       AMOUNT_TEST_DATA_DIR=str(data_root), AMOUNT_CHECK_STAGE=args.stage)
    code = record_command(command, run, metadata=metadata, env=environment)
    before_sources = metadata["source_and_fixture_sha256"]
    after_sources = source_fingerprints()
    changed_sources = [name for name in sorted(before_sources.keys() | after_sources.keys())
                       if before_sources.get(name) != after_sources.get(name)]
    _json(run / "source-after.json", {"sha256": after_sources, "changed_paths": changed_sources,
                                      "note": "Changed paths require impact review before reusing this evidence."})
    if hashes:
        after = {name: file_sha256(Path(metadata["dist_dir"]) / name) for name in hashes}
        _json(run / "executable-after.json", {"sha256": after, "unchanged": after == hashes})
        if after != hashes:
            print("候选exe在检查期间变化，本次证据不能通过", flush=True)
            receipt = json.loads((run / "run.json").read_text(encoding="utf-8"))
            receipt.update(status="INVALIDATED", exit_code=4, error="Executable bytes changed during checks")
            _json(run / "run.json", receipt)
            return 4
    return code


if __name__ == "__main__":
    raise SystemExit(main())
