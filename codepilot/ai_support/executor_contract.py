"""Executor contract and fallback telemetry helpers.

This module is intentionally declarative: runtime dispatch still lives in the
existing CLI family, gateway, and built-in executor modules. The contract here
gives those callsites one stable vocabulary for executor phases, future
registration points, and fallback failure reasons.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from codepilot.ai_support.cli_families import CLI_FAMILIES, get_family


EXECUTOR_CONTRACT_PHASES: tuple[str, ...] = (
    "prepare",
    "execute",
    "observe",
    "validate",
    "review",
    "repair",
)

_TELEMETRY_PREFIX = "CODEPILOT_EXECUTOR_TELEMETRY:"
_TELEMETRY_RE = re.compile(r"^CODEPILOT_EXECUTOR_TELEMETRY:\s*(\{.*\})\s*$", re.MULTILINE)


class ExecutorFallbackReason(str, Enum):
    """Stable enum for executor fallback decisions."""

    NONE = "none"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNSUPPORTED_MODEL = "unsupported_model"
    INVOCATION_ERROR = "invocation_error"


@dataclass(frozen=True)
class ExecutorContract:
    """Static contract for one executor family."""

    family: str
    provider_key: str
    command_env_var: str
    phases: tuple[str, ...]
    stage_handlers: Mapping[str, str]
    supports_structured_cli: bool = True
    supports_text_cli: bool = True
    supports_builtin_executor: bool = True
    reserved: bool = False


def _stage_handlers_for_family(family: str) -> dict[str, str]:
    return {
        "prepare": f"{family}.check_provider_availability + env_bridge",
        "execute": f"{family}.cli_subprocess",
        "observe": "stdout/stderr/exit_code + git/artifact collectors",
        "validate": "validation commands or executor-native validation output",
        "review": "review phase runner or reviewer verdict parser",
        "repair": "failure triage + retry prompt generation",
    }


def _contract_for_registered_family(family_name: str) -> ExecutorContract:
    family = CLI_FAMILIES[family_name]
    return ExecutorContract(
        family=family.name,
        provider_key=family.provider_key,
        command_env_var=family.env_var,
        phases=EXECUTOR_CONTRACT_PHASES,
        stage_handlers=_stage_handlers_for_family(family.name),
        supports_structured_cli=family.is_planner,
        supports_text_cli=True,
        supports_builtin_executor=True,
    )


EXECUTOR_CONTRACTS: dict[str, ExecutorContract] = {
    family_name: _contract_for_registered_family(family_name)
    for family_name in CLI_FAMILIES
}

RESERVED_EXECUTOR_CONTRACTS: dict[str, ExecutorContract] = {
    "aider": ExecutorContract(
        family="aider",
        provider_key="aider",
        command_env_var="CODEPILOT_AIDER_CMD",
        phases=EXECUTOR_CONTRACT_PHASES,
        stage_handlers={
            phase: "reserved registration point; no runtime invocation"
            for phase in EXECUTOR_CONTRACT_PHASES
        },
        supports_structured_cli=False,
        supports_text_cli=False,
        supports_builtin_executor=False,
        reserved=True,
    )
}


def get_executor_contract(family_name: str) -> ExecutorContract | None:
    """Return the registered or reserved contract for *family_name*."""
    family = get_family(family_name)
    if family is not None:
        return EXECUTOR_CONTRACTS.get(family.name)
    key = str(family_name or "").strip().lower()
    return RESERVED_EXECUTOR_CONTRACTS.get(key)


def reserved_executor_contract_names() -> list[str]:
    """Return executor names reserved for future implementations."""
    return list(RESERVED_EXECUTOR_CONTRACTS.keys())


_REASON_PATTERNS: tuple[tuple[ExecutorFallbackReason, tuple[str, ...]], ...] = (
    (
        ExecutorFallbackReason.TIMEOUT,
        (
            "timed out",
            "timeout",
            "silence timeout",
            "deadline exceeded",
        ),
    ),
    (
        ExecutorFallbackReason.PERMISSION_DENIED,
        (
            "permission denied",
            "permissions policy",
            "approval required",
            "operation not permitted",
            "denied by policy",
            "not allowed",
        ),
    ),
    (
        ExecutorFallbackReason.AUTHENTICATION_REQUIRED,
        (
            "authentication failed",
            "login required",
            "not logged in",
            "unauthorized",
            "api key",
        ),
    ),
    (
        ExecutorFallbackReason.UNSUPPORTED_MODEL,
        (
            "unsupported model",
            "model_not_found",
            "model not found",
            "unknown model",
            "invalid_request_error",
        ),
    ),
    (
        ExecutorFallbackReason.EXECUTOR_UNAVAILABLE,
        (
            "requires a newer version",
            "please upgrade to the latest app or cli",
            "command not found",
            "not recognized as",
            "no such file or directory",
            "no such file",
            "当前无法使用",
            "没有找到",
        ),
    ),
    (
        ExecutorFallbackReason.INVOCATION_ERROR,
        (
            "[errno 22] invalid argument",
            "invalid argument",
        ),
    ),
)


def classify_executor_fallback_reason(
    agent_label: str,
    output: str,
) -> ExecutorFallbackReason:
    """Classify one executor failure into the stable fallback enum."""
    family = executor_family_from_label(agent_label)
    text = f"{agent_label or ''}\n{output or ''}".lower()
    if not text.strip():
        return ExecutorFallbackReason.NONE
    for reason, patterns in _REASON_PATTERNS:
        if reason is ExecutorFallbackReason.INVOCATION_ERROR and family != "codex":
            continue
        if any(pattern in text for pattern in patterns):
            return reason
    return ExecutorFallbackReason.NONE


def executor_family_from_label(agent_label: str) -> str:
    """Map persisted labels such as ``codex-review`` to a contract family."""
    label = str(agent_label or "").strip().lower()
    family = get_family(label)
    if family is not None:
        return family.name
    if label.startswith("codex"):
        return "codex"
    if label.startswith("claude"):
        return "claude"
    if label.startswith("opencode"):
        return "opencode"
    return label


def executor_identity(agent_label: str, *, model: str = "") -> dict[str, str]:
    """Return a JSON-safe identity object for logs and trace output."""
    return {
        "label": str(agent_label or ""),
        "family": executor_family_from_label(agent_label),
        "model": str(model or ""),
    }


def build_executor_fallback_telemetry(
    *,
    phase: str,
    failed_agent: str,
    fallback_agent: str,
    output: str,
    failed_model: str = "",
    fallback_model: str = "",
) -> dict[str, Any]:
    """Build one structured telemetry payload for an executor fallback."""
    failed = executor_identity(failed_agent, model=failed_model)
    fallback = executor_identity(fallback_agent, model=fallback_model)
    reason = classify_executor_fallback_reason(failed_agent, output)
    fallback_path = [failed["family"], fallback["family"]]
    return {
        "kind": "executor_fallback",
        "phase": str(phase or ""),
        "executor_family": failed["family"],
        "executor_model": failed["model"],
        "fallback_reason": reason.value,
        "fallback_path": fallback_path,
        "failed_executor": failed,
        "fallback_executor": fallback,
    }


def format_executor_telemetry_marker(telemetry: Mapping[str, Any]) -> str:
    """Serialize telemetry as one stable task-log marker line."""
    return f"{_TELEMETRY_PREFIX} {json.dumps(dict(telemetry), ensure_ascii=False, sort_keys=True)}"


def append_executor_telemetry_marker(output: str, telemetry: Mapping[str, Any]) -> str:
    """Append a telemetry marker to human-readable executor output."""
    base = str(output or "").rstrip()
    marker = format_executor_telemetry_marker(telemetry)
    return f"{base}\n\n{marker}" if base else marker


def extract_executor_telemetry(output: str) -> dict[str, Any]:
    """Extract the last executor telemetry marker from task-log output."""
    matches = list(_TELEMETRY_RE.finditer(str(output or "")))
    if not matches:
        return {}
    raw = matches[-1].group(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def executor_telemetry_trace_fields(output: str) -> dict[str, Any]:
    """Return trace-event fields derived from task-log telemetry."""
    telemetry = extract_executor_telemetry(output)
    if not telemetry:
        return {}
    fields: dict[str, Any] = {}
    for key in (
        "executor_family",
        "executor_model",
        "fallback_reason",
        "fallback_path",
        "failed_executor",
        "fallback_executor",
    ):
        if key in telemetry:
            fields[key] = telemetry[key]
    return fields
