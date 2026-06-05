"""Codex CLI MCP launch-plan builder."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from codepilot import __version__
from codepilot.core.paths import global_storage_root
from codepilot.mcp.launchers import LaunchPlan, MCPServerSpec, normalize_mcp_servers
from codepilot.mcp.launchers.language import with_interaction_instructions

_CODEPILOT_PLUGIN_NAME = "codepilot"
_CODEPILOT_PLUGIN_MARKETPLACE = "codepilot"
_CODEPILOT_PROFILE_NAME = "codepilot"


def build_launch_plan(
    *,
    executable: str,
    prompt: str = "",
    mcp_servers: Mapping[str, Any] | Iterable[MCPServerSpec] | None = None,
    env: Mapping[str, str] | None = None,
    session: str | None = None,
    language: str = "en",
) -> LaunchPlan:
    servers = normalize_mcp_servers(mcp_servers)
    config = {"mcp_servers": _codex_servers(servers)}
    config_args = _config_args(servers)
    session_id = str(session or "").strip()
    config_files: dict[str, str] = {}
    if prompt:
        # Headless one-shot via `codex exec`.
        command = [
            *_codex_executable_command(executable, interactive=False),
            *config_args,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        command.append(with_interaction_instructions(prompt, language=language))
    else:
        # Interactive TUI. CodePilot registers a native Codex plugin command so
        # `/task ...` works inside the TUI instead of being parsed by chat.py.
        config_files = _codepilot_plugin_files(env)
        profile_args = ["--profile", _CODEPILOT_PROFILE_NAME]
        if session_id:
            command = [
                *_codex_executable_command(executable, interactive=True),
                *profile_args,
                *config_args,
                "resume",
                session_id,
            ]
        else:
            command = [
                *_codex_executable_command(executable, interactive=True),
                *profile_args,
                *config_args,
            ]
    return LaunchPlan(
        agent="codex",
        command=command,
        env=dict(env or {}),
        mcp_config=config,
        config_args=config_args,
        config_files=config_files,
    )


def _codex_executable_command(executable: str, *, interactive: bool) -> list[str]:
    native = _native_windows_codex_exe(executable) if interactive else ""
    if native:
        return [native]
    path = Path(str(executable))
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return [executable]
    codex_js = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not codex_js.is_file():
        return [executable]
    node = path.parent / "node.exe"
    return [str(node if node.is_file() else "node"), str(codex_js)]


def _native_windows_codex_exe(executable: str) -> str:
    if os.name != "nt":
        return ""
    path = Path(str(executable))
    if path.suffix.lower() == ".exe":
        return ""
    native = shutil.which("codex.exe")
    if not native:
        return ""
    try:
        native_path = Path(native).resolve()
        original = path.resolve()
        if native_path == original:
            return ""
    except Exception:
        pass
    return native


def _codex_servers(servers: list[MCPServerSpec]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.url:
            item: dict[str, Any] = {"url": server.url, "enabled": server.enabled}
            if server.headers:
                item["http_headers"] = dict(server.headers)
        else:
            item = {"command": server.command, "enabled": server.enabled}
            if server.args:
                item["args"] = list(server.args)
            if server.env:
                item["env"] = dict(server.env)
        result[server.name] = item
    return result


def _config_args(servers: list[MCPServerSpec]) -> list[str]:
    args: list[str] = []
    for server in servers:
        prefix = f"mcp_servers.{server.name}"
        values: list[tuple[str, Any]]
        if server.url:
            values = [("url", server.url)]
            if server.headers:
                values.append(("http_headers", dict(server.headers)))
        else:
            values = [("command", server.command)]
            if server.args:
                values.append(("args", list(server.args)))
            if server.env:
                values.append(("env", dict(server.env)))
        values.append(("enabled", server.enabled))
        for key, value in values:
            args.extend(["-c", f"{prefix}.{key}={_toml_literal(value)}"])
    return args


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if "'" not in value and "\n" not in value and "\r" not in value:
            return f"'{value}'"
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        pairs = [
            f"{_toml_key(str(key))}={_toml_literal(item)}"
            for key, item in value.items()
        ]
        return "{" + ",".join(pairs) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _toml_key(value: str) -> str:
    if value.replace("_", "").replace("-", "").isalnum() and value[:1].isalpha():
        return value
    return json.dumps(value, ensure_ascii=False)


def _codex_home(env: Mapping[str, str] | None) -> Path:
    raw = ""
    if env:
        raw = str(env.get("CODEX_HOME") or "").strip()
    raw = raw or os.environ.get("CODEX_HOME", "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".codex"


def _codepilot_marketplace_root() -> Path:
    return global_storage_root() / "codex" / "marketplace"


def _codepilot_plugin_version() -> str:
    return str(__version__ or "0.0.0").strip() or "0.0.0"


def _codepilot_plugin_cache_dir(env: Mapping[str, str] | None) -> Path:
    return (
        _codex_home(env)
        / "plugins"
        / "cache"
        / _CODEPILOT_PLUGIN_MARKETPLACE
        / _CODEPILOT_PLUGIN_NAME
        / _codepilot_plugin_version()
    )


def _codepilot_plugin_files(env: Mapping[str, str] | None) -> dict[str, str]:
    codex_home = _codex_home(env)
    marketplace_root = _codepilot_marketplace_root()
    marketplace_plugin = marketplace_root / "plugins" / _CODEPILOT_PLUGIN_NAME
    cache_plugin = _codepilot_plugin_cache_dir(env)
    manifest = _codepilot_plugin_manifest()
    command = _task_command_markdown()
    return {
        str(codex_home / f"{_CODEPILOT_PROFILE_NAME}.config.toml"): _codepilot_profile_toml(
            marketplace_root
        ),
        str(marketplace_root / ".agents" / "plugins" / "marketplace.json"): _marketplace_json(),
        str(marketplace_plugin / ".codex-plugin" / "plugin.json"): manifest,
        str(marketplace_plugin / "commands" / "task.md"): command,
        str(cache_plugin / ".codex-plugin" / "plugin.json"): manifest,
        str(cache_plugin / "commands" / "task.md"): command,
    }


def _codepilot_profile_toml(marketplace_root: Path) -> str:
    plugin_ref = f"{_CODEPILOT_PLUGIN_NAME}@{_CODEPILOT_PLUGIN_MARKETPLACE}"
    return (
        "[marketplaces.codepilot]\n"
        'last_updated = "2000-01-01T00:00:00Z"\n'
        'source_type = "local"\n'
        f"source = {_toml_literal(str(marketplace_root))}\n\n"
        f'[plugins."{plugin_ref}"]\n'
        "enabled = true\n"
    )


def _marketplace_json() -> str:
    return (
        json.dumps(
            {
                "name": _CODEPILOT_PLUGIN_MARKETPLACE,
                "interface": {"displayName": "CodePilot"},
                "plugins": [
                    {
                        "name": _CODEPILOT_PLUGIN_NAME,
                        "source": {
                            "source": "local",
                            "path": f"./plugins/{_CODEPILOT_PLUGIN_NAME}",
                        },
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": "Developer Tools",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _codepilot_plugin_manifest() -> str:
    return (
        json.dumps(
            {
                "name": _CODEPILOT_PLUGIN_NAME,
                "version": _codepilot_plugin_version(),
                "description": "CodePilot workflow slash commands for Codex",
                "interface": {
                    "displayName": "CodePilot",
                    "shortDescription": "Run CodePilot workflows from Codex slash commands",
                    "developerName": "CodePilot",
                    "category": "Developer Tools",
                    "capabilities": ["Interactive"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _task_command_markdown() -> str:
    return """# /task

Run a requirement through the CodePilot MCP pipeline in tool-only mode.

## Arguments

- requirement: $ARGUMENTS

## CodePilot TASK MODE

The user invoked `/task` with this requirement:
$ARGUMENTS

Hard constraints:

- Use ONLY CodePilot MCP tools for this request.
- Do NOT read, write, edit, patch, or inspect repository files directly.
- Do NOT run shell commands or implement code yourself.
- First call codepilot_pipeline(requirement=$ARGUMENTS).
- If codepilot_pipeline cannot complete, use only CodePilot MCP fallback tools such as codepilot_run_once, codepilot_workflow_next(auto=true), codepilot_workflow_status, codepilot_daemon_status, codepilot_create_task, and codepilot_generate_breakdown.
- Monitor tool results and report task ids, failures, manual follow-up commands, and validation results returned by the tools.
"""
