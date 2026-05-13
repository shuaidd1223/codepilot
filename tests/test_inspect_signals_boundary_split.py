from __future__ import annotations

from codepilot.commands import inspect_signals


def test_collect_signal_results_delegates_to_collector_map(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    calls: list[tuple[str, object]] = []

    specs = (
        inspect_signals.InspectSignalSpec(
            key="alpha",
            title="Alpha",
            order=1,
            aliases=("alpha",),
            collector_key="collect_alpha",
            collector_scope="project_path",
        ),
        inspect_signals.InspectSignalSpec(
            key="beta",
            title="Beta",
            order=2,
            aliases=("beta",),
            collector_key="collect_beta",
            collector_scope="project_name",
        ),
        inspect_signals.InspectSignalSpec(
            key="gamma",
            title="Gamma",
            order=3,
            aliases=("gamma", "g"),
            collector_key="collect_gamma",
            collector_scope="project_path",
        ),
    )

    collectors_by_key = {
        "collect_alpha": lambda arg: calls.append(("collect_alpha", arg)) or "alpha-content",
        "collect_beta": lambda arg: calls.append(("collect_beta", arg)) or "beta-content",
        "collect_gamma": lambda arg: calls.append(("collect_gamma", arg)) or "gamma-content",
    }

    results = inspect_signals.collect_signal_results(
        "demo",
        project,
        signals=("alpha", "g"),
        specs=specs,
        collectors_by_key=collectors_by_key,
        skipped_signal="（跳过）",
    )

    assert [item.key for item in results] == ["alpha", "beta", "gamma"]
    assert [item.enabled for item in results] == [True, False, True]
    assert [item.content for item in results] == ["alpha-content", "（跳过）", "gamma-content"]
    assert calls == [("collect_alpha", project), ("collect_gamma", project)]
