"""Read-only destination preflight, shared by all external write entrances."""
from pathlib import Path
import os
import re
import sqlite3
import stat
from urllib.parse import quote

DEFAULT_ROOTS = (Path("E:/number/data"), Path("E:/number/data_amount"))
_BATCH_LEAF = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)


class UnsafeOutputPath(ValueError):
    """An output preflight rejection; callers must produce no write side effects."""


def _directory(value):
    try:
        path = Path(value).resolve(strict=False)
        for parent in (path, *path.parents):
            try:
                mode = parent.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise ValueError("路径祖先不是目录：" + str(parent))
        return path
    except (OSError, RuntimeError, TypeError) as error:
        raise ValueError("无法安全解析目录：" + str(value)) from error


def _registered(root):
    database = root / "history.sqlite"
    native = {"batches": set(), "staging": set()}
    result = []
    if not database.exists():
        return _reserved_link_targets(root, native)
    connection = None
    try:
        connection = sqlite3.connect("file:" + quote(database.as_posix(), safe="/:") + "?mode=ro", uri=True)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "batches" not in tables:
            raise ValueError("历史库无法识别，不能确认路径安全：" + str(database))
        columns = {row[1] for row in connection.execute("PRAGMA table_info(batches)")}
        column = next((name for name in ("batch_relpath", "batch_dir", "path") if name in columns), None)
        if column is None:
            raise ValueError("历史路径字段无法识别")
        restricted = column == "batch_relpath" and "batch_id" in columns
        query = 'SELECT batch_id,"batch_relpath" FROM batches' if restricted else 'SELECT NULL,"' + column + '" FROM batches'
        for identity, value in connection.execute(query):
            if type(value) is not str or not value:
                raise ValueError("历史路径登记不合法")
            if restricted and type(identity) is str and _BATCH_LEAF.fullmatch(identity) is not None:
                region = next((name for name in native if value == name + "/" + identity), None)
                if region is not None:
                    # Exactly one validated leaf below an already canonical
                    # protected region: no per-batch resolve/stat is needed.
                    native[region].add(identity.casefold())
                    continue
            # Legacy absolute/custom paths, including every failed status and
            # any unknown/nonrestricted representation, keep full resolution.
            path = Path(value)
            result.append(_directory(path if path.is_absolute() else root / path))
    except sqlite3.Error as error:
        raise ValueError("无法只读检查历史批次路径：" + str(database)) from error
    finally:
        if connection is not None:
            connection.close()
    return result + _reserved_link_targets(root, native)


def _reserved_link_targets(root, native):
    """One metadata-only scan per reserved directory; never read batch contents."""
    targets = []
    for name in ("batches", "staging"):
        try:
            entries = os.scandir(root / name)
        except FileNotFoundError:
            # Ordinary not-yet-created leaves remain covered by the canonical
            # reserved boundary. Resolution errors/permissions fail closed.
            continue
        with entries:
            for entry in entries:
                # Include unregistered reparse roots too: an orphan link
                # inside a reserved area cannot create an output loophole.
                info = entry.stat(follow_symlinks=False)
                reparse = (entry.is_symlink() or entry.is_junction()
                           or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))
                if reparse:
                    targets.append(_directory(entry.path))
                elif entry.name.casefold() in native[name] and not entry.is_dir(follow_symlinks=False):
                    raise ValueError("已登记批次根不是目录：" + entry.path)
    return targets


def _reject_reserved_alias(value, boundaries):
    """Retain protected/marked ancestors when a deeper alias resolves away."""
    original = Path(value).absolute()
    ancestors = (original, *original.parents)
    aliases = [ancestor for ancestor in ancestors if ancestor.is_symlink() or ancestor.is_junction()]
    if not aliases:
        return
    # A marked external batch/package need not occur in a local registry. Keep
    # its canonical root before resolving away the child's lexical ancestry.
    protected = [*boundaries, *(ancestor.resolve(strict=False) for ancestor in ancestors if _marker_kind(ancestor))]
    for alias in aliases:
        parent = alias.parent.resolve(strict=False)
        if any(parent.is_relative_to(boundary) for boundary in protected):
            raise ValueError("写入目标通过受保护批次/备份区域内的链接越界：" + str(original))


def _marker_kind(path):
    if (path / "request.json").exists() and any((path / name).exists() for name in ("manifest.json", "items.jsonl", "group_plan.json", "quotas.json")):
        return "batch"
    if ((path / 'input_anchor.json').exists() and (path / 'catalog_record.json').exists()) or ((path / 'BACKUP_COMPLETE.json').exists() and (path / 'backup_manifest.json').exists()):
        return "backup"
    return None


def _protect_paths(*paths, workspace_roots=(), file_paths=()):
    """Return resolved paths; no mkdir, database creation or target modification."""
    roots = []
    for value in (*DEFAULT_ROOTS, *workspace_roots):
        root = _directory(value)
        if root not in roots:
            roots.append(root)
    boundaries = []
    for root in roots:
        boundaries.extend((_directory(root / "batches"), _directory(root / "staging")))
        boundaries.extend(_registered(root))
    files = {Path(value).absolute() for value in file_paths}
    output = []
    for value in paths:
        _reject_reserved_alias(value, boundaries)
        is_file = Path(value).absolute() in files
        if is_file:
            path = Path(value).resolve(strict=False)
            _directory(path.parent)
            if path.exists() and not path.is_file():
                raise ValueError("目标结果文件已是目录")
        else:
            path = _directory(value)
        for boundary in boundaries:
            if path.is_relative_to(boundary):
                raise ValueError("写入目标位于受保护批次/保留区域：" + str(path))
        for ancestor in (path.parent if is_file else path, *(path.parent if is_file else path).parents):
            kind = _marker_kind(ancestor)
            if kind == "batch":
                raise ValueError("写入目标位于外部批次内部：" + str(path))
            if kind == "backup":
                raise ValueError('写入目标位于已有自包含备份包内部：' + str(path))
        output.append(path)
    return tuple(output)


def protect_paths(*paths, workspace_roots=(), file_paths=()):
    try:
        return _protect_paths(*paths, workspace_roots=workspace_roots, file_paths=file_paths)
    except (ValueError, OSError, RuntimeError, TypeError) as error:
        raise UnsafeOutputPath(str(error)) from error


def validate_data_root(value):
    return protect_paths(value)[0]
