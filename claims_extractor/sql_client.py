"""
=========================================================
Claims Extractor -- Azure SQL push
=========================================================
Takes the DocumentResults produced by screen_parser.extract_screen and
upserts them into Azure SQL, using the same 4-table schema and the same
COALESCE-MERGE strategy as the cloud claims_extractor
(Regex_Data_Extraction @ Oscar_Health_Regex_Extraction, sql_client.py):

    claims                     1 row per claim_id (enriched over time)
    claim_state_log            1 row per screenshot that showed a claim
    screenshots_consolidated   1 row per screenshot (COALESCE-upserted)
    screenshots_raw            1 row per screenshot

Every UPDATE uses COALESCE(source.col, target.col): a later screenshot
that did not show a field never overwrites a value captured earlier
with NULL. Screenshots are pushed in capture order, so for the claims
table the LATEST non-empty state wins.

Each screenshot's 4 tables are written in ONE transaction -- either all
of its rows land, or none do (no orphaned claim_state_log rows).

Connection settings come from .env next to main.py -- either
    AZURE_SQL_CONNECTION_STRING=Driver={ODBC Driver 18 for SQL Server};Server=tcp:...
or the separate SQL_SERVER / SQL_PORT / SQL_DATABASE / SQL_USER /
SQL_PASSWORD / SQL_ENCRYPT / SQL_TRUST_SERVER_CERTIFICATE vars, plus
    CLAIMS_SQL_CONNECT_TIMEOUT_SEC=30
    CLAIMS_SQL_QUERY_TIMEOUT_SEC=90
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .field_extractor import DocumentResult

log = logging.getLogger("claims_local.sql")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # .env is optional -- plain environment variables also work
    pass

AZURE_SQL_CONNECTION_STRING = os.getenv("AZURE_SQL_CONNECTION_STRING", "").strip()
SQL_CONNECT_TIMEOUT_SEC = int(os.getenv("CLAIMS_SQL_CONNECT_TIMEOUT_SEC", "30"))
# Caps how long one MERGE can wait on a lock before raising instead of
# hanging forever (another writer -- e.g. the cloud worker -- may hold it).
SQL_QUERY_TIMEOUT_SEC = int(os.getenv("CLAIMS_SQL_QUERY_TIMEOUT_SEC", "90"))

# ---------------------------------------------------------------------------
# Schema -- identical to the cloud claims_extractor (claims_schema_v5.sql)
# ---------------------------------------------------------------------------

_SCREENSHOT_COLUMNS_DDL = """
            screenshot_id            VARCHAR(80)   NOT NULL,
            user_id                  VARCHAR(64),
            captured_at              DATETIME,
            detected_application     VARCHAR(50),
            allowed                  DECIMAL(12,2),
            assignee                 VARCHAR(100),
            billed                   DECIMAL(12,2),
            billing_type             VARCHAR(50),
            blocker                  VARCHAR(255),
            claim_id                 VARCHAR(20),
            claim_state              VARCHAR(50),
            claim_type               VARCHAR(50),
            closed_on                DATETIME,
            code                     VARCHAR(50),
            created_on               DATETIME,
            due                      DATE,
            goal                     VARCHAR(100),
            header                   VARCHAR(255),
            member_id                VARCHAR(20),
            member_id_osc            VARCHAR(30),
            member_state             VARCHAR(5),
            member_responsibility    DECIMAL(12,2),
            osc_id                   VARCHAR(30),
            originally_block_status  VARCHAR(50),
            pay_member               DECIMAL(12,2),
            pay_provider             DECIMAL(12,2),
            provider_claim_no        VARCHAR(30),
            reason                   VARCHAR(255),
            received_date            DATE,
            resolution               VARCHAR(100),
            resolved_by              VARCHAR(100),
            resolved_on              DATETIME,
            sla                      VARCHAR(100),
            service_end_date         DATE,
            service_start_date       DATE,
            skill                    VARCHAR(50),
            stay_type                VARCHAR(50),
            sub_status               VARCHAR(50),
            submission_type          VARCHAR(50),
            submitter                VARCHAR(50),
            tenant                   VARCHAR(50),
            ticket_id                VARCHAR(20),
            ticket_name              VARCHAR(100),
            ticket_state             VARCHAR(50),
            transmission_id          VARCHAR(30),
            updated_by               VARCHAR(100),
            updated_on               DATETIME,
            extra_fields             TEXT,
            flags                    TEXT,"""

TABLE_DDL: Dict[str, str] = {
    "claims": """
        CREATE TABLE claims (
            claim_id                 VARCHAR(20)   NOT NULL,
            ticket_no                VARCHAR(20),
            skill_code               VARCHAR(50),
            claim_state              VARCHAR(50),
            claim_type               VARCHAR(50),
            blocker                  VARCHAR(255),
            submission_type          VARCHAR(50),
            osc_id                   VARCHAR(30),
            ticket_created_on        DATETIME,
            ticket_updated_on        DATETIME,
            ticket_due               DATE,
            ticket_resolved_on       DATETIME,
            ticket_closed_on         DATETIME,
            claim_received_date      DATE,
            claim_service_start_date DATE,
            claim_service_end_date   DATE,
            assignee_name            VARCHAR(100),
            sla                      VARCHAR(100),
            scenario_id              VARCHAR(20),
            user_id                  VARCHAR(64),
            CONSTRAINT pk_claims PRIMARY KEY (claim_id)
        );
        -- Filtered so any number of claims may have no ticket number.
        CREATE UNIQUE INDEX uq_claims_ticket_no ON claims (ticket_no) WHERE ticket_no IS NOT NULL;
    """,
    "claim_state_log": """
        CREATE TABLE claim_state_log (
            screenshot_id  VARCHAR(80)  NOT NULL,
            claim_id       VARCHAR(20)  NOT NULL,
            user_id        VARCHAR(64),
            ticket_id      VARCHAR(20),
            claim_state    VARCHAR(50),
            ticket_status  VARCHAR(50),
            captured_at    DATETIME,
            CONSTRAINT pk_claim_state_log PRIMARY KEY (screenshot_id),
            CONSTRAINT fk_csl_claim FOREIGN KEY (claim_id) REFERENCES claims (claim_id)
        )
    """,
    "screenshots_consolidated": f"""
        CREATE TABLE screenshots_consolidated ({_SCREENSHOT_COLUMNS_DDL}
            cross_fill_notes         TEXT,
            CONSTRAINT pk_screenshots_consolidated PRIMARY KEY (screenshot_id),
            CONSTRAINT fk_sc_claim FOREIGN KEY (claim_id) REFERENCES claims (claim_id)
        )
    """,
    "screenshots_raw": f"""
        CREATE TABLE screenshots_raw ({_SCREENSHOT_COLUMNS_DDL}
            CONSTRAINT pk_screenshots_raw PRIMARY KEY (screenshot_id),
            CONSTRAINT fk_sr_claim FOREIGN KEY (claim_id) REFERENCES claims (claim_id)
        )
    """,
}

# Insert order matters: claims must exist before the FOREIGN KEY tables.
TABLE_ORDER = ["claims", "claim_state_log", "screenshots_consolidated", "screenshots_raw"]
PK_COLUMN = {
    "claims": "claim_id",
    "claim_state_log": "screenshot_id",
    "screenshots_consolidated": "screenshot_id",
    "screenshots_raw": "screenshot_id",
}

FIELD_TO_SQL_COLUMN: Dict[str, str] = {
    "Allowed": "allowed",
    "Assignee": "assignee",
    "Billed": "billed",
    "Billing Type": "billing_type",
    "Blocker": "blocker",
    "Claim ID": "claim_id",
    "Claim State": "claim_state",
    "Claim Type": "claim_type",
    "Closed On": "closed_on",
    "Code": "code",
    "Created On": "created_on",
    "Due": "due",
    "Goal": "goal",
    "Header": "header",
    "Member ID": "member_id",
    "Member ID (OSC)": "member_id_osc",
    "Member State": "member_state",
    "Member responsibilty": "member_responsibility",
    "OSC ID": "osc_id",
    "Pay member": "pay_member",
    "Pay provider": "pay_provider",
    "Provider Claim#": "provider_claim_no",
    "Reason": "reason",
    "Received date": "received_date",
    "Resolution": "resolution",
    "Resolved By": "resolved_by",
    "Resolved On": "resolved_on",
    "SLA": "sla",
    "Service End Date": "service_end_date",
    "Service Start Date": "service_start_date",
    "Skill": "skill",
    "Stay type": "stay_type",
    "Sub status": "sub_status",
    "Submission Type": "submission_type",
    "Submitter": "submitter",
    "Tenant": "tenant",
    "Ticket ID": "ticket_id",
    "Ticket Name": "ticket_name",
    "Ticket State": "ticket_state",
    "Transmission ID": "transmission_id",
    "Updated By": "updated_by",
    "Updated On": "updated_on",
}

# ---------------------------------------------------------------------------
# Value coercion -- SQL Server rejects the whole row on a bad DECIMAL /
# DATE / over-long VARCHAR, so every value is made type-safe first.
# ---------------------------------------------------------------------------

_VARCHAR_MAX_LEN = {
    "claim_id": 20, "ticket_no": 20, "skill_code": 50, "claim_state": 50, "claim_type": 50,
    "blocker": 255, "submission_type": 50, "osc_id": 30, "assignee_name": 100, "sla": 100,
    "scenario_id": 20, "user_id": 64, "screenshot_id": 80, "ticket_id": 20, "ticket_status": 50,
    "detected_application": 50, "assignee": 100, "billing_type": 50, "code": 50, "goal": 100,
    "header": 255, "member_id": 20, "member_id_osc": 30, "member_state": 5,
    "originally_block_status": 50, "provider_claim_no": 30, "reason": 255, "resolution": 100,
    "resolved_by": 100, "skill": 50, "stay_type": 50, "sub_status": 50, "submitter": 50,
    "tenant": 50, "ticket_name": 100, "ticket_state": 50, "transmission_id": 30, "updated_by": 100,
}
_DECIMAL_COLUMNS = {"allowed", "billed", "pay_member", "pay_provider", "member_responsibility"}
_DATE_COLUMNS = {
    "due", "received_date", "service_start_date", "service_end_date",
    "ticket_due", "claim_received_date", "claim_service_start_date", "claim_service_end_date",
}
_DATETIME_COLUMNS = {
    "created_on", "updated_on", "resolved_on", "closed_on",
    "ticket_created_on", "ticket_updated_on", "ticket_resolved_on", "ticket_closed_on",
}

_MONEY_CLEAN_RE = re.compile(r"[^0-9.\-]")
_DECIMAL_12_2_MAX = 10**10
# The Oscar apps render US dates: M/D/YYYY, optionally "h:mm AM/PM TZ".
_DATE_RE = re.compile(
    r"(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{2,4})"
    r"(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2})\s*(?P<ampm>[AP]M)?)?",
    re.IGNORECASE,
)


def _first(v: Any) -> str:
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v).strip() if v is not None else ""


def _parse_money(raw: str) -> Optional[float]:
    """"$1,234.56" -> 1234.56; None when it isn't a plausible amount."""
    cleaned = _MONEY_CLEAN_RE.sub("", raw or "")
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if abs(value) < _DECIMAL_12_2_MAX else None


def _parse_datetime(raw: str) -> Optional[dt.datetime]:
    """First date in the value (M/D/Y, falling back to D/M/Y when the
    first number can't be a month), with its time if one follows.
    Timezone suffixes (EDT, ...) are dropped. None if no valid date."""
    m = _DATE_RE.search(raw or "")
    if not m:
        return None
    a, b, y = int(m.group("a")), int(m.group("b")), int(m.group("y"))
    if y < 100:
        y += 2000
    month, day = (a, b) if a <= 12 else (b, a)
    hour = minute = 0
    if m.group("h"):
        hour, minute = int(m.group("h")), int(m.group("mi"))
        ampm = (m.group("ampm") or "").upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
    try:
        return dt.datetime(y, month, day, hour, minute)
    except ValueError:
        try:
            return dt.datetime(y, month, day)
        except ValueError:
            return None


def sql_value(v: Any, column: str) -> Any:
    """Coerce one extracted value to what its SQL column accepts.
    Unparseable money/dates become NULL -- never a made-up value."""
    if isinstance(v, (dt.datetime, dt.date, float, int)) or v is None:
        return v
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v)
    v = str(v).strip()
    if not v:
        return None
    if column in _DECIMAL_COLUMNS:
        return _parse_money(v)
    if column in _DATETIME_COLUMNS:
        return _parse_datetime(v)
    if column in _DATE_COLUMNS:
        d = _parse_datetime(v)
        return d.date() if d else None
    max_len = _VARCHAR_MAX_LEN.get(column)
    if max_len and len(v) > max_len:
        v = v[:max_len]
    return v


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

# Batch file names: cid_<user>_cid_<user>_<epoch ms>_<screenshot uuid>.txt
_SOURCE_NAME_RE = re.compile(r"^cid_(?P<user>[^_]+)_.*?_(?P<ms>\d{13})_(?P<uuid>[0-9a-fA-F-]{36})")


def parse_source_name(name: str) -> Tuple[str, Optional[str], Optional[dt.datetime]]:
    """(screenshot_id, user_id, captured_at UTC) from a batch file name.
    Files that don't follow the batch naming fall back to the file stem."""
    stem = Path(name).stem
    m = _SOURCE_NAME_RE.match(stem)
    if not m:
        return stem[:80], None, None
    captured = dt.datetime.fromtimestamp(int(m.group("ms")) / 1000, tz=dt.timezone.utc).replace(tzinfo=None)
    return m.group("uuid"), m.group("user"), captured


def _join(d: Dict[str, Any]) -> str:
    return "; ".join(f"{k}={'; '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in d.items())


def build_sql_rows(source_name: str, r: DocumentResult,
                   user_id: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """SQL-ready rows (all 4 tables) for ONE screenshot. claims /
    claim_state_log only get a row when a Claim ID was found."""
    screenshot_id, file_user, captured_at = parse_source_name(source_name)
    user_id = user_id or file_user
    fields = r.fields

    # Fields with no dedicated column are kept in extra_fields, not lost.
    unmapped = {k: v for k, v in fields.items() if k not in FIELD_TO_SQL_COLUMN and v}
    unmapped.update(r.extra_fields)

    shot: Dict[str, Any] = {
        "screenshot_id": screenshot_id,
        "user_id": user_id,
        "captured_at": captured_at,
        "detected_application": r.detected_application,
    }
    for field_name, col in FIELD_TO_SQL_COLUMN.items():
        shot[col] = _first(fields.get(field_name))
    shot["extra_fields"] = _join(unmapped)
    shot["flags"] = "; ".join(f"{k}: {v}" for k, v in r.flags.items())

    tables: Dict[str, List[Dict[str, Any]]] = {
        "claims": [],
        "claim_state_log": [],
        "screenshots_consolidated": [dict(shot)],
        "screenshots_raw": [dict(shot)],
    }
    claim_id = _first(fields.get("Claim ID"))
    if claim_id:
        f = lambda k: _first(fields.get(k))  # noqa: E731
        tables["claims"].append({
            "claim_id": claim_id,
            "ticket_no": f("Ticket ID"),
            "skill_code": f("Code") or f("Skill"),
            "claim_state": f("Claim State"),
            "claim_type": f("Claim Type"),
            "blocker": f("Blocker"),
            "submission_type": f("Submission Type"),
            "osc_id": f("OSC ID"),
            "ticket_created_on": f("Created On"),
            "ticket_updated_on": f("Updated On"),
            "ticket_due": f("Due"),
            "ticket_resolved_on": f("Resolved On"),
            "ticket_closed_on": f("Closed On"),
            "claim_received_date": f("Received date"),
            "claim_service_start_date": f("Service Start Date"),
            "claim_service_end_date": f("Service End Date"),
            "assignee_name": f("Assignee"),
            "sla": f("SLA"),
            "scenario_id": None,
            "user_id": user_id,
        })
        tables["claim_state_log"].append({
            "screenshot_id": screenshot_id,
            "claim_id": claim_id,
            "user_id": user_id,
            "ticket_id": f("Ticket ID"),
            "claim_state": f("Claim State"),
            "ticket_status": f("Ticket State"),
            "captured_at": captured_at,
        })
    for rows in tables.values():
        for row in rows:
            for col in row:
                row[col] = sql_value(row[col], col)
    return tables


# ---------------------------------------------------------------------------
# Connection / DDL / upsert
# ---------------------------------------------------------------------------

def build_connection_string(driver: Optional[str] = None) -> str:
    """AZURE_SQL_CONNECTION_STRING if set, else one assembled from the
    separate SQL_SERVER / SQL_DATABASE / SQL_USER / SQL_PASSWORD vars.
    Empty string when neither is configured."""
    if AZURE_SQL_CONNECTION_STRING:
        return AZURE_SQL_CONNECTION_STRING
    if not all(os.getenv(k) for k in ("SQL_SERVER", "SQL_DATABASE", "SQL_USER", "SQL_PASSWORD")):
        return ""
    if driver is None:
        import pyodbc

        drivers = [d for d in pyodbc.drivers() if "ODBC Driver" in d and "SQL Server" in d]
        driver = drivers[-1] if drivers else "ODBC Driver 18 for SQL Server"
    yes = lambda k, default: "yes" if os.getenv(k, default).lower() == "true" else "no"  # noqa: E731
    # {...} quoting lets the password contain ; & + etc.; a literal } is doubled.
    pwd = os.environ["SQL_PASSWORD"].replace("}", "}}")
    return (
        f"Driver={{{driver}}};"
        f"Server=tcp:{os.environ['SQL_SERVER']},{os.getenv('SQL_PORT', '1433')};"
        f"Database={os.environ['SQL_DATABASE']};"
        f"Uid={os.environ['SQL_USER']};"
        f"Pwd={{{pwd}}};"
        f"Encrypt={yes('SQL_ENCRYPT', 'true')};"
        f"TrustServerCertificate={yes('SQL_TRUST_SERVER_CERTIFICATE', 'false')};"
        f"Connection Timeout={SQL_CONNECT_TIMEOUT_SEC};"
    )


def get_connection(connection_string: Optional[str] = None):
    import pyodbc

    conn_str = connection_string or build_connection_string()
    if not conn_str:
        raise SystemExit("No SQL connection configured -- set AZURE_SQL_CONNECTION_STRING "
                         "or SQL_SERVER/SQL_DATABASE/SQL_USER/SQL_PASSWORD in .env (see .env.example).")
    conn = pyodbc.connect(conn_str, timeout=SQL_CONNECT_TIMEOUT_SEC)
    conn.timeout = SQL_QUERY_TIMEOUT_SEC
    return conn


def is_connection_error(exc: Exception) -> bool:
    """ODBC SQLSTATE 08xxx = the connection itself is dead (reconnect),
    as opposed to a bad row."""
    args = getattr(exc, "args", ())
    return bool(args) and isinstance(args[0], str) and args[0].startswith("08")


def ensure_tables_exist(conn) -> None:
    cur = conn.cursor()
    for table in TABLE_ORDER:
        cur.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?", table)
        if cur.fetchone()[0] == 0:
            log.info("Creating table %s", table)
            cur.execute(TABLE_DDL[table])
            conn.commit()


def _upsert_rows(cur, table: str, rows: List[Dict[str, Any]]) -> int:
    """COALESCE-MERGE (see module docstring); safe to re-run. HOLDLOCK
    serializes concurrent MERGEs on the same new key (e.g. this script
    and the cloud worker writing the same claim at once)."""
    if not rows:
        return 0
    pk = PK_COLUMN[table]
    columns = list(rows[0].keys())
    set_clause = ", ".join(
        f"target.{c} = COALESCE(source.{c}, target.{c})" for c in columns if c != pk
    )
    merge_sql = f"""
        MERGE {table} WITH (HOLDLOCK) AS target
        USING (SELECT {", ".join(f"? AS {c}" for c in columns)}) AS source
        ON target.{pk} = source.{pk}
        WHEN MATCHED THEN
            UPDATE SET {set_clause}
        WHEN NOT MATCHED THEN
            INSERT ({", ".join(columns)}) VALUES ({", ".join(f"source.{c}" for c in columns)});
    """
    for row in rows:
        cur.execute(merge_sql, [row[c] for c in columns])
    return len(rows)


def upload_screenshot_tables(conn, tables: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """All 4 tables for one screenshot in a single transaction."""
    counts: Dict[str, int] = {}
    cur = conn.cursor()
    try:
        for table in TABLE_ORDER:
            counts[table] = _upsert_rows(cur, table, tables.get(table, []))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass  # connection may already be dead; caller reconnects
        raise
    return counts


def push_results(results: List[Tuple[str, DocumentResult]], user_id: Optional[str] = None,
                 connection_string: Optional[str] = None, dry_run: bool = False) -> Dict[str, int]:
    """Push every (source_name, DocumentResult) in order. A screenshot
    that fails is rolled back, logged and skipped -- the rest still go
    in. Returns row totals per table plus 'failed' / 'screenshots'."""
    totals = {t: 0 for t in TABLE_ORDER}
    totals.update(screenshots=0, failed=0)
    batches = [(name, build_sql_rows(name, r, user_id)) for name, r in results]

    if dry_run:
        for _name, tables in batches:
            for t in TABLE_ORDER:
                totals[t] += len(tables[t])
            totals["screenshots"] += 1
        return totals

    conn = get_connection(connection_string)
    try:
        ensure_tables_exist(conn)
        for name, tables in batches:
            for attempt in (1, 2):
                try:
                    counts = upload_screenshot_tables(conn, tables)
                    break
                except Exception as exc:
                    if attempt == 1 and is_connection_error(exc):
                        log.warning("SQL connection dropped on %s -- reconnecting and retrying", name)
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = get_connection(connection_string)
                        continue
                    log.error("SQL push failed for %s: %s", name, exc)
                    counts = None
                    break
            if counts is None:
                totals["failed"] += 1
                continue
            for t, n in counts.items():
                totals[t] += n
            totals["screenshots"] += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return totals
