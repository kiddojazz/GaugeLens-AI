"""Re-look spike with the real models: inject ONE misread into first reads, let everything else run live.

    python -m eval.spikes.relook_spike                       # 1711 W -> 7711 W (seven-segment 1 -> 7)
    python -m eval.spikes.relook_spike --role battery_v --kind x2

The first read of the target field is corrupted after Muse Spark returns it (clearly an INJECTED test).
Physics, attribution, SAM 3.1 digit boxes, Spark re-read and correction are all real.
"""

import argparse
import asyncio
import sys

from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.db.pool import create_pool
from backend.app.evidence.store import LocalEvidenceStore
from backend.app.jobs.queue import PostgresJobQueue
from backend.app.jobs.worker import Worker
from backend.app.llm import ChatClient
from backend.app.pipeline.clips import create_clip

UNIT_OF = {"output_w": "W", "battery_v": "V", "battery_i": "A", "output_va": "VA"}


def corrupt(raw: str, kind: str) -> str:
    if kind == "x2":
        return f"{float(raw) * 2:g}"
    if kind == "x10":
        return f"{float(raw) * 10:g}"
    a, b = kind.split("to")
    return raw.replace(a, b, 1)


class InjectingClient(ChatClient):
    def __init__(self, settings, unit: str, kind: str):
        super().__init__(settings)
        self.unit, self.kind, self.injected = unit, kind, []

    async def structured(self, schema, messages, **kwargs):
        result, meta = await super().structured(schema, messages, **kwargs)
        if kwargs.get("purpose") == "read_digital":
            for fld in result.fields:
                if (fld.unit or "") == self.unit and fld.value is not None:
                    raw = corrupt(fld.raw_text, self.kind)
                    if raw != fld.raw_text:
                        self.injected.append((fld.raw_text, raw))
                        fld.raw_text, fld.value = raw, float(raw)
        return result, meta


async def main(role: str, kind: str, clip: str) -> int:
    settings = get_settings()
    pool = await create_pool(settings.database_url)
    try:
        evidence = LocalEvidenceStore(settings.evidence_dir)
        queue = PostgresJobQueue(pool)
        video = PROJECT_ROOT / clip
        clip_id, job_id = await create_clip(pool, evidence, queue, video=video.read_bytes(), filename=video.name,
                                            equipment_id="inv_mock", channel="seed")
        client = InjectingClient(settings, UNIT_OF[role], kind)
        await Worker(pool, settings, client=client, evidence=evidence).run_once()
        print(f"INJECTED first-read misreads ({role}, {kind}): {client.injected}")
        for r in await pool.fetch(
                """SELECT f.t_offset_s, pc.rule_id, pc.outcome, pc.candidate_fix, pc.relook FROM physics_checks pc
                   JOIN frames f USING (frame_id) WHERE pc.clip_id = $1 AND pc.relook IS NOT NULL ORDER BY pc.ts""",
                clip_id):
            print(f"\nt={r['t_offset_s']} {r['rule_id']} -> {r['outcome']} (candidate fix: "
                  f"{(r['candidate_fix'] or {}).get('value')})")
            for a in r["relook"]["attempts"]:
                print(f"   attempt {a['attempt']} [{a['strategy']}] read {a['raw_text']!r} {a['unit']} -> {a['outcome']}"
                      f"   crop: {a['crop_uri']}")
        done = await pool.fetchval("SELECT payload FROM clip_events WHERE clip_id = $1 AND type = 'clip_done'", clip_id)
        print(f"\nclip totals: {done}")
        rows = await pool.fetch(
            """SELECT i.physics_role, r.value, r.status, r.attempt, count(*) AS n FROM readings r
               JOIN frames f USING (frame_id) JOIN instruments i USING (instrument_id)
               WHERE f.clip_id = $1 AND i.physics_role = $2 GROUP BY 1, 2, 3, 4 ORDER BY 4, 2""", clip_id, role)
        for r in rows:
            print(f"   {r['physics_role']} = {r['value']:g}  status={r['status']}  attempt={r['attempt']}  x{r['n']}")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", default="output_w", choices=sorted(UNIT_OF))
    parser.add_argument("--kind", default="1to7", help="x2 | x10 | <digit>to<digit>")
    parser.add_argument("--clip", default="data/mock_clips/mock_coherent.mp4")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.role, args.kind, args.clip)))
