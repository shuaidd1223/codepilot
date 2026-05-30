from __future__ import annotations

import json
import stat
from pathlib import Path

from codepilot.commands import doctor as doctor_mod


def _write_manifest(install_dir: Path, providers: list[dict]) -> Path:
    manifest = install_dir / "vendor" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema": "codepilot-installed-cli-v1",
                "providers": providers,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _entry(provider: str, version: str = "1.2.3", path: str | None = None) -> dict:
    return {
        "provider": provider,
        "version": version,
        "platform": "linux",
        "arch": "x64",
        "path": path or f"vendor/{provider}",
        "checksum": "0" * 64,
        "package_name": f"@example/{provider}-linux-x64",
        "root_package": f"@example/{provider}",
    }


def _write_executable(path: Path, body: str = "binary") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _by_provider(results):
    return {item.to_dict().get("provider"): item.to_dict() for item in results if item.name.startswith("bundled_cli_")}


def test_bundled_cli_check_reports_healthy_providers_from_installed_manifest(tmp_path):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("opencode", "2.0.0"), _entry("codex", "1.2.3")])
    _write_executable(install_dir / "vendor" / "opencode")
    _write_executable(install_dir / "vendor" / "codex")
    calls: list[tuple[Path, list[str]]] = []

    def version_runner(command: Path, args: list[str]) -> str:
        calls.append((command, args))
        versions = {"opencode": "opencode version 2.0.0", "codex": "codex-cli 1.2.3"}
        return versions[command.name]

    results = doctor_mod._check_bundled_cli_tools(install_dir=install_dir, version_runner=version_runner)

    providers = _by_provider(results)
    assert providers["opencode"]["status"] == "healthy"
    assert providers["codex"]["status"] == "healthy"
    assert providers["opencode"]["severity"] == "ok"
    assert providers["codex"]["severity"] == "ok"
    assert all(args == ["--version"] for _, args in calls)
    assert set(providers) == {"opencode", "codex"}


def test_bundled_cli_check_resolves_build_manifest_paths_after_binary_install(tmp_path):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("codex", "1.2.3", path="bin/vendor/codex")])
    _write_executable(install_dir / "vendor" / "codex")

    results = doctor_mod._check_bundled_cli_tools(
        install_dir=install_dir,
        version_runner=lambda command, args: "codex-cli 1.2.3",
    )

    codex = _by_provider(results)["codex"]
    assert codex["severity"] == "ok"
    assert codex["status"] == "healthy"
    assert Path(codex["path"]) == install_dir.resolve() / "vendor" / "codex"


def test_bundled_cli_check_reports_missing_vendor_file(tmp_path):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("codex", "1.2.3")])

    results = doctor_mod._check_bundled_cli_tools(install_dir=install_dir, version_runner=lambda *_: "unused")

    codex = _by_provider(results)["codex"]
    assert codex["severity"] == "error"
    assert codex["status"] == "missing"
    assert "不存在" in codex["detail"]


def test_bundled_cli_check_reports_non_executable_vendor_file(tmp_path, monkeypatch):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("opencode", "2.0.0")])
    candidate = install_dir / "vendor" / "opencode"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("binary", encoding="utf-8")
    candidate.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Linux")

    results = doctor_mod._check_bundled_cli_tools(install_dir=install_dir, version_runner=lambda *_: "unused")

    opencode = _by_provider(results)["opencode"]
    assert opencode["severity"] == "error"
    assert opencode["status"] == "not_executable"
    assert "不可执行" in opencode["detail"]


def test_bundled_cli_check_accepts_windows_cmd_executables(tmp_path, monkeypatch):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("opencode", "2.0.0", path="vendor/opencode.cmd")])
    candidate = install_dir / "vendor" / "opencode.cmd"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Windows")

    results = doctor_mod._check_bundled_cli_tools(
        install_dir=install_dir,
        version_runner=lambda command, args: "opencode 2.0.0",
    )

    opencode = _by_provider(results)["opencode"]
    assert opencode["severity"] == "ok"
    assert opencode["status"] == "healthy"


def test_bundled_cli_check_reports_version_mismatch(tmp_path):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("codex", "1.2.3")])
    _write_executable(install_dir / "vendor" / "codex")

    results = doctor_mod._check_bundled_cli_tools(
        install_dir=install_dir,
        version_runner=lambda command, args: "codex-cli 9.9.9",
    )

    codex = _by_provider(results)["codex"]
    assert codex["severity"] == "error"
    assert codex["status"] == "version_mismatch"
    assert "1.2.3" in codex["detail"]
    assert "9.9.9" in codex["detail"]


def test_bundled_cli_check_does_not_require_claude_vendor(tmp_path):
    install_dir = tmp_path / "install-bin"
    _write_manifest(install_dir, [_entry("codex", "1.2.3")])
    _write_executable(install_dir / "vendor" / "codex")

    results = doctor_mod._check_bundled_cli_tools(
        install_dir=install_dir,
        version_runner=lambda command, args: "codex 1.2.3",
    )

    names = {item.name for item in results}
    assert "bundled_cli_claude" not in names
