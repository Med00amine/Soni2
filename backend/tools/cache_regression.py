"""Zero-cost regression: do the units the new code builds still hit the real narration cache?

    cd backend
    python -m tools.cache_regression --planilla "D:/Downloads/MODELO ONCE.docx"

For each real production kept in e2e-results (faro: without a planilla;
almendra: with the ONCE structure), the book is prepared again by the current
code and every unit's synthesis text is looked up in that production's own
audio cache. Nothing is narrated and the provider is never called: a miss is a
unit the change would pay for again.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import jobs as job_store  # noqa: E402
from app import main, planilla  # noqa: E402
from app.audio_cache import AudioCache  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--planilla", default="", help="the MODELO ONCE.docx the almendra run was produced with")
args = parser.parse_args()
BOOKS = {
    "faro": (ROOT / "backend/tests/corpus/el-faro-de-cabo-gris.xml", None),
    "almendra": (ROOT / "backend/tests/corpus/el-faro-de-la-isla-almendra.xml",
                 Path(args.planilla) if args.planilla else None),
}
report = {}
for name, (book, sheet_path) in BOOKS.items():
    storage = ROOT / "e2e-results" / name / "storage"
    if not storage.is_dir():
        continue
    cache = AudioCache(storage / "audio-cache")
    database = storage / "soni2-jobs.sqlite3"
    import sqlite3

    with sqlite3.connect(database) as connection:
        tables = [row[0] for row in connection.execute("select name from sqlite_master where type='table'")]
        table = next(item for item in tables if "job" in item and "event" not in item and "sentence" not in item)
        job_id = connection.execute(f"select job_id from {table} order by rowid desc limit 1").fetchone()[0]
    job = job_store.get_job(database, job_id)
    old_texts = [unit["synthesis_text"] for unit in job["sentences"]]
    structure = main.parse_book(book.name, book.read_bytes())
    metadata = main.book_metadata(structure)
    sheet = None
    if sheet_path is not None:
        values, _warnings = planilla.parse_docx(sheet_path.read_bytes())
        metadata.update({key: values.pop(key) for key in ("title", "author") if values.get(key)})
        sheet = planilla.from_dict(values)
    chapters, once = main.prepare_book(structure, metadata, narration_plan_json="", create_sections=False, sheet=sheet)
    if once is not None:
        units = main.build_units(once.chapters(), job["source_hash"], {}, structure, records=once.records())
    else:
        units = main.build_units(chapters, job["source_hash"], {}, structure)
    new_texts = [unit["synthesis_text"] for unit in units]
    hits = [cache.contains(main.narration_cache_key(text, job["voice_speed"], job["voice_id"])) for text in new_texts]
    report[name] = {
        "job": job_id, "voice": job["voice_id"], "units_then": len(old_texts), "units_now": len(new_texts),
        "same_synthesis_texts": old_texts == new_texts,
        "changed": [[old, new] for old, new in zip(old_texts, new_texts) if old != new][:10],
        "cache_hits": sum(hits), "cache_misses": [text for text, hit in zip(new_texts, hits) if not hit][:10],
    }
print(json.dumps(report, ensure_ascii=False, indent=1))
