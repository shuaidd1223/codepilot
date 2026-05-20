from __future__ import annotations

from codepilot.core import service_launcher


def test_service_launcher_uses_direct_detached_spawn_when_parent_is_frozen(tmp_path, monkeypatch):
    captured = {}

    class _Proc:
        pid = 2468

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(service_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(service_launcher.subprocess, "Popen", fake_popen)

    pid = service_launcher.spawn_detached_command_via_launcher(
        [r"C:\Tools\CodePilot\codepilot.exe", "feishu", "run"],
        log_file=tmp_path / "svc.log",
        cwd=tmp_path,
        env={"CODEPILOT_TEST": "1"},
    )

    assert pid == 2468
    assert captured["cmd"] == [r"C:\Tools\CodePilot\codepilot.exe", "feishu", "run"]
    assert captured["kwargs"]["env"]["CODEPILOT_TEST"] == "1"
    assert captured["kwargs"]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    if service_launcher.os.name == "nt":
        assert captured["kwargs"]["creationflags"] & service_launcher.DETACHED_PROCESS
        assert captured["kwargs"]["creationflags"] & service_launcher.CREATE_NEW_PROCESS_GROUP
        assert captured["kwargs"]["creationflags"] & service_launcher.CREATE_NO_WINDOW
        assert captured["kwargs"]["startupinfo"].wShowWindow == 0
    else:
        assert captured["kwargs"]["start_new_session"] is True


def test_service_launcher_keeps_python_launcher_for_source_windows_parent(tmp_path, monkeypatch):
    if service_launcher.os.name != "nt":
        return

    captured = {}

    class _Result:
        returncode = 0
        stdout = b"1234\n"
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.delattr(service_launcher.sys, "frozen", raising=False)
    monkeypatch.setattr(service_launcher.subprocess, "run", fake_run)

    pid = service_launcher.spawn_detached_command_via_launcher(
        ["python", "-m", "codepilot", "inspect", "--project", "demo"],
        log_file=tmp_path / "inspect.log",
    )

    assert pid == 1234
    assert captured["cmd"][1] == "-c"
    assert captured["kwargs"]["creationflags"] == service_launcher.CREATE_NO_WINDOW
