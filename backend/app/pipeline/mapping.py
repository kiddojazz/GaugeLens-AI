"""Maps the fields a reader found on one display to registered instruments (physics roles).

Each instrument has a `match` rule: {"unit": "V", "page": ["batt"], "label": ["batt"]}.
A field maps to an instrument when the unit matches and, if several instruments share that unit
(battery V vs AC V), when one of the instrument's page/label keywords appears in the reading's
page_hint or the field's label. "strict": true requires a keyword even for a lone candidate: a meter's
1.8.0 register must not swallow the 1.8.1 tariff register shown on the same screen. A field read without a
unit maps only when its display has a single quantity (a water meter's volume). Ambiguous or unknown
fields stay unmapped (shown in the registry UI), never guessed.
"""

from dataclasses import dataclass, field


@dataclass
class Mapping:
    mapped: list = field(default_factory=list)      # [(instrument dict, DisplayField)]
    unmapped: list = field(default_factory=list)    # [DisplayField]


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _unit(text: str | None) -> str:
    """'kW h', 'KWH' and 'kWh' are the same unit (real meters print a gap between kW and h); m³ is m3."""
    return "".join((text or "").split()).lower().replace("³", "3").replace("²", "2")


def _keywords_hit(keywords: list[str] | None, haystack: str) -> bool:
    return any(_norm(k) and _norm(k) in haystack for k in keywords or [])


def map_fields(reading, instruments: list[dict]) -> Mapping:
    result = Mapping()
    used: set[str] = set()
    page = _norm(reading.page_hint)
    for fld in reading.fields:
        unit = _unit(fld.unit)
        free = [i for i in instruments if i["instrument_id"] not in used]
        candidates = [i for i in free if _unit((i.get("match") or {}).get("unit") or i.get("unit")) == unit]
        if not unit and not candidates and len(instruments) == 1:
            candidates = free           # no unit shown or read, but this display has only one quantity
        haystack = f"{page} {_norm(fld.label)}"

        def hit(i) -> bool:
            rule = i.get("match") or {}
            return _keywords_hit(rule.get("page"), haystack) or _keywords_hit(rule.get("label"), haystack)

        if len(candidates) > 1:
            candidates = [i for i in candidates if hit(i)]
        candidates = [i for i in candidates if not (i.get("match") or {}).get("strict") or hit(i)]
        if len(candidates) == 1:
            used.add(candidates[0]["instrument_id"])
            result.mapped.append((candidates[0], fld))
        else:
            result.unmapped.append(fld)
    return result
