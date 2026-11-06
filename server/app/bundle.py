"""Portable ZIP layout and plain-language Xbox installation notes."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

PUBLIC_CONTENT = "Content/0000000000000000"


def bundle_member(path: Path, output_root: Path) -> str:
    parts = path.relative_to(output_root).parts
    if len(parts) < 3:
        raise ValueError("Output file has no game/title folder")
    _, title_id, *rest = parts
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", title_id):
        safe = re.sub(r'[<>:"/\\|?*]', "_", parts[0])
        return str(PurePosixPath("Unassigned", safe, *parts[1:]))
    if rest[0] == "Cache":
        return str(PurePosixPath("Cache", *rest[1:]))
    if rest[0] == "extras":
        return str(PurePosixPath("Extras", title_id, *rest[1:]))
    return str(PurePosixPath(PUBLIC_CONTENT, title_id, *rest))


def install_guide(report: dict | None = None) -> str:
    report = report or {}
    lines = [
        "GOD FOUNDRY — INSTALL ON YOUR XBOX 360", "",
        "1. Extract the latest game bundle on your computer.",
        "2. Copy the Content folder into Hdd1:/ on the Xbox. Merge folders when asked.",
        "   A GoD header belongs at:",
        "   Hdd1:/Content/0000000000000000/<TITLEID>/00007000/<HEADER>",
        "   Keep <HEADER>.data beside the header, with all Data#### fragments inside.",
        "3. If the ZIP contains Cache, copy its TU_* files into Hdd1:/Cache/.",
        "   Cache updates do not belong inside a Title ID's content folder.",
        "4. In Aurora, scan the content path and enable the matching title update.",
        "5. Test the game. If it exits while applying an update, disable that update",
        "   and test again to separate a game-package issue from an update issue.", "",
        "DLC:   Hdd1:/Content/0000000000000000/<TITLEID>/00000002/",
        "XBLA:  Hdd1:/Content/0000000000000000/<TITLEID>/000D0000/",
        "Lowercase tu... updates: <TITLEID>/000B0000/ within that same Content tree.", "",
        "Files under Extras or Unassigned are not automatically installable content.",
        "Previous job files may be partial or use the older <Game>/<TITLEID> layout.",
        "Use the latest game bundle for the complete current installation layout.", "",
        "CHECKS FOR THIS BUNDLE", "",
    ]
    for check in report.get("checks", []):
        lines.append(f"{check['label']}: {check['detail']}")
    for title in report.get("titles", []):
        lines += ["", f"{title['name']} [{title['title_id']}]"]
        for game in title.get("games", []):
            lines.append(f"  Media ID {game['media_id']} / base version {game['base_version']}")
        for update in title.get("updates", []):
            lines.append(f"  Title update: {update.get('message', update.get('status', 'unknown'))}")
    lines += ["", "Successful file checks do not confirm playback on a physical Xbox.",
              "GoD converter licenses are always preserved. The XBLA/DLC unlock switch",
              "applies to new processing jobs; switching it off does not relock old files."]
    return "\n".join(lines) + "\n"


def add_bundle_documents(archive, report: dict | None) -> None:
    archive.writestr("INSTALL-ON-XBOX.txt", install_guide(report))
    if report:
        archive.writestr("BUNDLE-CHECKS.json", json.dumps(report, indent=2) + "\n")
