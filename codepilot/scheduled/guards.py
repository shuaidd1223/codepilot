"""Guardrails for scheduled ``agent_job`` execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GUARDS_RELATIVE_PATH = Path(".codepilot") / "scheduled" / "guards.json"
MAX_AGENT_CHAIN_DEPTH = 5


@dataclass(frozen=True)
class GuardCheck:
    status: str = "ok"
    reason: str = ""
    notification: dict[str, Any] = field(default_factory=dict)
    agent_chain: list[str] = field(default_factory=list)

    @property
    def should_skip(self) -> bool:
        return self.status == "skipped"


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _day_key(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


def _guards_path(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / GUARDS_RELATIVE_PATH


def _load_state(project_root: str | Path) -> dict[str, Any]:
    path = _guards_path(project_root)
    if not path.is_file():
        return {"schema_version": 1, "agents": {}, "daily_usage": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    agents = data.get("agents")
    daily_usage = data.get("daily_usage")
    return {
        "schema_version": 1,
        "agents": agents if isinstance(agents, dict) else {},
        "daily_usage": daily_usage if isinstance(daily_usage, dict) else {},
    }


def _save_state(project_root: str | Path, state: dict[str, Any]) -> None:
    path = _guards_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def agent_job_key(job: dict[str, Any]) -> str:
    project = str(job.get("project") or "").strip() or "-"
    name = str(job.get("name") or "").strip() or str(job.get("agent") or "").strip() or "agent_job"
    return f"{project}:{name}"


def _chain_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("key") or item.get("name") or item.get("agent") or item)
        else:
            text = str(item)
        text = text.strip()
        if text:
            items.append(text)
    return items


def agent_job_chain(job: dict[str, Any]) -> list[str]:
    source_event = job.get("source_event") if isinstance(job.get("source_event"), dict) else {}
    payload = source_event.get("payload") if isinstance(source_event.get("payload"), dict) else {}
    chain = (
        _chain_items(job.get("agent_chain"))
        or _chain_items(job.get("execution_chain"))
        or _chain_items(job.get("chain"))
        or _chain_items(payload.get("agent_chain"))
        or _chain_items(payload.get("execution_chain"))
        or _chain_items(payload.get("chain"))
    )
    current = agent_job_key(job)
    if not chain or chain[-1] != current:
        chain = [*chain, current]
    return chain


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _notification(
    job: dict[str, Any],
    *,
    status: str,
    reason: str,
    agent_chain: list[str],
    now: datetime,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "type": "scheduled.agent.guard_triggered",
        "source": "codepilot.scheduled.guards",
        "project": str(job.get("project") or ""),
        "timestamp": now.isoformat(),
        "payload": {
            "status": status,
            "reason": reason,
            "job_name": str(job.get("name") or ""),
            "agent": str(job.get("agent") or ""),
            "agent_chain": list(agent_chain),
            **payload,
        },
    }


def preflight_agent_job_guards(
    project_root: str | Path,
    job: dict[str, Any],
    *,
    now: datetime | None = None,
) -> GuardCheck:
    current_time = _now(now)
    chain = agent_job_chain(job)
    if len(chain) > MAX_AGENT_CHAIN_DEPTH:
        reason = "loop_circuit_breaker"
        return GuardCheck(
            status="skipped",
            reason=reason,
            notification=_notification(
                job,
                status="skipped",
                reason=reason,
                agent_chain=chain,
                now=current_time,
                depth=len(chain),
                limit=MAX_AGENT_CHAIN_DEPTH,
            ),
            agent_chain=chain,
        )

    state = _load_state(project_root)
    agent_state = state["agents"].get(agent_job_key(job))
    if isinstance(agent_state, dict) and agent_state.get("disabled"):
        reason = "disabled"
        return GuardCheck(
            status="skipped",
            reason=reason,
            notification=_notification(
                job,
                status="skipped",
                reason=reason,
                agent_chain=chain,
                now=current_time,
                disabled_reason=str(agent_state.get("disabled_reason") or ""),
            ),
            agent_chain=chain,
        )
    return GuardCheck(agent_chain=chain)


def finalize_agent_job_guards(
    project_root: str | Path,
    job: dict[str, Any],
    *,
    token_usage: dict[str, int],
    cost: float,
    agent_chain: list[str],
    now: datetime | None = None,
) -> GuardCheck:
    current_time = _now(now)
    total_tokens = int(token_usage.get("total_tokens") or sum(int(v or 0) for v in token_usage.values()))
    max_tokens = _optional_int(job.get("max_tokens_per_call"))
    max_cost = _optional_float(job.get("max_cost_usd"))
    raw_daily_cost = (
        job.get("max_daily_cost_usd")
        if job.get("max_daily_cost_usd") is not None
        else job.get("max_cost_per_day")
    )
    max_daily_cost = _optional_float(raw_daily_cost)
    key = agent_job_key(job)
    state = _load_state(project_root)
    day = _day_key(current_time)
    daily_cost = None

    if max_daily_cost is not None:
        day_usage = state["daily_usage"].setdefault(day, {})
        agent_usage = day_usage.setdefault(key, {"cost_usd": 0.0})
        previous = float(agent_usage.get("cost_usd") or 0.0) if isinstance(agent_usage, dict) else 0.0
        daily_cost = round(previous + max(0.0, float(cost or 0.0)), 10)
        day_usage[key] = {"cost_usd": daily_cost}

    daily_over_limit = max_daily_cost is not None and daily_cost is not None and daily_cost > max_daily_cost
    if daily_over_limit:
        agents = state.setdefault("agents", {})
        agents[key] = {
            "disabled": True,
            "disabled_reason": "max_cost_per_day",
            "disabled_at": current_time.isoformat(),
            "day": day,
        }

    reason = ""
    payload: dict[str, Any] = {}
    if max_tokens is not None and total_tokens > max_tokens:
        reason = "max_tokens_per_call"
        payload = {"actual": total_tokens, "limit": max_tokens}
    elif max_cost is not None and float(cost or 0.0) > max_cost:
        reason = "max_cost_per_call"
        payload = {"actual": float(cost or 0.0), "limit": max_cost}
    elif daily_over_limit:
        reason = "max_cost_per_day"
        payload = {"daily_cost_usd": daily_cost, "limit": max_daily_cost, "day": day}

    if max_daily_cost is not None:
        _save_state(project_root, state)

    if not reason:
        return GuardCheck(agent_chain=agent_chain)
    return GuardCheck(
        status="tripped",
        reason=reason,
        notification=_notification(
            job,
            status="tripped",
            reason=reason,
            agent_chain=agent_chain,
            now=current_time,
            **payload,
        ),
        agent_chain=agent_chain,
    )
