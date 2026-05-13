"""Shared command-level JSON output contract helpers."""

from __future__ import annotations

import json
from typing import Any

import click


def resolve_json_mode(ctx: click.Context | None, local_json_mode: bool = False) -> bool:
    """Resolve effective JSON mode from local flag or root ``--json`` option."""
    if local_json_mode:
        return True
    if ctx is None:
        return False
    root = ctx.find_root()
    root_obj = root.obj if root and isinstance(root.obj, dict) else {}
    return bool(root_obj.get("json_mode", False))


def build_json_payload(
    command: str,
    *,
    ok: bool,
    data: Any,
    error: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build the normalized JSON envelope for CLI commands."""
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "command": command,
        "data": data,
    }
    if error:
        err: dict[str, str] = {"message": str(error)}
        if error_code:
            err["code"] = error_code
        payload["error"] = err
    return payload


def emit_json_payload(
    command: str,
    *,
    ok: bool,
    data: Any,
    error: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Serialize and print the normalized JSON envelope."""
    payload = build_json_payload(
        command,
        ok=ok,
        data=data,
        error=error,
        error_code=error_code,
    )
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return payload
