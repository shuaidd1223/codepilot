from __future__ import annotations

from codepilot.commands import inspect_lifecycle


def _lifecycle_options(**overrides):
    data = {
        "project": "demo",
        "max_new": None,
        "dry_run": False,
        "agent": "codex",
        "planner": None,
        "interval": None,
        "once": True,
        "foreground": False,
        "json_mode": False,
        "show_status": False,
        "stop_service": False,
    }
    data.update(overrides)
    return inspect_lifecycle.InspectServiceLifecycleOptions(**data)


def test_handle_service_lifecycle_routes_status_terminal_output():
    rendered: list[str] = []
    handled, exit_code = inspect_lifecycle.handle_service_lifecycle(
        _lifecycle_options(show_status=True),
        inspect_service_status_fn=lambda _: {
            "running": True,
            "pid": 8765,
            "project": "demo",
            "log": "D:/tmp/inspect.log",
        },
        start_inspect_service_fn=lambda *_args, **_kwargs: {},
        stop_inspect_service_fn=lambda _project: {},
        emit_json_payload_fn=lambda *_args, **_kwargs: None,
        echo_fn=rendered.append,
    )

    assert handled is True
    assert exit_code is None
    assert rendered[0] == "[green]巡检运行中[/green]  PID=8765  项目=demo"
    assert rendered[1] == "[dim]日志: D:/tmp/inspect.log[/dim]"


def test_handle_service_lifecycle_emits_json_error_and_exit_code_on_stop_failure():
    payload: dict[str, object] = {}

    def _emit_json(command, **kwargs):
        payload["command"] = command
        payload.update(kwargs)

    handled, exit_code = inspect_lifecycle.handle_service_lifecycle(
        _lifecycle_options(stop_service=True, json_mode=True),
        inspect_service_status_fn=lambda _: {"running": True, "pid": 8765, "project": "demo", "log": "x.log"},
        start_inspect_service_fn=lambda *_args, **_kwargs: {},
        stop_inspect_service_fn=lambda _project: (_ for _ in ()).throw(RuntimeError("stop failed")),
        emit_json_payload_fn=_emit_json,
        echo_fn=lambda *_args, **_kwargs: None,
    )

    assert handled is True
    assert exit_code == 1
    assert payload["command"] == "inspect"
    assert payload["ok"] is False
    assert payload["error_code"] == "stop_failed"
    assert payload["error"] == "stop failed"


def test_handle_service_lifecycle_background_start_calls_start_service():
    calls: list[dict] = []
    rendered: list[str] = []

    def _start(project, **kwargs):
        calls.append({"project": project, **kwargs})
        return {"started": True, "pid": 3456, "log": "D:/tmp/inspect.log"}

    handled, exit_code = inspect_lifecycle.handle_service_lifecycle(
        _lifecycle_options(once=False, foreground=False, json_mode=False, max_new=4, dry_run=True, planner="claude", interval=1800),
        inspect_service_status_fn=lambda _: {"running": False},
        start_inspect_service_fn=_start,
        stop_inspect_service_fn=lambda _project: {},
        emit_json_payload_fn=lambda *_args, **_kwargs: None,
        echo_fn=rendered.append,
    )

    assert handled is True
    assert exit_code is None
    assert calls == [
        {
            "project": "demo",
            "max_new": 4,
            "dry_run": True,
            "agent": "codex",
            "planner": "claude",
            "interval": 1800,
        }
    ]
    assert "后台启动" in rendered[0]


def test_run_foreground_inspection_loop_runs_once_and_clears_state():
    touched: list[dict] = []
    cleared: list[tuple[str, str]] = []
    headers: list[dict] = []
    emitted: list[dict] = []
    run_calls: list[dict] = []

    options = inspect_lifecycle.ForegroundInspectLoopOptions(
        project="demo",
        project_info={"name": "demo", "path": "D:/repo/demo"},
        signals=("git_log", "todos"),
        max_new_tasks=2,
        interval_seconds=600,
        planner="codex",
        priority="P3",
        auto_execute=False,
        agent="codex",
        dry_run=True,
        once=True,
        json_mode=False,
    )

    inspect_lifecycle.run_foreground_inspection_loop(
        options,
        touch_service_state_fn=lambda *args, **kwargs: touched.append({"args": args, "kwargs": kwargs}),
        clear_service_state_fn=lambda service, scope: cleared.append((service, scope)),
        service_log_path_fn=lambda _name: "D:/tmp/inspect.log",
        print_round_header_fn=lambda **kwargs: headers.append(kwargs),
        run_inspection_fn=lambda project_info, **kwargs: run_calls.append({"project_info": project_info, **kwargs}) or {"project": "demo", "created": [], "skipped": [], "candidates_total": 0},
        emit_inspection_result_fn=lambda result, **kwargs: emitted.append({"result": result, **kwargs}),
        echo_fn=lambda *_args, **_kwargs: None,
        get_pid_fn=lambda: 2222,
        sleep_fn=lambda _seconds: (_ for _ in ()).throw(AssertionError("once mode should not sleep")),
    )

    assert len(touched) == 1
    assert touched[0]["args"] == ("inspect", "demo")
    assert touched[0]["kwargs"]["pid"] == 2222
    assert touched[0]["kwargs"]["status"] == "running"
    assert headers[0]["project_name"] == "demo"
    assert headers[0]["max_new_tasks"] == 2
    assert run_calls[0]["project_info"]["name"] == "demo"
    assert run_calls[0]["signals"] == ("git_log", "todos")
    assert emitted[0]["dry_run"] is True
    assert emitted[0]["json_mode"] is False
    assert cleared == [("inspect", "demo")]
