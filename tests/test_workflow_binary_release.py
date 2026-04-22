from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from zipfile import ZipFile

import click
import pytest
from click.testing import CliRunner

from codepilot.agent_support import ai_guide_markdown, command_manifest
from codepilot import binary as binary_mod
from codepilot import binary_paths as binary_paths_mod
from codepilot import db
from codepilot import ai as ai_mod
from codepilot import progress_bus
from codepilot.ai_gateway import GatewayResponse
from codepilot import runtime as runtime_mod
from codepilot import webui as webui_mod
from codepilot.cli import main
from codepilot.commands import add as add_cmd
from codepilot.commands import auto as auto_cmd
from codepilot.commands import run as run_cmd
from codepilot.config import load_project_config
from tests.workflow_testkit import init_test_db as _init_test_db


def test_binary_default_install_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    install_dir = binary_mod.default_install_dir()

    assert install_dir == (tmp_path / "LocalAppData" / "Programs" / "CodePilot" / "bin").resolve()


def test_update_project_version_updates_pyproject_and_init(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    init_file = package_dir / "__init__.py"
    pyproject.write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    previous, current = binary_mod.update_project_version(tmp_path, "0.2.0")

    assert previous == "0.1.0"
    assert current == "0.2.0"
    assert 'version = "0.2.0"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.2.0"' in init_file.read_text(encoding="utf-8")


def test_binary_resolve_install_source_prefers_latest_build(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    dist_dir.mkdir(parents=True)
    binary_path = dist_dir / "codepilot.exe"
    binary_path.write_text("exe", encoding="utf-8")

    monkeypatch.setattr(binary_mod, "running_binary_path", lambda: None)
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Windows")

    resolved = binary_mod.resolve_install_source(None, project_root=tmp_path)

    assert resolved == binary_path.resolve()


def test_install_binary_copies_file_and_registers_path(tmp_path, monkeypatch):
    source = tmp_path / "codepilot"
    source.write_text("binary", encoding="utf-8")
    target_dir = tmp_path / "bin"
    captured = {}

    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        binary_mod,
        "register_install_dir",
        lambda directory: captured.update({"directory": Path(directory)}) or (True, "ok"),
    )

    result = binary_mod.install_binary(binary_path=source, target_dir=target_dir, register_path=True)

    assert result.installed_path == (target_dir / "codepilot").resolve()
    assert result.installed_path.exists()
    assert captured["directory"] == target_dir.resolve()


def test_register_windows_path_promotes_install_dir_to_front_and_deduplicates(monkeypatch, tmp_path):
    target = (tmp_path / "LocalAppData" / "Programs" / "CodePilot" / "bin").resolve()
    target_str = str(target)
    state = {
        "path": f"C:\\Tools\\A;{target_str};C:\\Tools\\B;{target_str}",
        "set_calls": 0,
    }

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _query_value(_key, _name):
        return state["path"], 1

    def _set_value(_key, _name, _reserved, _reg_type, value):
        state["set_calls"] += 1
        state["path"] = value

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        KEY_WRITE=2,
        REG_EXPAND_SZ=2,
        OpenKey=lambda *args, **kwargs: _FakeKey(),
        QueryValueEx=_query_value,
        SetValueEx=_set_value,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    notified = {"value": False}
    monkeypatch.setattr(binary_paths_mod, "_broadcast_windows_env_change", lambda: notified.update({"value": True}))

    changed, message = binary_mod._register_windows_path(target)

    assert changed is True
    assert "置顶" in message
    assert notified["value"] is True
    assert state["set_calls"] == 1
    entries = [entry for entry in state["path"].split(";") if entry]
    assert entries[0] == target_str
    target_norm = binary_mod._normalize_path(target)
    assert sum(1 for entry in entries if binary_mod._normalize_path(entry) == target_norm) == 1


def test_register_windows_path_keeps_existing_front_entry_without_rewrite(monkeypatch, tmp_path):
    target = (tmp_path / "LocalAppData" / "Programs" / "CodePilot" / "bin").resolve()
    target_str = str(target)
    state = {
        "path": f"{target_str};C:\\Tools\\A;C:\\Tools\\B",
        "set_calls": 0,
    }

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _query_value(_key, _name):
        return state["path"], 1

    def _set_value(_key, _name, _reserved, _reg_type, value):
        state["set_calls"] += 1
        state["path"] = value

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        KEY_WRITE=2,
        REG_EXPAND_SZ=2,
        OpenKey=lambda *args, **kwargs: _FakeKey(),
        QueryValueEx=_query_value,
        SetValueEx=_set_value,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    notified = {"value": False}
    monkeypatch.setattr(binary_paths_mod, "_broadcast_windows_env_change", lambda: notified.update({"value": True}))

    changed, message = binary_mod._register_windows_path(target)

    assert changed is False
    assert "前列" in message
    assert notified["value"] is False
    assert state["set_calls"] == 0
    assert state["path"] == f"{target_str};C:\\Tools\\A;C:\\Tools\\B"


def test_binary_build_command_invokes_pyinstaller(tmp_path, monkeypatch):
    (tmp_path / "codepilot").mkdir()
    (tmp_path / "codepilot" / "__main__.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "codepilot" / "templates").mkdir()
    (tmp_path / "codepilot" / "templates" / "demo.md").write_text("x", encoding="utf-8")
    dist_dir = tmp_path / "dist-out"
    build_dir = tmp_path / "build-out"
    captured = {}

    monkeypatch.setattr(binary_mod, "default_build_dir", lambda root: build_dir)
    monkeypatch.setattr(binary_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary_mod.platform, "machine", lambda: "x86_64")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        binary_path = dist_dir / "codepilot"
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        binary_path.write_text("exe", encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(binary_mod.subprocess, "run", fake_run)

    result = binary_mod.build_binary(project_root=tmp_path, output_dir=dist_dir, clean=True)

    assert result.binary_path == (dist_dir / "codepilot").resolve()
    assert "--onefile" in captured["cmd"]
    assert "--collect-all" in captured["cmd"]


def test_binary_where_command_prints_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_INSTALL_DIR", str(tmp_path / "custom-bin"))
    runner = CliRunner()

    result = runner.invoke(main, ["binary", "where"])

    assert result.exit_code == 0
    assert str((tmp_path / "custom-bin").resolve()) in result.output


def test_resolve_release_inputs_uses_dist_binaries_by_default(tmp_path, monkeypatch):
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    linux_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    win_dir.mkdir(parents=True)
    linux_dir.mkdir(parents=True)
    (win_dir / "codepilot.exe").write_text("exe", encoding="utf-8")
    (linux_dir / "codepilot").write_text("bin", encoding="utf-8")

    resolved = binary_mod.resolve_release_inputs(project_root=tmp_path)

    assert resolved == [
        ("linux-x86_64", (linux_dir / "codepilot").resolve()),
        ("windows-x86_64", (win_dir / "codepilot.exe").resolve()),
    ]


def test_create_release_bundle_generates_manifest_checksums_and_archives(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    output_dir = tmp_path / "release"

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=output_dir,
        version="1.2.3",
    )

    assert result.release_dir == output_dir.resolve()
    assert result.manifest_path.exists()
    assert result.checksum_path.exists()
    assert result.guide_path.exists()
    assert result.summary_path.exists()
    assert result.ai_guide_path.exists()
    assert result.ai_manifest_path.exists()
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.staged_path.exists()
    assert artifact.archive_path.exists()
    assert artifact.archive_format == "zip"
    install_script = output_dir / "windows-x86_64" / "install-codepilot.cmd"
    assert install_script.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.3"
    assert manifest["artifacts"][0]["platform"] == "windows-x86_64"
    assert manifest["artifacts"][0]["archive_format"] == "zip"
    assert "install_script" in manifest["artifacts"][0]
    checksums = result.checksum_path.read_text(encoding="utf-8")
    assert "windows-x86_64/codepilot.exe" in checksums.replace("\\", "/")
    assert artifact.archive_path.name in checksums
    assert "发布说明" in result.guide_path.read_text(encoding="utf-8")
    assert "AI 调用手册" in result.ai_guide_path.read_text(encoding="utf-8")
    assert json.loads(result.ai_manifest_path.read_text(encoding="utf-8"))["name"] == "CodePilot"
    assert "发布摘要" in result.summary_path.read_text(encoding="utf-8")


def test_windows_install_script_cleans_legacy_cmd_and_prioritizes_path():
    script = binary_mod._windows_install_script("codepilot.exe")

    assert "del /F /Q \"%TARGET_DIR%\\codepilot.cmd\"" in script
    assert "del /F /Q \"%TARGET_DIR%\\codepilot.bat\"" in script
    assert "$updatedParts=@($dir) + $filtered;" in script
    assert "$current + ';' + $dir" not in script


def test_create_release_bundle_ai_manifest_matches_release_version_and_name(tmp_path):
    binary_path = tmp_path / "pilot.exe"
    binary_path.write_text("binary", encoding="utf-8")

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release-custom",
        version="1.2.3",
        name="mypilot",
    )

    payload = json.loads(result.ai_manifest_path.read_text(encoding="utf-8"))
    release_prepare = next(item for item in payload["commands"] if item["name"] == "release_prepare")
    release_bundle = next(item for item in payload["commands"] if item["name"] == "release_bundle")

    assert payload["version"] == "1.2.3"
    assert payload["command_name"] == "mypilot"
    assert payload["structured_outputs"][0]["command"] == "mypilot ai manifest"
    assert release_prepare["syntax"] == "mypilot release prepare --version <版本号>"
    assert "dist/binary/linux-x86_64/mypilot" in release_bundle["examples"][1]


def test_binary_release_command_packages_existing_builds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    win_dir.mkdir(parents=True)
    (win_dir / "codepilot.exe").write_text("exe", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--version", "9.9.9"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-9.9.9"
    assert release_dir.exists()
    assert (release_dir / "release.json").exists()
    assert (release_dir / "SHA256SUMS.txt").exists()
    assert (release_dir / "README.zh-CN.md").exists()
    assert (release_dir / "AI_USAGE.zh-CN.md").exists()
    assert (release_dir / "AI_MANIFEST.json").exists()
    assert (release_dir / "SUMMARY.zh-CN.md").exists()
    assert (release_dir / "windows-x86_64" / "install-codepilot.cmd").exists()
    assert "guide:" in result.output
    assert "ai guide:" in result.output
    assert "ai manifest:" in result.output
    assert "summary:" in result.output


def test_verify_release_bundle_passes_for_valid_release(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release",
        version="1.0.0",
    )

    verification = binary_mod.verify_release_bundle(release.release_dir)

    assert verification.issues == []
    assert verification.checked_files == 2


def test_binary_verify_command_fails_on_broken_checksum(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "dist" / "release" / "codepilot-1.0.0",
        version="1.0.0",
    )
    release.checksum_path.write_text("broken line\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "verify", "--release-dir", str(release.release_dir)])

    assert result.exit_code != 0
    assert "校验失败" in result.output


def test_verify_release_bundle_fails_when_archive_missing_expected_files(tmp_path):
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "release",
        version="1.0.0",
    )
    archive_path = release.artifacts[0].archive_path
    with ZipFile(archive_path, "w") as bundle:
        bundle.writestr("broken/file.txt", "x")

    verification = binary_mod.verify_release_bundle(release.release_dir)

    assert any("压缩包缺少预期文件" in issue or "压缩包校验不匹配" in issue for issue in verification.issues)


def test_binary_release_build_current_merges_new_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    win_dir.mkdir(parents=True)
    existing = win_dir / "codepilot.exe"
    existing.write_text("old", encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "linux-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot"
    built_binary.write_text("new", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="linux-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--build-current", "--version", "2.0.0"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-2.0.0"
    assert (release_dir / "windows-x86_64" / "codepilot.exe").exists()
    assert (release_dir / "linux-x86_64" / "codepilot").exists()


def test_binary_release_build_current_works_without_existing_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "release", "--build-current", "--version", "3.0.0"])

    assert result.exit_code == 0
    release_dir = tmp_path / "dist" / "release" / "codepilot-3.0.0"
    assert (release_dir / "windows-x86_64" / "codepilot.exe").exists()


def test_create_release_bundle_uses_tar_gz_for_linux(tmp_path):
    binary_path = tmp_path / "codepilot"
    binary_path.write_text("binary", encoding="utf-8")

    result = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("linux-x86_64", binary_path)],
        output_dir=tmp_path / "release-linux",
        version="1.0.0",
    )

    artifact = result.artifacts[0]
    assert artifact.archive_format == "tar.gz"
    assert artifact.archive_path.name.endswith(".tar.gz")
    assert (result.release_dir / "linux-x86_64" / "install-codepilot.sh").exists()


def test_binary_prepare_command_updates_version_and_verifies_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    (package_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "prepare", "--version", "1.2.0"])

    assert result.exit_code == 0
    assert "版本已更新" in result.output
    assert 'version = "1.2.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "1.2.0"' in (package_dir / "__init__.py").read_text(encoding="utf-8")
    assert (tmp_path / "dist" / "release" / "codepilot-1.2.0" / "release.json").exists()
    assert "发布目录校验通过" in result.output


def test_binary_prepare_rolls_back_version_when_build_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    pyproject = tmp_path / "pyproject.toml"
    init_file = package_dir / "__init__.py"
    pyproject.write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    monkeypatch.setattr("codepilot.commands.binary.build_binary", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")))

    runner = CliRunner()
    result = runner.invoke(main, ["binary", "prepare", "--version", "2.0.0"])

    assert result.exit_code != 0
    assert 'version = "0.1.0"' in pyproject.read_text(encoding="utf-8")
    assert '__version__ = "0.1.0"' in init_file.read_text(encoding="utf-8")


def test_release_prepare_alias_invokes_prepare(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "codepilot"
    package_dir.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "codepilot"\nversion = "0.1.0"\n', encoding="utf-8")
    (package_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    built_dir = tmp_path / "dist" / "binary" / "windows-x86_64"
    built_dir.mkdir(parents=True)
    built_binary = built_dir / "codepilot.exe"
    built_binary.write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(
        "codepilot.commands.binary.build_binary",
        lambda **kwargs: binary_mod.BuildResult(
            binary_path=built_binary.resolve(),
            dist_dir=built_dir.resolve(),
            build_dir=(tmp_path / "build").resolve(),
            platform_tag="windows-x86_64",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["release", "prepare", "--version", "1.3.0"])

    assert result.exit_code == 0
    assert (tmp_path / "dist" / "release" / "codepilot-1.3.0" / "release.json").exists()


def test_release_verify_alias_invokes_verify(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary_path = tmp_path / "codepilot.exe"
    binary_path.write_text("binary", encoding="utf-8")
    release = binary_mod.create_release_bundle(
        project_root=tmp_path,
        artifacts=[("windows-x86_64", binary_path)],
        output_dir=tmp_path / "dist" / "release" / "codepilot-1.0.0",
        version="1.0.0",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["release", "verify", "--release-dir", str(release.release_dir)])

    assert result.exit_code == 0
    assert "发布目录校验通过" in result.output
