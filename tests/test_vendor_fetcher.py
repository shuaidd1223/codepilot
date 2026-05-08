from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from codepilot.binary_support import vendor_fetcher


def _integrity_for(payload: bytes, algorithm: str = "sha512") -> str:
    digest = hashlib.new(algorithm, payload).digest()
    return f"{algorithm}-{base64.b64encode(digest).decode('ascii')}"


def test_opencode_platform_package_name_maps_supported_hosts():
    assert (
        vendor_fetcher.platform_package_name("opencode", system="Windows", machine="AMD64")
        == "@opencode/opencode-win32-x64"
    )
    assert (
        vendor_fetcher.platform_package_name("opencode", system="Darwin", machine="arm64")
        == "@opencode/opencode-darwin-arm64"
    )
    assert (
        vendor_fetcher.platform_package_name("opencode", system="Linux", machine="x86_64")
        == "@opencode/opencode-linux-x64"
    )


def test_codex_platform_package_download_info_is_resolved_from_npm_metadata():
    payload = b"codex-binary"
    calls: list[str] = []

    def fake_json(url: str):
        calls.append(url)
        if url.endswith("@openai%2Fcodex"):
            return {
                "dist-tags": {"latest": "1.2.3"},
                "versions": {
                    "1.2.3": {
                        "optionalDependencies": {
                            "@openai/codex-linux-x64": "1.2.3",
                            "@openai/codex-darwin-arm64": "1.2.3",
                        }
                    }
                },
            }
        if url.endswith("@openai%2Fcodex-linux-x64"):
            return {
                "versions": {
                    "1.2.3": {
                        "dist": {
                            "tarball": "https://registry.example/@openai/codex-linux-x64/-/codex.tgz",
                            "integrity": _integrity_for(payload),
                            "shasum": hashlib.sha1(payload).hexdigest(),
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected url: {url}")

    info = vendor_fetcher.resolve_download_info(
        "codex",
        system="Linux",
        machine="x64",
        json_fetcher=fake_json,
    )

    assert info.root_package == "@openai/codex"
    assert info.package_name == "@openai/codex-linux-x64"
    assert info.version == "1.2.3"
    assert info.tarball_url.endswith("/codex.tgz")
    assert info.integrity.startswith("sha512-")
    assert calls == [
        "https://registry.npmjs.org/@openai%2Fcodex",
        "https://registry.npmjs.org/@openai%2Fcodex-linux-x64",
    ]


def test_opencode_platform_package_download_info_is_resolved_from_npm_metadata():
    payload = b"opencode-binary"

    def fake_json(url: str):
        if url.endswith("@opencode%2Fopencode"):
            return {
                "dist-tags": {"latest": "2.4.0"},
                "versions": {
                    "2.4.0": {
                        "optionalDependencies": {
                            "@opencode/opencode-win32-arm64": "2.4.0",
                        }
                    }
                },
            }
        if url.endswith("@opencode%2Fopencode-win32-arm64"):
            return {
                "versions": {
                    "2.4.0": {
                        "dist": {
                            "tarball": "https://registry.example/@opencode/opencode-win32-arm64/-/opencode.tgz",
                            "integrity": _integrity_for(payload),
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected url: {url}")

    info = vendor_fetcher.resolve_download_info(
        "opencode",
        system="Windows",
        machine="arm64",
        json_fetcher=fake_json,
    )

    assert info.root_package == "@opencode/opencode"
    assert info.package_name == "@opencode/opencode-win32-arm64"
    assert info.version == "2.4.0"
    assert info.tarball_url.endswith("/opencode.tgz")


def test_fetch_vendor_binary_reuses_valid_cache(tmp_path):
    payload = b"cached-binary"
    info = vendor_fetcher.VendorDownloadInfo(
        vendor="opencode",
        root_package="@opencode/opencode",
        package_name="@opencode/opencode-linux-x64",
        version="2.0.0",
        tarball_url="https://registry.example/opencode.tgz",
        integrity=_integrity_for(payload),
        shasum=hashlib.sha1(payload).hexdigest(),
    )
    first_calls = {"download": 0}

    def first_download(_url: str, _headers: dict[str, str]):
        first_calls["download"] += 1
        return vendor_fetcher.DownloadResponse(status=200, headers={}, chunks=[payload])

    first = vendor_fetcher.fetch_vendor_binary(info, cache_dir=tmp_path, downloader=first_download)

    assert first.cache_path.exists()
    assert first.cache_path.read_bytes() == payload
    assert first.from_cache is False
    assert first_calls["download"] == 1

    def fail_download(_url: str, _headers: dict[str, str]):
        raise AssertionError("cache hit must not download")

    second = vendor_fetcher.fetch_vendor_binary(info, cache_dir=tmp_path, downloader=fail_download)

    assert second.cache_path == first.cache_path
    assert second.from_cache is True


def test_fetch_vendor_binary_rejects_checksum_mismatch(tmp_path):
    info = vendor_fetcher.VendorDownloadInfo(
        vendor="codex",
        root_package="@openai/codex",
        package_name="@openai/codex-linux-x64",
        version="1.0.0",
        tarball_url="https://registry.example/codex.tgz",
        integrity=_integrity_for(b"expected"),
    )

    def fake_download(_url: str, _headers: dict[str, str]):
        return vendor_fetcher.DownloadResponse(status=200, headers={}, chunks=[b"actual"])

    with pytest.raises(vendor_fetcher.ChecksumMismatchError):
        vendor_fetcher.fetch_vendor_binary(info, cache_dir=tmp_path, downloader=fake_download)

    assert not any(path.suffix == ".tmp" for path in tmp_path.rglob("*") if path.is_file())


def test_fetch_vendor_binary_resumes_partial_temp_file(tmp_path):
    payload = b"abcdef"
    info = vendor_fetcher.VendorDownloadInfo(
        vendor="codex",
        root_package="@openai/codex",
        package_name="@openai/codex-linux-x64",
        version="1.0.0",
        tarball_url="https://registry.example/codex.tgz",
        integrity=_integrity_for(payload),
    )
    partial = vendor_fetcher.cache_path_for(info, tmp_path).with_suffix(".tgz.tmp")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"abc")
    captured: dict[str, dict[str, str]] = {}

    def fake_download(_url: str, headers: dict[str, str]):
        captured["headers"] = headers
        return vendor_fetcher.DownloadResponse(status=206, headers={}, chunks=[b"def"])

    result = vendor_fetcher.fetch_vendor_binary(info, cache_dir=tmp_path, downloader=fake_download)

    assert captured["headers"]["Range"] == "bytes=3-"
    assert result.resumed is True
    assert result.cache_path.read_bytes() == payload
    assert not partial.exists()


def test_resume_falls_back_to_full_download_when_server_ignores_range(tmp_path):
    payload = b"new-full"
    info = vendor_fetcher.VendorDownloadInfo(
        vendor="codex",
        root_package="@openai/codex",
        package_name="@openai/codex-linux-x64",
        version="1.0.0",
        tarball_url="https://registry.example/codex.tgz",
        integrity=_integrity_for(payload),
    )
    partial = vendor_fetcher.cache_path_for(info, tmp_path).with_suffix(".tgz.tmp")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"old")

    def fake_download(_url: str, headers: dict[str, str]):
        assert headers["Range"] == "bytes=3-"
        return vendor_fetcher.DownloadResponse(status=200, headers={}, chunks=[payload])

    result = vendor_fetcher.fetch_vendor_binary(info, cache_dir=tmp_path, downloader=fake_download)

    assert result.resumed is False
    assert result.cache_path.read_bytes() == payload
