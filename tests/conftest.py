"""Shared pytest bootstrap for the CodePilot test suite.

Normalises stdio to UTF-8 on Windows so assertion diffs containing Chinese
(e.g. reviewer verdict strings, task titles) render as readable text
instead of GBK mojibake (``û�����``). Without this, Python defaults to
the ``cp936`` code page when pytest writes failure reports, mangling
every UTF-8 string that flows through stderr.

Kept intentionally tiny: the CLI entrypoint already calls
:func:`codepilot.console_encoding.configure_console_encoding` on startup,
but pytest bypasses that path, so we re-use the helper here.
"""

from __future__ import annotations

from codepilot.core.console_encoding import configure_console_encoding


configure_console_encoding()

