"""Pytest argv compatibility for the Phase 5 MCP chat verification entrypoint."""

from __future__ import annotations

from pathlib import Path

_MCP_CHAT_GLOB = "tests/test_mcp_chat_*"


def expand_mcp_chat_test_globs(args: list[str], *, root: Path) -> list[str]:
    expanded: list[str] = []
    for arg in args:
        normalized = arg.replace("\\", "/")
        if normalized == _MCP_CHAT_GLOB:
            matches = sorted(
                path.relative_to(root).as_posix()
                for path in root.glob(_MCP_CHAT_GLOB)
                if path.is_file()
            )
            expanded.extend(matches or [arg])
            continue
        expanded.append(arg)
    return expanded


def pytest_load_initial_conftests(early_config, parser, args: list[str]) -> None:
    args[:] = expand_mcp_chat_test_globs(args, root=Path.cwd())
