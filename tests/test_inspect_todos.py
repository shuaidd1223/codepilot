from __future__ import annotations

from codepilot.commands.inspect import collect_todos


def test_collect_todos_only_reads_source_comments(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "app.py").write_text(
        "\n".join(
            [
                'message = "TODO: not real"',
                'example = "xxx[/green]"',
                "# TODO: wire the real backend",
                "value = 1  # FIXME: remove fallback",
                "# supports cp.xxx placeholder syntax",
                "# XXX: migrate this edge case",
            ]
        ),
        encoding="utf-8",
    )

    result = collect_todos(project)

    assert "wire the real backend" in result
    assert "remove fallback" in result
    assert "migrate this edge case" in result
    assert "not real" not in result
    assert "xxx[/green]" not in result
    assert "placeholder syntax" not in result


def test_collect_todos_skips_markdown_prose_and_fenced_code(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "README.md").write_text(
        "\n".join(
            [
                "This paragraph mentions TODO but is not an actionable item.",
                "```py",
                "# TODO: example only",
                "```",
                "- TODO: document the deployment switch",
                "<!-- FIXME: clarify setup note -->",
            ]
        ),
        encoding="utf-8",
    )

    result = collect_todos(project)

    assert "document the deployment switch" in result
    assert "clarify setup note" in result
    assert "paragraph mentions" not in result
    assert "example only" not in result


def test_collect_todos_returns_none_when_only_false_positives(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "seed.py").write_text(
        'content = """\n- 正常 `[green]xxx[/green]` 仍能高亮\nTODO/FIXME 扫描只是说明文字\n"""\n',
        encoding="utf-8",
    )

    assert collect_todos(project) == "（无）"
