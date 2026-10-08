"""
SQL push smoke test -- writes ONLY to a separate test table.

Runs the normal extraction on a batch (default: output/*_cleaned_text),
builds the same screenshot rows push_to_sql.py would send, and inserts
them into `test_extracted_fields` (created if missing). The real tables
(claims, claim_state_log, screenshots_*) are never touched.

    python tests/sql_push_test.py                       # default batch folder
    python tests/sql_push_test.py path/to/BATCH.zip
    python tests/sql_push_test.py --drop                # drop the test table afterwards

Connection comes from .env: SQL_SERVER / SQL_PORT / SQL_DATABASE /
SQL_USER / SQL_PASSWORD (or AZURE_SQL_CONNECTION_STRING).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pyodbc  # noqa: E402

from claims_extractor import sql_client  # noqa: E402  (also loads .env)
from push_to_sql import extract  # noqa: E402

TEST_TABLE = "test_extracted_fields"
log = logging.getLogger("claims_local.sqltest")


def connect():
    """(connection, placeholder). Prefers pyodbc with ODBC Driver 17/18;
    falls back to pymssql when only the legacy driver is installed
    (it can't do the TLS Azure SQL requires)."""
    odbc = [d for d in pyodbc.drivers() if "ODBC Driver" in d and "SQL Server" in d]
    if sql_client.AZURE_SQL_CONNECTION_STRING or odbc:
        return sql_client.get_connection(sql_client.build_connection_string(odbc[-1] if odbc else None)), "?"
    import pymssql  # pip install pymssql -- optional, only needed for this fallback

    log.info("No ODBC Driver 17/18 found -- using pymssql")
    return pymssql.connect(
        server=os.environ["SQL_SERVER"], port=os.getenv("SQL_PORT", "1433"),
        user=os.environ["SQL_USER"], password=os.environ["SQL_PASSWORD"],
        database=os.environ["SQL_DATABASE"], login_timeout=sql_client.SQL_CONNECT_TIMEOUT_SEC,
        timeout=sql_client.SQL_QUERY_TIMEOUT_SEC, tds_version="7.4",
    ), "%s"


def main() -> None:
    ap = argparse.ArgumentParser(description="Push extracted fields into a separate SQL test table.")
    ap.add_argument("input", nargs="?", help="Batch .zip / folder / .txt (default: first output/*_cleaned_text)")
    ap.add_argument("--drop", action="store_true", help="Drop the test table at the end")
    args = ap.parse_args()

    src = args.input or str(next((ROOT / "output").glob("*_cleaned_text")))
    results = extract(src, no_learn=False)
    rows = [sql_client.build_sql_rows(name, r)["screenshots_raw"][0] for name, r in results]
    log.info("Extracted %d screenshot(s) from %s", len(rows), src)
    if not rows:
        return

    conn, ph = connect()
    cur = conn.cursor()
    cur.execute("SELECT @@SERVERNAME + ' / ' + DB_NAME()")
    log.info("Connected: %s", cur.fetchone()[0])

    # Same columns as screenshots_raw, but no FK to claims -- fully standalone.
    cur.execute(f"""
        IF OBJECT_ID('{TEST_TABLE}', 'U') IS NULL
        CREATE TABLE {TEST_TABLE} ({sql_client._SCREENSHOT_COLUMNS_DDL}
            pushed_at DATETIME DEFAULT GETUTCDATE(),
            CONSTRAINT pk_{TEST_TABLE} PRIMARY KEY (screenshot_id)
        )
    """)
    conn.commit()

    columns = list(rows[0].keys())
    ok = failed = 0
    for row in rows:
        try:
            cur.execute(f"DELETE FROM {TEST_TABLE} WHERE screenshot_id = {ph}", (row["screenshot_id"],))
            cur.execute(
                f"INSERT INTO {TEST_TABLE} ({', '.join(columns)}) VALUES ({', '.join([ph] * len(columns))})",
                tuple(row[c] for c in columns),
            )
            conn.commit()
            ok += 1
        except Exception as exc:
            conn.rollback()
            failed += 1
            log.error("Insert failed for %s: %s", row["screenshot_id"], exc)

    cur.execute(f"SELECT COUNT(*), SUM(CASE WHEN claim_id IS NOT NULL THEN 1 ELSE 0 END) FROM {TEST_TABLE}")
    total, with_claim = cur.fetchone()
    log.info("Inserted %d, failed %d. %s now has %d row(s), %d with a claim_id.",
             ok, failed, TEST_TABLE, total, with_claim or 0)
    cur.execute(
        f"SELECT TOP 5 screenshot_id, detected_application, claim_id, ticket_id, claim_state, billed "
        f"FROM {TEST_TABLE} WHERE claim_id IS NOT NULL ORDER BY captured_at"
    )
    for r in cur.fetchall():
        log.info("  sample: %s", tuple(r))

    if args.drop:
        cur.execute(f"DROP TABLE {TEST_TABLE}")
        conn.commit()
        log.info("Dropped %s", TEST_TABLE)
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
