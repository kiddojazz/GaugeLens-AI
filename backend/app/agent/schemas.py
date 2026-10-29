"""The structured diagnosis (docs/10 §2): measured facts, computed physics and interpretation kept apart."""

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["low", "medium", "high"]


class Observation(BaseModel):
    statement: str = Field(description="A measured fact, with the numbers exactly as the tools returned them.")
    evidence: list[str] = Field(description="IDs from tool results that support it, e.g. 'reading:rd_..', 'frame:..'.")


class PhysicsFinding(BaseModel):
    rule_id: str = Field(description="The rule id exactly as returned by get_physics_checks.")
    result: str = Field(description="pass, fail, misread_fixed or persistent_violation, as returned.")
    residual_pct: float | None = Field(description="The residual exactly as returned; null for check rules.")
    evidence: list[str] = Field(description="The 'check:..' IDs this finding comes from.")


class Hypothesis(BaseModel):
    cause: str
    likelihood: Level
    supporting: list[str] = Field(description="Evidence IDs or short reasons for this cause.")
    against: list[str] = Field(description="Evidence IDs or short reasons against it.")


class DiagnosisOutput(BaseModel):
    headline: str = Field(description="One sentence: what is wrong, in plain language.")
    observations: list[Observation]
    physics: list[PhysicsFinding]
    hypotheses: list[Hypothesis]
    unknowns: list[str] = Field(description="What the evidence can't tell, e.g. 'no individual cell voltages'.")
    proposed_action: str = Field(description="ONE next check a technician can do safely.")
    safety_note: str
    confidence: Level
