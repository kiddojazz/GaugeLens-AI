"""Messaging webhooks (Meta): WhatsApp Cloud API and Messenger Platform.

    GET  /api/v1/whatsapp/webhook    Meta's verification handshake (hub.verify_token = WEBHOOK_VERIFY_TOKEN)
    POST /api/v1/whatsapp/webhook    WhatsApp messages and delivery statuses
    GET  /api/v1/messenger/webhook   the same handshake for the Messenger webhook
    POST /api/v1/messenger/webhook   Messenger messages and button taps
Each POST: signature checked (app secret), every message stored once, queued, answered 200 at once. The work
(downloading the video, replying) happens in the job worker (channels/conversation.py), so Meta never waits on
the model and never redelivers a message we already have.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..channels.conversation import Inbound, log_message
from ..channels.messenger import inbound_events
from ..channels.whatsapp import delivery_statuses, inbound_messages, signature_ok, to_inbound
from .clips import _state

log = logging.getLogger("uvicorn.error")            # shown in the server console with uvicorn's own messages
router = APIRouter(prefix="/api/v1")


def _handshake(request: Request, channel: str):
    q = request.query_params
    token = request.app.state.settings.webhook_verify_token
    given = (q.get("hub.verify_token") or "").strip()          # a pasted token often brings a space along
    if q.get("hub.mode") == "subscribe" and token and given == token.strip():
        log.info("%s webhook verified (Meta handshake OK)", channel)
        return PlainTextResponse(q.get("hub.challenge", ""))
    log.warning("%s webhook verification refused: the verify token doesn't match WEBHOOK_VERIFY_TOKEN "
                "(received %d characters, expected %d)", channel, len(given), len((token or "").strip()))
    raise HTTPException(status_code=403, detail="verification failed")


async def _payload(request: Request) -> dict:
    body = await request.body()
    if not signature_ok(request.app.state.settings.meta_app_secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="not JSON") from exc


async def _queue(state, m: Inbound, raw: dict) -> bool:
    log.info("%s message in: %s", m.channel, m.kind)
    if not await log_message(state.pool, m.channel, "in", m.sender, m.kind, raw, message_id=m.id):
        return False                                # Meta redelivers; each message is handled once
    await state.queue.enqueue("whatsapp_ingest", {"message": m.as_payload()}, dedupe_key=f"{m.channel}:{m.id}")
    return True


@router.get("/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    return _handshake(request, "WhatsApp")


@router.post("/whatsapp/webhook")
async def whatsapp_receive(request: Request):
    payload = await _payload(request)
    state = _state(request)
    queued = 0
    for raw in inbound_messages(payload):
        queued += await _queue(state, to_inbound(raw), raw)
    for status in delivery_statuses(payload):            # kept so a failed delivery says why
        if status["status"] == "failed":
            log.warning("WhatsApp delivery failed: %s", status["errors"])
        await log_message(state.pool, "whatsapp", "in", status["recipient"] or "", "status", status,
                          message_id=f"{status['id']}:{status['status']}")
    return {"received": queued}


@router.get("/messenger/webhook")
async def messenger_verify(request: Request):
    return _handshake(request, "Messenger")


@router.post("/messenger/webhook")
async def messenger_receive(request: Request):
    payload = await _payload(request)
    state = _state(request)
    queued = 0
    for m in inbound_events(payload):
        queued += await _queue(state, m, {"kind": m.kind, "text": m.text, "button_id": m.button_id, "media": m.media})
    return {"received": queued}
