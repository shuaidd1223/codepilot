"""Rich markup safety: untrusted text with fake tags must not crash echo."""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.markup import MarkupError
from rich.table import Table

from codepilot.commands.status import _short_text
from codepilot.core.output import safe


def _render(markup_text: str, *, markup: bool = True) -> str:
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, force_terminal=False, width=200)
    console.print(markup_text, markup=markup, highlight=False, soft_wrap=True)
    return buf.getvalue()


def test_safe_escapes_fake_closing_tag_without_raising():
    raw = "codex output says: [/dim] trailing [red]"
    # Without escape, rich would raise MarkupError on a stray closing tag.
    with pytest.raises(MarkupError):
        _render(raw)
    # With safe(), the string renders verbatim.
    out = _render(f"[green]prefix[/green] {safe(raw)}")
    assert "[/dim]" in out
    assert "trailing" in out


def test_short_text_escapes_markup_for_table_cells():
    table = Table()
    table.add_column("output")
    table.add_row(_short_text("agent err: [/red] oops"))
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(table)
    rendered = buf.getvalue()
    assert "[/red]" in rendered
    assert "oops" in rendered


def test_well_formed_markup_still_renders():
    out = _render("[green]ok[/green]")
    # rich strips tags when rendering to plain string but content survives
    assert "ok" in out

