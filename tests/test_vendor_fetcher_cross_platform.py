from __future__ import annotations

import base64
import hashlib
import json
import tarfile
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from codepilot.binary_support import manager as binary_mod
from codepilot.binary_support import vendor_fetcher


def _integrity_for(payload: bytes) -> str:
    digest = hashlib.sha512(payload).digest()
    return f"sha512-{base64.b64encode(digest).decode('ascii')}"


def _tgz_with_executable(name: str, payload: bytes) -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        info = tarfile.TarInfo(f"package/bin/{name}")
        info.mode = 0o755
        info.size = len(payload)
        bundle.addfile(info, BytesIO(payload))
    return stream.getvalue()


def _archive_members(path: Path, archive_format: str) -> set[str]:
    if archive_format == "zip":
        with ZipFile(path, "r") as bundle:
            return {name.rstrip("/") for name in bundle.namelist() if not name.endswith("/")}
    with tarfile.open(path, "r:gz") as bundle:
        return {member.name.rstrip("/") for member in bundle.getmembers() if member.isfile()}


def _fake_registry_and_downloader(platform_package: str, executable_name: str, payload: bytes):
    tgz = _tgz_with_executable(executable_name, payload)

    def fake_json(url: str):
        if url.endswith("opencode-ai"):
            return {
                "dist-tags": {"latest": "1.0.0"},
                "versions": {
                    "1.0.0": {
                        "optionalDependencies": {
                            platform_package: "1.0.0",
                        }
                    }
                },
            }
        if url.endswith(platform_package):
            return {
                "versions": {
                    "1.0.0": {
                        "dist": {
                            "tarball": f"https://registry.example/{platform_package}.tgz",
                            "integrity": _integrity_for(tgz),
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected metadata url: {url}")

    def fake_download(url: str, headers: dict[str, str]):
        assert url == f"https://registry.example/{platform_package}.tgz"
        assert headers == {}
        return vendor_fetcher.DownloadResponse(status=200, headers={}, chunks=[tgz])

    return fake_json, fake_download


@pytest.mark.parametrize(
    (
        "system",
        "machine",
        "platform_tag",
        "expected_npm_platform",
        "expected_npm_arch",
        "expected_package",
        "expected_vendor_name",
    ),
    [
        (
            "Windows",
            "AMD64",
            "windows-x86_64",
            "win32",
            "x64",
            "opencode-windows-x64",
            "opencode.exe",
        ),
        (
            "Darwin",
            "arm64",
            "darwin-arm64",
            "darwin",
            "arm64",
            "opencode-darwin-arm64",
            "opencode",
        ),
        (
            "Linux",
            "x86_64",
            "linux-x86_64",
            "linux",
            "x64",
            "opencode-linux-x64",
            "opencode",
        ),
    ],
)
def test_vendor_fetch_extract_release_and_install_smoke_cross_platform(
    tmp_path,
    monkeypatch,
    system: str,
    machine: str,
    platform_tag: str,
    expected_npm_platform: str,
    expected_npm_arch: str,
    expected_package: str,
    expected_vendor_name: str,
):
    monkeypatch.setattr(vendor_fetcher.platform, "system", lambda: system)
    monkeypatch.setattr(vendor_fetcher.platform, "machine", lambda: machine)

    payload = f"{platform_tag}-opencode".encode("utf-8")
    fake_json, fake_download = _fake_registry_and_downloader(expected_package, expected_vendor_name, payload)
    dist_dir = tmp_path / "dist" / "binary" / platform_tag
    dist_dir.mkdir(parents=True)
    binary_name = "codepilot.exe" if platform_tag.startswith("windows") else "codepilot"
    binary_path = dist_dir / binary_name
    binary_path.write_bytes(b"codepilot-binary")

    bundle_result = vendor_fetcher.bundle_vendor_clis(
        ["opencode"],
        output_dir=dist_dir,
        platform_tag=platform_tag,
        cache_dir=tmp_path / "cache",
        json_fetcher=fake_json,
        downloader=fake_download,
    )

    vendor_path = dist_dir / "bin" / "vendor" / expected_vendor_name
    manifest_path = dist_dir / "bin" / "vendor" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["providers"][0]
    assert bundle_result.providers[0].package_name == expected_package
    assert vendor_path.read_bytes() == payload
    assert entry["platform"] == expected_npm_platform
    assert entry["arch"] == expected_npm_arch
    assert entry["path"] == f"bin/vendor/{expected_vendor_name}"
    assert entry["package_name"] == expected_package
    assert len(entry["checksum"]) == 64
    assert vendor_path.suffix == (".exe" if platform_tag.startswith("windows") else "")

    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[(platform_tag, binary_path)],
        output_dir=tmp_path / "release" / platform_tag,
        version="9.9.9",
    )

    artifact = release.artifacts[0]
    folder_name = binary_mod._archive_root_folder(artifact.archive_path, artifact.archive_format)
    members = _archive_members(artifact.archive_path, artifact.archive_format)
    assert f"{folder_name}/bin/vendor/{expected_vendor_name}" in members
    assert f"{folder_name}/bin/vendor/manifest.json" in members
    verification = binary_mod.verify_release_bundle(release.release_dir)
    assert verification.issues == []

    install_dir = tmp_path / "install" / platform_tag
    copied = vendor_fetcher.install_bundled_vendor(binary_path, target_dir=install_dir)
    installed_vendor = install_dir / "vendor" / expected_vendor_name
    installed_manifest = json.loads((install_dir / "vendor" / "manifest.json").read_text(encoding="utf-8"))
    assert installed_vendor.read_bytes() == payload
    assert installed_manifest["providers"][0]["path"] == f"bin/vendor/{expected_vendor_name}"
    assert {path.name for path in copied} == {expected_vendor_name, "manifest.json"}
