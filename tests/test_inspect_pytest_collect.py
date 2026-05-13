from __future__ import annotations

from codepilot.commands.inspect_signal_collectors import _summarize_pytest_collect_output


def test_pytest_collect_summary_keeps_project_feature_test_files_visible():
    lines = [f"tests/test_ai_gateway_stage_{idx:02d}.py::test_gateway_case" for idx in range(35)]
    lines.append("tests/test_ai_gateway_stage_execute_dispatch.py::test_route_matrix[api-error-cli-fallback]")
    lines.extend(
        [
            "tests/test_event_plugins.py::test_event_schema_lists_known_event_contracts",
            "tests/test_exec_command.py::test_exec_command_runs_project_scoped_provider_check",
            "tests/test_hook_command.py::test_hook_validate_reports_provider_neutral_lifecycle_without_touching_global_hooks",
            "tests/test_self_update_command.py::test_self_update_dry_run_json_collects_preflight_evidence_and_plan",
            "tests/test_setup_command.py::test_setup_creates_project_codepilot_layout_config_and_registration",
            "40 tests collected in 0.12s",
        ]
    )

    summary = _summarize_pytest_collect_output(lines, limit=30)

    assert "40 tests collected" in summary
    assert "tests/test_ai_gateway_stage_00.py" in summary
    assert "tests/test_event_plugins.py" in summary
    assert "tests/test_exec_command.py" in summary
    assert "tests/test_hook_command.py" in summary
    assert "tests/test_self_update_command.py" in summary
    assert "tests/test_setup_command.py" in summary
    assert "::test_gateway_case" not in summary
    assert "::test_route_matrix" not in summary
