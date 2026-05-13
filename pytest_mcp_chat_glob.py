"""Pytest argv compatibility for documented verification entrypoint globs."""

from __future__ import annotations

from pathlib import Path

_KNOWN_TEST_GLOBS = {
    "tests/test_mcp_chat_*",
    "tests/test_opencode_*",
    "tests/test_scheduled_*",
}


def expand_mcp_chat_test_globs(args: list[str], *, root: Path) -> list[str]:
    expanded: list[str] = []
    for arg in args:
        normalized = arg.replace("\\", "/")
        if normalized in _KNOWN_TEST_GLOBS:
            matches = sorted(
                path.relative_to(root).as_posix()
                for path in root.glob(normalized)
                if path.is_file()
            )
            expanded.extend(matches or [arg])
            continue
        expanded.append(arg)
    return expanded


def pytest_load_initial_conftests(early_config, parser, args: list[str]) -> None:
    args[:] = expand_mcp_chat_test_globs(args, root=Path.cwd())
