"""Feishu notification sender — Python native replacement for feishu_notify.mjs.

Sends messages to Feishu chats via REST API. Can be called as a Python function
directly or invoked as a CLI subprocess (stdin JSON, stdout JSON for backwards compat).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from lark_oapi import Client, LogLevel
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

APP_ID = os.environ.get("CODEPILOT_FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("CODEPILOT_FEISHU_APP_SECRET", "")


def _build_post_content(title: str, body: str) -> dict[str, Any]:
    paragraphs = [
        [{"tag": "text", "text": line}]
        for line in str(body or "").split("\n")
        if line.strip()
    ][:20]
    return {
        "zh_cn": {
            "title": str(title or "CodePilot 通知")[:120],
            "content": paragraphs if paragraphs else [[{"tag": "text", "text": "CodePilot 通知"}]],
        },
    }


def send_feishu_notification(
    *,
    chat_ids: list[str],
    card: dict[str, Any] | None = None,
    text: str = "",
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict[str, Any]:
    """Send a message to one or more Feishu chats.

    Returns ``{"ok": bool, "sent": int, "failures": [...]}``.
    """
    _app_id = app_id or APP_ID
    _app_secret = app_secret or APP_SECRET

    if not chat_ids:
        return {"ok": True, "sent": 0}

    client = (
        Client.builder()
        .app_id(_app_id)
        .app_secret(_app_secret)
        .log_level(LogLevel.INFO)
        .build()
    )

    sent = 0
    failures: list[dict[str, Any]] = []
    for chat_id in chat_ids:
        try:
            if card:
                msg_type = "interactive"
                content = json.dumps(card, ensure_ascii=False)
            else:
                msg_type = "post"
                content = json.dumps(
                    _build_post_content("CodePilot 通知", text), ensure_ascii=False
                )

            req = (
                CreateMessageRequest.builder()
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )

            from lark_oapi import RequestOption

            opt = RequestOption()
            opt.receive_id_type = "chat_id"

            client.im.v1.message.create(req, opt)
            sent += 1
        except Exception as exc:
            failures.append(
                {
                    "chat_id": chat_id,
                    "error": str(exc),
                }
            )

    return {"ok": len(failures) == 0, "sent": sent, "failures": failures}


def main() -> None:
    """CLI entry point: read JSON payload from stdin, print result JSON to stdout."""
    raw = sys.stdin.read() or "{}"
    payload = json.loads(raw)
    chat_ids = [cid for cid in (payload.get("chat_ids") or []) if cid]
    card = payload.get("card")
    text = str(payload.get("text") or "")

    if not APP_ID or not APP_SECRET:
        print(json.dumps({"ok": False, "sent": 0, "error": "Missing app credentials"}))
        sys.exit(1)

    result = send_feishu_notification(chat_ids=chat_ids, card=card, text=text)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
