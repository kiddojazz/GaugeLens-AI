"""Facebook Messenger (Messenger Platform): the webhook format and the sending client. The conversation itself is
shared with WhatsApp (conversation.py).

A Page connected to the app talks to people who message it. While the app is in development mode, only people
with a role on the app (admins, developers, testers) can use it, which is enough to build and demo; letting the
public in needs App Review of pages_messaging.
"""

import logging
import re

import httpx

from ..pipeline.clips import ClipError
from .conversation import Inbound

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
MAX_VIDEO_BYTES = 25 * 1024 * 1024               # Messenger's attachment limit
MAX_TEXT = 2000                                   # Messenger text limit
MAX_BUTTON_TEXT = 640                             # button template text limit


def to_plain(text: str) -> str:
    """Messenger shows no markdown: drop the *bold* markers the conversation uses."""
    return re.sub(r"\*([^*\n]+)\*", r"\1", text)


def inbound_events(payload: dict) -> list[Inbound]:
    """Messages and button taps in a webhook call. Echoes of our own messages, deliveries and reads are skipped."""
    if payload.get("object") != "page":
        return []
    out = []
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []):
            sender = (ev.get("sender") or {}).get("id")
            if not sender:
                continue
            if "postback" in ev:                                        # a button tap
                pb = ev["postback"]
                out.append(Inbound(channel="messenger", id=pb.get("mid") or f"pb:{sender}:{ev.get('timestamp')}",
                                   sender=sender, kind="button", button_id=pb.get("payload", "")))
                continue
            msg = ev.get("message")
            if not msg or msg.get("is_echo"):
                continue
            base = {"channel": "messenger", "id": msg["mid"], "sender": sender}
            if (msg.get("quick_reply") or {}).get("payload"):
                out.append(Inbound(**base, kind="button", button_id=msg["quick_reply"]["payload"]))
                continue
            attachments = msg.get("attachments") or []
            video = next((a for a in attachments if a.get("type") == "video"), None)
            if video:
                out.append(Inbound(**base, kind="video", media={"url": (video.get("payload") or {}).get("url")},
                                   caption=msg.get("text") or ""))
            elif any(a.get("type") == "image" for a in attachments):
                out.append(Inbound(**base, kind="image"))
            elif msg.get("text"):
                out.append(Inbound(**base, kind="text", text=msg["text"]))
            else:
                out.append(Inbound(**base, kind="other"))
    return out


class MessengerClient:
    def __init__(self, settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.base = f"{GRAPH}/{settings.graph_version}"
        self.http = http or httpx.AsyncClient(timeout=60)

    @property
    def configured(self) -> bool:
        return bool(self.settings.messenger_page_access_token)

    async def _send(self, payload: dict) -> str | None:
        if not self.configured:
            log.warning("Messenger is not configured; not sending")
            return None
        response = await self.http.post(f"{self.base}/me/messages",
                                        params={"access_token": self.settings.messenger_page_access_token},
                                        json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"Messenger send failed ({response.status_code}): {response.text[:300]}")
        return response.json().get("message_id")

    async def send_text(self, to: str, body: str) -> str | None:
        return await self._send({"recipient": {"id": to}, "messaging_type": "RESPONSE",
                                 "message": {"text": to_plain(body)[:MAX_TEXT]}})

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str | None:
        """A button template: up to 3 postback buttons (id, title <= 20 characters); text up to 640 characters.
        Longer text goes first as a normal message, then the question with the buttons."""
        text = to_plain(body)
        if len(text) > MAX_BUTTON_TEXT:
            head, _, question = text.rpartition("\n\n")
            await self.send_text(to, head)
            text = question or "Approve the suggested check?"
        return await self._send({"recipient": {"id": to}, "messaging_type": "RESPONSE", "message": {"attachment": {
            "type": "template", "payload": {"template_type": "button", "text": text[:MAX_BUTTON_TEXT], "buttons": [
                {"type": "postback", "title": t[:20], "payload": i} for i, t in buttons[:3]]}}}})

    async def mark_read(self, m: Inbound) -> None:
        try:
            await self._send({"recipient": {"id": m.sender}, "sender_action": "mark_seen"})
        except RuntimeError as exc:                                     # cosmetic: never fail a job over it
            log.info("mark_seen failed: %s", exc)

    async def media(self, media: dict) -> tuple[bytes, str]:
        if not media.get("url"):
            raise ClipError("the video had no download link")
        response = await self.http.get(media["url"], follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > MAX_VIDEO_BYTES:
            raise ClipError("video is larger than 25 MB")
        return response.content, response.headers.get("content-type", "video/mp4")
