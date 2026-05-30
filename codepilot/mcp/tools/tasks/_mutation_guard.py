from __future__ import annotations

from codepilot.core.task_mutation_guard import RunnerTaskMutationError, guard_current_runner_task_mutation
from codepilot.mcp.protocol import CodePilotToolError


def guard_mcp_task_mutation(task_id: int, action: str) -> None:
    try:
        guard_current_runner_task_mutation(task_id, action=action)
    except RunnerTaskMutationError as exc:
        raise CodePilotToolError(exc.message, code=exc.code, details=exc.details) from exc
