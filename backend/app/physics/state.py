"""Role state: the latest reading per physics role (with its age), the reading before it, and the
load steps seen so far. Inverter screens cycle pages, so a rule combines values read seconds apart;
age limits and steady-state windows keep that honest (docs/04 §3)."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RoleReading:
    role: str
    value: float | None
    ts: float                         # seconds (any consistent clock)
    reading_id: str | None = None
    confidence: float = 1.0
    resolution: float | None = None   # display step, for rounding-aware tolerance
    raw_text: str | None = None       # exactly as displayed, for digit-error search


class RoleState:
    def __init__(self, steady_role: str | None = None, step_pct: float = 15.0):
        self.steady_role = steady_role
        self.step_pct = step_pct
        self.current: dict[str, RoleReading] = {}
        self.previous: dict[str, RoleReading] = {}
        self.steps: list[float] = []

    def update(self, reading: RoleReading) -> None:
        old = self.current.get(reading.role)
        if old is not None:
            self.previous[reading.role] = old
            if (reading.role == self.steady_role and old.value is not None and reading.value is not None
                    and abs(reading.value - old.value) / max(abs(old.value), 1e-9) * 100 > self.step_pct):
                self.steps.append(reading.ts)
        self.current[reading.role] = reading

    def steady_between(self, t0: float, t1: float, settle_s: float) -> bool:
        """No load step from settle_s before the oldest input up to the newest one."""
        return not any(t0 - settle_s <= step <= t1 for step in self.steps)

    def with_value(self, role: str, value: float) -> "RoleState":
        """A copy with one reading's value replaced (attribution trials). Steps are kept."""
        trial = RoleState(self.steady_role, self.step_pct)
        trial.current = dict(self.current)
        trial.previous = dict(self.previous)
        trial.steps = list(self.steps)
        trial.current[role] = replace(self.current[role], value=value)
        return trial

    def with_previous_value(self, role: str, value: float) -> "RoleState":
        """A copy with the PREVIOUS reading's value replaced: for transition rules the earlier reading may be
        the misread one."""
        trial = self.with_value(role, self.current[role].value)
        trial.previous[role] = replace(self.previous[role], value=value)
        return trial
