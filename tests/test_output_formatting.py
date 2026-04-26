"""Console prefix formatting for user-facing echo output."""

from __future__ import annotations

import io

from rich.console import Console

from codepilot.core import output as output_mod


def _test_console(buffer: io.StringIO) -> Console:
    return Console(file=buffer, highlight=False, force_terminal=False, width=200)


def test_echo_adds_timestamp_and_inferred_level_prefix(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(output_mod, "_CONSOLE", None)
    monkeypatch.setattr(output_mod, "_CONSOLE_STREAM", None)
    monkeypatch.setattr(output_mod, "_build_console", lambda stream: _test_console(buf))
    monkeypatch.setattr(output_mod, "_LINE_OPEN", False)
    monkeypatch.setattr(output_mod, "_now_text", lambda: "09:08:07")

    output_mod.echo("[yellow]需要确认参数[/yellow]")

    rendered = buf.getvalue()
    assert "09:08:07" in rendered
    assert "WARN" in rendered
    assert "需要确认参数" in rendered


def test_echo_keeps_single_prefix_for_nl_false_continuation(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(output_mod, "_CONSOLE", None)
    monkeypatch.setattr(output_mod, "_CONSOLE_STREAM", None)
    monkeypatch.setattr(output_mod, "_build_console", lambda stream: _test_console(buf))
    monkeypatch.setattr(output_mod, "_LINE_OPEN", False)
    monkeypatch.setattr(output_mod, "_now_text", lambda: "09:08:07")

    output_mod.echo("[dim]1/3[/dim] 导入中 ", nl=False)
    output_mod.echo("[green]+ #12[/green]")

    rendered = buf.getvalue()
    assert rendered.count("09:08:07") == 1
    assert "1/3 导入中 + #12" in rendered


def test_echo_preserves_leading_blank_line_before_prefix(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(output_mod, "_CONSOLE", None)
    monkeypatch.setattr(output_mod, "_CONSOLE_STREAM", None)
    monkeypatch.setattr(output_mod, "_build_console", lambda stream: _test_console(buf))
    monkeypatch.setattr(output_mod, "_LINE_OPEN", False)
    monkeypatch.setattr(output_mod, "_now_text", lambda: "09:08:07")

    output_mod.echo("\n[bold]分节标题[/bold]")

    rendered = buf.getvalue()
    assert rendered.startswith("\n09:08:07")
    assert "分节标题" in rendered


def test_echo_rebuilds_console_when_stdout_changes(monkeypatch):
    first = io.StringIO()
    second = io.StringIO()

    monkeypatch.setattr(output_mod, "_CONSOLE", None)
    monkeypatch.setattr(output_mod, "_CONSOLE_STREAM", None)
    monkeypatch.setattr(output_mod, "_build_console", lambda stream: _test_console(stream))
    monkeypatch.setattr(output_mod, "_LINE_OPEN", False)
    monkeypatch.setattr(output_mod, "_now_text", lambda: "09:08:07")
    monkeypatch.setattr(output_mod.sys, "stdout", first)

    output_mod.echo("[cyan]first[/cyan]")

    first.close()
    monkeypatch.setattr(output_mod.sys, "stdout", second)

    output_mod.echo("[green]second[/green]")

    rendered = second.getvalue()
    assert "09:08:07" in rendered
    assert "second" in rendered
