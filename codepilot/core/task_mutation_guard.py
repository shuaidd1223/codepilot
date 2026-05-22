"""Guards for task mutations attempted from runner-owned agent phases."""

from __future__ import annotations

import os
from typing import Any


RUNNER_TASK_ID_ENV = "CODEPILOT_RUNNER_TASK_ID"
RUNNER_PHASE_ENV = "CODEPILOT_RUNNER_PHASE"
RUNNER_TASK_STATUS_OWNED_CODE = "runner_task_status_owned"


class RunnerTaskMutationError(RuntimeError):
    """Raised when an agent phase tries to mutate its own task status."""

    def __init__(self, *, task_id: int, action: str, phase: str) -> None:
        safe_action = str(action or "mutate").strip() or "mutate"
        safe_phase = str(phase or "").strip() or "-"
        message = (
            f"当前任务 #{task_id} 的任务状态由 runner 管理"
            f"（phase={safe_phase}）；执行智能体不能通过 task {safe_action} 修改自己的任务状态。"
        )
        super().__init__(message)
        self.message = message
        self.code = RUNNER_TASK_STATUS_OWNED_CODE
        self.details: dict[str, Any] = {
            "task_id": task_id,
            "action": safe_action,
            "phase": safe_phase,
            "owner": "runner",
        }


def runner_task_context_env(task_id: int | str | None, phase: str | None) -> dict[str, str]:
    """Return env values that identify the runner-owned task phase."""
    try:
        tid = int(task_id or 0)
    except (TypeError, ValueError):
        return {}
    if tid <= 0:
        return {}
    return {
        RUNNER_TASK_ID_ENV: str(tid),
        RUNNER_PHASE_ENV: str(phase or "").strip() or "unknown",
    }


def current_runner_task_id() -> int | None:
    raw = os.environ.get(RUNNER_TASK_ID_ENV)
    if not raw:
        return None
    try:
        tid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def current_runner_phase() -> str:
    return str(os.environ.get(RUNNER_PHASE_ENV) or "").strip()


def guard_current_runner_task_mutation(task_id: int | str, *, action: str) -> None:
    """Reject status-owning mutations against the task currently run by runner."""
    current_task_id = current_runner_task_id()
    if current_task_id is None:
        return
    try:
        target_task_id = int(task_id)
    except (TypeError, ValueError):
        return
    if target_task_id != current_task_id:
        return
    raise RunnerTaskMutationError(
        task_id=target_task_id,
        action=action,
        phase=current_runner_phase(),
    )
