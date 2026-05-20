from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path

from click.testing import CliRunner

from codepilot.binary_support import vendor_fetcher
from codepilot.binary_support import manager as binary_mod
from codepilot.cli import main


def _tgz_with_executable(name: str, payload: bytes) -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        info = tarfile.TarInfo(f"package/bin/{name}")
        info.mode = 0o755
        info.size = len(payload)
        bundle.addfile(info, BytesIO(payload))
    return stream.getvalue()


def test_binary_build_bundle_cli_writes_vendor_files_and_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dist_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    dist_dir.mkdir(parents=True)
    binary_path = dist_dir / "codepilot"
    binary_path.write_text("binary", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=binary_path.resolve(),
            dist_dir=dist_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="linux-x86_64",
        ),
    )
    monkeypatch.setattr(vendor_fetcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(vendor_fetcher.platform, "machine", lambda: "x86_64")
    resolved: list[str] = []

    def fake_resolve(provider: str, **_kwargs):
        resolved.append(provider)
        return vendor_fetcher.VendorDownloadInfo(
            vendor=provider,
            root_package=f"@example/{provider}",
            package_name=f"@example/{provider}-linux-x64",
            version=f"1.0.{len(resolved)}",
            tarball_url=f"https://registry.example/{provider}.tgz",
        )

    def fake_fetch(info: vendor_fetcher.VendorDownloadInfo, *, cache_dir: str | Path, downloader=None):
        path = Path(cache_dir) / info.vendor / f"{info.vendor}.tgz"
        path.parent.mkdir(parents=True)
        path.write_bytes(_tgz_with_executable(info.vendor, f"{info.vendor}-binary".encode("utf-8")))
        return vendor_fetcher.VendorFetchResult(
            info=info,
            cache_path=path,
            from_cache=False,
            checksum_verified=True,
            resumed=False,
        )

    monkeypatch.setattr(vendor_fetcher, "resolve_download_info", fake_resolve)
    monkeypatch.setattr(vendor_fetcher, "fetch_vendor_binary", fake_fetch)

    result = CliRunner().invoke(main, ["binary", "build", "--bundle-cli=opencode,codex"])

    assert result.exit_code == 0, result.output
    vendor_dir = dist_dir / "bin" / "vendor"
    assert (vendor_dir / "opencode").read_bytes() == b"opencode-binary"
    assert (vendor_dir / "codex").read_bytes() == b"codex-binary"
    manifest = json.loads((vendor_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [item["provider"] for item in manifest["providers"]] == ["opencode", "codex"]
    assert manifest["providers"][0]["platform"] == "linux"
    assert manifest["providers"][0]["arch"] == "x64"
    assert manifest["providers"][0]["path"] == "bin/vendor/opencode"
    assert len(manifest["providers"][0]["checksum"]) == 64


def test_binary_build_bundle_cli_rejects_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["binary", "build", "--bundle-cli=claude"])

    assert result.exit_code != 0
    assert "不支持的 --bundle-cli provider" in result.output
    assert "opencode, codex" in result.output


def test_binary_install_releases_bundled_vendor_files(tmp_path, monkeypatch):
    source_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    source_dir.mkdir(parents=True)
    source = source_dir / "codepilot"
    source.write_text("binary", encoding="utf-8")
    web_dir = source_dir / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    feishu_dir = source_dir / "feishu" / "node_modules" / "@larksuiteoapi" / "node-sdk"
    feishu_dir.mkdir(parents=True)
    (source_dir / "feishu" / "package.json").write_text("{}", encoding="utf-8")
    (source_dir / "feishu" / "feishu_worker.mjs").write_text("import 'x';\n", encoding="utf-8")
    (feishu_dir / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    vendor_dir = source_dir / "bin" / "vendor"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "opencode").write_text("opencode-binary", encoding="utf-8")
    (vendor_dir / "manifest.json").write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "opencode",
                        "version": "1.0.0",
                        "platform": "linux",
                        "arch": "x64",
                        "path": "bin/vendor/opencode",
                        "checksum": "0" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    target_dir = tmp_path / "install-bin"

    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")

    result = CliRunner().invoke(
        main,
        [
            "binary",
            "install",
            "--binary",
            str(source),
            "--target-dir",
            str(target_dir),
            "--no-register-path",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (target_dir / "codepilot").exists()
    assert (target_dir / "web" / "index.html").read_text(encoding="utf-8") == "<html></html>"
    assert (target_dir / "feishu" / "package.json").exists()
    assert (target_dir / "feishu" / "feishu_worker.mjs").exists()
    assert (target_dir / "feishu" / "node_modules" / "@larksuiteoapi" / "node-sdk" / "index.js").exists()
    assert (target_dir / "vendor" / "opencode").read_text(encoding="utf-8") == "opencode-binary"
    assert (target_dir / "vendor" / "manifest.json").exists()
