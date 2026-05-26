"""SQLite database access for projects, tasks, and task logs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, TypeVar

from codepilot.core.paths import _slugify_project_name, global_storage_root, project_storage_root
from codepilot.storage.config import (
    default_db_path as _cfg_default_db_path,
    get_cache_ttl_seconds as _cfg_get_cache_ttl_seconds,
    get_db_path as _cfg_get_db_path,
    open_connection as _cfg_open_connection,
)
from codepilot.storage.project_store import (
    delete_project_with_children as _delete_project_with_children,
    fetch_project_by_name as _fetch_project_by_name,
    fetch_projects as _fetch_projects,
    upsert_project_by_path as _upsert_project_by_path,
)
from codepilot.storage.schema_store import (  # noqa: F401 (re-export SCHEMA_VERSION)
    SCHEMA_VERSION,
    _MIGRATIONS,
    _ensure_service_states_schema,
    _has_column,
    initialize_schema as _initialize_schema,
    load_schema_status as _load_schema_status,
)
from codepilot.storage.service_state_store import (
    _normalize_service_scope,
    delete_service_state_row as _delete_service_state_row,
    fetch_service_state as _fetch_service_state,
    insert_service_state_row_if_absent as _insert_service_state_row_if_absent,
    query_service_states as _query_service_states,
    upsert_service_state_row as _upsert_service_state_row,
)
from codepilot.storage.session_store import (
    delete_session_messages as _delete_session_messages,
    delete_session_row as _delete_session_row,
    fetch_session_by_id as _fetch_session_by_id,
    fetch_session_message_by_id as _fetch_session_message_by_id,
    insert_session as _insert_session,
    insert_session_message as _insert_session_message,
    query_session_messages as _query_session_messages,
    query_sessions as _query_sessions,
    touch_session_updated_at as _touch_session_updated_at,
    update_session_message_fields as _update_session_message_fields,
    update_session_fields as _update_session_fields,
)
from codepilot.storage.task_read_model import (
    fetch_active_dedup_keys as _fetch_active_dedup_keys,
    fetch_task_by_id as _fetch_task_by_id,
    find_active_duplicate as _find_active_duplicate,
    query_done_task_windows as _query_done_task_windows,
    query_next_backlog_task as _query_next_backlog_task,
    query_old_done_tasks as _query_old_done_tasks,
    query_orphan_log_paths as _query_orphan_log_paths,
    query_stale_in_progress as _query_stale_in_progress,
    query_task_logs as _query_task_logs,
    query_task_status_counts as _query_task_status_counts,
    query_tasks as _query_tasks,
)
from codepilot.storage.task_write_store import (
    delete_task_with_logs as _delete_task_with_logs,
    insert_task_log_row as _insert_task_log_row,
    insert_task_row as _insert_task_row,
    prepare_task_updates as _prepare_task_updates,
    update_task_fields as _update_task_fields,
)


DB_PATH = _cfg_default_db_path()

_CACHE_TTL_SECONDS = _cfg_get_cache_ttl_seconds()
_CACHE_MISS = object()
_QUERY_CACHE: dict[tuple, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple) -> object:
    if _CACHE_TTL_SECONDS <= 0:
        return _CACHE_MISS
    now = time.time()
    with _CACHE_LOCK:
        payload = _QUERY_CACHE.get(key)
        if not payload:
            return _CACHE_MISS
        cached_at, value = payload
        if now - cached_at > _CACHE_TTL_SECONDS:
            _QUERY_CACHE.pop(key, None)
            return _CACHE_MISS
        return copy.deepcopy(value)


_V = TypeVar("_V")


def _cache_set(key: tuple, value: _V) -> _V:
    if _CACHE_TTL_SECONDS <= 0:
        return value
    with _CACHE_LOCK:
        _QUERY_CACHE[key] = (time.time(), copy.deepcopy(value))
    return value


def _cache_invalidate(*prefixes: str) -> None:
    with _CACHE_LOCK:
        if not prefixes:
            _QUERY_CACHE.clear()
            return
        targets = set(prefixes)
        for key in list(_QUERY_CACHE):
            if key and key[0] in targets:
                _QUERY_CACHE.pop(key, None)


def _invalidate_project_caches() -> None:
    _cache_invalidate(
        "project_by_name",
        "projects",
        "project_by_path",
        "tasks",
        "task_by_id",
        "task_stats",
        "sessions",
    )


def _invalidate_task_caches(*extra_prefixes: str) -> None:
    _cache_invalidate("tasks", "task_by_id", "task_stats", *extra_prefixes)


def _invalidate_session_caches() -> None:
    _cache_invalidate("sessions", "session_by_id", "session_messages")


def _invalidate_service_state_caches() -> None:
    _cache_invalidate("service_state", "service_states")


def _default_db_path() -> Path:
    """Compatibility shim for existing tests and private imports."""
    return _cfg_default_db_path()


def _get_db_path() -> Path:
    """Compatibility shim for existing tests and private imports."""
    return _cfg_get_db_path()


@contextmanager
def get_conn():
    """Compatibility shim: use the read-query connection entrypoint."""
    with get_read_conn() as conn:
        yield conn


@contextmanager
def get_read_conn():
    """Yield a SQLite connection for read-query paths."""
    conn = _cfg_open_connection(_get_db_path())
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_write_conn():
    """Yield a SQLite connection wrapped in one write transaction."""
    conn = _cfg_open_connection(_get_db_path())
    try:
        yield conn
    except Exception:  # noqa: BLE001
        # 事务异常时回滚
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create baseline tables and run versioned migrations in order."""
    with get_write_conn() as conn:
        _initialize_schema(conn)
    _cache_invalidate()


def schema_status() -> dict:
    """Return current schema version and applied migration history."""
    with get_read_conn() as conn:
        return _load_schema_status(conn)


def register_project(
    name: str,
    path: str,
    base_branch: str = "dev",
    default_mode: str = "dual",
    worktree_base: Optional[str] = None,
    config_file: Optional[str] = None,
) -> dict:
    """Register or update a project."""
    with get_write_conn() as conn:
        effective_name = _upsert_project_by_path(
            conn,
            name=name,
            path=path,
            base_branch=base_branch,
            default_mode=default_mode,
            worktree_base=worktree_base,
            config_file=config_file,
        )
    _invalidate_project_caches()
    if effective_name != name:
        return rename_project(effective_name, name)["project"]
    return get_project(name) or get_project(effective_name)


def get_project(name: str) -> Optional[dict]:
    """Fetch a project by registered name or a stable project alias.

    The registered name remains the canonical key used by tasks and service
    state. For user-facing lookup, also accept the registered directory name,
    full project path, and the `[project].name` value from that project's
    config file when they point to exactly one registered project.
    """
    ref = str(name or "").strip()
    if not ref:
        return None
    cache_key = ("project_by_name", ref)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_project_by_name(conn, ref)
    if result is None and not _config_rename_sync_active():
        sync_project_config_renames()
        with get_read_conn() as conn:
            result = _fetch_project_by_name(conn, ref)
    if result is None:
        result = _find_project_by_alias(ref)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_projects() -> list[dict]:
    """Return all registered projects."""
    if not _config_rename_sync_active():
        sync_project_config_renames()
    cache_key = ("projects",)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_projects(conn)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def find_project_by_path(path: str | Path) -> Optional[dict]:
    """Find the deepest registered project that contains the given path."""
    target = Path(path).resolve()
    cache_key = ("project_by_path", str(target))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]

    matches: list[tuple[int, dict]] = []
    for project in list_projects():
        project_path = Path(project["path"]).resolve()
        try:
            target.relative_to(project_path)
        except ValueError:
            continue
        matches.append((len(project_path.parts), project))

    if not matches:
        return _cache_set(cache_key, None)  # type: ignore[return-value]
    matches.sort(key=lambda item: item[0], reverse=True)
    return _cache_set(cache_key, matches[0][1])  # type: ignore[return-value]


def _find_project_by_alias(ref: str) -> Optional[dict]:
    if _looks_like_path(ref):
        by_path = find_project_by_path(Path(ref).expanduser())
        if by_path:
            return by_path

    normalized = ref.casefold()
    matches: list[dict] = []
    for project in list_projects():
        aliases = _project_aliases(project)
        if normalized in aliases:
            matches.append(project)

    if len(matches) == 1:
        return matches[0]
    return None


def _looks_like_path(ref: str) -> bool:
    if not ref:
        return False
    if os.sep in ref or (os.altsep and os.altsep in ref):
        return True
    return bool(Path(ref).drive)


def _project_aliases(project: dict) -> set[str]:
    aliases: set[str] = set()
    path_text = str(project.get("path") or "").strip()
    if path_text:
        path = Path(path_text)
        aliases.add(path.name.casefold())
        aliases.add(str(path.resolve()).casefold())
    return {alias for alias in aliases if alias}


def delete_project(name: str) -> bool:
    """Delete a project and its related tasks."""
    with get_write_conn() as conn:
        removed = _delete_project_with_children(conn, name)
    if removed:
        _invalidate_project_caches()
        _invalidate_task_caches("task_logs")
        _invalidate_session_caches()
    return removed


def _normalize_project_name_for_update(raw: str, *, label: str) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise ValueError(f"{label}不能为空")
    if "/" in name or "\\" in name:
        raise ValueError(f"{label}不能包含路径分隔符")
    return name


def _rewrite_project_meta_refs(value: object, old_name: str, new_name: str) -> object:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key == "project" and str(item or "") == old_name:
                out[key] = new_name
            else:
                out[key] = _rewrite_project_meta_refs(item, old_name, new_name)
        return out
    if isinstance(value, list):
        return [_rewrite_project_meta_refs(item, old_name, new_name) for item in value]
    return value


def _decode_json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _toml_string(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _project_config_candidates(project: dict) -> list[Path]:
    candidates: list[Path] = []
    config_text = str(project.get("config_file") or "").strip()
    if config_text:
        config_path = Path(config_text).expanduser()
        if config_path.is_dir():
            config_path = config_path / "AGENTS.toml"
        candidates.append(config_path)

    project_path = str(project.get("path") or "").strip()
    if project_path:
        fallback = Path(project_path).expanduser() / "AGENTS.toml"
        if all(str(existing) != str(fallback) for existing in candidates):
            candidates.append(fallback)
    return candidates


def _rewrite_toml_section_key_text(
    text: str,
    *,
    section_name: str,
    key: str,
    new_value: str,
    insert_if_missing: bool = False,
) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=.*$")
    section_header_idx: int | None = None
    in_section = False
    for idx, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        match = section_re.match(bare)
        if match:
            section = match.group(1).strip()
            in_section = section == section_name
            if in_section:
                section_header_idx = idx
            elif section_header_idx is not None:
                break
            continue
        key_match = key_re.match(bare)
        if in_section and key_match:
            newline = line[len(bare):]
            indent = key_match.group(1)
            lines[idx] = f"{indent}{key} = {_toml_string(new_value)}{newline}"
            return "".join(lines), True

    if insert_if_missing and section_header_idx is not None:
        lines.insert(section_header_idx + 1, f"{key} = {_toml_string(new_value)}\n")
        return "".join(lines), True

    if insert_if_missing:
        prefix = f"[{section_name}]\n{key} = {_toml_string(new_value)}\n\n"
        return prefix + text, True
    return text, False


def _write_project_config_text(config_path: Path, text: str) -> None:
    config_path.write_text(text, encoding="utf-8")


def _sync_project_config_name(
    project: dict,
    old_name: str,
    new_name: str,
    *,
    fail_on_error: bool = False,
) -> dict:
    for config_path in _project_config_candidates(project):
        if not config_path.is_file():
            continue
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            message = f"{config_path}: {exc}"
            if fail_on_error:
                raise ValueError(message) from exc
            return {"config_file": str(config_path), "config_updated": False, "config_error": message}

        try:
            try:
                import tomllib
            except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
                import tomli as tomllib  # type: ignore[no-redef]
            parsed = tomllib.loads(text)
        except Exception as exc:  # noqa: BLE001
            message = f"AGENTS.toml 解析失败：{exc}"
            if fail_on_error:
                raise ValueError(message) from exc
            return {"config_file": str(config_path), "config_updated": False, "config_error": message}

        project_data = parsed.get("project") if isinstance(parsed, dict) else None
        next_text = text
        changed = False
        if not (isinstance(project_data, dict) and str(project_data.get("name") or "").strip() == new_name):
            next_text, project_changed = _rewrite_toml_section_key_text(
                next_text,
                section_name="project",
                key="name",
                new_value=new_name,
                insert_if_missing=True,
            )
            changed = changed or project_changed

        feishu_data = parsed.get("feishu_bot") if isinstance(parsed, dict) else None
        if isinstance(feishu_data, dict) and str(feishu_data.get("default_project") or "").strip() == old_name:
            next_text, feishu_changed = _rewrite_toml_section_key_text(
                next_text,
                section_name="feishu_bot",
                key="default_project",
                new_value=new_name,
                insert_if_missing=True,
            )
            changed = changed or feishu_changed

        if not changed:
            return {"config_file": str(config_path), "config_updated": False, "config_error": ""}

        try:
            tomllib.loads(next_text)
        except Exception as exc:  # noqa: BLE001
            message = f"AGENTS.toml 更新后无法解析：{exc}"
            if fail_on_error:
                raise ValueError(message) from exc
            return {"config_file": str(config_path), "config_updated": False, "config_error": message}

        try:
            _write_project_config_text(config_path, next_text)
        except OSError as exc:
            message = f"{config_path}: {exc}"
            if fail_on_error:
                raise ValueError(message) from exc
            return {"config_file": str(config_path), "config_updated": False, "config_error": message}
        return {"config_file": str(config_path), "config_updated": True, "config_error": ""}

    return {"config_file": "", "config_updated": False, "config_error": ""}


_CONFIG_RENAME_SYNC = threading.local()


def _config_rename_sync_active() -> bool:
    return bool(getattr(_CONFIG_RENAME_SYNC, "active", False))


def _empty_data_migration_result() -> dict:
    return {
        "data_migrated": False,
        "data_backup_path": "",
        "data_conflicts": [],
        "data_error": "",
        "pending_cleanup": [],
        "data_files_copied": 0,
        "updated_log_paths": 0,
    }


def _project_runtime_root_pairs(old_name: str, new_name: str) -> list[tuple[str, Path, Path]]:
    home = global_storage_root()
    return [
        (
            "data",
            project_storage_root(project_name=old_name),
            project_storage_root(project_name=new_name),
        ),
        (
            "daemon",
            home / "daemon" / _slugify_project_name(old_name),
            home / "daemon" / _slugify_project_name(new_name),
        ),
        (
            "inspect",
            home / "inspect" / _slugify_project_name(old_name),
            home / "inspect" / _slugify_project_name(new_name),
        ),
        (
            "opencode",
            home / "opencode" / _slugify_project_name(old_name),
            home / "opencode" / _slugify_project_name(new_name),
        ),
    ]


def _backup_root_for_project_rename(old_name: str, new_name: str) -> Path:
    old_slug = _slugify_project_name(old_name)
    new_slug = _slugify_project_name(new_name)
    return global_storage_root() / "migration-backups" / f"project-rename-{old_slug}-to-{new_slug}-{time.time_ns()}"


def _resolved_path_key(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _ensure_backup_root(result: dict, backup_root: Path) -> None:
    if not result.get("data_backup_path"):
        backup_root.mkdir(parents=True, exist_ok=True)
        result["data_backup_path"] = str(backup_root)


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_conflict_to_backup(
    source: Path,
    backup_target: Path,
    *,
    result: dict,
    backup_root: Path,
    kind: str,
    existing: Path,
) -> None:
    _ensure_backup_root(result, backup_root)
    if source.is_dir():
        shutil.copytree(source, backup_target, dirs_exist_ok=True)
        for child in source.rglob("*"):
            if child.is_file():
                rel = child.relative_to(source)
                result["_path_rewrites"][_resolved_path_key(child)] = str(backup_target / rel)
    else:
        _copy_file(source, backup_target)
        result["_path_rewrites"][_resolved_path_key(source)] = str(backup_target)
    result["data_conflicts"].append(
        {
            "kind": kind,
            "source": str(source),
            "existing": str(existing),
            "backup": str(backup_target),
        }
    )


def _merge_runtime_tree(
    *,
    kind: str,
    old_root: Path,
    new_root: Path,
    backup_root: Path,
    result: dict,
) -> None:
    if not old_root.exists():
        return
    result["data_migrated"] = True
    result.setdefault("data_paths", []).append({"kind": kind, "old": str(old_root), "new": str(new_root)})
    new_root.mkdir(parents=True, exist_ok=True)
    skipped_dirs: list[Path] = []

    for source in sorted(old_root.rglob("*"), key=lambda item: len(item.parts)):
        if any(source == skipped or source.is_relative_to(skipped) for skipped in skipped_dirs):
            continue
        relative = source.relative_to(old_root)
        target = new_root / relative
        backup_target = backup_root / kind / relative

        if source.is_dir():
            if target.exists() and not target.is_dir():
                _copy_conflict_to_backup(
                    source,
                    backup_target,
                    result=result,
                    backup_root=backup_root,
                    kind=kind,
                    existing=target,
                )
                skipped_dirs.append(source)
                continue
            target.mkdir(parents=True, exist_ok=True)
            continue

        if target.exists():
            _copy_conflict_to_backup(
                source,
                backup_target,
                result=result,
                backup_root=backup_root,
                kind=kind,
                existing=target,
            )
            continue

        if target.parent.exists() and not target.parent.is_dir():
            _copy_conflict_to_backup(
                source,
                backup_target,
                result=result,
                backup_root=backup_root,
                kind=kind,
                existing=target.parent,
            )
            continue

        _copy_file(source, target)
        result["data_files_copied"] += 1


def _cleanup_old_runtime_roots(root_pairs: list[tuple[str, Path, Path]], result: dict) -> None:
    for _kind, old_root, _new_root in root_pairs:
        if old_root.resolve(strict=False) == _new_root.resolve(strict=False):
            continue
        if not old_root.exists():
            continue
        try:
            shutil.rmtree(old_root)
        except OSError:
            result["pending_cleanup"].append(str(old_root))


def _migrate_project_runtime_data(old_name: str, new_name: str) -> tuple[dict, list[tuple[str, Path, Path]]]:
    result = _empty_data_migration_result()
    result["_path_rewrites"] = {}
    root_pairs = _project_runtime_root_pairs(old_name, new_name)
    backup_root = _backup_root_for_project_rename(old_name, new_name)
    try:
        for kind, old_root, new_root in root_pairs:
            if old_root.resolve(strict=False) == new_root.resolve(strict=False):
                continue
            _merge_runtime_tree(
                kind=kind,
                old_root=old_root,
                new_root=new_root,
                backup_root=backup_root,
                result=result,
            )
    except OSError as exc:
        result["data_error"] = str(exc)
        raise ValueError(f"项目数据迁移失败：{exc}") from exc

    return result, root_pairs


def _rewrite_migrated_runtime_path(
    raw_path: object,
    root_pairs: list[tuple[str, Path, Path]],
    path_rewrites: dict[str, str],
) -> str:
    text = str(raw_path or "").strip()
    if not text:
        return text

    source = Path(text).expanduser()
    source_key = _resolved_path_key(source)
    if source_key in path_rewrites:
        target = Path(path_rewrites[source_key])
        if not target.exists():
            raise ValueError(f"迁移后的日志路径不存在：{target}")
        return str(target)

    resolved = source.resolve(strict=False)
    for _kind, old_root, new_root in root_pairs:
        try:
            relative = resolved.relative_to(old_root.resolve(strict=False))
        except ValueError:
            continue
        target = new_root / relative
        if not target.exists():
            raise ValueError(f"迁移后的日志路径不存在：{target}")
        return str(target)
    return text


def _rewrite_project_task_log_paths(
    conn,
    project: str,
    root_pairs: list[tuple[str, Path, Path]],
    path_rewrites: dict[str, str],
) -> int:
    rows = conn.execute(
        """
        SELECT id, current_log_path
          FROM tasks
         WHERE project = ?
           AND current_log_path IS NOT NULL
           AND current_log_path != ''
        """,
        (project,),
    ).fetchall()
    changed = 0
    for row in rows:
        current = str(row["current_log_path"] or "")
        next_path = _rewrite_migrated_runtime_path(current, root_pairs, path_rewrites)
        if next_path == current:
            continue
        conn.execute("UPDATE tasks SET current_log_path = ? WHERE id = ?", (next_path, row["id"]))
        changed += 1
    return changed


def _project_config_name(project: dict) -> str:
    for config_path in _project_config_candidates(project):
        if not config_path.is_file():
            continue
        try:
            text = config_path.read_text(encoding="utf-8")
            try:
                import tomllib
            except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
                import tomli as tomllib  # type: ignore[no-redef]
            parsed = tomllib.loads(text)
        except Exception:  # noqa: BLE001
            return ""
        project_data = parsed.get("project") if isinstance(parsed, dict) else None
        if isinstance(project_data, dict):
            return str(project_data.get("name") or "").strip()
    return ""


def _raw_list_projects() -> list[dict]:
    with get_read_conn() as conn:
        return _fetch_projects(conn)


def sync_project_config_renames() -> list[dict]:
    """Synchronize registered names when AGENTS.toml was manually renamed."""
    if _config_rename_sync_active():
        return []
    _CONFIG_RENAME_SYNC.active = True
    results: list[dict] = []
    try:
        for project in _raw_list_projects():
            config_name = _project_config_name(project)
            if not config_name or config_name == str(project.get("name") or ""):
                continue
            results.append(rename_project(str(project["name"]), config_name))
    finally:
        _CONFIG_RENAME_SYNC.active = False
    return results


def _rename_project_service_state_refs(
    conn,
    old_name: str,
    new_name: str,
    *,
    root_pairs: list[tuple[str, Path, Path]] | None = None,
    path_rewrites: dict[str, str] | None = None,
) -> int:
    rows = conn.execute("SELECT * FROM service_states").fetchall()
    changed = 0
    runtime_roots = root_pairs or []
    rewrite_map = path_rewrites or {}
    for row in rows:
        meta = _decode_json_object(row["meta"])
        rewritten_meta = _rewrite_project_meta_refs(meta, old_name, new_name)
        next_scope = new_name if str(row["scope"] or "") == old_name else str(row["scope"] or "")
        next_log_path = _rewrite_migrated_runtime_path(row["log_path"], runtime_roots, rewrite_map)
        if next_scope == str(row["scope"] or "") and rewritten_meta == meta and next_log_path == str(row["log_path"] or ""):
            continue

        conn.execute(
            """
            INSERT INTO service_states
                (service, scope, pid, status, log_path, heartbeat_at, meta, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, scope) DO UPDATE SET
                pid = excluded.pid,
                status = excluded.status,
                log_path = excluded.log_path,
                heartbeat_at = excluded.heartbeat_at,
                meta = excluded.meta,
                updated_at = excluded.updated_at
            """,
            (
                row["service"],
                next_scope,
                row["pid"],
                row["status"],
                next_log_path,
                row["heartbeat_at"],
                json.dumps(rewritten_meta, ensure_ascii=False) if rewritten_meta else None,
                row["created_at"],
                row["updated_at"],
            ),
        )
        if next_scope != str(row["scope"] or ""):
            conn.execute(
                "DELETE FROM service_states WHERE service = ? AND scope = ?",
                (row["service"], row["scope"]),
            )
        changed += 1
    return changed


def _refresh_project_task_dedup_keys(conn, project: str) -> int:
    rows = conn.execute(
        """
        SELECT id, title, content, dedup_key
          FROM tasks
         WHERE project = ?
           AND dedup_key IS NOT NULL
           AND dedup_key != ''
        """,
        (project,),
    ).fetchall()
    changed = 0
    for row in rows:
        next_key = compute_dedup_key(project, row["title"], row["content"] or "")
        if next_key == row["dedup_key"]:
            continue
        conn.execute("UPDATE tasks SET dedup_key = ? WHERE id = ?", (next_key, row["id"]))
        changed += 1
    return changed


def rename_project(name: str, new_name: str) -> dict:
    """Rename a registered project and every DB row keyed by its name."""
    old_ref = _normalize_project_name_for_update(name, label="项目名称")
    target_name = _normalize_project_name_for_update(new_name, label="新项目名称")
    project = get_project(old_ref)
    if not project:
        raise ValueError(f"项目 '{old_ref}' 不存在。")

    old_name = str(project["name"])
    with get_read_conn() as conn:
        existing = _fetch_project_by_name(conn, target_name)
    if existing and str(existing.get("name") or "") != old_name:
        raise ValueError(f"项目 '{target_name}' 已存在。")

    if target_name == old_name:
        config_result = _sync_project_config_name(project, old_name, target_name, fail_on_error=True)
        return {
            "ok": True,
            "renamed": False,
            "old_name": old_name,
            "new_name": target_name,
            "path": project["path"],
            "updated_tasks": 0,
            "updated_sessions": 0,
            "updated_service_states": 0,
            **_empty_data_migration_result(),
            **config_result,
        }

    migration_result, root_pairs = _migrate_project_runtime_data(old_name, target_name)
    path_rewrites = migration_result.get("_path_rewrites") if isinstance(migration_result.get("_path_rewrites"), dict) else {}

    with get_write_conn() as conn:
        task_count = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE project = ?", (old_name,)).fetchone()[0])
        session_count = int(conn.execute("SELECT COUNT(*) FROM sessions WHERE project = ?", (old_name,)).fetchone()[0])
        log_path_count = _rewrite_project_task_log_paths(conn, old_name, root_pairs, path_rewrites)
        original_path = str(project["path"])
        temp_path = f"{original_path}#rename-{time.time_ns()}"
        conn.execute("UPDATE projects SET path = ? WHERE name = ?", (temp_path, old_name))
        conn.execute(
            """
            INSERT INTO projects
                (name, path, base_branch, default_mode, worktree_base, config_file, created_at)
            SELECT ?, ?, base_branch, default_mode, worktree_base, config_file, created_at
              FROM projects
             WHERE name = ?
            """,
            (target_name, original_path, old_name),
        )
        conn.execute("UPDATE tasks SET project = ? WHERE project = ?", (target_name, old_name))
        conn.execute("UPDATE sessions SET project = ? WHERE project = ?", (target_name, old_name))
        conn.execute("DELETE FROM projects WHERE name = ?", (old_name,))
        service_count = _rename_project_service_state_refs(
            conn,
            old_name,
            target_name,
            root_pairs=root_pairs,
            path_rewrites=path_rewrites,
        )
        dedup_count = _refresh_project_task_dedup_keys(conn, target_name)
        config_result = _sync_project_config_name(project, old_name, target_name, fail_on_error=True)

    _cleanup_old_runtime_roots(root_pairs, migration_result)
    _invalidate_project_caches()
    _invalidate_task_caches()
    _invalidate_session_caches()
    _invalidate_service_state_caches()
    renamed = get_project(target_name) or {**project, "name": target_name}
    migration_public = {key: value for key, value in migration_result.items() if not str(key).startswith("_")}
    migration_public["updated_log_paths"] = log_path_count
    return {
        "ok": True,
        "renamed": True,
        "old_name": old_name,
        "new_name": target_name,
        "path": renamed["path"],
        "updated_tasks": task_count,
        "updated_sessions": session_count,
        "updated_service_states": service_count,
        "updated_dedup_keys": dedup_count,
        **migration_public,
        **config_result,
        "project": renamed,
    }


def compute_dedup_key(project: str, title: str, content: str = "") -> str:
    """Return a 16-char hex dedup key for project/title/content."""
    normalized = (project.strip() + "|" + title.strip() + "|" + content.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _resolve_project_path(project: str, project_path: Optional[str]) -> str:
    if project_path:
        return project_path
    proj = get_project(project)
    return proj["path"] if proj else ""


_TASK_EVENT_FIELDS = {
    "status",
    "run_phase",
    "error_message",
    "delivery_record",
    "started_at",
    "completed_at",
    "retry_count",
    "heartbeat_at",
    "active_pid",
    "current_log_path",
    "last_output",
    "stop_requested",
    "stop_reason",
}
_MEMORY_TASK_TERMINAL_STATUSES = {"done", "failed", "cancelled"}


def _task_update_summary(task: dict) -> str:
    for field in ("error_message", "delivery_record", "last_output"):
        value = str(task.get(field) or "").strip()
        if value:
            return value[:4000]
    status = str(task.get("status") or "").strip()
    phase = str(task.get("run_phase") or "").strip()
    if status and phase:
        return f"{status}:{phase}"
    return status or phase


def _record_task_memory_event(
    task: dict,
    *,
    event_type: str,
    status: str,
    tags: list[str],
    changed_fields: set[str] | None = None,
    project_info: Optional[dict] = None,
    extra_details: Optional[dict] = None,
) -> None:
    project_name = str(task.get("project") or "").strip()
    if not project_name:
        return
    try:
        from codepilot.core.memory import append_memory_event

        project_info = project_info or get_project(project_name)
        if not project_info:
            return
        task_id = int(task.get("id"))
        title = str(task.get("title") or "").strip()
        summary = f"任务 #{task_id} {status}：{title}" if title else f"任务 #{task_id} {status}"
        details = {
            "task_id": task_id,
            "status": status,
            "title": title,
            "source": str(task.get("source") or ""),
            "changed_fields": sorted(changed_fields or set()),
            "retry_count": int(task.get("retry_count") or 0),
            "error_message": str(task.get("error_message") or "")[:1000],
            "delivery_record": str(task.get("delivery_record") or "")[:1000],
            "completed_at": task.get("completed_at"),
        }
        details.update(extra_details or {})
        append_memory_event(
            project_info,
            event_type=event_type,
            source="codepilot.task",
            summary=summary,
            details=details,
            tags=tags,
        )
    except Exception:  # noqa: BLE001
        return


def _record_task_update_memory(task: dict, changed_fields: set[str], project_info: Optional[dict] = None) -> None:
    status = str(task.get("status") or "").strip()
    if "status" not in changed_fields:
        return
    if status == "archived":
        _record_task_memory_event(
            task,
            event_type="task.archived",
            status=status,
            tags=["task", "feedback", "archived"],
            changed_fields=changed_fields,
            project_info=project_info,
        )
        return
    if status not in _MEMORY_TASK_TERMINAL_STATUSES:
        return
    _record_task_memory_event(
        task,
        event_type="task.updated",
        status=status,
        tags=["task", "outcome", status],
        changed_fields=changed_fields,
        project_info=project_info,
    )


def _task_timeline_project_path(task: dict, project_info: Optional[dict] = None) -> str:
    project_path = str(task.get("project_path") or "").strip()
    if project_path:
        return project_path
    project_name = str(task.get("project") or "").strip()
    if not project_name:
        return ""
    project_info = project_info or get_project(project_name)
    return str((project_info or {}).get("path") or "").strip()


def _append_task_timeline(
    task: dict | None,
    *,
    event: str,
    actor: str = "",
    source: str = "",
    message: str = "",
    artifact_path: str = "",
    timestamp: str | None = None,
    project_info: Optional[dict] = None,
) -> None:
    if not task:
        return
    try:
        task_id = int(task.get("id") or 0)
    except (TypeError, ValueError):
        return
    if task_id <= 0:
        return
    project_path = _task_timeline_project_path(task, project_info=project_info)
    if not project_path:
        return
    try:
        from codepilot.core.workflow_state import append_task_timeline_event

        append_task_timeline_event(
            project_path,
            task_id,
            event=event,
            actor=actor or str(task.get("agent") or ""),
            source=source or "codepilot.task",
            message=message,
            artifact_path=artifact_path,
            time=timestamp,
        )
    except Exception:  # noqa: BLE001
        return


def _record_task_created_timeline(task: dict | None) -> None:
    if not task:
        return
    task_id = int(task.get("id") or 0)
    title = str(task.get("title") or "").strip()
    source = str(task.get("source") or "user").strip()
    _append_task_timeline(
        task,
        event="created",
        source="codepilot.task.create",
        message=f"任务 #{task_id} 已创建：{title}" if title else f"任务 #{task_id} 已创建",
        timestamp=str(task.get("created_at") or "") or None,
    )
    if source and source not in {"user", "webhook"}:
        _append_task_timeline(
            task,
            event="planned",
            actor=source,
            source="codepilot.task.create",
            message=f"任务 #{task_id} 来自 {source} 规划/导入",
            timestamp=str(task.get("created_at") or "") or None,
        )


def _record_task_update_timeline(before: dict | None, task: dict | None, changed_fields: set[str]) -> None:
    if not task:
        return
    before = before or {}
    status = str(task.get("status") or "").strip()
    previous_status = str(before.get("status") or "").strip()
    phase = str(task.get("run_phase") or "").strip()
    task_id = int(task.get("id") or 0)
    actor = str(task.get("agent") or "runner")
    log_path = str(task.get("current_log_path") or "")

    if "status" in changed_fields and status == "in_progress" and previous_status != "in_progress":
        _append_task_timeline(
            task,
            event="claimed",
            actor=actor,
            source="codepilot.task.update",
            message=f"任务 #{task_id} 已领取执行",
            artifact_path=log_path,
            timestamp=str(task.get("started_at") or task.get("heartbeat_at") or "") or None,
        )

    runtime_changed = changed_fields & {"active_pid", "current_log_path", "run_phase"}
    if status == "in_progress" and runtime_changed and (task.get("active_pid") or log_path):
        if (
            str(before.get("active_pid") or "") != str(task.get("active_pid") or "")
            or str(before.get("current_log_path") or "") != log_path
            or str(before.get("run_phase") or "") != phase
        ):
            _append_task_timeline(
                task,
                event="agent_started",
                actor=actor,
                source="codepilot.runtime",
                message=f"任务 #{task_id} agent 启动阶段：{phase or 'runtime'}",
                artifact_path=log_path,
                timestamp=str(task.get("heartbeat_at") or "") or None,
            )

    if status == "backlog" and "error_message" in changed_fields and str(task.get("error_message") or "").strip():
        _append_task_timeline(
            task,
            event="blocked",
            actor="runner",
            source="codepilot.task.update",
            message=str(task.get("error_message") or "")[:500],
            artifact_path=log_path,
            timestamp=str(task.get("heartbeat_at") or task.get("completed_at") or "") or None,
        )

    if "status" in changed_fields and status == "done":
        _append_task_timeline(
            task,
            event="done",
            actor="runner",
            source="codepilot.task.update",
            message=_task_update_summary(task)[:500] or f"任务 #{task_id} 已完成",
            artifact_path=log_path,
            timestamp=str(task.get("completed_at") or "") or None,
        )

    if "status" in changed_fields and status == "failed":
        _append_task_timeline(
            task,
            event="failed",
            actor="runner",
            source="codepilot.task.update",
            message=_task_update_summary(task)[:500] or f"任务 #{task_id} 失败",
            artifact_path=log_path,
            timestamp=str(task.get("completed_at") or "") or None,
        )

    if "status" in changed_fields and status == "cancelled":
        _append_task_timeline(
            task,
            event="blocked",
            actor="runner",
            source="codepilot.task.update",
            message=_task_update_summary(task)[:500] or f"任务 #{task_id} 已取消",
            artifact_path=log_path,
            timestamp=str(task.get("completed_at") or "") or None,
        )


def _record_task_log_timeline(task_id: int, log: dict) -> None:
    task = get_task(task_id)
    if not task:
        return
    phase = str(log.get("phase") or "").strip()
    agent = str(log.get("agent") or task.get("agent") or "").strip()
    log_path = str(task.get("current_log_path") or "")
    if log.get("started_at"):
        _append_task_timeline(
            task,
            event="agent_started",
            actor=agent,
            source="codepilot.task_log",
            message=f"{phase or 'phase'} 阶段开始",
            artifact_path=log_path,
            timestamp=str(log.get("started_at") or ""),
        )
    phase_key = f"{phase} {agent}".lower()
    if "review" in phase_key and log.get("finished_at"):
        exit_code = log.get("exit_code")
        suffix = f" exit={exit_code}" if exit_code is not None else ""
        _append_task_timeline(
            task,
            event="reviewed",
            actor=agent or "reviewer",
            source="codepilot.task_log",
            message=f"{phase or 'reviewer'} 阶段结束{suffix}",
            artifact_path=log_path,
            timestamp=str(log.get("finished_at") or ""),
        )


def _publish_task_updated_event(task: Optional[dict], changed_fields: set[str]) -> None:
    if not task or not (changed_fields & _TASK_EVENT_FIELDS):
        return
    project_info: Optional[dict] = None
    project_name = str(task.get("project") or "")
    if not project_name:
        return
    try:
        from codepilot.core.event_plugins import build_event, dispatch_event_to_sinks

        project_root = str(task.get("project_path") or "")
        if not project_root:
            project_info = get_project(project_name)
            project_root = str((project_info or {}).get("path") or "")
        if not project_root:
            raise ValueError("project root is empty")
        else:
            payload = {
                "task_id": int(task.get("id")),
                "status": str(task.get("status") or ""),
                "phase": str(task.get("run_phase") or ""),
                "summary": _task_update_summary(task),
                "changed_fields": sorted(changed_fields),
                "retry_count": int(task.get("retry_count") or 0),
                "max_retries": int(task.get("max_retries") or 0),
                "error_message": str(task.get("error_message") or ""),
                "completed_at": task.get("completed_at"),
            }
            event = build_event(
                project_name,
                "task.updated",
                source="codepilot.task",
                payload=payload,
                event_id_prefix=f"task-{task.get('id')}",
            )
            dispatch_event_to_sinks(project_root, event)
    except Exception:  # noqa: BLE001
        # 发布事件失败不应阻止主流程
        pass
    _record_task_update_memory(task, changed_fields, project_info=project_info)


def create_task(
    project: str,
    title: str,
    content: str = "",
    agent: str = "dual",
    priority: str = "P2",
    depends_on: Optional[list[int]] = None,
    project_path: Optional[str] = None,
    max_retries: int = 3,
    source: str = "user",
    dedup_key: Optional[str] = None,
    fallback_reason: Optional[str] = None,
    work_item: Optional[dict] = None,
) -> dict:
    """Create a task or return the existing active duplicate."""
    normalized_work_item = None
    if work_item is not None:
        from codepilot.core.work_item import coerce_work_item

        normalized_work_item = coerce_work_item(work_item, fallback_source=source, fallback_raw_text=title)
        source = normalized_work_item["source"] or source or "user"

    dedup_key = dedup_key or compute_dedup_key(project, title, content)
    resolved_project_path = _resolve_project_path(project, project_path)

    with get_write_conn() as conn:
        existing = _find_active_duplicate(conn, project, dedup_key)
        if existing:
            import click

            click.echo(f"[i] 已存在任务 #{existing['id']}")
            return existing

        task_id = _insert_task_row(
            conn,
            project=project,
            title=title,
            content=content,
            agent=agent,
            priority=priority,
            depends_on=depends_on,
            project_path=resolved_project_path,
            max_retries=max_retries,
            source=source,
            dedup_key=dedup_key,
            fallback_reason=fallback_reason,
        )
    _invalidate_task_caches()
    created = get_task(task_id)
    if normalized_work_item is not None and created:
        from codepilot.core.work_item import persist_task_work_item

        persist_task_work_item(created, normalized_work_item)
    _record_task_created_timeline(created)
    return created


def existing_dedup_keys(project: str) -> set[str]:
    """Return dedup_keys already present in active tasks."""
    with get_read_conn() as conn:
        return _fetch_active_dedup_keys(conn, project)


def get_task(task_id: int) -> Optional[dict]:
    """Fetch a task by id."""
    cache_key = ("task_by_id", int(task_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_task_by_id(conn, task_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_tasks(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List tasks with optional filters."""
    cache_key = ("tasks", project or "", status or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_tasks(conn, project, status)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def update_task(task_id: int, **fields) -> Optional[dict]:
    """Update a task with the provided field values."""
    updates = _prepare_task_updates(fields)
    if not updates:
        return get_task(task_id)

    before = get_task(task_id)
    with get_write_conn() as conn:
        _update_task_fields(conn, task_id, updates)
    _invalidate_task_caches()
    updated = get_task(task_id)
    _publish_task_updated_event(updated, set(updates))
    _record_task_update_timeline(before, updated, set(updates))
    return updated


def update_task_if_status(task_id: int, expected_status: str, **fields) -> Optional[dict]:
    """Update a task only while it still has the expected status."""
    updates = _prepare_task_updates(fields)
    if not updates:
        return get_task(task_id)

    before = get_task(task_id)
    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [task_id, expected_status]
    with get_write_conn() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ? AND status = ?",
            values,
        )
        changed = cur.rowcount > 0
    _invalidate_task_caches()
    updated = get_task(task_id)
    if changed:
        _publish_task_updated_event(updated, set(updates))
        _record_task_update_timeline(before, updated, set(updates))
    return updated


def delete_task(task_id: int) -> bool:
    """Delete one task row and its logs."""
    task = get_task(task_id)
    with get_write_conn() as conn:
        removed = _delete_task_with_logs(conn, task_id)
    if removed:
        _invalidate_task_caches()
        if task:
            _record_task_memory_event(
                task,
                event_type="task.deleted",
                status=str(task.get("status") or "deleted"),
                tags=["task", "feedback", "deleted"],
            )
    return removed


def increment_task_retry(task_id: int, error_message: str) -> dict:
    """Increment retry counter and move the task to backlog or failed."""
    task = get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} does not exist")

    retry_count = int(task.get("retry_count") or 0) + 1
    max_retries = int(task.get("max_retries") or 3)
    next_status = "failed" if retry_count >= max_retries else "backlog"
    return update_task(
        task_id,
        retry_count=retry_count,
        status=next_status,
        error_message=error_message,
        run_phase=None,
        heartbeat_at=None,
        active_pid=None,
        current_log_path=None,
        last_output=None,
        stop_requested=0,
        stop_reason=None,
    )


def reset_task_for_retry(task_id: int, *, reset_retry_count: bool = True) -> dict:
    """Reset a task into backlog so it can be manually retried."""
    task = get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} does not exist")

    updates: dict[str, object] = {
        "status": "backlog",
        "branch_name": None,
        "worktree_path": None,
        "error_message": None,
        "delivery_record": None,
        "started_at": None,
        "completed_at": None,
        "run_phase": None,
        "heartbeat_at": None,
        "active_pid": None,
        "current_log_path": None,
        "last_output": None,
        "stop_requested": 0,
        "stop_reason": None,
    }
    if reset_retry_count:
        updates["retry_count"] = 0
    updated = update_task(task_id, **updates)
    if updated:
        _record_task_memory_event(
            updated,
            event_type="task.retried",
            status="backlog",
            tags=["task", "feedback", "retried"],
            changed_fields=set(updates),
            extra_details={
                "from_status": str(task.get("status") or ""),
                "from_retry_count": int(task.get("retry_count") or 0),
                "reset_retry_count": bool(reset_retry_count),
            },
        )
    return updated


def next_backlog_task(project: str, *, exclude_task_ids: Optional[set[int]] = None) -> list[dict]:
    """Return the next runnable backlog task for a project."""
    with get_read_conn() as conn:
        return _query_next_backlog_task(conn, project, exclude_task_ids=exclude_task_ids)


def compute_agent_eta_seconds(
    project: str,
    agent: Optional[str] = None,
    *,
    sample_size: int = 20,
) -> Optional[int]:
    """Return a rough ETA in seconds from recent completed tasks."""
    with get_read_conn() as conn:
        rows = _query_done_task_windows(
            conn,
            project,
            agent=agent,
            sample_size=sample_size,
        )

    durations: list[float] = []
    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
            completed = datetime.fromisoformat(row["completed_at"])
        except (ValueError, TypeError):
            continue
        delta = (completed - started).total_seconds()
        if delta > 0:
            durations.append(delta)

    if len(durations) < 3:
        return None

    durations.sort()
    mid = len(durations) // 2
    if len(durations) % 2:
        return int(durations[mid])
    return int((durations[mid - 1] + durations[mid]) / 2)


def get_task_stats(project: str) -> dict:
    """Return aggregate task counts for a project."""
    cache_key = ("task_stats", project)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        stats = _query_task_status_counts(conn, project)
    result = {
        "backlog": stats.get("backlog", 0),
        "in_progress": stats.get("in_progress", 0),
        "done": stats.get("done", 0),
        "failed": stats.get("failed", 0),
        "cancelled": stats.get("cancelled", 0),
        "total": sum(stats.values()),
    }
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def get_task_stats_by_path(path: str | Path) -> Optional[dict]:
    """Return task counts for the deepest registered project containing *path*."""
    project = find_project_by_path(path)
    if not project:
        return None
    return get_task_stats(project["name"])


def get_current_project_task_stats(path: str | Path | None = None) -> Optional[dict]:
    """Return task counts for the registered project containing *path* or cwd."""
    return get_task_stats_by_path(path or Path.cwd())


def get_current_project_stats(path: str | Path | None = None) -> Optional[dict]:
    """Backward-compatible alias for current-project task counts."""
    return get_current_project_task_stats(path)


def create_task_log(
    task_id: int,
    agent: str,
    phase: str,
    output: str = "",
    exit_code: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration: Optional[int] = None,
) -> dict:
    """Create a task execution log row."""
    with get_write_conn() as conn:
        result = _insert_task_log_row(
            conn,
            task_id=task_id,
            agent=agent,
            phase=phase,
            output=output,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
        )
    _cache_invalidate("task_logs")
    _record_task_log_timeline(task_id, result)
    return result


def list_task_logs(task_id: int) -> list[dict]:
    """List logs for a task."""
    cache_key = ("task_logs", int(task_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_task_logs(conn, task_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def find_stale_in_progress(
    project: str,
    stale_minutes: int = 30,
) -> list[dict]:
    """Return in_progress tasks whose heartbeat exceeds *stale_minutes*."""
    with get_read_conn() as conn:
        return _query_stale_in_progress(conn, project, stale_minutes)


def find_orphan_log_paths(project: str) -> list[dict]:
    """Return tasks whose current_log_path is set but the file no longer exists."""
    with get_read_conn() as conn:
        return _query_orphan_log_paths(conn, project)


def find_old_done_tasks(
    project: str,
    retention_days: int = 30,
) -> list[dict]:
    """Return done tasks older than *retention_days* that still have a log path."""
    with get_read_conn() as conn:
        return _query_old_done_tasks(conn, project, retention_days)


def create_session(
    project: str,
    title: str = "新会话",
) -> dict:
    """Create a new conversation session for a project."""
    with get_write_conn() as conn:
        session_id = _insert_session(conn, project, title)
    _invalidate_session_caches()
    return get_session(session_id)  # type: ignore[return-value]


def get_session(session_id: int) -> Optional[dict]:
    """Fetch a session by id."""
    cache_key = ("session_by_id", int(session_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_session_by_id(conn, session_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_sessions(
    project: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """List sessions with optional filters, most recent first."""
    cache_key = ("sessions", project or "", status or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_sessions(conn, project, status)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def update_session(session_id: int, **fields) -> Optional[dict]:
    """Update session fields."""
    updates = {key: value for key, value in fields.items() if key in {"title", "status"}}
    if not updates:
        return get_session(session_id)
    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with get_write_conn() as conn:
        _update_session_fields(conn, session_id, updates)
    _cache_invalidate("sessions", "session_by_id")
    return get_session(session_id)


def delete_session(session_id: int) -> bool:
    """Delete a session and its messages."""
    with get_write_conn() as conn:
        _delete_session_messages(conn, session_id)
        removed = _delete_session_row(conn, session_id) > 0
    if removed:
        _invalidate_session_caches()
    return removed


def create_session_message(
    session_id: int,
    role: str,
    content: str,
    intent: Optional[str] = None,
    task_ids: Optional[list[int]] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Add a message to a session and touch updated_at."""
    with get_write_conn() as conn:
        msg_id = _insert_session_message(conn, session_id, role, content, intent, task_ids, metadata)
        _touch_session_updated_at(conn, session_id)
        result = _fetch_session_message_by_id(conn, msg_id)
    _invalidate_session_caches()
    return result  # type: ignore[return-value]


def update_session_message(
    message_id: int,
    *,
    content: Optional[str] = None,
    intent: Optional[str] = None,
    task_ids: Optional[list[int]] = None,
    metadata: Optional[dict] = None,
) -> Optional[dict]:
    """Update one session message and invalidate session read caches."""
    updates: dict[str, object] = {}
    if content is not None:
        updates["content"] = content
    if intent is not None:
        updates["intent"] = intent
    if task_ids is not None:
        updates["task_ids"] = json.dumps(task_ids) if task_ids else None
    if metadata is not None:
        updates["metadata"] = json.dumps(metadata, ensure_ascii=False) if metadata else None
    if not updates:
        return None

    with get_write_conn() as conn:
        row = _fetch_session_message_by_id(conn, message_id)
        if not row:
            return None
        _update_session_message_fields(conn, message_id, updates)
        _touch_session_updated_at(conn, int(row["session_id"]))
        result = _fetch_session_message_by_id(conn, message_id)
    _invalidate_session_caches()
    return result  # type: ignore[return-value]


def list_session_messages(session_id: int) -> list[dict]:
    """List messages in a session, oldest first."""
    cache_key = ("session_messages", int(session_id))
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_session_messages(conn, session_id)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def get_service_state(service: str, scope: str = "") -> Optional[dict]:
    """Return one service runtime state row."""
    normalized_scope = _normalize_service_scope(scope)
    cache_key = ("service_state", service, normalized_scope)
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _fetch_service_state(conn, service, normalized_scope)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def list_service_states(service: Optional[str] = None) -> list[dict]:
    """List service runtime state rows."""
    cache_key = ("service_states", service or "")
    cached = _cache_get(cache_key)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    with get_read_conn() as conn:
        result = _query_service_states(conn, service)
    return _cache_set(cache_key, result)  # type: ignore[return-value]


def upsert_service_state(
    service: str,
    scope: str = "",
    *,
    pid: Optional[int] = None,
    status: str = "running",
    log_path: Optional[str] = None,
    heartbeat_at: Optional[str] = None,
    meta: Optional[dict] = None,
) -> dict:
    """Create or update a service runtime state row."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    normalized_scope = _normalize_service_scope(scope)
    with get_write_conn() as conn:
        _upsert_service_state_row(
            conn,
            _ensure_service_states_schema,
            service=service,
            scope=normalized_scope,
            pid=pid,
            status=status,
            log_path=log_path,
            heartbeat_at=heartbeat_at or now_iso,
            meta_json=json.dumps(meta or {}, ensure_ascii=False),
            updated_at=now_iso,
        )
    _invalidate_service_state_caches()
    state = get_service_state(service, normalized_scope)
    return state or {}


def claim_service_state(
    service: str,
    scope: str = "",
    *,
    pid: Optional[int] = None,
    status: str = "running",
    log_path: Optional[str] = None,
    heartbeat_at: Optional[str] = None,
    meta: Optional[dict] = None,
) -> bool:
    """Atomically create one service-state row; return False when it already exists."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    normalized_scope = _normalize_service_scope(scope)
    with get_write_conn() as conn:
        claimed = _insert_service_state_row_if_absent(
            conn,
            _ensure_service_states_schema,
            service=service,
            scope=normalized_scope,
            pid=pid,
            status=status,
            log_path=log_path,
            heartbeat_at=heartbeat_at or now_iso,
            meta_json=json.dumps(meta or {}, ensure_ascii=False),
            updated_at=now_iso,
        )
    if claimed:
        _invalidate_service_state_caches()
    return claimed


def touch_service_state(
    service: str,
    scope: str = "",
    *,
    pid: Optional[int] = None,
    log_path: Optional[str] = None,
    status: str = "running",
) -> dict:
    """Refresh only heartbeat/PID/log_path while preserving existing meta."""
    normalized_scope = _normalize_service_scope(scope)
    existing = get_service_state(service, normalized_scope) or {}
    meta = existing.get("meta") if isinstance(existing.get("meta"), dict) else {}
    next_pid = pid if pid is not None else existing.get("pid")
    next_log = log_path if log_path is not None else existing.get("log_path")
    return upsert_service_state(
        service,
        normalized_scope,
        pid=next_pid,
        status=status,
        log_path=next_log,
        meta=meta,
    )


def clear_service_state(service: str, scope: str = "") -> bool:
    """Delete one service runtime state row."""
    with get_write_conn() as conn:
        removed = _delete_service_state_row(
            conn,
            _ensure_service_states_schema,
            service=service,
            scope=scope,
        )
    if removed:
        _invalidate_service_state_caches()
    return removed
