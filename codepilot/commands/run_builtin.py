"""Built-in executor helpers re-exported from focused submodules.

This module remains the public import surface so ``run.py`` and existing tests
can keep importing ``codepilot.commands.run_builtin`` without knowing about the
internal split.
"""

from codepilot.commands.run_builtin_core import (  # noqa: F401
    ExecutionResult,
    _builtin_base_branch_lock_error,
    _builtin_preflight_error,
    _builtin_review_requires_git,
    _builtin_runtime_dir,
    _bullet_lines,
    _collect_project_conventions_snippet,
    _extract_task_sections,
    _read_output_file,
    _resolve_dual_phase_agents_for_task,
    _task_phase_override,
    _write_task_log,
)
from codepilot.commands.run_builtin_executor import (  # noqa: F401
    _BuiltinLoopOutcome,
    _ExecutorContext,
    _PhaseOutcome,
    _agent_label_runner,
    _expected_phase_agent_label,
    _extract_reviewer_findings,
    _finalize_executor_success,
    _is_builtin_agent_tooling_failure,
    _make_phase_output_path,
    _map_builtin_loop_outcome,
    _resolve_builtin_phase_agent,
    _resolve_builtin_single_agent,
    _run_builder_round,
    _run_builtin_executor,
    _run_builtin_phase,
    _run_builtin_round_loop,
    _run_phase_with_tooling_fallback,
    _run_reviewer_round,
    _select_builtin_phase_fallback_agent,
)
from codepilot.commands.run_builtin_prompts import (  # noqa: F401
    _build_builtin_prompt,
    _build_review_prompt,
    _extract_review_verdict,
    parse_review_output,
)

