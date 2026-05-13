"""Tests for the stdio MCP protocol guard.

Background: when the MCP server is launched with stdio transport, ``sys.stdout``
is the JSON-RPC pipe. Any text written to it by tool implementations (via
``print``, ``click.echo``, ``rich.Console``, …) corrupts the protocol and
causes both sides to hang. The guard replaces ``sys.stdout`` so any text-level
write goes to ``sys.stderr`` while ``.buffer`` still points to the real stdout
so MCP SDK's stdio_server can write protocol frames.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from click.testing import CliRunner

from codepilot.storage import database as db


def _init_project(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "tasks.db"
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(db_path))
    db.init_db()
    project_path = tmp_path / "project"
    project_path.mkdir()
    db.register_project("demo", str(project_path))
    return project_path


def test_protect_stdio_redirects_writes_to_stderr(capsys):
    from codepilot.mcp.stdio_guard import protect_stdio

    with protect_stdio():
        sys.stdout.write("PROTOCOL_BREAK\n")
        print("SECOND_BREAK")
        click.echo("THIRD_BREAK")
        sys.stdout.flush()

    captured = capsys.readouterr()
    assert "PROTOCOL_BREAK" in captured.err
    assert "SECOND_BREAK" in captured.err
    assert "THIRD_BREAK" in captured.err
    # Crucially, none of these messages must reach the real stdout — that channel
    # belongs to the MCP JSON-RPC protocol.
    assert "PROTOCOL_BREAK" not in captured.out
    assert "SECOND_BREAK" not in captured.out
    assert "THIRD_BREAK" not in captured.out


def test_protect_stdio_preserves_buffer_for_mcp_sdk():
    from codepilot.mcp.stdio_guard import protect_stdio

    original = sys.stdout
    original_buffer = original.buffer
    with protect_stdio():
        # The MCP SDK reads sys.stdout.buffer to write JSON-RPC frames; it must
        # still point at the real binary stdout, not stderr.
        assert sys.stdout.buffer is original_buffer
        assert sys.stdout is not original
    # Restored after the context exits.
    assert sys.stdout is original


def test_protect_stdio_restores_on_exception():
    from codepilot.mcp.stdio_guard import protect_stdio

    original = sys.stdout
    try:
        with protect_stdio():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert sys.stdout is original


def test_rich_console_writing_to_sys_stdout_goes_to_stderr(capsys):
    """The hanging tools route output via codepilot.core.output.echo, which
    builds a rich.Console targeting ``sys.stdout``. After the guard is
    installed, that console must write to stderr rather than the protocol pipe.
    """
    from codepilot.mcp.stdio_guard import protect_stdio

    with protect_stdio():
        from codepilot.core import output as core_output

        # Reset Rich console cache so it picks up our guard as its stream.
        core_output._CONSOLE = None
        core_output._CONSOLE_STREAM = None
        core_output.echo("RICH_BREAK")

    # Reset cache again so other tests are not affected by our captured stream.
    from codepilot.core import output as core_output_after

    core_output_after._CONSOLE = None
    core_output_after._CONSOLE_STREAM = None

    captured = capsys.readouterr()
    assert "RICH_BREAK" in captured.err
    assert "RICH_BREAK" not in captured.out


def test_mcp_serve_stdio_installs_protocol_guard(tmp_path, monkeypatch):
    """When ``codepilot mcp serve --transport stdio`` runs, it must wrap
    ``server.run`` in the protocol guard so subsequent tool calls cannot
    corrupt the JSON-RPC channel."""
    _init_project(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    from codepilot.commands import mcp as mcp_cmd
    from codepilot.mcp.stdio_guard import _StdoutProtocolGuard

    class FakeServer:
        def run(self, **kwargs):
            observed["stdout_is_guard"] = isinstance(sys.stdout, _StdoutProtocolGuard)
            observed["run_kwargs"] = kwargs

    monkeypatch.setattr(mcp_cmd, "create_mcp_server", lambda context, **kwargs: FakeServer())

    from codepilot.cli import main

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--transport", "stdio", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert observed["stdout_is_guard"] is True, observed
    assert observed["run_kwargs"] == {"transport": "stdio"}


def test_protect_stdio_isolates_subprocess_stdin_via_spawned_python(tmp_path):
    """End-to-end check: a Python subprocess that itself enters protect_stdio
    and spawns a grandchild reading fd 0 must observe immediate EOF.

    This validates the fd-level isolation in an environment that does *not*
    have pytest's stdin shim interfering. We spawn a child Python with a
    real pipe stdin (mirroring the MCP server's situation) and have it run
    the guard, then exec a grandchild that drains fd 0.
    """
    import subprocess
    from textwrap import dedent

    repo_root = Path(__file__).resolve().parents[1]
    script = dedent(
        """
        import os, subprocess, sys
        from codepilot.mcp.stdio_guard import protect_stdio
        with protect_stdio():
            probe = subprocess.run(
                [sys.executable, "-c", "import os; data = os.read(0, 16); print(len(data))"],
                capture_output=True, text=True, timeout=5,
            )
        sys.stdout.buffer.write((probe.stdout or "").encode("utf-8"))
        sys.stdout.buffer.write(b"|")
        sys.stdout.buffer.write(str(probe.returncode).encode("utf-8"))
        sys.stdout.buffer.flush()
        """
    )

    # Driver pipes ample data into the child's stdin — without the guard
    # the grandchild would inherit fd 0 and read this data.
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        input=b"INHERIT_LEAK\n" * 100,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    stdout, _, rc = result.stdout.decode("utf-8", "replace").partition("|")
    assert rc.strip() == "0", f"grandchild exited {rc!r}: {result.stderr!r}"
    assert stdout.strip() == "0", (
        f"grandchild inherited stdin from MCP-like parent: {stdout!r}"
    )


def test_mcp_serve_http_does_not_install_protocol_guard(tmp_path, monkeypatch):
    """HTTP transport already keeps stdout free; no guard should be installed."""
    _init_project(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    from codepilot.commands import mcp as mcp_cmd
    from codepilot.mcp.stdio_guard import _StdoutProtocolGuard

    class FakeServer:
        def run(self, **kwargs):
            observed["stdout_is_guard"] = isinstance(sys.stdout, _StdoutProtocolGuard)

    monkeypatch.setattr(mcp_cmd, "create_mcp_server", lambda context, **kwargs: FakeServer())

    from codepilot.cli import main

    result = CliRunner().invoke(
        main,
        ["mcp", "serve", "--transport", "http", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert observed["stdout_is_guard"] is False
