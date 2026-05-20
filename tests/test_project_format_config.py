from __future__ import annotations

import subprocess
from pathlib import Path


def test_editorconfig_requires_lf_line_endings():
    content = Path(".editorconfig").read_text(encoding="utf-8")

    assert "root = true" in content
    assert "[*]" in content
    assert "end_of_line = lf" in content
    assert "charset = utf-8" in content
    assert "insert_final_newline = true" in content


def test_gitattributes_requires_lf_for_text_files():
    content = Path(".gitattributes").read_text(encoding="utf-8")

    assert "* text=auto eol=lf" in content
    assert "*.py text eol=lf" in content
    assert "*.md text eol=lf" in content


def test_tracked_text_files_use_lf_line_endings():
    raw = subprocess.check_output(["git", "ls-files", "--eol", "-z"])
    offenders: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        if b"i/-text" in record or b"attr/-text" in record:
            continue
        try:
            _meta, path_bytes = record.split(b"\t", 1)
        except ValueError:
            continue
        path = Path(path_bytes.decode("utf-8"))
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\r\n" in data or b"\r" in data:
            offenders.append(path.as_posix())

    assert offenders == []
