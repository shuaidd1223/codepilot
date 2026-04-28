from __future__ import annotations

import base64
import hashlib
import hmac
import json

from codepilot.webapp import webhook as webhook_mod


class _FakeHTTPResponse:
    def __init__(self, payload: dict, *, status: int = 200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._payload


def test_notify_task_status_sends_signed_feishu_payload(tmp_path, monkeypatch):
    project_path = tmp_path / "demo"
    project_path.mkdir()

    monkeypatch.setattr(
        webhook_mod,
        "_get_webhook_config",
        lambda _path: {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/demo",
            "provider": "feishu",
            "webhook_secret": "sign-secret",
            "enabled": True,
        },
    )
    monkeypatch.setattr(webhook_mod.time, "time", lambda: 1710000000)
    monkeypatch.setattr(webhook_mod, "_send_desktop_notification", lambda **kwargs: True)

    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse({"code": 0})

    monkeypatch.setattr(webhook_mod.urllib.request, "urlopen", fake_urlopen)

    ok = webhook_mod.notify_task_status(
        str(project_path),
        12,
        "修复 webhook 通知",
        "done",
    )

    assert ok is True
    assert captured["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/demo"

    payload = captured["payload"]
    assert payload["msg_type"] == "interactive"
    assert payload["timestamp"] == "1710000000"
    expected_sign = base64.b64encode(
        hmac.new(b"1710000000\nsign-secret", digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    assert payload["sign"] == expected_sign
    card = payload["card"]
    assert "CodePilot #12 已完成" in card["header"]["title"]["content"]
    card_text = json.dumps(card, ensure_ascii=False)
    assert "demo" in card_text
    assert "#12" in card_text
    assert "修复 webhook 通知" in card_text


def test_notify_task_status_auto_detects_wecom_payload_shape(tmp_path, monkeypatch):
    project_path = tmp_path / "demo"
    project_path.mkdir()

    monkeypatch.setattr(
        webhook_mod,
        "_get_webhook_config",
        lambda _path: {
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=demo",
            "provider": "auto",
            "webhook_secret": "",
            "enabled": True,
        },
    )
    monkeypatch.setattr(webhook_mod, "_send_desktop_notification", lambda **kwargs: True)

    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=10):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse({"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(webhook_mod.urllib.request, "urlopen", fake_urlopen)

    ok = webhook_mod.notify_task_status(
        str(project_path),
        7,
        "处理失败任务",
        "failed",
        "boom",
    )

    assert ok is True
    assert captured["payload"] == {
        "msgtype": "text",
        "text": {
            "content": "[X] CodePilot #7 失败\n项目: demo\n任务: 处理失败任务\n错误: boom",
        },
    }


def test_notify_task_event_sends_generic_progress_payload(tmp_path, monkeypatch):
    project_path = tmp_path / "demo"
    project_path.mkdir()

    monkeypatch.setattr(
        webhook_mod,
        "_get_webhook_config",
        lambda _path: {
            "webhook_url": "https://example.invalid/progress",
            "provider": "generic",
            "webhook_secret": "",
            "enabled": True,
        },
    )

    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=10):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse({"ok": True})

    monkeypatch.setattr(webhook_mod.urllib.request, "urlopen", fake_urlopen)

    ok = webhook_mod.notify_task_event(
        str(project_path),
        9,
        "执行阶段反馈",
        event="phase_end",
        phase="builder",
        level="info",
        message="builder 完成",
        status="in_progress",
    )

    assert ok is True
    assert captured["payload"] == {
        "event": "task_progress_event",
        "task_id": 9,
        "task_title": "执行阶段反馈",
        "task_event": "phase_end",
        "phase": "builder",
        "level": "info",
        "status": "in_progress",
        "project": "demo",
        "message": "builder 完成",
        "summary": "",
    }


def test_notify_task_event_sends_feishu_interactive_card(tmp_path, monkeypatch):
    project_path = tmp_path / "demo"
    project_path.mkdir()

    monkeypatch.setattr(
        webhook_mod,
        "_get_webhook_config",
        lambda _path: {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/demo",
            "provider": "feishu",
            "webhook_secret": "",
            "enabled": True,
        },
    )

    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=10):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse({"code": 0})

    monkeypatch.setattr(webhook_mod.urllib.request, "urlopen", fake_urlopen)

    ok = webhook_mod.notify_task_event(
        str(project_path),
        9,
        "执行阶段反馈",
        event="phase_end",
        phase="builder",
        level="info",
        message="builder 完成",
        status="in_progress",
    )

    assert ok is True
    payload = captured["payload"]
    assert payload["msg_type"] == "interactive"
    card_text = json.dumps(payload["card"], ensure_ascii=False)
    assert "CodePilot #9 阶段完成" in card_text
    assert "builder 完成" in card_text
    assert "lark_md" in card_text
