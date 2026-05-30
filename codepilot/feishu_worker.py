"""Feishu WebSocket long-connection worker — Python native replacement for feishu_worker.mjs.

Uses lark-oapi SDK for WebSocket event subscription and REST API message sending.
All event handling is in-process (no subprocess per message).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from lark_oapi import Client, EventDispatcherHandler, LogLevel
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.ws import Client as WSClient

from codepilot.feishu_bot import handle_event_payload, handle_card_action_payload
from codepilot.feishu_bot.constants import _NOTIFY_DEDUPE_SERVICE

APP_ID = os.environ.get("CODEPILOT_FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("CODEPILOT_FEISHU_APP_SECRET", "")


def _log(msg: str, extra: Any = None) -> None:
    if extra is not None:
        print(f"[feishu-worker] {msg}", extra)
    else:
        print(f"[feishu-worker] {msg}")


def _extract_text(raw_content: str) -> str:
    if not raw_content:
        return ""
    try:
        parsed = json.loads(raw_content)
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            return parsed["text"]
    except (json.JSONDecodeError, TypeError):
        pass
    return str(raw_content)


def _build_error_card(text: str) -> dict[str, Any]:
    return {
        "type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "CodePilot 飞书处理失败"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": str(text or "CodePilot 飞书处理失败。")},
                },
            ],
        },
    }


def _build_processing_card(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    display = (
        f"已收到您的消息「{raw[:200]}」，正在处理中，请稍候..."
        if raw
        else "已收到您的消息，正在处理中，请稍候..."
    )
    return {
        "type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "CodePilot 处理中"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": display}},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "智能体正在处理中，处理完成后会自动更新此卡片。"},
                    ],
                },
            ],
        },
    }


def _build_post_content(title: str, text: str, content: Any = None) -> dict[str, Any]:
    if isinstance(content, list):
        return {"zh_cn": {"title": str(title)[:120], "content": content}}

    paragraphs = [
        [{"tag": "text", "text": line}]
        for line in str(text or "").split("\n")
        if line.strip()
    ][:20]
    return {
        "zh_cn": {
            "title": str(title or "CodePilot 回复")[:120],
            "content": paragraphs if paragraphs else [[{"tag": "text", "text": "CodePilot 已收到。"}]],
        },
    }


class FeishuWorker:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._rest_client = (
            Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(LogLevel.INFO)
            .build()
        )

    # ── REST API helpers ──────────────────────────────────────────────

    def send_reply(self, chat_id: str, reply: dict[str, Any]) -> str | None:
        """Send a reply to a chat. Returns message_id or None."""
        if not reply or reply.get("type") == "ignore":
            _log("skip reply", {"chat_id": chat_id, "reason": "ignore"})
            return None

        if reply.get("type") == "multi" and isinstance(reply.get("messages"), list):
            _log("send multi reply", {"chat_id": chat_id, "count": len(reply["messages"])})
            last_msg_id = None
            for msg in reply["messages"]:
                last_msg_id = self.send_reply(chat_id, msg)
            return last_msg_id

        if reply.get("type") == "interactive" and reply.get("card"):
            _log("send interactive reply", {"chat_id": chat_id})
            req = (
                CreateMessageRequest.builder()
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(json.dumps(reply["card"], ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = self._rest_client.im.v1.message.create(req, lark_oapi_option("receive_id_type", "chat_id"))
            return resp.data.message_id if resp.data else None

        if reply.get("type") == "post":
            title = str(reply.get("title", "CodePilot 详情"))
            _log("send post reply", {"chat_id": chat_id, "title": title[:80]})
            req = (
                CreateMessageRequest.builder()
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("post")
                    .content(
                        json.dumps(
                            _build_post_content(title, reply.get("text", ""), reply.get("content")),
                            ensure_ascii=False,
                        )
                    )
                    .build()
                )
                .build()
            )
            resp = self._rest_client.im.v1.message.create(req, lark_oapi_option("receive_id_type", "chat_id"))
            return resp.data.message_id if resp.data else None

        text = str(reply.get("text", "CodePilot 已收到，但没有可发送的结果。"))
        _log("send rich text reply", {"chat_id": chat_id, "preview": text[:80]})
        req = (
            CreateMessageRequest.builder()
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("post")
                .content(json.dumps(_build_post_content("CodePilot 回复", text), ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self._rest_client.im.v1.message.create(req, lark_oapi_option("receive_id_type", "chat_id"))
        return resp.data.message_id if resp.data else None

    def update_reply(self, chat_id: str, message_id: str, card: dict[str, Any]) -> None:
        """Patch an existing interactive card message."""
        if not message_id or not card:
            _log("skip update reply", {"chat_id": chat_id, "reason": "no message_id" if not message_id else "no card"})
            return
        _log("patch interactive reply", {"chat_id": chat_id, "message_id": message_id})
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        self._rest_client.im.v1.message.patch(req)

    # ── Event handlers ────────────────────────────────────────────────

    def _handle_message(self, event: P2ImMessageReceiveV1) -> None:
        data = event.event
        if not data or not data.message:
            return

        message = data.message
        if str(message.message_type or "").lower() != "text":
            _log("ignore non-text message", {"message_type": str(message.message_type or "")})
            return

        chat_id = message.chat_id
        text = _extract_text(message.content or "")
        if not chat_id or not text.strip():
            _log("ignore empty message", {"chat_id": chat_id, "text": text})
            return

        event_id = getattr(event, "event_id", "") or ""
        _log(
            "received text message",
            {"chat_id": chat_id, "event_id": event_id, "message_id": message.message_id or "", "text": text},
        )

        self._process_incoming_message(
            {
                "text": text,
                "chat_id": chat_id,
                "message_id": message.message_id or "",
                "event_id": event_id,
            }
        )

    def _handle_card_action(self, card: Any) -> Any:
        event = card.action or {}
        chat_id = str(
            event.open_chat_id
            or getattr(event, "context", {}).get("open_chat_id", "")
            or getattr(card, "open_chat_id", "")
            or ""
        )
        command_value = ""
        if isinstance(event, dict):
            value = event.get("value") or {}
            if isinstance(value, dict):
                command_value = str(value.get("command") or value.get("cmd") or value.get("text") or "").strip()
        elif hasattr(event, "value") and getattr(event, "value", None):
            v = getattr(event, "value")
            if isinstance(v, dict):
                command_value = str(v.get("command") or v.get("cmd") or v.get("text") or "").strip()

        if not chat_id or not command_value:
            _log("ignore card action without command", {"chat_id": chat_id, "command": command_value})
            return {"toast": {"type": "warning", "content": "这个按钮没有可执行命令"}}

        _log("received card action", {"chat_id": chat_id, "command": command_value})

        try:
            processing_msg_id = self.send_reply(chat_id, _build_processing_card(command_value))
            reply = handle_card_action_payload(
                {
                    "event_type": "card.action.trigger",
                    "text": command_value,
                    "chat_id": chat_id,
                    "message_id": str(getattr(card, "open_message_id", "") or ""),
                    "event_id": getattr(event, "event_id", "") if isinstance(event, dict) else "",
                    "action": event if isinstance(event, dict) else {},
                    "context": getattr(event, "context", {}) if hasattr(event, "context") else {},
                    "operator": getattr(event, "operator", {}) if hasattr(event, "operator") else {},
                }
            )

            if reply and reply.get("type") != "ignore":
                if processing_msg_id and reply.get("type") == "interactive" and reply.get("card"):
                    self.update_reply(chat_id, processing_msg_id, reply["card"])
                elif processing_msg_id:
                    done = _build_processing_card("操作已完成。")
                    done["card"]["header"]["template"] = "green"
                    self.update_reply(chat_id, processing_msg_id, done["card"])
                    self.send_reply(chat_id, reply)
                else:
                    self.send_reply(chat_id, reply)

            return {"toast": {"type": "success", "content": "已执行"}}
        except Exception as exc:
            detail = str(exc)
            _log("card action failed", {"chat_id": chat_id, "detail": detail})
            return {"toast": {"type": "error", "content": "执行失败"}}

    def _process_incoming_message(self, payload: dict[str, Any]) -> None:
        chat_id = payload["chat_id"]
        processing_msg_id = None
        try:
            processing_msg_id = self.send_reply(chat_id, _build_processing_card(payload["text"]))
            reply = handle_event_payload(payload)

            if reply and reply.get("type") != "ignore":
                if processing_msg_id and reply.get("type") == "interactive" and reply.get("card"):
                    self.update_reply(chat_id, processing_msg_id, reply["card"])
                elif processing_msg_id:
                    done = _build_processing_card("处理完成，请查看下方回复。")
                    done["card"]["header"]["template"] = "green"
                    self.update_reply(chat_id, processing_msg_id, done["card"])
                    self.send_reply(chat_id, reply)
                else:
                    self.send_reply(chat_id, reply)
        except Exception as exc:
            detail = str(exc)
            _log("handler failed", {"chat_id": chat_id, "detail": detail})
            try:
                if processing_msg_id:
                    self.update_reply(
                        chat_id,
                        processing_msg_id,
                        _build_error_card(f"CodePilot 飞书处理失败：{detail}")["card"],
                    )
                else:
                    self.send_reply(chat_id, _build_error_card(f"CodePilot 飞书处理失败：{detail}"))
            except Exception as reply_err:
                _log("failed to send error reply", {"chat_id": chat_id, "detail": str(reply_err)})

    # ── Main entry ────────────────────────────────────────────────────

    def run(self) -> None:
        _log("starting long connection")

        event_handler = (
            EventDispatcherHandler.builder(APP_SECRET, "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .register_p2_card_action_trigger(self._handle_card_action)
            .build()
        )

        ws_client = WSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            log_level=LogLevel.INFO,
            auto_reconnect=True,
        )

        try:
            ws_client.start()
        except KeyboardInterrupt:
            _log("worker shutting down")
        except Exception as exc:
            _log(f"worker exited with error: {exc}")
            raise


def _lark_oapi_option(key: str, value: str):
    """Build a lark_oapi request option for receive_id_type etc."""
    from lark_oapi import RequestOption

    opt = RequestOption()
    setattr(opt, key, value)
    return opt


lark_oapi_option = lambda k, v: _lark_oapi_option(k, v)


def main() -> None:
    """Entry point for `python -m codepilot feishu run-worker`."""
    if not APP_ID or not APP_SECRET:
        print("Missing CODEPILOT_FEISHU_APP_ID or CODEPILOT_FEISHU_APP_SECRET", file=sys.stderr)
        sys.exit(1)

    worker = FeishuWorker(APP_ID, APP_SECRET)
    worker.run()


if __name__ == "__main__":
    main()
