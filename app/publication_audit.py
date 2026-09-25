"""Read-only current publication audit; never initializes or repairs data."""
from __future__ import annotations
import argparse
from .data_publication import current_data_root
import gzip
import json
import sqlite3
import time
import zlib
from pathlib import Path

PRODUCTS = ("latest.json", "daily.json", "lookup.json", "computer-college.json",
            "software-college.json", "class-intensity.json", "major-intensity.json",
            "weekly.json", "monthly.json", "class-images/manifest.json",
            "major-images/manifest.json")


def read_json(path):
    # Bound each metadata/product read; never scan complete DB history.
    with path.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("document exceeds audit size limit")
    document = json.loads(data)
    if not isinstance(document, dict):
        raise ValueError("expected object")
    return document


def latest(database, prefix):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        db.execute("PRAGMA query_only=ON")
        return db.execute("SELECT id, fetched_at FROM snapshots WHERE prefix=? ORDER BY id DESC LIMIT 1",
                          (prefix,)).fetchone()


def audit(database, share, prefix, *, runtime=None, verify_snapshot=False):
    target = latest(database, prefix)
    if target is None:
        return {"state": "no_snapshot", "issues": ["database:no_snapshot"]}
    sid, fetched_at = target
    issues = []
    data_root = current_data_root(share)
    def check(relative):
        try:
            document = read_json(data_root / relative)
            if (document.get("snapshot_id"), document.get("fetched_at")) != target:
                issues.append(relative + ":version_mismatch")
            return document
        except (OSError, ValueError):
            issues.append(relative + ":missing_or_invalid")
            return {}
    for relative in PRODUCTS:
        check(relative)
    # Inspect only registry-declared files; stale unreferenced files are not live products.
    for kind, key in (("class", "classes"), ("major", "majors")):
        try:
            registry = read_json(data_root / f"{kind}-images/manifest.json")
            items = registry[key]
            if not isinstance(items, list) or len(items) > 1000:
                raise ValueError("invalid registry size")
            for item in items:
                entity = item[f"{kind}_id"]
                if not isinstance(entity, str) or not entity.isascii() or not entity.isalnum():
                    raise ValueError("invalid registry identifier")
                check(f"{kind}-ranklists/{entity}.json")
        except (OSError, ValueError, KeyError, TypeError):
            issues.append(kind + ":invalid_registry")
    try:
        index = read_json(share / "statistics-snapshots.json")
        matches = [x for x in index["snapshots"] if x["snapshot_id"] == sid]
        if len(matches) != 1 or matches[0].get("fetched_at") != fetched_at:
            issues.append("history:index_mismatch")
        path = share / "statistics-snapshots" / f"snapshot-{sid}.json.gz"
        if not path.is_file():
            issues.append("history:gzip_missing")
        elif verify_snapshot:
            with gzip.open(path, "rb") as stream:
                raw = stream.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError("snapshot exceeds audit size limit")
            doc = json.loads(raw)
            if ((doc.get("snapshot_id"), doc.get("fetched_at")) != target
                    or not isinstance(doc.get("users"), list)
                    or doc.get("user_count") != len(doc["users"])):
                issues.append("history:gzip_mismatch")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, zlib.error):
        issues.append("history:invalid")
    # Images are asynchronous and may legitimately lag the data.
    try:
        images = read_json(share / "rank-images/manifest.json")
        image_state = "current" if images.get("snapshot_id") == sid else "pending"
    except (OSError, ValueError):
        image_state = "unavailable"
    advancing = latest(database, prefix) != target or current_data_root(share) != data_root
    publishing = False
    if runtime is not None:
        try:
            state = read_json(runtime)
            age = time.time() - float(state["time"])
            publishing = state.get("phase") == "publishing" and 0 <= age < 900
        except (OSError, ValueError, KeyError, TypeError):
            pass
    status = "in_progress" if advancing or (issues and publishing) else "partial" if issues else "consistent"
    return {"state": status, "snapshot_id": sid, "fetched_at": fetched_at,
            "issues": issues, "images": image_state, "gzip_verified": verify_snapshot}


def main():
    from .config import Settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-snapshot", action="store_true",
                        help="also decompress only the latest historical snapshot")
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        result = audit(settings.database_path, settings.share_dir, settings.prefix,
                       runtime=Path("/run/oj-rank-resources/collector.json"),
                       verify_snapshot=args.verify_snapshot)
    except (OSError, ValueError, sqlite3.Error):
        result = {"state": "unavailable", "issues": ["audit_input_unavailable"]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "consistent" else 2 if result["state"] == "in_progress" else 1


if __name__ == "__main__":
    raise SystemExit(main())
