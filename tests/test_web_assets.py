from __future__ import annotations

import base64
import json
import shutil
import subprocess
import textwrap
from http import HTTPStatus
from pathlib import Path

import pytest

from codepilot.webapp import server as webui_mod


def test_web_ui_loads_index_from_package_resources_when_web_dir_path_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(webui_mod, "_WEB_DIR", tmp_path / "missing-web-dir")

    body = webui_mod._load_web_file("index.html")

    assert b"/static/app.js" in body


def test_web_ui_loads_index_from_pkgutil_when_traversable_resources_unavailable(tmp_path, monkeypatch):
    def _missing_resources(_package: str):
        raise FileNotFoundError("resources unavailable")

    monkeypatch.setattr(webui_mod, "_WEB_DIR", tmp_path / "missing-web-dir")
    monkeypatch.setattr(webui_mod.resources, "files", _missing_resources)

    body = webui_mod._load_web_file("index.html")

    assert b"/static/app.js" in body


def test_web_ui_loads_index_from_installed_binary_sidecar_web_dir(tmp_path, monkeypatch):
    def _missing_resources(_package: str):
        raise FileNotFoundError("resources unavailable")

    install_dir = tmp_path / "install-bin"
    sidecar = install_dir / "web"
    sidecar.mkdir(parents=True)
    (sidecar / "index.html").write_bytes(b"<script src=\"/static/app.js\"></script>")

    monkeypatch.setattr(webui_mod, "_WEB_DIR", tmp_path / "missing-web-dir")
    monkeypatch.setattr(webui_mod.pkgutil, "get_data", lambda _package, _resource: None)
    monkeypatch.setattr(webui_mod.resources, "files", _missing_resources)
    monkeypatch.setattr(webui_mod.sys, "executable", str(install_dir / "codepilot.exe"))

    body = webui_mod._load_web_file("index.html")

    assert b"/static/app.js" in body


def test_web_ui_uses_codepilot_logo_assets():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    readme_en = Path("README.en-US.md").read_text(encoding="utf-8")
    overview_zh = Path("docs/说明文档.zh-CN.md").read_text(encoding="utf-8")
    overview_en = Path("docs/说明文档.en-US.md").read_text(encoding="utf-8")

    assert Path("codepilot/web/codepilot-logo.png").is_file()
    assert Path("codepilot/web/favicon.ico").is_file()
    assert Path("docs/codepilot-logo.png").is_file()
    assert '<link rel="icon" href="/favicon.ico" sizes="any">' in index_html
    assert '<link rel="apple-touch-icon" href="/static/codepilot-logo.png">' in index_html
    assert '<img class="brand-logo-img" src="/static/codepilot-logo.png" alt="">' in sidebar
    assert ".brand-logo-img" in styles
    assert "web/*.png" in pyproject
    assert "web/*.ico" in pyproject
    assert "docs/codepilot-logo.png" in readme
    assert "docs/codepilot-logo.png" in readme_en
    assert "codepilot-logo.png" in overview_zh
    assert "codepilot-logo.png" in overview_en


def test_web_ui_favicon_route_serves_packaged_icon():
    class _FaviconProbe:
        sent: tuple[bytes, str, int] | None = None

        def _send_bytes(self, body: bytes, content_type: str, status=HTTPStatus.OK) -> None:
            self.sent = (body, content_type, status)

    probe = _FaviconProbe()

    assert webui_mod.DashboardHandler._dispatch_get_asset(probe, "/favicon.ico") is True
    assert probe.sent is not None
    assert probe.sent[0] == webui_mod._load_web_file("favicon.ico")
    assert probe.sent[1] == "image/x-icon"
    assert probe.sent[2] == HTTPStatus.OK


def test_web_ui_resource_loader_rejects_static_path_traversal():
    with pytest.raises(FileNotFoundError):
        webui_mod._load_web_file("../pyproject.toml")


def test_web_ui_serves_nested_static_package_resources_when_web_dir_path_missing(tmp_path, monkeypatch):
    class _StaticProbe:
        sent: tuple[bytes, str] | None = None

        def _send_bytes(self, body: bytes, content_type: str, status=200) -> None:
            self.sent = (body, content_type)

    monkeypatch.setattr(webui_mod, "_WEB_DIR", tmp_path / "missing-web-dir")
    probe = _StaticProbe()

    assert webui_mod.DashboardHandler._serve_static(probe, "boundaries/AppStateBoundary.js") is True
    assert probe.sent is not None
    assert b"CP.AppStateBoundary" in probe.sent[0]
    assert probe.sent[1] == "application/javascript; charset=utf-8"


def test_task_detail_component_keeps_single_computed_block():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert source.count("computed:") == 1
    assert "task() { return this.s.taskDetail; }" in source
    assert "canSplit()" in source


def test_task_detail_keeps_single_live_log_panel():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert "执行过程" in source
    assert "task-phase-log-list" in source
    assert "phase_logs" in source
    assert "loadPhaseLog(phase)" in source
    assert "/phase-logs/${encodeURIComponent(phase.key)}" in source
    assert "加载原始日志" in source
    assert "showFullTaskContent" in source
    assert "task-content-block" in source
    assert "实时进度" not in source
    assert "<cp-live-log" not in source


def test_task_detail_distinguishes_loading_and_empty_state():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert 'v-if="s.taskDetailLoading && !task"' in source
    assert "暂无任务详情" in source
    assert "taskContentText()" in source
    assert "任务正文为空" in source


def test_task_detail_renders_structured_reviewer_verdict():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    # Computed helpers that hydrate the verdict panel from either the
    # top-level latest_review payload or the per-log review blocks.
    assert "latestReview()" in source
    assert "verdictToneClass()" in source
    assert "t.latest_review" in source

    # Render blocks: badge, AC table, blockers, advisory.
    assert '"block reviewer-verdict"' in source
    assert "最新审查结论" in source
    assert "reviewer-verdict-badge" in source
    assert "ac-checks-table" in source
    assert "阻塞点" in source
    assert "非阻塞观察" in source

    # CSS must carry the tone classes the template applies.
    assert ".reviewer-verdict-badge.verdict-pass" in styles
    assert ".reviewer-verdict-badge.verdict-fail" in styles
    assert ".ac-status-chip.ac-status-pass" in styles
    assert ".ac-status-chip.ac-status-fail" in styles


def test_task_detail_renders_task_timeline_summary():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "timelineEvents()" in source
    assert "任务时间线" in source
    assert "timeline-event-row" in source
    assert "formatTimelineEvent" in source
    assert ".timeline-event-row" in styles
    assert ".timeline-event-dot" in styles


def test_task_detail_phase_log_panel_has_collapsible_styles():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "isPhaseExpanded(phase)" in source
    assert "togglePhase(phase)" in source
    assert "phase.default_expanded" in source
    assert "phase.active" in source
    assert 'class="task-phase-run-indicator"' in source
    assert ".task-process-block" in styles
    assert ".task-phase-run-indicator" in styles
    assert ".task-phase-log.status-failed" in styles
    assert ".task-phase-log.status-running" in styles
    assert ".phase-active-spinner" in styles
    assert ".task-phase-log-head .task-phase-log-meta { display: none; }" in styles
    assert ".task-content-block.is-collapsed" in styles


def test_task_detail_action_warns_when_api_reports_service_error():
    source = Path("codepilot/web/boundaries/TaskDetailBoundary.js").read_text(encoding="utf-8")

    assert "out.service_error" in source
    assert "out.ok === false" in source
    assert "pushToast(out.message || '操作完成', toastType)" in source


def test_task_detail_template_avoids_nested_backticks_in_vue_bindings():
    source = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")

    assert ':class="`' not in source


def test_agent_log_template_avoids_nested_backticks_in_vue_bindings():
    source = Path("codepilot/web/components/AgentLog.js").read_text(encoding="utf-8")

    assert ':class="[`' not in source
    assert 'blockClass(b, idx)' in source
    assert "if (this.followEnabled) this._scrollToBottom();" in source


def _parse_agent_log_blocks(text: str, context: dict | None = None) -> list[dict]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for AgentLogRenderBoundary parser checks")
    script = textwrap.dedent(
        """
        const fs = require('fs');
        const vm = require('vm');
        const sourcePath = process.argv[1];
        const raw = Buffer.from(process.argv[2], 'base64').toString('utf8');
        const context = JSON.parse(Buffer.from(process.argv[3], 'base64').toString('utf8'));
        global.window = global;
        global.CP = {
          escapeHtml(text) {
            return String(text || '')
              .replace(/&/g, '&amp;')
              .replace(/</g, '&lt;')
              .replace(/>/g, '&gt;');
          },
        };
        vm.runInThisContext(fs.readFileSync(sourcePath, 'utf8'), { filename: sourcePath });
        const blocks = CP.AgentLogRenderBoundary.parseMarkdownBlocks(raw, context);
        process.stdout.write(JSON.stringify(blocks));
        """
    )
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(Path("codepilot/web/boundaries/AgentLogRenderBoundary.js")),
            base64.b64encode(text.encode("utf-8")).decode("ascii"),
            base64.b64encode(json.dumps(context or {}).encode("utf-8")).decode("ascii"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_agent_log_parser_surfaces_reviewer_verdict_as_structured_block():
    blocks = _parse_agent_log_blocks(
        """
        ## Live Output

        AC #1: PASS — storage migration is covered.

        ## 需要修复的点

        - 回滚 AgentLog 渲染重构相关改动

        VERDICT: FAIL

        ```json
        {
          "verdict": "fail",
          "ac_checks": [
            {"id": "AC-1", "status": "PASS", "reason": "storage migration is covered"}
          ],
          "blockers": ["回滚 AgentLog 渲染重构相关改动"],
          "advisory": []
        }
        ```
        """,
        {
            "phase": "reviewer",
            "agent": "claude-review",
        },
    )

    review = blocks[0]
    assert review["type"] == "review-verdict"
    assert review["verdict"] == "fail"
    assert review["acChecks"][0]["id"] == "AC-1"
    assert review["blockers"] == ["回滚 AgentLog 渲染重构相关改动"]
    assert any(block["type"] == "review-raw" and block["collapsed"] is True for block in blocks)


def test_agent_log_parser_keeps_terminal_summaries_visible_while_folding_noise():
    summary_lines = "\n".join(f"- 验证结果 {idx}" for idx in range(70))
    blocks = _parse_agent_log_blocks(
        f"""
        exec
        pytest -m "not slow" tests/test_web_assets.py -q
         succeeded in 1200ms:
        51 passed

        已完成 #429。

        验证结果：
        {summary_lines}

        VERDICT: PASS
        """,
        {
            "phase": "builder",
            "agent": "opencode",
        },
    )

    command_group = next(block for block in blocks if block["type"] == "command-group")
    visible_summary = next(block for block in blocks if "已完成 #429" in block.get("raw", ""))
    assert command_group["collapsed"] is True
    assert visible_summary["collapsed"] is False
    assert visible_summary["importance"] == "critical"


def test_agent_log_parser_is_executor_neutral_and_surfaces_fallback_telemetry():
    blocks = _parse_agent_log_blocks(
        """
        exec
        python -m pytest tests/test_project_rename.py -q
         succeeded in 800ms:
        12 passed
        claude-review
        CODEPILOT_EXECUTOR_TELEMETRY: {"kind":"executor_fallback","phase":"reviewer","fallback_reason":"timeout","failed_executor":{"family":"claude","label":"claude-review"},"fallback_executor":{"family":"opencode","label":"opencode-review"},"fallback_path":["claude","opencode"]}
        """,
        {
            "phase": "reviewer",
            "agent": "claude-review",
        },
    )

    command_group = next(block for block in blocks if block["type"] == "command-group")
    telemetry = next(block for block in blocks if block["type"] == "telemetry")
    assert command_group["runs"][0]["command"] == "python -m pytest tests/test_project_rename.py -q"
    assert "claude-review" not in command_group["runs"][0]["raw"]
    assert telemetry["title"] == "执行器回退"
    assert telemetry["summary"] == "claude-review → opencode-review · timeout"


def test_agent_log_parser_expands_failed_command_group_but_folds_long_stdout():
    lines = "\n".join(f"diff line {idx}" for idx in range(120))
    blocks = _parse_agent_log_blocks(
        f"""
        exec
        git diff --check
         failed in 900ms:
        {lines}
        """,
        {
            "phase": "builder",
            "agent": "codex",
        },
    )

    command_group = next(block for block in blocks if block["type"] == "command-group")
    failed_run = command_group["runs"][0]
    assert command_group["failedCount"] == 1
    assert command_group["collapsed"] is False
    assert failed_run["tone"] == "fail"
    assert failed_run["collapsed"] is True


def test_agent_log_parser_extracts_unified_diff_blocks():
    blocks = _parse_agent_log_blocks(
        """
        codex
        我会先补回归测试。

        diff --git a/tests/test_shutdown_command.py b/tests/test_shutdown_command.py
        new file mode 100644
        index 0000000..1111111
        --- /dev/null
        +++ b/tests/test_shutdown_command.py
        @@ -0,0 +1,5 @@
        +from click.testing import CliRunner
        +
        +def test_shutdown_stops_services():
        +    assert True
        -legacy placeholder

        接下来运行 pytest。
        """,
        {
            "phase": "builder",
            "agent": "codex",
        },
    )

    diff = next(block for block in blocks if block["type"] == "diff")
    generic = "\n".join(block.get("raw", "") for block in blocks if block["type"] == "log")
    assert diff["file"] == "tests/test_shutdown_command.py"
    assert diff["addedCount"] == 4
    assert diff["deletedCount"] == 1
    assert diff["collapsed"] is False
    assert any(line["kind"] == "add" for line in diff["lines"])
    assert any(line["kind"] == "del" for line in diff["lines"])
    assert "diff --git" not in generic


def test_agent_log_parser_marks_short_successful_commands_inline_only():
    blocks = _parse_agent_log_blocks(
        """
        exec
        git status --short
         succeeded in 90ms:

        exec
        rg --files

        codex
        工作区干净。
        """,
        {
            "phase": "builder",
            "agent": "codex",
        },
    )

    command_group = next(block for block in blocks if block["type"] == "command-group")
    first, second = command_group["runs"]
    assert first["inlineOnly"] is True
    assert first["collapsed"] is False
    assert first["lineCount"] == 3
    assert second["inlineOnly"] is True
    assert second["collapsed"] is False
    assert second["lineCount"] == 2


def test_agent_log_parser_folds_leading_task_context_before_commands():
    context_lines = "\n".join(f"- AGENTS rule {idx}" for idx in range(80))
    blocks = _parse_agent_log_blocks(
        f"""
        # Task #429 · builder

        ## TDD Mode

        {context_lines}

        [Requirements]
        1. Read the task file.

        exec
        pytest -q tests/test_web_assets.py
         succeeded in 1200ms:
        55 passed
        """,
        {
            "phase": "builder",
            "agent": "codex",
        },
    )

    assert blocks[0]["type"] == "log"
    assert blocks[0]["collapsed"] is True
    assert blocks[0]["title"].startswith("任务输入上下文")
    assert "AGENTS rule 0" in blocks[0]["raw"]
    assert blocks[1]["type"] == "command-group"


def test_agent_log_parser_uses_codepilot_log_meta_and_folds_runner_preamble():
    raw = """
    # Task #429 · builder

    CODEPILOT_LOG_META: {"kind":"live_command","version":1,"task_id":429,"phase":"builder","cwd":"D:\\\\myCode\\\\workflow","timeout_seconds":3600,"stdin_chars":0,"command_redactions":[{"index":6,"chars":12000,"reason":"prompt"}]}

    - started_at: `2026-05-26T11:16:40`
    - phase: `builder`
    - cwd: `D:\\myCode\\workflow`
    - timeout_seconds: `3600`

    ## Command

    ```shell
    codex -C D:\\myCode\\workflow exec -o out.md "[omitted prompt argument: 12000 chars]"
    ```

    ## Input

    - command_args: `1 omitted`

    ## Live Output

    OpenAI Codex v0.130.0
    workdir: D:\\myCode\\workflow

    exec
    pytest -q tests/test_web_assets.py
     succeeded in 1200ms:
    57 passed
    """

    blocks = _parse_agent_log_blocks(raw, {"phase": "builder", "agent": "codex"})

    meta = blocks[0]
    preamble = blocks[1]
    command_group = next(block for block in blocks if block["type"] == "command-group")
    assert meta["type"] == "run-meta"
    assert "builder" in meta["summary"]
    assert "命令参数已省略 1 项" in meta["summary"]
    assert preamble["collapsed"] is True
    assert preamble["title"].startswith("运行器上下文")
    assert "CODEPILOT_LOG_META" not in preamble["raw"]
    assert command_group["runs"][0]["command"] == "pytest -q tests/test_web_assets.py"


def test_agent_log_parser_folds_supporting_prompt_docs_between_commands():
    support_lines = "\n".join([
        "## Standard Flow",
        "",
        "Treat status, totals, and service health as questions.",
        "",
        "```bash",
        "codepilot status -p workflow --json",
        "codepilot hud -p workflow --preset full --json",
        "```",
        "",
        "Do not mutate Git state from exploration.",
    ])
    blocks = _parse_agent_log_blocks(
        f"""
        exec
        Get-Content C:\\Users\\Administrator\\.codex\\skills\\codepilot-workflow\\SKILL.md
         succeeded in 100ms:
        {support_lines}

        exec
        pytest -q tests/test_web_assets.py
         succeeded in 1000ms:
        56 passed
        """,
        {
            "phase": "builder",
            "agent": "codex",
        },
    )

    support = next(block for block in blocks if support_lines in block.get("raw", ""))
    command_groups = [block for block in blocks if block["type"] == "command-group"]
    assert support["collapsed"] is True
    assert all(group["lineCount"] > 0 for group in command_groups)


def test_sidebar_category_toggle_uses_project_scoped_accordion():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "this.cp.toggleCategory(project, cat);" in sidebar
    assert "const CATEGORY_VIEWS = ['sessions', 'tasks', 'jobs'];" in app_state
    assert "function openProjectCategory(project, view)" in app_state


def test_sidebar_task_leaf_includes_quick_actions():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")

    assert "requestTaskQuickAction(task, act, ev)" in sidebar
    assert "confirmTaskQuickAction(task, ev)" in sidebar
    assert "taskQuickConfirmMessage(task)" in sidebar
    assert "t.actions.cancel" in sidebar
    assert "t.actions.archive" in sidebar
    assert "t.actions.delete" in sidebar
    assert "requestTaskQuickAction(t, 'cancel', $event)" in sidebar
    assert "requestTaskQuickAction(t, 'archive', $event)" in sidebar
    assert "requestTaskQuickAction(t, 'delete', $event)" in sidebar
    assert "class=\"tree-inline-confirm\"" in sidebar


def test_sidebar_projects_support_drag_sort_and_whole_row_toggle():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "PROJECT_ORDER_STORAGE_KEY = 'cp-sidebar-project-order-v1'" in sidebar
    assert "orderedProjects()" in sidebar
    assert "v-for=\"p in orderedProjects\"" in sidebar
    assert "draggable=\"true\"" in sidebar
    assert "onProjectDragStart(project, ev)" in sidebar
    assert "onProjectDragOver(project, ev)" in sidebar
    assert "onProjectDrop(project, ev)" in sidebar
    assert "persistProjectOrder()" in sidebar
    assert "@click=\"toggleProjectRow(p.name, $event)\"" in sidebar
    assert "@click=\"toggleCategoryRow(p.name, 'tasks', $event)\"" in sidebar
    assert "@click=\"toggleCategoryRow(p.name, 'jobs', $event)\"" in sidebar
    assert ".tree-project.drag-over" in styles
    assert ".tree-project-row[draggable=\"true\"]" in styles


def test_sidebar_project_row_click_selects_before_toggling_expand_state():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")

    assert "isProjectOverviewActive(name)" in sidebar
    assert """if (!this.isProjectOverviewActive(name)) {
        this.cp.selectProject(name);
        return;
      }
      this.cp.toggleProject(name);""" in sidebar


def test_sidebar_wires_project_rename_action_and_migration_toast():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    submission = Path("codepilot/web/boundaries/AppSubmissionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "renameProject(project, ev)" in sidebar
    assert "this.cp.renameProject(project.name, this.renameDraft)" in sidebar
    assert "title=\"重命名项目\"" in sidebar
    assert "project-rename-inline" in sidebar
    assert "async function renameProject(name, newName)" in submission
    assert "/rename`" in submission
    assert "formatProjectRenameMessage(out)" in submission
    assert "renameProject," in app_state


def test_task_section_includes_batch_quick_actions():
    task_section = Path("codepilot/web/components/TaskSection.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "selectedTaskIds" in task_section
    assert "requestBatchAction(act, ev)" in task_section
    assert "confirmBatchAction(ev)" in task_section
    assert "this.cp.taskBatchAction(ids, act);" in task_section
    assert "批量取消" in task_section
    assert "批量归档" in task_section
    assert "批量删除" in task_section
    assert "taskBatchAction," in app_state


def test_web_ui_task_cards_render_phase_progress():
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    task_section = Path("codepilot/web/components/TaskSection.js").read_text(encoding="utf-8")
    task_detail = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "CP.TASK_PHASE_STEPS" in utils
    assert "CP.taskPhaseProgress" in utils
    assert "下一步：Review 验收" in utils
    assert "taskPhaseProgress: CP.taskPhaseProgress" in utils
    assert "task-phase-progress" in task_section
    assert "task-phase-progress-detail" in task_detail
    assert "task-phase-progress" in project_view
    assert ".task-phase-track" in styles
    assert ".task-phase-step.state-current" in styles


def test_task_list_uses_detail_phase_progress_style():
    task_section = Path("codepilot/web/components/TaskSection.js").read_text(encoding="utf-8")

    assert 'class="task-phase-progress task-phase-progress-detail"' in task_section
    assert "task-phase-track" not in task_section
    assert "task-phase-fill" not in task_section


def test_web_ui_bootstrap_wires_app_state_boundary_before_mount():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/boundaries/AppFeedbackBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppSessionBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppSubmissionBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AppStateBoundary.js\"></script>" in index_html
    assert "AppClarifyBoundary.js" not in index_html
    assert "setup: CP.AppStateBoundary.setup," in app


def test_web_ui_daemon_banner_points_to_ui_start_command():
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")

    assert "codepilot ui start" in app
    assert "codepilot webui start" not in app


def test_metrics_panel_renders_deepseek_balance_and_token_usage():
    metrics_panel = Path("codepilot/web/components/MetricsPanel.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "aiStatus" in app_state
    assert "loadAIStatus" in app_state
    assert "/api/ai/status" in app_state
    assert "project-status-card" in metrics_panel
    assert "project-status-grid" in metrics_panel
    assert "project-status-summary" in metrics_panel
    assert "project-usage-panel" in metrics_panel
    assert "project-usage-grid" in metrics_panel
    assert "DeepSeek" in metrics_panel
    assert "总 tokens" in metrics_panel
    assert "思考" in metrics_panel
    assert ".project-status-grid" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert ".project-usage-panel" in styles
    assert ".project-usage-grid" in styles


def test_app_state_rebinds_daemon_health_when_project_changes():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "watch(() => state.nav.project" in app_state
    assert "loadDaemonHealth();" in app_state
    assert "closeEventStream();" in app_state
    assert "openEventStream();" in app_state


def test_app_state_filters_task_state_notifications_to_current_project():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    task_state_branch = app_state.split("if (event && event.stage === 'task-state') {", 1)[1].split(
        "state.liveEvents.push(event);",
        1,
    )[0]
    assert "const extra = event.extra || {};" in task_state_branch
    assert "if (!projectMatchesCurrent(extra.project)) return;" in task_state_branch


def test_sse_remembers_last_event_id_across_project_stream_reopens():
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "CP.sseLastEventIds = CP.sseLastEventIds || {};" in utils
    assert "const streamKey = url.split('?')[0];" in utils
    assert "let lastEventId = CP.sseLastEventIds[streamKey] || '';" in utils
    assert "CP.sseLastEventIds[streamKey] = lastEventId;" in utils


def test_app_state_keeps_project_form_drafts_scoped_by_project():
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "const PROJECT_DRAFTS_STORAGE_KEY = 'cp-project-drafts-v1';" in app_state
    assert "function saveProjectDraft(project" in app_state
    assert "function loadProjectDraft(project" in app_state
    assert "saveProjectDraft(prevProject);" in app_state
    assert "loadProjectDraft(partial.project);" in app_state
    for marker in (
        "activeProjectSessionId: state.activeProjectSessionId",
        "opencodeRuntime: cloneProjectDraftValue(state.opencodeRuntime)",
        "chatText: state.chatText",
    ):
        assert marker in app_state
    for removed in (
        "goalText",
        "goalCategory",
        "composerMode",
        "batchComposer",
        "goalClarify",
        "composerClarify",
    ):
        assert removed not in app_state


def test_web_ui_uses_in_app_confirm_dialog_instead_of_browser_dialogs():
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")
    task_detail = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")

    assert "<script src=\"/static/components/ConfirmDialog.js\"></script>" in index_html
    assert "<cp-confirm-dialog></cp-confirm-dialog>" in app
    assert "if (!confirm(" not in app
    assert "if (!confirm(" not in task_detail
    assert "window.confirm(" not in app
    assert "window.confirm(" not in task_detail
    assert "window.alert(" not in app
    assert "window.prompt(" not in app


def test_web_ui_wires_plugin_diff_assets():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    diff_viewer = Path("codepilot/web/components/DiffViewer.js").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "diff2html/bundles/css/diff2html.min.css" in index_html
    assert "diff2html/bundles/js/diff2html.min.js" in index_html
    assert "<script src=\"/static/components/DiffViewer.js\"></script>" in index_html
    assert "CP.Components.DiffViewer" in diff_viewer
    assert "window.Diff2Html.html" in diff_viewer
    assert "CP.ensureMonaco" not in utils


def test_render_output_keeps_diff_and_ansi_logs_in_code_blocks():
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    render_output_body = utils.split("CP.renderOutput = (text) => {", 1)[1].split("CP.fmtTime", 1)[0]
    assert "if (looksDiff || hasAnsi) {" in render_output_body
    assert "&& !hasMd" not in render_output_body
    assert "CP._MD_SIGNAL_RE" not in utils


def test_web_ui_action_protocol_exposes_scoped_pending_keys():
    feedback_boundary = Path("codepilot/web/boundaries/AppFeedbackBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "const ACTION_KEYS = Object.freeze({" in feedback_boundary
    assert "SESSION_SEND: 'session.send'" in feedback_boundary
    assert "SESSION_STOP: 'session.stop'" in feedback_boundary
    assert "SESSION_DELETE: 'session.delete'" in feedback_boundary
    assert "JOB_ACTION: 'job.action'" in feedback_boundary
    assert "GOAL_SUBMIT" not in feedback_boundary
    assert "COMPOSER_SUBMIT" not in feedback_boundary
    assert "TASK_BATCH_IMPORT" not in feedback_boundary
    assert "CLARIFY" not in feedback_boundary
    assert "const feedbackBoundary = CP.createAppFeedbackBoundary({ state });" in app_state
    assert "isActionPending: (actionKey) => isActionPending(actionKey)," in app_state
    assert "ACTION_KEYS," in app_state


def test_web_ui_wires_requirement_job_actions_and_filtered_events():
    feedback_boundary = Path("codepilot/web/boundaries/AppFeedbackBoundary.js").read_text(encoding="utf-8")
    submission_boundary = Path("codepilot/web/boundaries/AppSubmissionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    job_detail = Path("codepilot/web/components/JobDetail.js").read_text(encoding="utf-8")
    jobs_view = Path("codepilot/web/components/JobsView.js").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "JOB_ACTION: 'job.action'" in feedback_boundary
    assert "async function jobAction(job, action)" in submission_boundary
    assert "/api/jobs/${job.id}/${action}" in submission_boundary
    assert "jobAction," in app_state
    assert "const liveEventsForJob = (jobId) => {" in app_state
    assert "kind === 'job_log'" in app_state
    assert "scheduleRefresh();" not in app_state.split("state.liveEvents.push(event);", 2)[-1].split("const taskId", 1)[0]
    assert "canCancel()" in job_detail
    assert "@click=\"cancelJob\"" in job_detail
    assert "@click.stop=\"jobAction(j, 'retry')\"" in jobs_view
    assert "cancelling: '停止中'" in utils


def test_web_ui_removes_legacy_intake_components_and_goal_route():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    submission_boundary = Path("codepilot/web/boundaries/AppSubmissionBoundary.js").read_text(encoding="utf-8")
    server = Path("codepilot/webapp/server.py").read_text(encoding="utf-8")
    requirements = Path("codepilot/webapp/action_requirements.py").read_text(encoding="utf-8")
    sessions = Path("codepilot/webapp/action_sessions.py").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")

    assert not Path("codepilot/web/components/GoalInput.js").exists()
    assert not Path("codepilot/web/components/Composer.js").exists()
    assert not Path("codepilot/web/components/TaskBatchImport.js").exists()
    assert "GoalInput.js" not in index_html
    assert "Composer.js" not in index_html
    assert "TaskBatchImport.js" not in index_html
    assert "submitGoal" not in app_state
    assert "submitComposer" not in app_state
    assert "submitTaskBatch" not in app_state
    assert "submitGoal" not in submission_boundary
    assert "/api/goal" not in server
    assert "submit_goal_action" not in requirements
    assert "_dispatch_goal" not in requirements
    assert "_dispatch_session_message" not in sessions
    assert "_SessionDispatchContext" not in sessions
    assert "chatPending()" in chat
    assert "deletePending()" in chat
    assert "s.sending" not in chat


def test_chat_view_wires_streaming_session_runs_and_stop_action():
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")
    session_boundary = Path("codepilot/web/boundaries/AppSessionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "sessionRunForMessage(m)" in chat
    assert "session-process-panel" in chat
    assert "<cp-agent-log" in chat
    assert "stopSessionRun()" in chat
    assert "runtime-control-bar" in chat
    assert "s.opencodeRuntime" in chat
    assert "终止会话" in chat
    assert "Agent" in chat
    assert "agentModeOptions" in chat
    assert "agentMode" in chat
    assert "setAgentMode" in chat
    assert "CP.AGENT_MODE_OPTIONS" in Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    assert "运行" not in chat
    assert "分支" not in chat
    assert "模型" not in chat
    assert "上下文" not in chat
    assert "工具权限" not in chat
    assert "run_async: true" in session_boundary
    assert "async function stopSessionRun" in session_boundary
    assert "handleSessionRunEvent(event)" in app_state
    assert "event.stage === 'session-run'" in app_state
    assert "type === 'error' ? (error || delta || msg)" in app_state
    assert "type === 'error' ? (String(extra.error || '') || extra.content_delta || item.message)" in chat
    assert "sessionRuns: {}" in app_state
    assert "opencodeRuntime" in app_state
    assert ".session-process-panel" in styles
    assert ".chat-input-area.is-streaming" in styles
    assert ".runtime-control-bar" in styles
    assert ".embedded-messages" in styles
    assert "overflow-y: auto" in styles


def test_project_view_uses_unified_workbench_layout():
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "project-workbench-grid" in project_view
    assert "project-session-list" in project_view
    assert "cp.selectEmbeddedSession" in project_view
    assert "project-service-panel" in project_view
    assert "project-context-rail" in project_view
    assert "project-metrics-panel" in project_view
    assert "会话就是和 OpenCode 的交互" in project_view
    assert "intake-segmented" not in project_view
    assert "cp-composer" not in project_view
    assert "cp-task-batch-import" not in project_view
    assert ".project-workbench-grid" in styles
    assert ".project-service-panel" in styles
    assert ".opencode-session-card" in styles
    assert "@media (max-width: 1280px)" in styles
    assert "grid-template-columns: 220px minmax(420px, 1fr);" in styles
    assert "grid-column: 1 / -1;" in styles
    assert ".project-metrics-panel" in styles
    assert "grid-column: span 2;" in styles
    assert "max-height: 280px;\n    overflow-y: auto;" in styles


def test_project_workbench_embeds_streaming_session_chat_instead_of_goal_form():
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")
    session_boundary = Path("codepilot/web/boundaries/AppSessionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "cp-goal-input" not in project_view
    assert "<cp-session-chat-panel" in project_view
    assert "project-session-workbench" in project_view
    assert "intake-segmented" not in project_view
    assert "CP.Components.SessionChatPanel" in chat
    assert "async function sendEmbeddedChat" in session_boundary
    assert "ensureProjectSessionForSend" in session_boundary
    assert "`/api/sessions/${sessionId}/messages`" in session_boundary
    assert "run_async: true" in session_boundary
    assert "activeProjectSessionId" in app_state
    assert "selectEmbeddedSession" in app_state
    assert "openSessionPage" in app_state
    assert "document.querySelector('.chat-textarea')" in app_state
    assert ".goal-input" not in app_state
    assert "input[placeholder]" not in app_state


def test_sidebar_removes_session_category_with_advanced_page_escape():
    sidebar = Path("codepilot/web/components/Sidebar.js").read_text(encoding="utf-8")
    project_view = Path("codepilot/web/components/ProjectView.js").read_text(encoding="utf-8")

    assert "会话" not in sidebar
    assert "selectEmbeddedSession" not in sidebar
    assert "projectSessions(" not in sidebar
    assert "sessionCount(" not in sidebar
    assert "newSession(p.name)" not in sidebar
    assert "cp.openSessionPage" in project_view
    assert "会话详情" in project_view
    assert "打开高级页" not in project_view


def test_web_ui_views_share_console_toolbar_contract():
    tasks_view = Path("codepilot/web/components/TasksView.js").read_text(encoding="utf-8")
    sessions_view = Path("codepilot/web/components/SessionsView.js").read_text(encoding="utf-8")
    jobs_view = Path("codepilot/web/components/JobsView.js").read_text(encoding="utf-8")
    task_detail = Path("codepilot/web/components/TaskDetail.js").read_text(encoding="utf-8")
    job_detail = Path("codepilot/web/components/JobDetail.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    for source in (tasks_view, sessions_view, jobs_view, task_detail, job_detail):
        assert "view-toolbar" in source
    assert ".view-toolbar" in styles
    assert ".ops-panel" in styles
    assert ".detail-grid" in styles


def test_tasks_view_renders_workflow_board_from_backend_payload():
    tasks_view = Path("codepilot/web/components/TasksView.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")
    state_boundary = Path("codepilot/web/boundaries/StateBoundary.js").read_text(encoding="utf-8")
    task_payloads = Path("codepilot/webapp/task_payloads.py").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "taskBoard" in app_state
    assert "task_board_by_project" in app_state
    assert "taskBoardByProject" in app_state
    assert "pivotProjectAliases" in state_boundary
    assert "workflow_board_payload" in task_payloads
    assert "WORKFLOW_BOARD_COLUMNS" in task_payloads
    assert "boardColumns()" in tasks_view
    assert "workflow-board" in tasks_view
    assert "workflow-card-actions" in tasks_view
    assert "filter(t => t.status" not in tasks_view
    assert ".workflow-board" in styles
    assert ".workflow-column" in styles


def test_sessions_view_wires_history_search():
    sessions = Path("codepilot/web/components/SessionsView.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "searchQuery" in sessions
    assert "runSearch()" in sessions
    assert "/api/sessions?${qs.toString()}" in sessions
    assert "搜索会话标题、消息正文或任务编号" in sessions
    assert "session-snippet" in sessions
    assert ".session-search-input" in styles
    assert ".session-snippet" in styles


def test_web_ui_removes_legacy_clarification_frontend():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")
    chat = Path("codepilot/web/components/ChatView.js").read_text(encoding="utf-8")
    session_boundary = Path("codepilot/web/boundaries/AppSessionBoundary.js").read_text(encoding="utf-8")
    app_state = Path("codepilot/web/boundaries/AppStateBoundary.js").read_text(encoding="utf-8")

    assert "ClarifyFields.js" not in index_html
    assert "AppClarifyBoundary.js" not in index_html
    assert "<script src=\"/static/boundaries/AppSessionBoundary.js\"></script>" in index_html
    assert "normalizeClarifyQuestion" not in utils
    assert "createClarifyAnswerState" not in utils
    assert "exportClarifyAnswers" not in utils
    assert "<cp-clarify-fields" not in chat
    assert "cancelSessionClarify" not in session_boundary
    assert "submitClarifyAnswer" not in app_state
    assert "cancelGoalClarify" not in app_state
    assert "cancelComposerClarify" not in app_state
    assert "cancelSessionClarify" not in app_state


def test_batch_task_import_component_removed_from_web_bundle():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    utils = Path("codepilot/web/utils.js").read_text(encoding="utf-8")

    assert "TaskBatchImport.js" not in index_html
    assert "CP.Components.TaskBatchImport" not in utils
    assert "validateTaskBatchImport" not in utils


def test_agent_log_splits_rendering_and_interaction_state_into_boundaries():
    index_html = Path("codepilot/web/index.html").read_text(encoding="utf-8")
    agent_log = Path("codepilot/web/components/AgentLog.js").read_text(encoding="utf-8")
    render_boundary = Path("codepilot/web/boundaries/AgentLogRenderBoundary.js").read_text(encoding="utf-8")
    interaction_boundary = Path("codepilot/web/boundaries/AgentLogInteractionBoundary.js").read_text(encoding="utf-8")
    contract_boundary = Path("codepilot/web/boundaries/AgentLogBoundaryContract.js").read_text(encoding="utf-8")

    assert "<script src=\"/static/boundaries/AgentLogRenderBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AgentLogInteractionBoundary.js\"></script>" in index_html
    assert "<script src=\"/static/boundaries/AgentLogBoundaryContract.js\"></script>" in index_html
    assert "const AgentLogAdapter = CP.AgentLogBoundaryContract.createAdapter({" in agent_log
    assert "const AgentLogRender = AgentLogAdapter.render;" in agent_log
    assert "const AgentLogInteraction = AgentLogAdapter.interaction;" in agent_log
    assert "CP.AgentLogRenderBoundary = CP.AgentLogRenderBoundary || (() => {" in render_boundary
    assert "CP.AgentLogInteractionBoundary = CP.AgentLogInteractionBoundary || (() => {" in interaction_boundary
    assert "CP.AgentLogBoundaryContract = CP.AgentLogBoundaryContract || (() => {" in contract_boundary
    assert "function createAdapter(options = {}) {" in contract_boundary
    assert "_scheduleEnhance" not in interaction_boundary


def test_agent_log_contract_exposes_stable_adapter_surface():
    contract_boundary = Path("codepilot/web/boundaries/AgentLogBoundaryContract.js").read_text(encoding="utf-8")

    for marker in (
        "createMarkdownCache:",
        "renderMarkdown:",
        "parseMarkdownBlocks:",
        "findSearchMatches:",
        "searchSummary:",
        "linesLabel:",
        "jumpLabel:",
        "handleTextLengthChanged:",
        "handleLineCountChanged:",
        "handleSearchQueryChanged:",
        "handleSearchMatchesChanged:",
        "toggleFollow:",
        "onSearchKeydown:",
        "nextMatch:",
        "prevMatch:",
        "scrollToBlock:",
        "onScroll:",
        "scrollToBottom:",
    ):
        assert marker in contract_boundary


def test_agent_log_renders_runtime_output_as_safe_local_scroll_text():
    agent_log = Path("codepilot/web/components/AgentLog.js").read_text(encoding="utf-8")
    render_boundary = Path("codepilot/web/boundaries/AgentLogRenderBoundary.js").read_text(encoding="utf-8")
    interaction_boundary = Path("codepilot/web/boundaries/AgentLogInteractionBoundary.js").read_text(encoding="utf-8")
    contract_boundary = Path("codepilot/web/boundaries/AgentLogBoundaryContract.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "v-html" not in agent_log
    assert 'v-text="b.raw"' in agent_log
    assert "class=\"al-log-wrap\"" in agent_log
    assert "class=\"al-log-text\"" in agent_log
    assert "CP.renderOutput" not in render_boundary
    assert "CP.renderMarkdown" not in render_boundary
    assert "CP.renderOutput" not in contract_boundary
    assert "CP.renderMarkdown" not in contract_boundary
    assert "`log:${lineStart}:${i}:${rawChunk.length}`" in render_boundary
    assert "querySelector(`.al-log-wrap[data-idx=\"${idx}\"]`)" in interaction_boundary
    assert "querySelector(`.al-log-wrap[data-idx=\"${idx}\"]`)" in contract_boundary
    assert ".agent-log-body {" in styles
    assert "overscroll-behavior: contain;" in styles
    assert "contain: paint;" in styles
    assert ".al-log-text" in styles
    assert "white-space: pre-wrap;" in styles
    assert "overflow-wrap: anywhere;" in styles


def test_agent_log_collapses_noncritical_command_details_by_default():
    agent_log = Path("codepilot/web/components/AgentLog.js").read_text(encoding="utf-8")
    render_boundary = Path("codepilot/web/boundaries/AgentLogRenderBoundary.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "expandedBlocks: {}" in agent_log
    assert "toggleBlock(key, block = null)" in agent_log
    assert "isBlockExpanded(key, block = null)" in agent_log
    assert "isBlockCollapsible(block)" in agent_log
    assert "commandGroupTitle(block)" in agent_log
    assert "reviewTone(block)" in agent_log
    assert "telemetryTone(block)" in agent_log
    assert "al-run-meta" in agent_log
    assert "al-command-group-head" in agent_log
    assert "al-command-list" in agent_log
    assert "al-command-run-head" in agent_log
    assert "run.inlineOnly" in agent_log
    assert "b.type === 'diff'" in agent_log
    assert "diffLineClass" in agent_log
    assert "v-if=\"isBlockExpanded(b.key, b)\"" in agent_log
    assert "v-if=\"isBlockExpanded(run.key, run)\"" in agent_log
    assert "parseCommandRuns" in render_boundary
    assert "type: 'command-group'" in render_boundary
    assert "type: 'command-run'" in render_boundary
    assert "type: 'diff'" in render_boundary
    assert "parseDiffBlock" in render_boundary
    assert "collapsed: true" in render_boundary
    assert "type: 'review-verdict'" in render_boundary
    assert "type: 'telemetry'" in render_boundary
    assert "type: 'run-meta'" in render_boundary
    assert "summarizeCommandRun" in render_boundary
    assert ".al-fold-row" in styles
    assert ".al-command-list" in styles
    assert ".al-command-run-head" in styles
    assert ".al-command-text" in styles
    assert ".al-review-card" in styles
    assert ".al-telemetry" in styles
    assert ".al-run-meta" in styles


def test_web_ui_professional_console_style_contract():
    app = Path("codepilot/web/app.js").read_text(encoding="utf-8")
    styles = Path("codepilot/web/styles.css").read_text(encoding="utf-8")

    assert "professional-shell" in app
    for token in (
        "--surface:",
        "--surface-raised:",
        "--sidebar:",
        "--shadow-md:",
        "--focus-soft:",
    ):
        assert token in styles

    for selector in (
        ".professional-shell",
        ".main-header::after",
        ".content-pane::before",
        ".card-head::before",
        ".task-item::before",
        ".service-row::before",
        ".metric::before",
        ".big-empty svg",
        "[data-theme=\"dark\"] .professional-shell",
    ):
        assert selector in styles

    assert ".shell { grid-template-columns: minmax(260px, 304px) minmax(0, 1fr);" in styles
    assert ".view { width: min(1440px, 100%);" in styles
    assert "@media (max-width: 720px)" in styles
    assert ".main-header { min-height: 60px;" in styles
