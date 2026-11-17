"""Relocate legacy test outputs without editing legacy tests or their fixtures.

Use only through scripts/amount_checks.py. Collection evidence lists the actual
collected module aliases and resolved tmp_path providers. This is an I/O-path
adapter, not a replacement for any legacy assertion or production behavior.
"""
import ast
import importlib
import importlib.util
import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
_RELOCATIONS = []


def _run_root():
    value = os.environ.get("AMOUNT_TEST_RUN_DIR")
    if not value:
        raise pytest.UsageError("legacy_isolation仅供amount_checks运行器使用")
    root = Path(value).resolve()
    boundary = (PROJECT / "qa/amount_mode").resolve()
    if root == boundary or not root.is_relative_to(boundary):
        raise pytest.UsageError("测试输出必须位于本轮amount_mode QA子目录")
    return root


def _data_root():
    """Legacy copies use a short sibling while detailed receipts stay in checks."""
    value = os.environ.get("AMOUNT_TEST_DATA_DIR")
    root = Path(value).resolve() if value else _run_root()
    boundary = (PROJECT / "qa/amount_mode").resolve()
    if root == boundary or not root.is_relative_to(boundary):
        raise pytest.UsageError("旧测试数据必须位于本轮amount_mode QA子目录")
    return root


def pytest_addoption(parser):
    parser.addoption("--dist-dir", default=None)


def _prepare_release_support(root):
    """Change exactly the module-level ROOT before its import-time mkdir.

    Keeping __file__ and the original code filename preserves tracebacks. The
    only AST replacement is the output-directory expression of ROOT; functions,
    test assertions and production imports are executed unchanged.
    """
    name = "tests.release.support"
    if name in sys.modules:
        raise pytest.UsageError("旧release.support过早导入，无法证明导入前I/O隔离")
    source = PROJECT / "tests/release/support.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    matches = [node for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "ROOT" for target in node.targets)]
    if len(matches) != 1:
        raise pytest.UsageError("旧发布支持模块ROOT声明变化，需要重新核对隔离适配")
    matches[0].value = ast.Call(func=ast.Name(id="Path", ctx=ast.Load()),
                               args=[ast.Constant(str(root / "r"))], keywords=[])
    ast.fix_missing_locations(tree)
    package = importlib.import_module("tests.release")
    module = types.ModuleType(name)
    module.__file__ = str(source)
    module.__package__ = "tests.release"
    module.__spec__ = importlib.util.spec_from_file_location(name, source)
    sys.modules[name] = module
    try:
        exec(compile(tree, str(source), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    setattr(package, "support", module)
    _RELOCATIONS.append({"module": name, "file": str(source), "variable": "ROOT",
                         "target": str(module.ROOT), "stage": "before_import_time_mkdir"})


def pytest_configure(config):
    root = _run_root()
    root.mkdir(parents=True, exist_ok=True)
    _data_root().mkdir(parents=True, exist_ok=True)
    if os.environ.get("AMOUNT_CHECK_STAGE") == "legacy-release":
        _prepare_release_support(_data_root())


@pytest.fixture
def tmp_path():
    path = _data_root() / "t" / uuid.uuid4().hex[:10]
    path.mkdir(parents=True)
    return path


def pytest_collection_modifyitems(session, config, items):
    root = _run_root()
    data_root = _data_root()
    legacy_paths = (PROJECT / "tests/integration/test_output_paths.py").resolve()
    release_root_consumers = {
        (PROJECT / "tests/release/support.py").resolve(),
        (PROJECT / "tests/release/test_release_flow.py").resolve(),
        (PROJECT / "tests/release/test_release_faults.py").resolve(),
    }
    modules = {item.module for item in items}
    # An old test can be imported both by package name and pytest's top-level
    # module name. Patch actual collected modules and already imported aliases.
    modules.update(module for module in tuple(sys.modules.values())
                   if isinstance(module, types.ModuleType) and getattr(module, "__file__", None)
                   and str(getattr(module, "__file__", "")).replace("\\", "/").endswith("test_output_paths.py"))
    for module in modules:
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if path == legacy_paths or path == (PROJECT / "tests/release/test_output_paths.py").resolve():
            module.QA = data_root
            _RELOCATIONS.append({"module": module.__name__, "file": str(path),
                                 "variable": "QA", "target": str(data_root), "stage": "actual_collected_module"})
        if path in release_root_consumers:
            module.ROOT = data_root / "r"
            _RELOCATIONS.append({"module": module.__name__, "file": str(path),
                                 "variable": "ROOT", "target": str(module.ROOT), "stage": "collected_or_imported_module"})
    forbidden = (PROJECT / "tests/conftest.py").resolve()
    conftests = sorted(str(Path(module.__file__).resolve()) for module in config.pluginmanager._conftest_plugins)
    if str(forbidden) in conftests:
        raise pytest.UsageError("旧tests/conftest.py已加载，tmp_path隔离未建立")
    providers = {}
    for item in items:
        definitions = item._fixtureinfo.name2fixturedefs.get("tmp_path", ())
        if definitions:
            function = definitions[-1].func
            providers[item.nodeid] = {"module": function.__module__, "file": str(Path(function.__code__.co_filename).resolve())}
            provider = Path(function.__code__.co_filename).resolve()
            allowed = {(PROJECT / "tests/amount_mode/legacy_isolation.py").resolve(),
                       (PROJECT / "tests/amount_mode/conftest.py").resolve()}
            if provider not in allowed:
                raise pytest.UsageError("tmp_path提供者未隔离：" + str(provider))
    evidence = {"collected": len(items), "nodeids": [item.nodeid for item in items],
                "run_root": str(root), "test_data_root": str(data_root), "conftest_files": conftests,
                "relocations": _RELOCATIONS, "tmp_path_providers": providers,
                "collect_only": bool(config.option.collectonly)}
    (root / "collection-isolation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
