"""Unit tests for the heuristic intent classifier."""

from __future__ import annotations

import pytest

from codepilot.ai import _heuristic_intent


# ─── question ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "这个项目是做什么的",
    "目前的项目能提供哪些工具",
    "当前有什么能力",
    "怎么启动 web ui",
    "如何配置 AGENTS.toml",
    "为什么任务卡住了",
    "有没有巡检功能",
    "是不是必须装 codex",
    "哪个命令可以查询数据",
    "解释一下 dedup_key 字段",
    "告诉我当前 backlog 有多少",
    "请问怎么安装",
    "codepilot 支持什么 agent",
    "这个字段啥意思",
    "有几个项目？",
    "它能做什么",
    "codepilot 是干什么的",
    "这个工具干嘛的",
    "你好吗？",
    "backlog 有多少条呢",
])
def test_heuristic_detects_question(text: str):
    assert _heuristic_intent(text) == "question"


# ─── requirement ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "帮我实现一个导出功能",
    "修复登录 bug",
    "新增一个 /doctor 命令",
    "优化日志输出格式",
    "重构 runtime 模块",
    "删除旧的 dispatch 脚本",
    "添加 webhook 通知支持",
    "把 default_mode 升级为 claude",
    "接入飞书消息推送",
    "迁移数据库到 PostgreSQL",
])
def test_heuristic_detects_requirement(text: str):
    assert _heuristic_intent(text) == "requirement"


# ─── command ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "查看状态",
    "看一下状态",
    "看看任务",
    "查看日志",
    "重试任务 7",
    "停止任务 21",
    "跑一下巡检",
    "发布 0.2.0",
    "构建二进制",
])
def test_heuristic_detects_command(text: str):
    assert _heuristic_intent(text) == "command"


# ─── unsure (returns None) ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "hello",
    "跑一下",
    "OK",
])
def test_heuristic_returns_none_when_unsure(text: str):
    assert _heuristic_intent(text) is None


def test_heuristic_returns_none_for_empty():
    assert _heuristic_intent("") is None
    assert _heuristic_intent("   ") is None
