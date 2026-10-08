"""Telegram HTTP adapter; no event or SQLite policy lives here."""
from __future__ import annotations
import json
from urllib.parse import quote
from urllib.request import Request, urlopen

def send_telegram(token: str, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
    endpoint = f"https://api.telegram.org/bot{quote(token, safe=':')}/sendMessage"
    request = Request(
        endpoint,
        data=json.dumps({"chat_id": chat_id, "text": text, "disable_notification": disable_notification}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Telegram returned a non-success status")


