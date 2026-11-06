from __future__ import annotations

from pathlib import Path

from db import Database


def make_db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return database


def test_catalog_queue_is_deduplicated_and_recoverable(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.upsert_catalog("disc", [{
        "source_path": "roms/xbox360/Game.zip",
        "name": "Game.zip",
        "size": 123,
        "modified": None,
        "type": "archive",
    }])
    item = database.catalog("disc")[0][0]
    first = database.enqueue_items([item["id"]], {item["id"]: ["415607E1", "4D530860"]})
    assert first[0]["status"] == "queued"
    claimed_before = database.get_job(first[0]["job_id"])
    assert claimed_before is not None
    assert Database.decode_output_paths(claimed_before["target_title_ids"]) == ["415607E1", "4D530860"]
    second = database.enqueue_items([item["id"]])
    assert second[0]["status"] == "queued"
    assert second[0]["job_id"] == first[0]["job_id"]

    claimed = database.claim_next_job()
    assert claimed is not None
    assert claimed["status"] == "working"
    database.recover_running_jobs()
    queued = database.get_job(first[0]["job_id"])
    assert queued is not None
    assert queued["status"] == "queued"


def test_output_path_encoding_round_trip() -> None:
    paths = ["/data/games-GoD/Game [415607E1].zip"]
    assert Database.decode_output_paths(Database.encode_output_paths(paths)) == paths


def test_reprocess_deduplicates_queue_and_preserves_finished_job(tmp_path):
    db = make_db(tmp_path)
    db.upsert_catalog("digital", [{"source_path": "game.zip", "name": "Game", "size": 1}])
    item = db.catalog("digital")[0][0]
    first = db.enqueue_items([item["id"]])[0]
    db.claim_next_job()
    db.finish_job(first["job_id"], item["id"], True, "Ready", snapshot_path="/tmp/previous.zip")
    queued = db.reprocess_job(first["job_id"])
    duplicate = db.reprocess_job(first["job_id"])
    assert queued["job_id"] == duplicate["job_id"]
    assert db.get_job(first["job_id"])["snapshot_path"] == "/tmp/previous.zip"
    assert db.get_job(first["job_id"])["status"] == "complete"
    assert db.get_job(queued["job_id"])["reprocess_of"] == first["job_id"]


def test_download_confirmation_is_persistent(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.upsert_catalog("disc", [{
        "source_path": "roms/xbox360/confirmed.zip",
        "name": "confirmed.zip",
        "size": 1,
        "modified": None,
        "type": "archive",
    }])
    item = database.catalog("disc")[0][0]
    queued = database.enqueue_items([item["id"]])[0]
    claimed = database.claim_next_job()
    assert claimed is not None
    database.finish_job(
        queued["job_id"], item["id"], True, "Complete",
        database.encode_output_paths(["/data/games-GoD/confirmed.zip"]),
    )
    affected = database.mark_downloaded(queued["job_id"])
    assert affected == [queued["job_id"]]
    assert database.get_job(queued["job_id"])["download_confirmed_at"] is not None
    # Downloaded jobs should be hidden from the active queue
    active = database.recent_jobs()
    assert all(j["id"] != queued["job_id"] for j in active)


def test_catalog_search_ignores_punctuation_and_requires_all_terms(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.upsert_catalog("disc", [
        {
            "source_path": "roms/xbox360/Batman - Arkham Asylum (USA).zip",
            "name": "Batman - Arkham Asylum (USA).zip",
            "size": 1,
            "modified": None,
            "type": "archive",
        },
        {
            "source_path": "roms/xbox360/Batman Arkham City.zip",
            "name": "Batman Arkham City.zip",
            "size": 1,
            "modified": None,
            "type": "archive",
        },
    ])
    items, total = database.catalog("disc", "Batman Arkham Asylum")
    assert total == 1
    assert items[0]["name"].startswith("Batman - Arkham Asylum")
