"""The technician's conversation with GaugeLens, the same on every messaging channel (WhatsApp, Messenger).

    a clip arrives ──> create_clip (source_channel = the channel, reply_to = the sender) -> "Got it, reading..."
    a button tap   ──> Approve / Reject the diagnosis (decided_by "<channel>:<sender>"); only the clip's sender
    a text         ──> a question about their latest clip -> the chat agent (agent/chat.py)
    process_clip done ──> 'notify' clip_done: what was read, misreads corrected, anomalies found
    diagnose done     ──> 'notify' diagnosis: headline, likely cause, safe next step + Approve / Reject buttons

Channels turn their webhook events into an Inbound and provide a client with send_text, send_buttons, mark_read
and media; everything else lives here. Replies go out within 24 h of the sender's last message (every flow
starts with them). Nothing here acts on equipment: a button only records a decision.
"""

import logging
import re
from dataclasses import dataclass, field

from ..agent import chat
from ..agent.guardrails import ID, no_dashes
from ..ids import new_id
from ..jobs.events import emit
from ..pipeline.clips import ClipError, create_clip

log = logging.getLogger(__name__)

SIM_TAG = "#sim"                  # marks a clip as a SIMULATED test (caption, or its own message just before/after)
SIM_WINDOW = "10 minutes"
VIDEO_SUFFIX = {"video/mp4": ".mp4", "video/3gpp": ".3gp", "video/quicktime": ".mov"}

HELP = ("Hi, I'm GaugeLens. Send me a 30-60 second video of your inverter's screen (and the clamp meter, if you "
        "have one). I'll read every display, check the numbers against physics, fix misreads and explain any fault. "
        "After that you can ask me questions about it, or approve the suggested next step.")


@dataclass
class Inbound:
    """One message from a technician, whatever the channel."""
    channel: str                 # whatsapp | messenger
    id: str                      # the channel's message id (dedupe key)
    sender: str                  # WhatsApp number or Messenger PSID: where replies go
    kind: str                    # video | image | text | button | other
    text: str = ""
    button_id: str = ""
    media: dict = field(default_factory=dict)   # what the client's media() needs (id or url), plus mime_type
    caption: str = ""

    def as_payload(self) -> dict:
        return self.__dict__.copy()


def plain(text: str) -> str:
    """Chat answers cite evidence IDs for the web UI; in a messaging app they are noise. Markdown bold -> *bold*."""
    text = re.sub(r"\[\s*(?:(?:reading|check|frame|anomaly):[^\]]*)\]", "", text)
    text = ID.sub("", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text).replace("`", "")
    text = re.sub(r"[ \t]+([,.;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text))
    return no_dashes(text).strip()


async def log_message(pool, channel: str, direction: str, address: str, kind: str, body: dict, *,
                      message_id: str | None = None, clip_id: str | None = None) -> bool:
    """Stores one message; False if this message id was already stored (a retried delivery)."""
    return bool(await pool.fetchval(
        """INSERT INTO channel_messages (message_id, channel, direction, address, type, body, clip_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (message_id) DO NOTHING RETURNING 1""",
        message_id or new_id("out_"), channel, direction, address, kind, body, clip_id))


class Conversation:
    def __init__(self, ctx, channel: str, client):
        self.ctx, self.pool, self.channel, self.client = ctx, ctx.pool, channel, client

    async def reply(self, to: str, text: str, clip_id: str | None = None) -> None:
        sent = await self.client.send_text(to, text)
        await log_message(self.pool, self.channel, "out", to, "text", {"text": text}, message_id=sent, clip_id=clip_id)

    async def handle(self, m: Inbound) -> None:
        await self.client.mark_read(m)
        if m.kind == "video":
            await self._video(m)
        elif m.kind == "button":
            await self._decision(m.sender, m.button_id)
        elif m.kind == "text":
            await self._question(m.sender, m.text)
        elif m.kind == "image":
            await self.reply(m.sender, "Thanks! I work from video so I can follow the screen as it changes. "
                                       "Please send a 30-60 second clip of the displays.")
        else:
            await self.reply(m.sender, HELP)

    async def _video(self, m: Inbound) -> None:
        settings = self.ctx.settings
        try:
            data, mime = await self.client.media(m.media)
            suffix = VIDEO_SUFFIX.get((mime or "").split(";")[0], ".mp4")
            tagged = SIM_TAG in m.caption.lower() or bool(await self.pool.fetchval(   # Messenger sends text apart
                f"""SELECT 1 FROM channel_messages WHERE channel = $1 AND address = $2 AND direction = 'in'
                    AND type = 'text' AND body::text ILIKE '%{SIM_TAG}%' AND created_at > now() - interval '{SIM_WINDOW}'""",
                self.channel, m.sender))
            clip_id, _ = await create_clip(self.pool, self.ctx.evidence, self.ctx.queue, video=data,
                                           filename=f"{self.channel}{suffix}", equipment_id=settings.default_equipment,
                                           channel=self.channel, is_demo_replay=tagged)
        except ClipError as exc:
            await self.reply(m.sender, f"Sorry, I couldn't use that video: {exc}. A 30-60 second MP4 works best.")
            return
        await self.pool.execute("UPDATE clips SET reply_to = $2 WHERE clip_id = $1", clip_id, m.sender)
        await emit(self.pool, clip_id, "message", {"channel": self.channel, "direction": "in", "type": "video"})
        await self.reply(m.sender, "Got it. Reading the displays and checking the numbers against physics now; "
                                   "I'll message you the results in a minute or two.", clip_id)

    async def _decision(self, sender: str, button_id: str) -> None:
        action, _, diagnosis_id = button_id.partition(":")
        if action not in ("approve", "reject") or not diagnosis_id:
            await self.reply(sender, HELP)
            return
        row = await self.pool.fetchrow(
            """SELECT d.decision, a.clip_id, c.reply_to, c.source_channel FROM diagnoses d JOIN anomalies a USING (anomaly_id)
               JOIN clips c ON c.clip_id = a.clip_id WHERE d.diagnosis_id = $1""", diagnosis_id)
        if row is None or row["reply_to"] != sender or row["source_channel"] != self.channel:
            await self.reply(sender, "I couldn't find that diagnosis for you.")   # only the clip's sender decides
            return
        if row["decision"] != "pending":
            await self.reply(sender, f"That was already {row['decision']}. Nothing changed.", row["clip_id"])
            return
        decision = "approved" if action == "approve" else "rejected"
        await self.pool.execute(
            """UPDATE diagnoses SET decision = $2, decided_by = $3, decided_at = now(), decision_note = $4
               WHERE diagnosis_id = $1 AND decision = 'pending'""",
            diagnosis_id, decision, f"{self.channel}:{sender}", f"via {self.channel.capitalize()}")
        await emit(self.pool, row["clip_id"], "decision", {"diagnosis_id": diagnosis_id, "decision": decision,
                                                           "by": self.channel})
        await self.reply(sender, (
            "Approved. The suggested check is recorded as your next step. GaugeLens doesn't control the equipment: "
            "please do it safely, and send a new clip afterwards." if decision == "approved" else
            "Rejected and recorded. Tell me what looks wrong, or send a new clip."), row["clip_id"])

    async def _question(self, sender: str, text: str) -> None:
        if text.strip().lower() == SIM_TAG:
            clip_id = await self.pool.fetchval(
                f"""UPDATE clips SET is_demo_replay = true WHERE clip_id = (
                        SELECT clip_id FROM clips WHERE reply_to = $1 AND source_channel = $2
                        AND created_at > now() - interval '{SIM_WINDOW}' ORDER BY created_at DESC LIMIT 1)
                    RETURNING clip_id""", sender, self.channel)
            if clip_id:                                            # anything already found is a test fault too
                await self.pool.execute("UPDATE anomalies SET is_simulated = true WHERE clip_id = $1", clip_id)
            await self.reply(sender, "Noted: that clip is marked as a SIMULATED test." if clip_id else
                             "Noted: the video you send next will be marked as a SIMULATED test.", clip_id)
            return
        if text.strip().lower() in ("hi", "hello", "help", "start", "menu", "?", "get started"):
            await self.reply(sender, HELP)
            return
        clip_id = await self.pool.fetchval(
            """SELECT clip_id FROM clips WHERE reply_to = $1 AND source_channel = $2 AND status = 'done'
               ORDER BY created_at DESC LIMIT 1""", sender, self.channel)
        if clip_id is None:
            busy = await self.pool.fetchval(
                "SELECT 1 FROM clips WHERE reply_to = $1 AND source_channel = $2 AND status IN ('queued', 'processing')",
                sender, self.channel)
            await self.reply(sender, "I'm still reading your clip; I'll message you when it's done." if busy else HELP)
            return
        answer = await chat.ask(self.ctx, clip_id, text, channel=self.channel)
        note = "" if answer["verified"] else "\n\n(I couldn't check every number in this answer against the evidence.)"
        await self.reply(sender, plain(answer["content"]) + note, clip_id)

    async def notify(self, kind: str, clip, anomaly_id: str | None = None) -> None:
        to = clip["reply_to"]
        if kind == "clip_done":
            await self.reply(to, await clip_report(self.pool, clip), clip["clip_id"])
        elif kind == "clip_failed":
            await self.reply(to, "Sorry, I couldn't process that clip. Could you send it again, holding the phone "
                                 "steady with the whole screen in view?", clip["clip_id"])
        elif kind == "diagnosis" and anomaly_id:
            await self._send_diagnosis(to, anomaly_id, clip["clip_id"])

    async def _send_diagnosis(self, to: str, anomaly_id: str, clip_id: str) -> None:
        d = await self.pool.fetchrow("SELECT diagnosis_id, verified, output, decision FROM diagnoses WHERE anomaly_id = $1",
                                     anomaly_id)
        if d is None:
            return
        o = d["output"]
        top = sorted(o.get("hypotheses") or [], key=lambda h: {"high": 0, "medium": 1, "low": 2}.get(h.get("likelihood"), 3))
        body = [f"*{plain(o['headline'])}*"]
        if top:
            body.append(f"\nMost likely: {plain(top[0]['cause'])} ({top[0]['likelihood']}).")
        body.append(f"\n*Suggested next check:* {plain(o['proposed_action'])}")
        body.append(f"\n*Safety:* {plain(o['safety_note'])}")
        body.append("\nEvidence-checked." if d["verified"] else "\nNot fully verified: an expert should review it.")
        text = no_dashes("\n".join(body))
        if d["decision"] == "pending":
            sent = await self.client.send_buttons(to, text + "\n\nApprove the suggested check?",
                                                  [(f"approve:{d['diagnosis_id']}", "Approve"),
                                                   (f"reject:{d['diagnosis_id']}", "Reject")])
            await log_message(self.pool, self.channel, "out", to, "buttons",
                              {"text": text, "diagnosis_id": d["diagnosis_id"]}, message_id=sent, clip_id=clip_id)
        else:
            await self.reply(to, text, clip_id)


async def clip_report(pool, clip) -> str:
    clip_id = clip["clip_id"]
    counts = await pool.fetchrow(
        """SELECT (SELECT count(*) FROM frames WHERE clip_id = $1) AS frames,
                  (SELECT count(*) FROM readings r JOIN frames f USING (frame_id) WHERE f.clip_id = $1
                       AND r.reused_from IS NULL) AS readings,
                  (SELECT count(*) FROM physics_checks WHERE clip_id = $1 AND outcome <> 'skipped') AS checks""", clip_id)
    fixes = await pool.fetch(
        """SELECT DISTINCT ON (r.raw_text, i.physics_role) i.label, i.unit, r.raw_text, pc.relook
           FROM physics_checks pc JOIN readings r ON r.reading_id = pc.suspect_reading_id
           JOIN instruments i ON i.instrument_id = r.instrument_id
           WHERE pc.clip_id = $1 AND pc.outcome = 'misread_fixed' LIMIT 3""", clip_id)
    anomalies = await pool.fetch("SELECT rule_id FROM anomalies WHERE clip_id = $1 ORDER BY ts_start", clip_id)
    lines = [f"*Clip read.* {counts['readings']} readings from {counts['frames']} frames, "
             f"{counts['checks']} physics checks."]
    if clip["is_demo_replay"]:
        lines[0] = "*SIMULATED test clip.* " + lines[0]
    if fixes:
        lines.append("\n*Misreads I caught and corrected:*")
        for f in fixes:
            fixed = next((a.get("raw_text") for a in (f["relook"] or {}).get("attempts", []) if a.get("outcome") == "fixed"), "?")
            lines.append(f"- {f['label']}: first read {f['raw_text']} {f['unit']}, re-read as {fixed} {f['unit']}")
    if anomalies:
        lines.append(f"\n*{len(anomalies)} possible fault{'s' if len(anomalies) > 1 else ''}* confirmed on a second "
                     "look. I'm investigating and will send you what I find.")
    else:
        lines.append("\nEverything checked out: no faults found.")
    lines.append("\nAsk me anything about this clip.")
    return no_dashes("\n".join(lines))


def client_for(channel: str, settings):
    from . import messenger, whatsapp
    return {"whatsapp": whatsapp.WhatsAppClient, "messenger": messenger.MessengerClient}[channel](settings)


async def ingest_job(job, ctx) -> None:
    """Job 'whatsapp_ingest' (any channel): payload {"message": Inbound fields}."""
    m = Inbound(**job.payload["message"])
    allowed = ctx.settings.channel_allowed_senders
    if allowed and m.sender not in allowed:
        log.info("ignoring %s message from an unlisted sender", m.channel)
        return
    await Conversation(ctx, m.channel, client_for(m.channel, ctx.settings)).handle(m)


async def notify_job(job, ctx) -> None:
    """Job 'notify': payload {"kind", "clip_id", "anomaly_id"?}; the clip says which channel to answer on."""
    p = job.payload
    clip = await ctx.pool.fetchrow("SELECT * FROM clips WHERE clip_id = $1", p["clip_id"])
    if clip is None or not clip["reply_to"] or clip["source_channel"] not in ("whatsapp", "messenger"):
        return
    channel = clip["source_channel"]
    await Conversation(ctx, channel, client_for(channel, ctx.settings)).notify(p["kind"], clip, p.get("anomaly_id"))


async def notify_if_channel(pool, queue, clip_id: str, kind: str, **extra) -> None:
    """Queues a report when the clip came in on a messaging channel (no-op for web uploads)."""
    if await pool.fetchval("SELECT reply_to IS NOT NULL FROM clips WHERE clip_id = $1", clip_id):
        key = f"notify:{kind}:{extra.get('anomaly_id') or clip_id}"
        await queue.enqueue("notify", {"kind": kind, "clip_id": clip_id, **extra}, dedupe_key=key)
