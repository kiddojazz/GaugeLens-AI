"""WhatsApp (Meta Cloud API): the webhook format and the sending client. The conversation itself is shared with
Messenger (conversation.py).

Status: our side works end to end (a message from a phone arrived, GaugeLens replied, Meta accepted the reply),
but Meta delivers nothing until the WhatsApp Business Account's business is verified (error 131031).
"""

import hashlib
import hmac
import logging

import httpx

from ..pipeline.clips import ClipError
from .conversation import Inbound

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
MAX_VIDEO_BYTES = 16 * 1024 * 1024               # WhatsApp's own limit for videos
MAX_TEXT = 4000                                   # WhatsApp allows 4096 characters


def signature_ok(app_secret: str | None, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: HMAC-SHA256 of the raw body with the app secret (Meta signs WhatsApp and Messenger
    webhooks the same way). No secret configured = dev mode."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _changes(payload: dict) -> list[dict]:
    """The 'changes' of a webhook call. Real deliveries wrap them in entry[]; the dashboard's sample may not."""
    if "field" in payload and "value" in payload:
        return [payload]
    return [c for entry in payload.get("entry", []) for c in entry.get("changes", [])]


def inbound_messages(payload: dict) -> list[dict]:
    """The raw messages in a webhook call (statuses, e.g. 'delivered', are separate: delivery_statuses)."""
    out = []
    for change in _changes(payload):
        value = change.get("value", {})
        names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
        for m in value.get("messages", []):
            if m.get("from") and m.get("id"):                # usernames-only senders (no number) aren't supported yet
                out.append(m | {"profile_name": names.get(m.get("from"))})
    return out


def to_inbound(m: dict) -> Inbound:
    kind, base = m.get("type"), {"channel": "whatsapp", "id": m["id"], "sender": m["from"]}
    body = m.get(kind) or {}
    if kind in ("video", "document") and body.get("mime_type", "").startswith("video/"):
        return Inbound(**base, kind="video", media={"id": body["id"], "mime_type": body.get("mime_type")},
                       caption=body.get("caption") or "")
    if kind == "interactive" and body.get("type") == "button_reply":
        return Inbound(**base, kind="button", button_id=body["button_reply"]["id"])
    if kind == "button":                                   # quick-reply on a template message
        return Inbound(**base, kind="button", button_id=body.get("payload", ""))
    if kind == "text":
        return Inbound(**base, kind="text", text=body.get("body", ""))
    if kind == "image":
        return Inbound(**base, kind="image")
    return Inbound(**base, kind="other")


def delivery_statuses(payload: dict) -> list[dict]:
    """Delivery updates for messages we sent: sent / delivered / read / failed (with Meta's error code and reason)."""
    out = []
    for change in _changes(payload):
        for s in change.get("value", {}).get("statuses", []):
            errors = s.get("errors") or []
            out.append({"id": s.get("id"), "status": s.get("status"), "recipient": s.get("recipient_id"),
                        "errors": [{"code": e.get("code"), "title": e.get("title"),
                                    "detail": (e.get("error_data") or {}).get("details") or e.get("message")}
                                   for e in errors]})
    return out


class WhatsAppClient:
    def __init__(self, settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.base = f"{GRAPH}/{settings.graph_version}"
        self.http = http or httpx.AsyncClient(timeout=60)

    @property
    def configured(self) -> bool:
        return bool(self.settings.whatsapp_access_token and self.settings.whatsapp_phone_number_id)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.whatsapp_access_token}"}

    async def _send(self, payload: dict) -> str | None:
        if not self.configured:
            log.warning("WhatsApp is not configured; not sending %s", payload.get("type"))
            return None
        response = await self.http.post(f"{self.base}/{self.settings.whatsapp_phone_number_id}/messages",
                                        headers=self._auth(), json={"messaging_product": "whatsapp", **payload})
        if response.status_code >= 400:
            raise RuntimeError(f"WhatsApp send failed ({response.status_code}): {response.text[:300]}")
        return (response.json().get("messages") or [{}])[0].get("id")

    async def send_text(self, to: str, body: str) -> str | None:
        return await self._send({"to": to, "type": "text", "text": {"body": body[:MAX_TEXT], "preview_url": False}})

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str | None:
        """Up to 3 reply buttons: (id, title <= 20 characters)."""
        return await self._send({"to": to, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": body[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]}}})

    async def mark_read(self, m: Inbound) -> None:
        try:
            await self._send({"status": "read", "message_id": m.id})
        except RuntimeError as exc:                                     # cosmetic: never fail a job over it
            log.info("mark_read failed: %s", exc)

    async def media(self, media: dict) -> tuple[bytes, str]:
        meta = await self.http.get(f"{self.base}/{media['id']}", headers=self._auth())
        meta.raise_for_status()
        info = meta.json()
        if int(info.get("file_size") or 0) > MAX_VIDEO_BYTES:
            raise ClipError("video is larger than 16 MB")
        data = await self.http.get(info["url"], headers=self._auth())
        data.raise_for_status()
        return data.content, info.get("mime_type", "")
