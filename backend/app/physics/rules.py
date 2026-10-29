"""Rule packs (backend/rules/<name>.yaml), validated with Pydantic when loaded.

A rule has exactly one form:
    expected + observed   residual % = (observed - expected) / expected; passes within the tolerance
    check                 a boolean expression (bands, inequalities, transitions with delta())
    check_ratio           observed / expected must sit inside [lo, hi]
`params` holds constants; a list [lo, hi] is a band, and the rule passes if any value in it does.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

RULES_DIR = Path(__file__).resolve().parents[2] / "rules"


class Rule(BaseModel):
    id: str
    description: str = ""
    inputs: list[str]
    kind: Literal["instant", "transition"] = "instant"
    when: str | None = None
    expected: str | None = None
    observed: str | None = None
    check: str | None = None
    check_ratio: dict | None = None                     # {observed, expected, band: [lo, hi]}
    params: dict[str, float | list[float]] = {}
    tolerance_pct: float | None = None
    tolerance_pct_low_load: float | None = None
    low_load_when: str | None = None
    max_age_s: float | None = None
    requires_steady: bool | None = None
    severity_on_persist: Literal["info", "warning", "critical"] = "warning"

    @model_validator(mode="after")
    def _one_form(self):
        forms = [self.expected is not None and self.observed is not None, self.check is not None,
                 self.check_ratio is not None]
        if sum(forms) != 1 or (self.expected is None) != (self.observed is None):
            raise ValueError(f"rule {self.id}: give exactly one of expected+observed, check, check_ratio")
        if self.check_ratio is not None and not {"observed", "expected", "band"} <= set(self.check_ratio):
            raise ValueError(f"rule {self.id}: check_ratio needs observed, expected and band")
        return self


class PackDefaults(BaseModel):
    max_age_s: float = 20.0
    requires_steady: bool = True


class SteadyConfig(BaseModel):
    role: str | None = None          # the reading whose jumps mark a load step
    step_pct: float = 15.0
    settle_s: float = 5.0


class RulePack(BaseModel):
    equipment_kind: str
    prefix: str = ""
    controller_type: str | None = None
    defaults: PackDefaults = PackDefaults()
    steady: SteadyConfig = SteadyConfig()
    constants: dict[str, float] = {}
    rules: list[Rule]


@lru_cache(maxsize=None)
def load_pack(name: str) -> RulePack:
    data = yaml.safe_load((RULES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return RulePack(prefix=f"{name}.", **data)
