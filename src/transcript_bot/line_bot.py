from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx


LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_CONTENT_URL = "https://api-data.line.me/v2/bot/message/{message_id}/content"


class LineBotError(RuntimeError):
    """Raised when the LINE Messaging API cannot be reached."""


def verify_webhook_signature(raw_body: bytes, signature: str, channel_secret: str) -> bool:
    """Verify the unmodified body as required by the LINE Messaging API."""
    if not signature or not channel_secret:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def reply_to_line(reply_token: str, access_token: str, text: str) -> None:
    response = httpx.post(
        LINE_REPLY_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=15.0,
    )
    if response.status_code >= 400:
        raise LineBotError("LINE Bot 回覆失敗，請確認 Channel access token 與 webhook 設定。")


def download_line_message_content(message_id: str, access_token: str) -> bytes:
    response = httpx.get(
        LINE_CONTENT_URL.format(message_id=message_id),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60.0,
    )
    if response.status_code >= 400:
        raise LineBotError("無法從 LINE 取得音檔內容。")
    return response.content


def acknowledgement_for_event(event: dict[str, Any]) -> str | None:
    message = event.get("message") or {}
    message_type = message.get("type")
    if message_type in {"audio", "file", "video"}:
        return "已收到音檔，已開始在背景轉錄。完成後可在本機會議工作台查看與下載逐字稿。"
    if message_type == "text":
        return "LINE Bot 測試連線正常。請傳送錄音檔來進行下一階段測試。"
    return None
