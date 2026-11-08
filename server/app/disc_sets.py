"""Conservative disc selection hints. Actual package metadata wins at install."""
import hashlib
import re

from compatibility import compatibility_rule
from db import normalize_search_text


_NUMBER = re.compile(r"\((?:Game\s+)?(?:Dis[ck]|Disque|Disco)\s+(\d+)(?:\s*(?:of|/)\s*\d+)?\)", re.I)
_ROLE = re.compile(r"\((?:Install(?:ation)?|Play|Game)\s+Dis[ck]\)", re.I)


def disc_key(name):
    # Keep region, languages, revisions, edition and per-disc subtitles. It is
    # safer to miss a pairing than to silently mix two different releases.
    value = re.sub(r"\.(zip|rar|7z|iso)$", "", name, flags=re.I)
    return normalize_search_text(_ROLE.sub("", _NUMBER.sub("", value)))


def disc_info(name):
    match = _NUMBER.search(name)
    number = int(match[1]) if match else 0
    rule = compatibility_rule(name)
    role, note = "play", "Playable disc: install as GoD."
    if rule and rule.action in {"content", "mixed", "skip"}:
        role = rule.action
    elif re.search(r"\((?:Install(?:ation)?|Add-On Content|Bonus Content)\s+Dis[ck]\)", name, re.I):
        role = "content"
    if role == "content": note = "Extract and validate install/add-on packages; do not make a playable GoD."
    if role == "mixed": note = "Install both the playable GoD and this disc's extracted content packages."
    if role == "skip": note = "This multiplayer disc has no supported GoD installation; it is not selected automatically."
    key = normalize_search_text(name)
    if key.startswith("watch dogs ") or key.startswith("watchdogs "):
        role, note = "manual", "Watch_Dogs needs a custom merged image from both discs. Separate GoD conversion is unsupported."
    if "wolfenstein the new order" in key and number == 1:
        note = "GoD installer disc: launch Disc 1 once on the Xbox before playing Discs 2-4."
    return {"disc_number": number, "disc_role": role, "disc_note": note,
            "recommended": role not in {"skip", "manual"}}


def known_disc_sets(metadata):
    index = {}
    for row in metadata:
        for names in row.get("disc_sets", []):
            members = tuple(sorted(set(n.casefold() for n in names)))
            if 1 < len(members) <= 8:
                for name in members: index.setdefault(name, set()).add(members)
    return {name: next(iter(sets)) for name, sets in index.items() if len(sets) == 1}


def disc_plan(releases, selected, known=None):
    anchor = next((r for r in releases if r["release_id"] == selected), None)
    if anchor is None: return None
    info = disc_info(anchor["name"])
    expected = (known or {}).get(anchor["name"].casefold())
    if not expected and not info["disc_number"] and info["disc_role"] == "play": return None
    matches = [r for r in releases if (r["name"].casefold() in expected if expected else
               disc_key(r["name"]) == disc_key(anchor["name"]) and disc_info(r["name"])["disc_number"] > 0)]
    if anchor not in matches: matches.append(anchor)
    # A duplicate disc/filename is ambiguous, so offer only the chosen source.
    names = [r["name"].casefold() for r in matches]
    numbers = [disc_info(r["name"])["disc_number"] for r in matches]
    if len(set(names)) != len(names) or len([n for n in numbers if n]) != len(set(n for n in numbers if n)):
        matches = [anchor]
    matches.sort(key=lambda r: (disc_info(r["name"])["disc_number"], r["name"].casefold()))
    if len(matches) > 8: matches = [anchor]
    available = {r["name"].casefold() for r in matches}
    missing = [n for n in expected or () if n not in available]
    return {"set_id": hashlib.sha256("\n".join(sorted(available)).encode()).hexdigest()[:32],
            "expected_count": len(expected) if expected else None, "missing_count": len(missing),
            "items": [{**r, **disc_info(r["name"])} for r in matches],
            "note": "Review the discs below. Matching editions only; server preparation is required."
                    + (" Some discs in this known set are unavailable." if missing else "")
                    + (" Complete disc count is unknown; check this selection." if not expected else "")}
