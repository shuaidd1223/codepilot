"""Signal selection and aggregation helpers for the `inspect` command."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codepilot.commands.inspect_signal_collectors import (
    collect_code_metrics,
    collect_dependency_health,
    collect_failed_tasks,
    collect_git_log,
    collect_pytest_collect,
    collect_ruff,
    collect_todos,
)


InspectSignalCollectorScope = Literal["project_name", "project_path"]
InspectSignalCollector = Callable[[object], str]
_COLLECTOR_ARG_GETTER_BY_SCOPE: dict[InspectSignalCollectorScope, Callable[[str, Path], object]] = {
    "project_name": lambda project_name, _project_path: project_name,
    "project_path": lambda _project_name, project_path: project_path,
}


@dataclass(frozen=True)
class InspectSignalSpec:
    """Canonical signal definition decoupled from command-layer dispatch."""

    key: str
    title: str
    order: int
    aliases: tuple[str, ...]
    collector_key: str
    collector_scope: InspectSignalCollectorScope = "project_path"


@dataclass(frozen=True)
class InspectSignalResult:
    """Unified signal result model used by prompt aggregation."""

    key: str
    title: str
    order: int
    enabled: bool
    content: str


def normalize_signal_tokens(signals: Iterable[str]) -> set[str]:
    return {signal.strip().lower() for signal in signals if isinstance(signal, str) and signal.strip()}


def is_signal_enabled(spec: InspectSignalSpec, requested_tokens: set[str]) -> bool:
    return bool(requested_tokens & set(spec.aliases))


def collect_signal_content(
    spec: InspectSignalSpec,
    *,
    enabled: bool,
    project_name: str,
    project_path: Path,
    collectors_by_key: Mapping[str, InspectSignalCollector],
    skipped_signal: str,
) -> str:
    if not enabled:
        return skipped_signal
    return collect_enabled_signal_content(
        spec,
        project_name=project_name,
        project_path=project_path,
        collectors_by_key=collectors_by_key,
    )


def collect_enabled_signal_content(
    spec: InspectSignalSpec,
    *,
    project_name: str,
    project_path: Path,
    collectors_by_key: Mapping[str, InspectSignalCollector],
) -> str:
    collector = collectors_by_key[spec.collector_key]
    arg = _COLLECTOR_ARG_GETTER_BY_SCOPE[spec.collector_scope](project_name, project_path)
    return collector(arg)


def resolve_signal_output(
    spec: InspectSignalSpec,
    *,
    requested_tokens: set[str],
    project_name: str,
    project_path: Path,
    collectors_by_key: Mapping[str, InspectSignalCollector],
    skipped_signal: str,
) -> tuple[bool, str]:
    """Resolve one signal's output through explicit enabled/disabled paths."""
    enabled = is_signal_enabled(spec, requested_tokens)
    content = collect_signal_content(
        spec,
        enabled=enabled,
        project_name=project_name,
        project_path=project_path,
        collectors_by_key=collectors_by_key,
        skipped_signal=skipped_signal,
    )
    return enabled, content


def collect_signal_results(
    project_name: str,
    project_path: Path,
    *,
    signals: tuple[str, ...],
    specs: Iterable[InspectSignalSpec],
    collectors_by_key: Mapping[str, InspectSignalCollector],
    skipped_signal: str,
) -> list[InspectSignalResult]:
    requested = normalize_signal_tokens(signals)
    results: list[InspectSignalResult] = []
    for spec in specs:
        enabled, content = resolve_signal_output(
            spec,
            requested_tokens=requested,
            project_name=project_name,
            project_path=project_path,
            collectors_by_key=collectors_by_key,
            skipped_signal=skipped_signal,
        )
        results.append(
            InspectSignalResult(
                key=spec.key,
                title=spec.title,
                order=spec.order,
                enabled=enabled,
                content=content,
            )
        )
    return results
