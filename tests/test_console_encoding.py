from __future__ import annotations

from codepilot.core import console_encoding as encoding_mod


class _DummyStream:
    def __init__(self):
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_configure_console_encoding_sets_utf8_on_windows(monkeypatch):
    stdin = _DummyStream()
    stdout = _DummyStream()
    stderr = _DummyStream()
    flags = {"codepage_called": 0}

    monkeypatch.setattr(encoding_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        encoding_mod,
        "_set_windows_console_codepage_utf8",
        lambda: flags.__setitem__("codepage_called", flags["codepage_called"] + 1),
    )
    monkeypatch.setattr(encoding_mod.sys, "stdin", stdin)
    monkeypatch.setattr(encoding_mod.sys, "stdout", stdout)
    monkeypatch.setattr(encoding_mod.sys, "stderr", stderr)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    encoding_mod.configure_console_encoding()

    assert flags["codepage_called"] == 1
    assert stdin.calls == [{"encoding": "utf-8"}]
    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert encoding_mod.os.environ["PYTHONIOENCODING"] == "utf-8"
    assert encoding_mod.os.environ["PYTHONUTF8"] == "1"


def test_configure_console_encoding_is_noop_on_non_windows(monkeypatch):
    stdin = _DummyStream()
    stdout = _DummyStream()
    stderr = _DummyStream()
    flags = {"codepage_called": 0}

    monkeypatch.setattr(encoding_mod.os, "name", "posix", raising=False)
    monkeypatch.setattr(
        encoding_mod,
        "_set_windows_console_codepage_utf8",
        lambda: flags.__setitem__("codepage_called", flags["codepage_called"] + 1),
    )
    monkeypatch.setattr(encoding_mod.sys, "stdin", stdin)
    monkeypatch.setattr(encoding_mod.sys, "stdout", stdout)
    monkeypatch.setattr(encoding_mod.sys, "stderr", stderr)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    encoding_mod.configure_console_encoding()

    assert flags["codepage_called"] == 0
    assert stdin.calls == []
    assert stdout.calls == []
    assert stderr.calls == []
    assert "PYTHONIOENCODING" not in encoding_mod.os.environ
    assert "PYTHONUTF8" not in encoding_mod.os.environ

