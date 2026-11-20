"""Minimal Telegram Bot API client (long polling, so no inbound access is needed)."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request

from .http import ApiError, request

Keyboard = list[list[tuple[str, str]]]  # rows of (label, callback data)


class TelegramError(Exception):
    pass


def _markup(keyboard: Keyboard | None) -> dict:
    rows = [[{"text": label, "callback_data": data} for label, data in row] for row in keyboard or []]
    return {"inline_keyboard": rows}


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        base = api_base.rstrip("/")
        self._api = f"{base}/bot{token}"
        self._files = f"{base}/file/bot{token}"

    def call(self, method: str, http_timeout: float = 20, **params):
        """`params` are the Bot API method's own parameters (getUpdates has one called `timeout`)."""
        try:
            raw = request("POST", f"{self._api}/{method}", json_body=params, timeout=http_timeout)
        except ApiError as e:
            try:
                detail = json.loads(e.body).get("description", e.body)
            except ValueError:
                detail = e.body
            raise TelegramError(f"{method}: {detail}") from None
        except OSError as e:
            raise TelegramError(f"{method}: could not reach Telegram ({e})") from None
        return json.loads(raw)["result"]

    def send(self, chat_id: int, text: str, keyboard: Keyboard | None = None, *,
             force_reply: bool = False, placeholder: str | None = None) -> int:
        """`force_reply` opens the reply box on the user's side; it cannot be combined with buttons."""
        if force_reply:
            if keyboard:
                raise ValueError("a ForceReply message cannot carry an inline keyboard")
            markup = {"force_reply": True}
            if placeholder:
                markup["input_field_placeholder"] = placeholder[:64]
        else:
            markup = _markup(keyboard)
        msg = self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                        link_preview_options={"is_disabled": True}, reply_markup=markup)
        return msg["message_id"]

    def edit(self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                      parse_mode="HTML", link_preview_options={"is_disabled": True},
                      reply_markup=_markup(keyboard))
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def answer(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id,
                      text=(text or "")[:190], show_alert=alert)
        except TelegramError:
            pass  # the tap is already handled; a late acknowledgement is harmless

    def get_updates(self, offset: int | None, wait: int = 50) -> list[dict]:
        params = {"timeout": wait, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", http_timeout=wait + 15, **params)

    def send_document(self, chat_id: int, filename: str, data: bytes, caption: str = "") -> int:
        """Upload a file. sendDocument needs multipart/form-data, which urllib has no helper for."""
        boundary = secrets.token_hex(16)
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        body = bytearray()
        for name, value in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n").encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                 f'filename="{safe_name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self._api}/sendDocument", data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())["result"]["message_id"]
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw).get("description", raw)
            except ValueError:
                detail = raw
            raise TelegramError(f"sendDocument: {detail}") from None
        except OSError as e:
            raise TelegramError(f"sendDocument: could not reach Telegram ({e})") from None

    def download(self, file_id: str) -> bytes:
        path = self.call("getFile", file_id=file_id)["file_path"]
        try:
            return request("GET", f"{self._files}/{path}", timeout=30)
        except (ApiError, OSError) as e:
            raise TelegramError(f"download failed ({e})") from None
