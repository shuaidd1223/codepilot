"""Unit tests for tail_text() encoding auto-detection."""

from __future__ import annotations

import codecs

from codepilot.core.runtime import _detect_text_encoding, tail_text


# ---------------------------------------------------------------------------
# _detect_text_encoding
# ---------------------------------------------------------------------------


def test_detect_utf16_le_via_bom(tmp_path):
    f = tmp_path / "utf16le.log"
    f.write_bytes(codecs.BOM_UTF16_LE + "hello".encode("utf-16-le"))
    assert _detect_text_encoding(f) == "utf-16"


def test_detect_utf16_be_via_bom(tmp_path):
    f = tmp_path / "utf16be.log"
    f.write_bytes(codecs.BOM_UTF16_BE + "hello".encode("utf-16-be"))
    assert _detect_text_encoding(f) == "utf-16"


def test_detect_utf8_no_bom(tmp_path):
    f = tmp_path / "utf8.log"
    f.write_text("hello", encoding="utf-8")
    assert _detect_text_encoding(f) == "utf-8"


def test_detect_empty_file_defaults_to_utf8(tmp_path):
    f = tmp_path / "empty.log"
    f.write_bytes(b"")
    assert _detect_text_encoding(f) == "utf-8"


def test_detect_nonexistent_file_returns_utf8(tmp_path):
    f = tmp_path / "missing.log"
    assert not f.exists()
    assert _detect_text_encoding(f) == "utf-8"


# ---------------------------------------------------------------------------
# tail_text
# ---------------------------------------------------------------------------


def test_tail_text_utf16_le_file(tmp_path):
    f = tmp_path / "live.log"
    f.write_bytes(
        codecs.BOM_UTF16_LE
        + "line1\nline2\n使用执行器 builtin\nline4\n".encode("utf-16-le")
    )
    out = tail_text(str(f), max_lines=3, max_chars=2000)
    assert "使用执行器" in out


def test_tail_text_utf8_file(tmp_path):
    f = tmp_path / "live.log"
    f.write_text("line1\nline2\n中文测试\nline4\n", encoding="utf-8")
    out = tail_text(str(f), max_lines=3, max_chars=2000)
    assert "中文测试" in out


def test_tail_text_nonexistent_returns_empty():
    assert tail_text("/nonexistent/path.log") == ""


def test_tail_text_none_path_returns_empty():
    assert tail_text(None) == ""


def test_tail_text_max_lines_truncation(tmp_path):
    f = tmp_path / "live.log"
    f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    out = tail_text(str(f), max_lines=2, max_chars=2000)
    assert out == "d\ne"


def test_tail_text_max_chars_truncation(tmp_path):
    f = tmp_path / "live.log"
    f.write_text("abcdefghij\nklmnopqrst\n", encoding="utf-8")
    out = tail_text(str(f), max_lines=10, max_chars=5)
    # When max_chars < content, tail_text slices from the end.
    assert len(out) == 5
    # read_text + strip + splitlines + join yields "abcdefghij\nklmnopqrst"
    # last 5 chars of that = "pqrst"
    assert out == "pqrst"


def test_tail_text_path_is_path_object(tmp_path):
    f = tmp_path / "live.log"
    f.write_text("hello", encoding="utf-8")
    out = tail_text(f)  # Path object, not str
    assert out == "hello"
