"""What a re-look may conclude: only real fixes are stored as corrections, and each check is stored once."""

import asyncio
from datetime import datetime, timezone

from backend.app.physics.rules import PackDefaults, Rule, RulePack
from backend.app.pipeline.physics_stage import PhysicsStage
from backend.app.pipeline.relook import RelookResult


class FakePool:
    """Records the checks stored; the rest of the stage's writes don't matter here."""

    def __init__(self):
        self.checks = []

    async def execute(self, sql, *args):
        if "INTO physics_checks" in sql:
            self.checks.append({"rule_id": args[5], "passed": args[11], "outcome": args[16]})

    async def fetch(self, *args):
        return []

    async def fetchval(self, *args):
        return 1

    async def fetchrow(self, *args):
        return {"event_id": 1}


# Volts x amps = watts (the failing rule), and watts within what the fuse allows (a second rule watts feeds).
PACK = RulePack(equipment_kind="test", prefix="t.", defaults=PackDefaults(max_age_s=60, requires_steady=False), rules=[
    Rule(id="t.balance", inputs=["w", "v"], check="w <= v * 10"),
    Rule(id="t.floor", inputs=["w"], check="w >= 100"),
    Rule(id="t.other", inputs=["v"], check="v <= 5"),                        # fails too; doesn't use watts
])


def row(role, value, raw):
    return {"reading_id": f"rd_{role}", "instrument_id": f"i_{role}", "physics_role": role, "value": value,
            "raw_text": raw, "confidence": 0.95, "unit": None, "status": "pending", "display_id": "d", "frame_pos": 0}


def observe(reread_value, rows=None):
    pool = FakePool()
    stage = PhysicsStage(pool, {"clip_id": "c", "recorded_at": datetime.now(timezone.utc)},
                         {"equipment_id": "e", "kind": "test", "profile": None}, PACK)

    async def relook(suspect, attempt):
        return RelookResult(attempt, "tight_crop", reread_value, str(reread_value), None, True, "crop", [], {}, [])

    async def go():
        rows_in = rows or [row("w", 838.0, "838"), row("v", 39.3, "39.3")]
        return await stage.observe("f0", 0.0, rows_in, {"w", "v"}, relooker=relook)

    checks, corrections = asyncio.run(go())
    return pool, stage, corrections


def balance_rows(watts=838.0, raw="838"):
    return [row("w", watts, raw), row("v", 39.3, "39.3")]


def test_a_confirmed_reading_stores_each_failed_check_once():
    pool, stage, corrections = observe(838.0)
    balance = [c for c in pool.checks if c["rule_id"] == "t.balance"]
    assert len(balance) == 1 and balance[0]["outcome"] == "persistent_violation"
    assert corrections and "row" not in corrections[0]                       # status only: nothing to recheck
    assert stage.totals["checks_fail"] == 2                                  # t.balance + t.other, not doubled


def test_a_real_fix_is_accepted_even_when_an_unrelated_rule_also_fails():
    pool, stage, corrections = observe(338.0)
    assert stage.totals["misread_fixed"] == 1 and corrections[0]["row"]["value"] == 338.0


def test_a_value_that_breaks_another_rule_is_not_a_fix():
    # 50 W satisfies the balance rule, but breaks t.floor, which 838 W passed.
    pool, stage, corrections = observe(50.0)
    assert stage.totals["misread_fixed"] == 0
    assert not any("row" in c for c in corrections)
