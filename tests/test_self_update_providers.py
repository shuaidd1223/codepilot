from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

from click.testing import CliRunner

from codepilot.binary_support import vendor_fetcher
from codepilot.cli import main
from codepilot.commands import setup as setup_cmd


def _init_env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))
    target_dir = tmp_path / "install-bin"
    monkeypatch.setenv("CODEPILOT_INSTALL_DIR", str(target_dir))
    return target_dir


def _fake_vendor_update(monkeypatch, calls: list[tuple[list[str], Path, Path]]) -> None:
    def fake_update(
        providers,
        *,
        target_dir,
        cache_dir,
        json_fetcher=None,
        downloader=None,
    ):
        provider_list = list(providers)
        target = Path(target_dir)
        cache = Path(cache_dir)
        calls.append((provider_list, target, cache))
        entries = [
            vendor_fetcher.BundledVendorEntry(
                provider=provider,
                version=f"1.0.{index}",
                platform="linux",
                arch="x64",
                path=f"vendor/{provider}",
                checksum=str(index) * 64,
                package_name=f"@example/{provider}-linux-x64",
                root_package=f"@example/{provider}",
            )
            for index, provider in enumerate(provider_list, start=1)
        ]
        manifest = target / "vendor" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"providers": [entry.__dict__ for entry in entries]}), encoding="utf-8")
        return vendor_fetcher.BundledVendorResult(
            vendor_dir=target / "vendor",
            manifest_path=manifest,
            providers=entries,
        )

    monkeypatch.setattr(vendor_fetcher, "update_installed_vendor_clis", fake_update)


def test_self_update_providers_all_updates_vendors_and_claude(tmp_path, monkeypatch):
    target_dir = _init_env(tmp_path, monkeypatch)
    vendor_calls: list[tuple[list[str], Path, Path]] = []
    npm_calls: list[list[str]] = []
    _fake_vendor_update(monkeypatch, vendor_calls)
    monkeypatch.setattr(setup_cmd, "_find_first_command", lambda names: str(tmp_path / "bin" / "npm"))
    monkeypatch.setattr(setup_cmd, "_run_command", lambda args: npm_calls.append(args))

    result = CliRunner().invoke(main, ["self-update", "--providers", "all", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    results = payload["data"]["results"]
    assert payload["command"] == "self-update"
    assert [item["provider"] for item in results] == ["opencode", "codex", "claude"]
    assert all(item["status"] == "updated" for item in results)
    assert vendor_calls == [(["opencode", "codex"], target_dir.resolve(), tmp_path / "home" / ".codepilot" / "vendor-cache")]
    assert npm_calls == [[str(tmp_path / "bin" / "npm"), "install", "-g", "@anthropic-ai/claude-code"]]
    assert payload["data"]["manifest_path"].endswith("manifest.json")


def test_self_update_providers_single_codex_calls_vendor_fetcher(tmp_path, monkeypatch):
    target_dir = _init_env(tmp_path, monkeypatch)
    vendor_calls: list[tuple[list[str], Path, Path]] = []
    _fake_vendor_update(monkeypatch, vendor_calls)

    result = CliRunner().invoke(main, ["self-update", "--providers", "codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["results"][0]["provider"] == "codex"
    assert vendor_calls == [(["codex"], target_dir.resolve(), tmp_path / "home" / ".codepilot" / "vendor-cache")]


def test_update_installed_vendor_clis_writes_manifest_with_checksum(tmp_path, monkeypatch):
    payload = b"opencode-binary"

    def fake_resolve(provider: str, **_kwargs):
        return vendor_fetcher.VendorDownloadInfo(
            vendor=provider,
            root_package="opencode-ai",
            package_name="opencode-linux-x64",
            version="2.0.0",
            tarball_url="https://registry.example/opencode.tgz",
        )

    def fake_fetch(info: vendor_fetcher.VendorDownloadInfo, *, cache_dir: str | Path, downloader=None):
        archive = Path(cache_dir) / info.vendor / "opencode.tgz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"archive")
        return vendor_fetcher.VendorFetchResult(
            info=info,
            cache_path=archive,
            from_cache=False,
            checksum_verified=True,
            resumed=False,
        )

    def fake_extract(archive_path: str | Path, *, provider: str, vendor_dir: str | Path, platform_tag=None):
        path = Path(vendor_dir) / provider
        path.write_bytes(payload)
        return path

    monkeypatch.setattr(vendor_fetcher, "resolve_download_info", fake_resolve)
    monkeypatch.setattr(vendor_fetcher, "fetch_vendor_binary", fake_fetch)
    monkeypatch.setattr(vendor_fetcher, "extract_vendor_binary", fake_extract)
    monkeypatch.setattr(vendor_fetcher.platform, "system", lambda: "Linux")
    monkeypatch.setattr(vendor_fetcher.platform, "machine", lambda: "x86_64")

    result = vendor_fetcher.update_installed_vendor_clis(
        ["opencode"],
        target_dir=tmp_path / "install-bin",
        cache_dir=tmp_path / "cache",
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    entry = manifest["providers"][0]
    assert manifest["schema"] == "codepilot-installed-cli-v1"
    assert entry["provider"] == "opencode"
    assert entry["version"] == "2.0.0"
    assert entry["path"] == "vendor/opencode"
    assert entry["checksum"] == hashlib.sha256(payload).hexdigest()


def test_self_update_providers_rejects_invalid_provider(tmp_path, monkeypatch):
    _init_env(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["self-update", "--providers", "gemini", "--json"])

    assert result.exit_code == 2
    assert "Invalid value for '--providers'" in result.output


def test_self_update_providers_claude_passes_npm_registry(tmp_path, monkeypatch):
    _init_env(tmp_path, monkeypatch)
    npm_calls: list[list[str]] = []
    monkeypatch.setattr(setup_cmd, "_find_first_command", lambda names: str(tmp_path / "bin" / "npm.cmd"))
    monkeypatch.setattr(setup_cmd, "_run_command", lambda args: npm_calls.append(args))

    result = CliRunner().invoke(
        main,
        [
            "self-update",
            "--providers",
            "claude",
            "--npm-registry",
            "https://registry.example",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert npm_calls == [
        [
            str(tmp_path / "bin" / "npm.cmd"),
            "install",
            "-g",
            "@anthropic-ai/claude-code",
            "--registry",
            "https://registry.example",
        ]
    ]


def test_self_update_providers_all_reports_each_provider_when_one_fails(tmp_path, monkeypatch):
    _init_env(tmp_path, monkeypatch)

    def fake_update(**_kwargs):
        raise vendor_fetcher.VendorFetcherError("registry unavailable")

    monkeypatch.setattr(vendor_fetcher, "update_installed_vendor_clis", fake_update)
    monkeypatch.setattr(setup_cmd, "_find_first_command", lambda names: str(tmp_path / "bin" / "npm"))
    monkeypatch.setattr(
        setup_cmd,
        "_run_command",
        lambda args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args, stderr="npm failed")),
    )

    result = CliRunner().invoke(main, ["self-update", "--providers", "all", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    statuses = {item["provider"]: item for item in payload["data"]["results"]}
    assert statuses["opencode"]["status"] == "failed"
    assert statuses["codex"]["status"] == "failed"
    assert "registry unavailable" in statuses["codex"]["error"]
    assert statuses["claude"]["status"] == "failed"
    assert "npm failed" in statuses["claude"]["error"]
