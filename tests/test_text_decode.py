"""Unit tests for subprocess text decoding with multi-encoding support."""

from __future__ import annotations

import codecs

import pytest

from codepilot.core.text_decode import decode_subprocess_text


# ---------------------------------------------------------------------------
# UTF-16 LE / BE with BOM
# ---------------------------------------------------------------------------

_CHINESE_UTF8 = "你好世界".encode("utf-8")
_CHINESE_UTF16_LE = codecs.BOM_UTF16_LE + "你好世界".encode("utf-16-le")
_CHINESE_UTF16_BE = codecs.BOM_UTF16_BE + "你好世界".encode("utf-16-be")
_MIXED_UTF16_LE = codecs.BOM_UTF16_LE + "INFO  使用执行器 builtin".encode("utf-16-le")


def test_decode_utf16_le_with_bom_returns_readable_chinese():
    result = decode_subprocess_text(_CHINESE_UTF16_LE)
    assert result == "你好世界"


def test_decode_utf16_be_with_bom_returns_readable_chinese():
    result = decode_subprocess_text(_CHINESE_UTF16_BE)
    assert result == "你好世界"


def test_decode_mixed_ascii_chinese_utf16_le():
    result = decode_subprocess_text(_MIXED_UTF16_LE)
    assert "使用执行器" in result
    assert "INFO" in result


# ---------------------------------------------------------------------------
# UTF-8 (unchanged – regression)
# ---------------------------------------------------------------------------


def test_decode_utf8_unchanged():
    result = decode_subprocess_text(_CHINESE_UTF8)
    assert result == "你好世界"


def test_decode_utf8_ascii_unchanged():
    assert decode_subprocess_text(b"hello") == "hello"


# ---------------------------------------------------------------------------
# GB18030 / CP936 (unchanged – regression)
# ---------------------------------------------------------------------------


def test_decode_gb18030_unchanged():
    gb_bytes = "中文测试".encode("gb18030")
    result = decode_subprocess_text(gb_bytes)
    assert result == "中文测试"


def test_decode_cp936_unchanged():
    cp_bytes = "命令行".encode("cp936")
    result = decode_subprocess_text(cp_bytes)
    assert result == "命令行"


# ---------------------------------------------------------------------------
# Fallback (invalid bytes → U+FFFD)
# ---------------------------------------------------------------------------


def test_decode_invalid_bytes_falls_back_to_replace():
    # Deliberately invalid: a lone continuation byte 0x80 cannot start a
    # valid UTF-8, UTF-16, GB18030, or CP936 sequence.
    result = decode_subprocess_text(b"\x80\x81")
    assert "\ufffd" in result


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_decode_empty_bytes_returns_empty_string():
    assert decode_subprocess_text(b"") == ""


def test_decode_none_returns_empty_string():
    assert decode_subprocess_text(None) == ""


def test_decode_str_passthrough():
    assert decode_subprocess_text("already_str") == "already_str"


def test_decode_utf16_no_bom_falls_back_to_replace():
    """Without a BOM, pure UTF-16 LE bytes are ambiguous and should
    not be auto-detected — fallback to replace mode is acceptable."""
    raw = "测试".encode("utf-16-le")  # no BOM
    result = decode_subprocess_text(raw)
    # The result may contain replacement characters because utf-16 is only
    # tried when a BOM is present.  This is by design — without a BOM,
    # UTF-16 LE bytes are indistinguishable from garbage in other encodings.
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Parametrized smoke: BOM → correct codec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,payload",
    [
        ("utf16-le", _CHINESE_UTF16_LE),
        ("utf16-be", _CHINESE_UTF16_BE),
        ("utf8", _CHINESE_UTF8),
        ("gb18030", "中文测试".encode("gb18030")),
        ("cp936", "命令行".encode("cp936")),
    ],
)
def test_smoke_roundtrip(label, payload):
    """Every supported encoding must round-trip through decode_subprocess_text."""
    expected = payload
    # Re-encode the round-tripped result so we compare byte-for-byte.
    result_text = decode_subprocess_text(expected)
    # For the tested encodings, result_text must contain the original text.
    # Use the corresponding codec to reconstruct expected bytes.
    if label == "utf16-le":
        reference = "你好世界"
    elif label == "utf16-be":
        reference = "你好世界"
    elif label == "utf8":
        reference = "你好世界"
    elif label == "gb18030":
        reference = "中文测试"
    elif label == "cp936":
        reference = "命令行"
    else:
        pytest.fail(f"Unknown label {label}")
    assert result_text == reference
