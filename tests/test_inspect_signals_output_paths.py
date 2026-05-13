from __future__ import annotations

from codepilot.commands import inspect_signals


def test_resolve_signal_output_returns_skipped_without_calling_collector(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    calls: list[object] = []
    spec = inspect_signals.InspectSignalSpec(
        key="git_log",
        title="最近 git 提交",
        order=1,
        aliases=("git_log",),
        collector_key="collect_git_log",
        collector_scope="project_path",
    )

    enabled, content = inspect_signals.resolve_signal_output(
        spec,
        requested_tokens={"deps"},
        project_name="demo",
        project_path=project,
        collectors_by_key={"collect_git_log": lambda arg: calls.append(arg) or "git-log"},
        skipped_signal="（跳过）",
    )

    assert enabled is False
    assert content == "（跳过）"
    assert calls == []


def test_collect_enabled_signal_content_routes_scope_argument(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    calls: list[object] = []

    path_spec = inspect_signals.InspectSignalSpec(
        key="git_log",
        title="最近 git 提交",
        order=1,
        aliases=("git_log",),
        collector_key="collect_git_log",
        collector_scope="project_path",
    )
    assert (
        inspect_signals.collect_enabled_signal_content(
            path_spec,
            project_name="demo",
            project_path=project,
            collectors_by_key={"collect_git_log": lambda arg: calls.append(arg) or "path-content"},
        )
        == "path-content"
    )

    name_spec = inspect_signals.InspectSignalSpec(
        key="failed_tasks",
        title="最近失败或取消的任务",
        order=2,
        aliases=("failed_tasks",),
        collector_key="collect_failed_tasks",
        collector_scope="project_name",
    )
    assert (
        inspect_signals.collect_enabled_signal_content(
            name_spec,
            project_name="demo",
            project_path=project,
            collectors_by_key={"collect_failed_tasks": lambda arg: calls.append(arg) or "name-content"},
        )
        == "name-content"
    )
    assert calls == [project, "demo"]


def test_collect_signal_results_uses_resolve_signal_output(monkeypatch, tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    seen: list[str] = []
    specs = (
        inspect_signals.InspectSignalSpec(
            key="git_log",
            title="最近 git 提交",
            order=1,
            aliases=("git_log",),
            collector_key="collect_git_log",
            collector_scope="project_path",
        ),
        inspect_signals.InspectSignalSpec(
            key="failed_tasks",
            title="最近失败或取消的任务",
            order=2,
            aliases=("failed_tasks",),
            collector_key="collect_failed_tasks",
            collector_scope="project_name",
        ),
    )

    def _fake_resolve(spec, **kwargs):
        seen.append(spec.key)
        return spec.key == "git_log", f"{spec.key}-content"

    monkeypatch.setattr(inspect_signals, "resolve_signal_output", _fake_resolve)

    results = inspect_signals.collect_signal_results(
        "demo",
        project,
        signals=("git_log",),
        specs=specs,
        collectors_by_key={},
        skipped_signal="（跳过）",
    )

    assert seen == ["git_log", "failed_tasks"]
    assert [item.enabled for item in results] == [True, False]
    assert [item.content for item in results] == ["git_log-content", "failed_tasks-content"]
