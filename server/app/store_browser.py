"""Browsable console catalog. Artwork hints never replace verified package IDs."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from contextlib import closing

from db import normalize_search_text


def is_addon_source(name):
    return bool(re.search(r"\((?:Addon|DLC)\)", name, re.I))


def title_key(name):
    name = re.sub(r"\.(zip|rar|7z|iso)$", "", name, flags=re.I)
    name = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", name)
    match = re.match(r"^(.*),\s*(The|An|A)\s*$", name, re.I)
    if match: name = match[2] + " " + match[1]
    return normalize_search_text(name)


def display_name(name):
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)|\[[^]]*\]", " ", re.sub(r"\.(zip|rar|7z|iso)$", "", name, flags=re.I))).strip(" -") or name


class StoreBrowser:
    def __init__(self, database, metadata=None):
        self.database = database
        self.lock = threading.Lock()
        self.checked = 0
        self.revision = None
        self.groups = []
        self.by_id = {}
        rows = metadata if metadata is not None else json.loads(Path(__file__).with_name("store_metadata.json").read_text())
        exacts, candidates = {}, {}
        for row in rows:
            kind = row["kind"]
            for alias in [row["name"], *row.get("aliases", [])]:
                exact = normalize_search_text(re.sub(r"\.(zip|rar|7z|iso)$", "", alias, flags=re.I))
                exacts.setdefault((kind, exact), {})[row["title_id"]] = row
                candidates.setdefault((kind, title_key(alias)), {})[row["title_id"]] = row
        # Ambiguous names get a plain tile instead of another game's artwork.
        self.exact = {key: next(iter(values.values())) for key, values in exacts.items() if len(values) == 1}
        self.names = {key: next(iter(values.values())) for key, values in candidates.items() if len(values) == 1}
        parents = {}
        for (_, key), values in candidates.items(): parents.setdefault(key, {}).update(values)
        self.parents = parents

    def refresh(self):
        with self.lock:
            if time.monotonic() - self.checked < 5: return
            with closing(self.database.connect()) as c:
                fingerprint = tuple(c.execute("SELECT count(*),max(id),max(updated_at),sum(status='complete') FROM catalog_items").fetchone())
                if fingerprint == self.revision:
                    self.checked = time.monotonic(); return
                rows = c.execute("SELECT id,source_kind,name,remote_size,status FROM catalog_items ORDER BY id").fetchall()
            groups = {}
            for item in rows:
                addon = is_addon_source(item["name"])
                kind = "digital" if addon else "game" if item["source_kind"] == "disc" else "digital"
                exact = normalize_search_text(re.sub(r"\.(zip|rar|7z|iso)$", "", item["name"], flags=re.I))
                hint = self.exact.get((kind, exact)) or self.names.get((kind, title_key(item["name"])))
                parent = None
                if addon:
                    full = title_key(item["name"])
                    words = full.split()
                    for count in range(len(words), 0, -1):
                        key = " ".join(words[:count])
                        best = self.parents.get(key)
                        if best:
                            if len(best) == 1: parent, hint = key, next(iter(best.values()))
                            break
                name = display_name(hint["name"]) if hint and not addon else display_name(item["name"])
                key = ("dlc" if addon else kind) + ":" + title_key(name)
                gid = hashlib.sha256(key.encode()).hexdigest()[:32]
                group = groups.setdefault(gid, {"group_id": gid, "kind": kind, "name": name, "content_kind": "dlc" if addon else kind,
                    "parent_key": parent,
                    "art_title_id": hint["title_id"] if hint and hint["title_id"] != "00000000" else None,
                    "genre": hint.get("genre", "") if hint else "", "ratings": hint.get("ratings", 0) if hint else 0,
                    "stars": hint.get("stars", 0) if hint else 0, "prepared": False, "_releases": []})
                group["prepared"] |= item["status"] == "complete"
                group["_releases"].append({"release_id": "catalog-" + str(item["id"]), "name": item["name"],
                    "source_bytes": str(item["remote_size"] or 0), "availability": "prepared" if item["status"] == "complete" else item["status"]})
            ordered = sorted(groups.values(), key=lambda g: (normalize_search_text(g["name"]), g["group_id"]))
            for group in ordered:
                group["release_count"] = len(group["_releases"])
                first = normalize_search_text(group["name"])[:1].upper()
                group["letter"] = first if "A" <= first <= "Z" else "#"
                group["_releases"].sort(key=lambda r: (r["availability"] != "prepared", r["name"].casefold(), r["release_id"]))
            self.groups = ordered
            self.by_id = {g["group_id"]: g for g in ordered}
            self.revision = fingerprint
            self.checked = time.monotonic()

    @staticmethod
    def public(group): return {k: v for k, v in group.items() if not k.startswith("_")}

    def home(self):
        self.refresh()
        games = [g for g in self.groups if g["kind"] == "game"]
        art = [g for g in games if g["art_title_id"]]
        popular = sorted(art, key=lambda g: (-g["ratings"], g["name"]))
        rows = []
        def add(name, values):
            if values: rows.append({"name": name, "items": [self.public(g) for g in values[:24]]})
        add("Popular on Foundry", popular)
        add("Ready to install", [g for g in games if g["prepared"]])
        add("Top rated", sorted(art, key=lambda g: (-g["stars"], -g["ratings"], g["name"])))
        for genre in sorted({g["genre"] for g in games if g["genre"]}):
            add(genre, [g for g in popular if g["genre"] == genre])
        add("XBLA / DLC", [g for g in self.groups if g["kind"] == "digital"])
        if not rows: add("Available on Foundry", self.groups)
        return {"featured": [self.public(g) for g in (popular or self.groups)[:4]], "rows": rows[:16],
                "game_count": len(games), "digital_count": len(self.groups) - len(games),
                "artwork_note": "Artwork is a catalog hint. Installation validates the actual package identity."}

    def browse(self, kind="game", letter="*", query="", offset=0, limit=48, title_id=None, parent_name=""):
        self.refresh()
        terms = normalize_search_text(query).split()
        parent_key = title_key(parent_name) if parent_name else ""
        def matches_parent(g):
            if not title_id and not parent_key: return True
            if g["art_title_id"] and title_id: return g["art_title_id"].upper() == title_id
            key = title_key(g["name"])
            return bool(parent_key and (g["parent_key"] == parent_key or (not g["parent_key"] and (key == parent_key or key.startswith(parent_key + " ")))))
        rows = [g for g in self.groups if (g["content_kind"] == "dlc" if kind == "dlc" else g["kind"] == kind)
                and (letter == "*" or g["letter"] == letter) and (kind != "dlc" or matches_parent(g))
                and all(t in normalize_search_text(g["name"]) for t in terms)]
        return {"items": [self.public(g) for g in rows[offset:offset+limit]], "total": len(rows),
                "next_cursor": str(offset+limit) if offset+limit < len(rows) else None,
                "previous_cursor": str(max(0, offset-limit)) if offset else None}

    def releases(self, gid, offset=0, limit=50):
        self.refresh()
        group = self.by_id.get(gid)
        if group is None: return None
        rows = group["_releases"]
        return {**self.public(group), "releases": rows[offset:offset+limit],
                "next_cursor": str(offset+limit) if offset+limit < len(rows) else None,
                "previous_cursor": str(max(0, offset-limit)) if offset else None}
