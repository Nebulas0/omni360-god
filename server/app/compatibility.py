from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class CompatibilityRule:
    name_terms: tuple[str, ...]
    disc_terms: tuple[str, ...]
    action: str
    target_title_id: str | None = None


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii")
    value = value.lower().replace("disk", "disc")
    value = re.sub(r"\b(?:disque|disco)(?=\s+\d)", "disc", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


# These are the high-confidence collection/install-disc exceptions documented
# by the classic Windows Iso2God compatibility list. Normal STFS metadata
# still wins when a package carries a real Title ID.
_RULES = (
    CompatibilityRule(("assassin s creed iv black flag",), ("disc 2",), "skip"),
    CompatibilityRule(("alien isolation",), ("disc 1",), "content", "5345085E"),
    CompatibilityRule(("batman arkham city game of the year edition",), ("disc 2",), "content", "57520802"),
    CompatibilityRule(("batman arkham origins",), ("disc 2",), "content", "57520828"),
    CompatibilityRule(("battlefield 4",), ("disc 1",), "content", "454109BA"),
    CompatibilityRule(("bioshock 2",), ("disc 2",), "content", "54540861"),
    CompatibilityRule(("bioshock infinite",), ("disc 2",), "content", "5454085D"),
    CompatibilityRule(("bioshock",), ("disc 2",), "content", "545407D8"),
    CompatibilityRule(("call of duty advanced warfare",), ("disc 2",), "content", "41560914"),
    CompatibilityRule(("call of duty ghosts",), ("disc 2",), "content", "415608FC"),
    CompatibilityRule(("dark souls ii scholar of the first sin",), ("disc 2",), "content", "465307E4"),
    CompatibilityRule(("dishonored game of the year edition",), ("disc 2",), "content", "425307E3"),
    CompatibilityRule(("dragon s dogma dark arisen",), ("disc 2",), "content", "43430814"),
    CompatibilityRule(("elder scrolls iv oblivion game of the year edition",), ("disc 2",), "content", "425307D1"),
    CompatibilityRule(("elder scrolls v skyrim legendary edition",), ("disc 2",), "content", "425307E6"),
    CompatibilityRule(("fallout 3 game of the year edition",), ("disc 2",), "content", "425307D5"),
    CompatibilityRule(("fallout new vegas ultimate edition",), ("disc 2",), "content", "425307E0"),
    CompatibilityRule(("forza motorsport 2",), ("disc 2",), "content", "4D5307EA"),
    CompatibilityRule(("forza motorsport 3",), ("disc 2",), "content", "4D53084D"),
    CompatibilityRule(("grand theft auto v",), ("disc 1",), "content", "545408A7"),
    CompatibilityRule(("mafia 2",), ("disc 2",), "content", "545407E6"),
    CompatibilityRule(("mass effect",), ("disc 2",), "content", "4D5307E8"),
    CompatibilityRule(("metal gear solid v the phantom pain",), ("disc 1",), "content", "4B4E085E"),
    CompatibilityRule(("saints row the third the full package",), ("disc 1",), "content", "5451086D"),
    CompatibilityRule(("saints row iv national treasure edition",), ("disc 1",), "content", "4B4D07F6"),
    CompatibilityRule(("tom clancy s splinter cell blacklist",), ("disc 2",), "mixed", "555308B6"),
    CompatibilityRule(("wolfenstein the new order",), ("disc 1",), "god"),
)


def compatibility_rule(name: str) -> CompatibilityRule | None:
    normalized = _normalize(name)
    for rule in _RULES:
        if rule.name_terms == ("mass effect",) and re.search(r"\bmass effect [23]\b", normalized):
            continue  # continuation game discs are not Mass Effect 1's DLC installer
        if all(term in normalized for term in rule.name_terms) and all(
            term in normalized for term in rule.disc_terms
        ):
            return rule
    return None
