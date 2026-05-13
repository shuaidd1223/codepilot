"""Shared pytest bootstrap for the CodePilot test suite.

Normalises stdio to UTF-8 on Windows so assertion diffs containing Chinese
(e.g. reviewer verdict strings, task titles) render as readable text
instead of GBK mojibake. Without this, Python defaults to the ``cp936``
code page when pytest writes failure reports, mangling every UTF-8 string
that flows through stderr.

Kept intentionally tiny: the CLI entrypoint already calls
:func:`codepilot.console_encoding.configure_console_encoding` on startup,
but pytest bypasses that path, so we re-use the helper here.

MCP stdio smoke test utilities have moved to ``tests/mcp_stdio_shim.py``.
"""

from __future__ import annotations

import os

from codepilot.core.console_encoding import configure_console_encoding


configure_console_encoding()
os.environ.setdefault("CODEPILOT_DESKTOP_NOTIFY", "0")

# Re-export the MCP stdio smoke fixture from its dedicated module.
# Test files that depend on it only need to mention the fixture name;
# pytest discovers it via conftest.py.
from tests.mcp_stdio_shim import mcp_stdio_smoke_server  # noqa: E402, F401
