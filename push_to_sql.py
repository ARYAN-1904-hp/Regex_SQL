"""
=========================================================
Push extracted claims data to Azure SQL
=========================================================
Runs the same extraction as main.py on a batch ZIP / folder / .txt file
and upserts the results into Azure SQL (claims, claim_state_log,
screenshots_consolidated, screenshots_raw) -- see
claims_extractor/sql_client.py for the schema and upsert rules.

Setup (once):
    pip install -r requirements.txt
    copy .env.example .env      and fill in AZURE_SQL_CONNECTION_STRING
    Install "ODBC Driver 18 for SQL Server" from Microsoft if missing.

Usage:
    python push_to_sql.py "C:\\path\\to\\BATCH_xxx.zip"
    python push_to_sql.py "C:\\path\\to\\BATCH_xxx.zip" --dry-run   (no DB, just counts)
    python push_to_sql.py "C:\\path\\to\\BATCH_xxx.zip" --user-id 1040...

Or extract + push in one go:  python main.py BATCH_xxx.zip --push-sql
Re-running a batch is safe -- rows are MERGEd, never duplicated.
"""

from __future__ import annotations

import argparse
import logging
from typing import List, Tuple

from claims_extractor import field_extractor
from claims_extractor.field_extractor import DocumentResult
from claims_extractor.pattern_learner import KnowledgeBase, PatternLearner
from claims_extractor.screen_parser import extract_screen, learn_batch
from claims_extractor.sql_client import TABLE_ORDER, push_results
from main import read_inputs

log = logging.getLogger("claims_local")


def extract(src: str, kb_path: str = None, no_learn: bool = False) -> List[Tuple[str, DocumentResult]]:
    """Same extraction as main.py, but the knowledge base is only read
    (run main.py to grow it)."""
    items = read_inputs(src)
    log.info("Found %d text file(s) in %s", len(items), src)
    learner = None
    if not no_learn:
        kb = KnowledgeBase.load(kb_path)
        learner = PatternLearner(kb)
        learn_batch(learner, [raw for _n, raw in items], src)
        log.info("Agent detected: %s", learner.agent or "(none)")
        field_extractor._KNOWN_USER_NAMES = list(dict.fromkeys(
            ([learner.agent] if learner.agent else []) + list(kb["people"])))
    results = []
    for name, raw in items:
        try:
            r, _cleaned = extract_screen(raw, learner=learner, source_name=name)
        except Exception:
            log.exception("Failed on %s", name)
            continue
        results.append((name, r))
    return results


def log_totals(totals: dict, dry_run: bool = False) -> None:
    verb = "Would push" if dry_run else "Pushed"
    log.info("%s %d screenshot(s) -- %s", verb, totals["screenshots"],
             ", ".join(f"{t}: {totals[t]}" for t in TABLE_ORDER))
    if totals["failed"]:
        log.warning("%d screenshot(s) failed and were rolled back -- see errors above.", totals["failed"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract a batch and push it to Azure SQL.")
    ap.add_argument("input", nargs="?", help="Batch .zip, a folder of .txt files, or one .txt file")
    ap.add_argument("--user-id", help="user_id to store (default: the cid_<id> in each file name)")
    ap.add_argument("--connection-string", help="Override AZURE_SQL_CONNECTION_STRING from .env")
    ap.add_argument("--dry-run", action="store_true", help="Extract and build rows, but don't touch the DB")
    ap.add_argument("--kb", help="Knowledge base JSON (default: knowledge_base.json next to main.py)")
    ap.add_argument("--no-learn", action="store_true", help="Use the fixed rules only")
    args = ap.parse_args()

    src = args.input or input("Path to the batch .zip (or folder): ").strip().strip('"')
    results = extract(src, args.kb, args.no_learn)
    if not results:
        log.warning("No documents extracted -- nothing to push.")
        return
    totals = push_results(results, user_id=args.user_id,
                          connection_string=args.connection_string, dry_run=args.dry_run)
    log_totals(totals, args.dry_run)


if __name__ == "__main__":
    main()
