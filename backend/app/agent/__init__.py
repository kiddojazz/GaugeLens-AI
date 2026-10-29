"""Diagnosis agent: explains a confirmed physics anomaly with cited evidence, for a human to approve.
Design: docs/10_agent_design.md (the GridIQ pattern: context card, narrow read-only tools, a tool budget,
a deterministic grounding/citation check with one retry, and an honest fallback)."""
