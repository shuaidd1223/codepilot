"""Signal selection and aggregation helpers for the `inspect` command."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal



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


# Lint codes considered low-value noise for grouping and dedup purposes.
# These produce many repetitive inspect candidates that dilute truly actionable tasks.
LINT_NOISE_CODES: tuple[str, ...] = ("F401", "F841", "F541", "E402")


def lint_group_key(evidence: str) -> str | None:
    """Extract the first lint-noise code from evidence, or None.

    Used to determine whether a candidate falls into a low-value lint category
    that should be grouped with peers of the same code.
    """
    if not evidence:
        return None
    m = re.search(r"(F401|F841|F541|E402)", evidence)
    return m.group(1) if m else None


def lint_fingerprint(lint_code: str) -> str:
    """Stable fingerprint string for a lint code category.

    Embedded in task content so that later inspect rounds can detect that
    an open task already covers this category and skip re-creation.
    """
    return f"inspect:ruff:{lint_code}"


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
