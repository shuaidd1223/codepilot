"""Runner for scheduled and event-triggered ``agent_job`` payloads."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from codepilot.ai_support.cli_families import get_family
from codepilot.ai_support.providers import CLI_PROVIDERS
from codepilot.scheduled.audit import append_agent_job_audit


SubprocessRun = Callable[..., Any]


@dataclass(frozen=True)
class AgentJobResult:
    agent: str
    command: list[str]
    stdout: str
    stderr: str
    exit_code: int | None
    tool_call_count: int
    token_usage: dict[str, int]
    cost: float
    tools: list[dict[str, Any]]
    dry_run: bool
    audit_log_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_command(agent: str, prompt: str, commands: dict[str, str] | None) -> tuple[str, list[str], int]:
    family = get_family(agent)
    if family is None:
        raise ValueError(f"Unsupported agent family: {agent!r}")
    provider = CLI_PROVIDERS[family.provider_key]
    cmd = str((commands or {}).get(family.name) or provider.cmd)
    args: list[str] = []
    for item in provider.args_template:
        args.append(
            str(item)
            .replace("{prompt}", str(prompt or ""))
            .replace("{node_modules}", _node_modules_path())
        )
    return family.name, [cmd, *args], int(provider.timeout or 180)


def _node_modules_path() -> str:
    # Kept local to avoid relying on provider private helpers from this narrow runner.
    return str(Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules")


def _json_default(value: Any) -> str:
    return str(value)


def _build_env(mcp_servers: Any | None) -> dict[str, str]:
    env = os.environ.copy()
    if mcp_servers:
        # Integration point for the Phase 5 MCP launcher: it can hand this runner
        # already-resolved server descriptors without forcing scheduler code to know
        # per-agent MCP flag syntax yet.
        env["CODEPILOT_MCP_SERVERS"] = json.dumps(
            mcp_servers,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    return env


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def _normalize_tool(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        name = str(item.get("name") or item.get("tool") or item.get("type") or "")
        out = dict(item)
        if name and "name" not in out:
            out["name"] = name
        return out
    return {"name": str(item)}


def _int_usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    usage: dict[str, int] = {}
    for key, value in raw.items():
        try:
            usage[str(key)] = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    return usage


def _extract_run_metadata(stdout: str, stderr: str) -> tuple[list[dict[str, Any]], dict[str, int], float]:
    tools: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    cost = 0.0
    for obj in _iter_json_objects(stdout) + _iter_json_objects(stderr):
        raw_tools = obj.get("tool_calls", obj.get("tools", []))
        if isinstance(raw_tools, list):
            tools.extend(_normalize_tool(item) for item in raw_tools)
        raw_usage = obj.get("token_usage", obj.get("usage", {}))
        if isinstance(raw_usage, dict):
            usage.update(_int_usage(raw_usage))
        if "cost" in obj:
            try:
                cost = float(obj.get("cost") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
    return tools, usage, cost


def run_agent_job(
    job: dict[str, Any],
    *,
    project_root: str | Path,
    dry_run: bool = False,
    subprocess_run: SubprocessRun = subprocess.run,
    commands: dict[str, str] | None = None,
    mcp_servers: Any | None = None,
    timeout_seconds: int | None = None,
) -> AgentJobResult:
    """Run or dry-run one ``agent_job`` payload and append its audit record."""

    if str(job.get("type") or "") != "agent_job":
        raise ValueError("scheduled runner only accepts agent_job payloads")

    prompt = str(job.get("prompt") or "")
    agent, command, default_timeout = _build_command(str(job.get("agent") or ""), prompt, commands)
    timeout = int(timeout_seconds or default_timeout)
    env = _build_env(mcp_servers)

    if dry_run:
        result = AgentJobResult(
            agent=agent,
            command=command,
            stdout="",
            stderr="",
            exit_code=None,
            tool_call_count=0,
            token_usage={},
            cost=0.0,
            tools=[],
            dry_run=True,
        )
        audit_path = append_agent_job_audit(
            project_root,
            _audit_entry(job, result, prompt=prompt),
        )
        return _with_audit_path(result, audit_path)

    try:
        completed = subprocess_run(
            command,
            cwd=str(Path(project_root).resolve()),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        exit_code = int(getattr(completed, "returncode", 0))
        stdout = _text(getattr(completed, "stdout", ""))
        stderr = _text(getattr(completed, "stderr", ""))
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr) or f"timeout after {timeout}s"
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)

    tools, token_usage, cost = _extract_run_metadata(stdout, stderr)
    result = AgentJobResult(
        agent=agent,
        command=command,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        tool_call_count=len(tools),
        token_usage=token_usage,
        cost=cost,
        tools=tools,
        dry_run=False,
    )
    audit_path = append_agent_job_audit(project_root, _audit_entry(job, result, prompt=prompt))
    return _with_audit_path(result, audit_path)


def _audit_entry(job: dict[str, Any], result: AgentJobResult, *, prompt: str) -> dict[str, Any]:
    return {
        "trigger": job.get("trigger") or {},
        "agent": result.agent,
        "prompt": prompt,
        "tools": result.tools,
        "cost": result.cost,
        "exit_code": result.exit_code,
        "dry_run": result.dry_run,
        "job_name": job.get("name") or "",
        "project": job.get("project") or "",
    }


def _with_audit_path(result: AgentJobResult, path: Path) -> AgentJobResult:
    return AgentJobResult(
        agent=result.agent,
        command=result.command,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        tool_call_count=result.tool_call_count,
        token_usage=result.token_usage,
        cost=result.cost,
        tools=result.tools,
        dry_run=result.dry_run,
        audit_log_path=str(path),
    )
