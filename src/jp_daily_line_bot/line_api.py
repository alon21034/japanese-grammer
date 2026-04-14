from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

PUSH_URL = "https://api.line.me/v2/bot/message/push"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"


class LineClient:
    def __init__(
        self,
        channel_access_token: str,
        channel_secret: str,
        *,
        local_test_mode: bool = False,
        local_test_log_path: Path | None = None,
    ) -> None:
        self.channel_access_token = channel_access_token
        self.channel_secret = channel_secret
        self.local_test_mode = local_test_mode
        self.local_test_log_path = local_test_log_path

    @property
    def is_configured(self) -> bool:
        if self.local_test_mode:
            return True
        return bool(self.channel_access_token and self.channel_secret)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json",
        }

    def push_text(self, user_id: str, text: str, timeout_sec: int = 20) -> None:
        self.push_texts(user_id, [text], timeout_sec=timeout_sec)

    def push_texts(self, user_id: str, texts: list[str], timeout_sec: int = 20) -> None:
        clean_texts = [str(text).strip() for text in texts if str(text).strip()]
        if not clean_texts:
            return
        messages = [{"type": "text", "text": text[:5000]} for text in clean_texts[:5]]
        if self.local_test_mode:
            for msg in messages:
                self._append_local_log(
                    {
                        "type": "push",
                        "to": user_id,
                        "text": msg["text"],
                    }
                )
            return
        payload = {
            "to": user_id,
            "messages": messages,
        }
        headers = self._headers()
        headers["X-Line-Retry-Key"] = str(uuid.uuid4())
        res = requests.post(PUSH_URL, json=payload, headers=headers, timeout=timeout_sec)
        res.raise_for_status()

    def reply_text(self, reply_token: str, text: str, timeout_sec: int = 20) -> None:
        self.reply_texts(reply_token, [text], timeout_sec=timeout_sec)

    def reply_texts(self, reply_token: str, texts: list[str], timeout_sec: int = 20) -> None:
        clean_texts = [str(text).strip() for text in texts if str(text).strip()]
        if not clean_texts:
            return
        messages = [{"type": "text", "text": text[:5000]} for text in clean_texts[:5]]
        if self.local_test_mode:
            for msg in messages:
                self._append_local_log(
                    {
                        "type": "reply",
                        "replyToken": reply_token,
                        "text": msg["text"],
                    }
                )
            return
        payload = {
            "replyToken": reply_token,
            "messages": messages,
        }
        res = requests.post(REPLY_URL, json=payload, headers=self._headers(), timeout=timeout_sec)
        res.raise_for_status()

    def verify_signature(self, body: bytes, x_line_signature: str) -> bool:
        if self.local_test_mode:
            return True
        if not x_line_signature:
            return False
        digest = hmac.new(
            self.channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, x_line_signature)

    def _append_local_log(self, payload: dict[str, str]) -> None:
        if self.local_test_log_path is None:
            return
        self.local_test_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with self.local_test_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
