from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

APP_NAME = "Hug Bear Touch"
BUILD_VERSION = "2026-08-18-hardware-ready-v5"
SCHEMA_VERSION = 4

DB_PATH = Path(os.getenv("DATA_DIR", "/data")) / "touch.db"
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "")
MCP_TOKEN = os.getenv("MCP_TOKEN", "")
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "Asia/Shanghai")
LOCAL_TZ = ZoneInfo(LOCAL_TIMEZONE)

TAP_MAX_MS = int(os.getenv("TAP_MAX_MS", "400"))
HUG_MIN_MS = int(os.getenv("HUG_MIN_MS", "2500"))
TIGHT_HUG_PEAK = int(os.getenv("TIGHT_HUG_PEAK", "2800"))
LONG_HUG_MIN_MS = int(os.getenv("LONG_HUG_MIN_MS", str(10 * 60 * 1000)))
SLEEP_HUG_MIN_MS = int(os.getenv("SLEEP_HUG_MIN_MS", str(30 * 60 * 1000)))
MAX_EVENT_MS = int(os.getenv("MAX_EVENT_MS", str(24 * 60 * 60 * 1000)))

# Firmware later should normally heartbeat every ~60 s.
HEARTBEAT_RECOMMENDED_SECONDS = int(
    os.getenv("HEARTBEAT_RECOMMENDED_SECONDS", "60")
)

# No heartbeat for 15 min -> stale.
# We close at the last confirmed heartbeat time.
HEARTBEAT_TIMEOUT_SECONDS = int(
    os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "900")
)

# A second START very soon after the first
# is treated as a retry/double-click.
DUPLICATE_START_WINDOW_SECONDS = int(
    os.getenv("DUPLICATE_START_WINDOW_SECONDS", "120")
)

if not DEVICE_TOKEN or not MCP_TOKEN:
    raise RuntimeError(
        "Missing DEVICE_TOKEN or MCP_TOKEN. "
        "Set both in Zeabur > Variables before starting the service."
    )

if not (24 <= len(MCP_TOKEN) <= 128) or any(
    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    for c in MCP_TOKEN
):
    raise RuntimeError(
        "MCP_TOKEN must be 24-128 URL-safe characters: "
        "letters, numbers, _ or -."
    )

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# Time
# ============================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso_datetime(
    value: Any,
    field_name: str,
) -> datetime:

    text = str(value).strip()

    if not text:
        raise ValueError(
            f"{field_name} must not be empty"
        )

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)

    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO-8601 datetime"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(
        timezone.utc
    )


# ============================================================
# Database
# ============================================================

def db_connect() -> sqlite3.Connection:

    conn = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA busy_timeout=5000"
    )

    return conn


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:

    columns = {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN {column} {definition}"
        )


def init_db() -> None:

    with db_connect() as conn:

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS touch_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,

                time_source TEXT
                    NOT NULL
                    DEFAULT 'server_derived',

                device_id TEXT NOT NULL,

                peak INTEGER NOT NULL,
                average INTEGER NOT NULL,

                duration_ms INTEGER NOT NULL,

                action TEXT NOT NULL,

                source TEXT
                    NOT NULL
                    DEFAULT 'device',

                session_id TEXT,
                end_reason TEXT
            )
            """
        )

        ensure_column(
            conn,
            "touch_events",
            "started_at",
            "TEXT",
        )

        ensure_column(
            conn,
            "touch_events",
            "ended_at",
            "TEXT",
        )

        ensure_column(
            conn,
            "touch_events",
            "time_source",
            "TEXT NOT NULL DEFAULT 'server_derived'",
        )

        ensure_column(
            conn,
            "touch_events",
            "session_id",
            "TEXT",
        )

        ensure_column(
            conn,
            "touch_events",
            "end_reason",
            "TEXT",
        )

        conn.execute(
            """
            UPDATE touch_events
            SET ended_at = created_at
            WHERE ended_at IS NULL
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hug_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                session_id TEXT
                    NOT NULL
                    UNIQUE,

                device_id TEXT NOT NULL,

                started_at TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL,
                ended_at TEXT,

                status TEXT
                    NOT NULL
                    DEFAULT 'active',

                end_reason TEXT,

                peak_max INTEGER
                    NOT NULL
                    DEFAULT 0,

                average_sum INTEGER
                    NOT NULL
                    DEFAULT 0,

                sample_count INTEGER
                    NOT NULL
                    DEFAULT 0,

                last_peak INTEGER
                    NOT NULL
                    DEFAULT 0,

                last_average INTEGER
                    NOT NULL
                    DEFAULT 0,

                source TEXT
                    NOT NULL
                    DEFAULT 'device_session',

                time_source TEXT
                    NOT NULL
                    DEFAULT 'server',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_touch_created
            ON touch_events(created_at DESC)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_touch_ended
            ON touch_events(ended_at DESC)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_hug_sessions_status
            ON hug_sessions(
                status,
                last_heartbeat_at
            )
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_touch_session_unique

            ON touch_events(session_id)

            WHERE session_id IS NOT NULL
            """
        )


# ============================================================
# Classification
# ============================================================

def classify_touch(
    peak: int,
    duration_ms: int,
) -> str:

    if duration_ms < TAP_MAX_MS:
        return "tap"

    if duration_ms >= SLEEP_HUG_MIN_MS:
        return "sleep_hug"

    if duration_ms >= LONG_HUG_MIN_MS:
        return "long_hug"

    if (
        duration_ms >= HUG_MIN_MS
        and peak >= TIGHT_HUG_PEAK
    ):
        return "tight_hug"

    if duration_ms >= HUG_MIN_MS:
        return "hug"

    return "press"


def action_zh(
    action: str,
) -> str:

    return {
        "tap":
            "轻碰",

        "press":
            "按住",

        "hug":
            "抱住",

        "tight_hug":
            "紧紧抱住",

        "long_hug":
            "长时间抱住",

        "sleep_hug":
            "抱着睡",

    }.get(
        action,
        action,
    )


# ============================================================
# Local time formatting
# ============================================================

def local_time_fields(
    started_at: str,
    ended_at: str,
    received_at: str,
) -> dict[str, Any]:

    start = parse_iso_datetime(
        started_at,
        "started_at",
    ).astimezone(
        LOCAL_TZ
    )

    end = parse_iso_datetime(
        ended_at,
        "ended_at",
    ).astimezone(
        LOCAL_TZ
    )

    received = parse_iso_datetime(
        received_at,
        "received_at",
    ).astimezone(
        LOCAL_TZ
    )

    return {
        "timezone":
            LOCAL_TIMEZONE,

        "started_at_local":
            start.isoformat(),

        "ended_at_local":
            end.isoformat(),

        "received_at_local":
            received.isoformat(),

        "started_at_text":
            start.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "ended_at_text":
            end.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "received_at_text":
            received.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "time_range_text":
            (
                f"{start.strftime('%Y-%m-%d %H:%M:%S')}"
                f" → "
                f"{end.strftime('%H:%M:%S')}"
            ),
    }


# ============================================================
# Validation
# ============================================================

def validate_sensor_values(
    payload: dict[str, Any],
) -> tuple[str, int, int]:

    device_id = str(
        payload.get(
            "device_id",
            "hug-bear-01",
        )
    )[:64]

    try:

        peak = int(
            payload["peak"]
        )

        average = int(
            payload.get(
                "average",
                peak,
            )
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:

        raise ValueError(
            "peak and average must be integers"
        ) from exc

    if not 0 <= peak <= 4095:
        raise ValueError(
            "peak must be between 0 and 4095"
        )

    if not 0 <= average <= 4095:
        raise ValueError(
            "average must be between 0 and 4095"
        )

    return (
        device_id,
        peak,
        average,
    )


def validate_session_id(
    value: Any,
) -> str:

    session_id = str(
        value
    ).strip()

    if not session_id:
        raise ValueError(
            "session_id is required"
        )

    if len(session_id) > 96:
        raise ValueError(
            "session_id is too long"
        )

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789_-"
    )

    if any(
        c not in allowed
        for c in session_id
    ):
        raise ValueError(
            "session_id must contain "
            "only letters, numbers, _ or -"
        )

    return session_id


def token_ok(
    request: Request,
    header: str,
    expected: str,
) -> bool:

    return (
        request.headers.get(
            header,
            "",
        )
        == expected
    )


# ============================================================
# Completed events
# ============================================================

def insert_completed_event(
    *,
    received_at: str,
    started_at: str,
    ended_at: str,
    time_source: str,
    device_id: str,
    peak: int,
    average: int,
    duration_ms: int,
    source: str,
    session_id: str | None = None,
    end_reason: str | None = None,
) -> dict[str, Any]:

    duration_ms = max(
        20,
        min(
            int(duration_ms),
            MAX_EVENT_MS,
        ),
    )

    action = classify_touch(
        peak,
        duration_ms,
    )

    try:

        with db_connect() as conn:

            cur = conn.execute(
                """
                INSERT INTO touch_events (
                    created_at,
                    started_at,
                    ended_at,
                    time_source,

                    device_id,

                    peak,
                    average,
                    duration_ms,

                    action,
                    source,

                    session_id,
                    end_reason
                )

                VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
                """,
                (
                    received_at,
                    started_at,
                    ended_at,
                    time_source,

                    device_id,

                    peak,
                    average,
                    duration_ms,

                    action,
                    source,

                    session_id,
                    end_reason,
                ),
            )

            event_id = (
                cur.lastrowid
            )

    except sqlite3.IntegrityError:

        if session_id:

            existing = (
                get_event_by_session_id(
                    session_id
                )
            )

            if existing:
                return existing

        raise

    event = {
        "id":
            event_id,

        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "created_at":
            received_at,

        "received_at":
            received_at,

        "started_at":
            started_at,

        "ended_at":
            ended_at,

        "time_source":
            time_source,

        "device_id":
            device_id,

        "peak":
            peak,

        "average":
            average,

        "duration_ms":
            duration_ms,

        "duration_s":
            round(
                duration_ms / 1000,
                2,
            ),

        "action":
            action,

        "action_zh":
            action_zh(
                action
            ),

        "source":
            source,

        "session_id":
            session_id,

        "end_reason":
            end_reason,
    }

    event.update(
        local_time_fields(
            started_at,
            ended_at,
            received_at,
        )
    )

    return event


# ============================================================
# Old single-event API
# ============================================================

def insert_single_event(
    payload: dict[str, Any],
    source: str,
) -> dict[str, Any]:

    (
        device_id,
        peak,
        average,
    ) = validate_sensor_values(
        payload
    )

    try:

        duration_ms = int(
            payload["duration_ms"]
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:

        raise ValueError(
            "duration_ms must be an integer"
        ) from exc

    if not (
        20
        <= duration_ms
        <= MAX_EVENT_MS
    ):
        raise ValueError(
            f"duration_ms must be between "
            f"20 and {MAX_EVENT_MS}"
        )

    received = now_utc()

    raw_started = (
        payload.get(
            "started_at"
        )
    )

    raw_ended = (
        payload.get(
            "ended_at"
        )
    )

    if raw_ended is not None:

        ended = parse_iso_datetime(
            raw_ended,
            "ended_at",
        )

    else:

        ended = received

    if raw_started is not None:

        started = parse_iso_datetime(
            raw_started,
            "started_at",
        )

    else:

        started = (
            ended
            - timedelta(
                milliseconds=duration_ms
            )
        )

    if started > ended:

        raise ValueError(
            "started_at must be <= ended_at"
        )

    if (
        raw_started is not None
        and raw_ended is not None
    ):

        time_source = "device"

    elif raw_ended is not None:

        time_source = (
            "device_end_plus_duration"
        )

    else:

        time_source = (
            "server_derived"
        )

    return insert_completed_event(
        received_at=iso_utc(
            received
        ),

        started_at=iso_utc(
            started
        ),

        ended_at=iso_utc(
            ended
        ),

        time_source=time_source,

        device_id=device_id,

        peak=peak,
        average=average,

        duration_ms=duration_ms,

        source=source,

        end_reason="single_event",
    )


# ============================================================
# Event reads
# ============================================================

def row_to_event(
    row: sqlite3.Row,
) -> dict[str, Any]:

    keys = set(
        row.keys()
    )

    received_at = (
        row["created_at"]
    )

    ended_at = (
        row["ended_at"]
        or received_at
    )

    if row["started_at"]:

        started_at = (
            row["started_at"]
        )

    else:

        started_at = iso_utc(
            parse_iso_datetime(
                ended_at,
                "ended_at",
            )
            - timedelta(
                milliseconds=(
                    row["duration_ms"]
                )
            )
        )

    event = {
        "id":
            row["id"],

        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "created_at":
            received_at,

        "received_at":
            received_at,

        "started_at":
            started_at,

        "ended_at":
            ended_at,

        "time_source":
            (
                row["time_source"]
                or "server_derived"
            ),

        "device_id":
            row["device_id"],

        "peak":
            row["peak"],

        "average":
            row["average"],

        "duration_ms":
            row["duration_ms"],

        "duration_s":
            round(
                row["duration_ms"]
                / 1000,
                2,
            ),

        "action":
            row["action"],

        "action_zh":
            action_zh(
                row["action"]
            ),

        "source":
            row["source"],

        "session_id":
            (
                row["session_id"]
                if "session_id" in keys
                else None
            ),

        "end_reason":
            (
                row["end_reason"]
                if "end_reason" in keys
                else None
            ),
    }

    event.update(
        local_time_fields(
            started_at,
            ended_at,
            received_at,
        )
    )

    return event


def get_event_by_session_id(
    session_id: str,
) -> dict[str, Any] | None:

    with db_connect() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM touch_events

            WHERE session_id = ?

            ORDER BY id DESC

            LIMIT 1
            """,
            (
                session_id,
            ),
        ).fetchone()

    return (
        row_to_event(row)
        if row
        else None
    )


# ============================================================
# Session reads
# ============================================================

def get_session_row(
    session_id: str,
) -> sqlite3.Row | None:

    with db_connect() as conn:

        return conn.execute(
            """
            SELECT *
            FROM hug_sessions

            WHERE session_id = ?

            LIMIT 1
            """,
            (
                session_id,
            ),
        ).fetchone()


def get_active_session_row_for_device(
    device_id: str,
) -> sqlite3.Row | None:

    with db_connect() as conn:

        return conn.execute(
            """
            SELECT *
            FROM hug_sessions

            WHERE
                device_id = ?
                AND status = 'active'

            ORDER BY
                last_heartbeat_at DESC,
                id DESC

            LIMIT 1
            """,
            (
                device_id,
            ),
        ).fetchone()


def session_row_to_dict(
    row: sqlite3.Row,
    now: datetime | None = None,
) -> dict[str, Any]:

    now = (
        now
        or now_utc()
    )

    started = (
        parse_iso_datetime(
            row["started_at"],
            "started_at",
        )
    )

    heartbeat = (
        parse_iso_datetime(
            row["last_heartbeat_at"],
            "last_heartbeat_at",
        )
    )

    if row["ended_at"]:

        ended = parse_iso_datetime(
            row["ended_at"],
            "ended_at",
        )

    else:

        ended = None

    reference = (
        ended
        or now
    )

    elapsed_s = max(
        0.0,
        (
            reference
            - started
        ).total_seconds(),
    )

    heartbeat_age_s = max(
        0.0,
        (
            now
            - heartbeat
        ).total_seconds(),
    )

    if row["sample_count"] > 0:

        average = round(
            row["average_sum"]
            / row["sample_count"]
        )

    else:

        average = (
            row["last_average"]
        )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "session_id":
            row["session_id"],

        "device_id":
            row["device_id"],

        "status":
            row["status"],

        "end_reason":
            row["end_reason"],

        "started_at":
            row["started_at"],

        "last_heartbeat_at":
            row["last_heartbeat_at"],

        "ended_at":
            row["ended_at"],

        "started_at_text":
            started.astimezone(
                LOCAL_TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "last_heartbeat_at_text":
            heartbeat.astimezone(
                LOCAL_TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "elapsed_seconds":
            round(
                elapsed_s,
                1,
            ),

        "elapsed_minutes":
            round(
                elapsed_s / 60,
                2,
            ),

        "peak_max":
            row["peak_max"],

        "average":
            average,

        "last_peak":
            row["last_peak"],

        "last_average":
            row["last_average"],

        "sample_count":
            row["sample_count"],

        "source":
            row["source"],

        "time_source":
            row["time_source"],

        "heartbeat_age_seconds":
            round(
                heartbeat_age_s,
                1,
            ),

        "heartbeat_recommended_seconds":
            HEARTBEAT_RECOMMENDED_SECONDS,

        "heartbeat_timeout_seconds":
            HEARTBEAT_TIMEOUT_SECONDS,

        "heartbeat_healthy":
            (
                heartbeat_age_s
                <= HEARTBEAT_TIMEOUT_SECONDS
            ),
    }


def observed_session_ms(
    row: sqlite3.Row,
) -> int:

    started = (
        parse_iso_datetime(
            row["started_at"],
            "started_at",
        )
    )

    heartbeat = (
        parse_iso_datetime(
            row["last_heartbeat_at"],
            "last_heartbeat_at",
        )
    )

    return max(
        0,
        int(
            (
                heartbeat
                - started
            ).total_seconds()
            * 1000
        ),
    )


# ============================================================
# Cancel without fake event
# ============================================================

def cancel_session_without_event(
    session_id: str,
    reason: str,
    ended_at: datetime | None = None,
) -> None:

    row = get_session_row(
        session_id
    )

    if (
        not row
        or row["status"] != "active"
    ):
        return

    end = (
        ended_at
        or parse_iso_datetime(
            row["last_heartbeat_at"],
            "last_heartbeat_at",
        )
    )

    stamp = iso_utc(
        now_utc()
    )

    with db_connect() as conn:

        conn.execute(
            """
            UPDATE hug_sessions

            SET
                status = 'cancelled',
                ended_at = ?,
                end_reason = ?,
                updated_at = ?

            WHERE
                session_id = ?
                AND status = 'active'
            """,
            (
                iso_utc(end),
                reason,
                stamp,
                session_id,
            ),
        )


# ============================================================
# Finish session
# ============================================================

def finalize_hug_session(
    session_id: str,
    ended_at: datetime | None = None,
    end_reason: str = "released",
) -> dict[str, Any]:

    session_id = (
        validate_session_id(
            session_id
        )
    )

    existing_event = (
        get_event_by_session_id(
            session_id
        )
    )

    if existing_event:
        return existing_event

    row = get_session_row(
        session_id
    )

    if not row:
        raise LookupError(
            "session not found"
        )

    if row["status"] == "cancelled":
        raise LookupError(
            "session was cancelled "
            "without a completed hug event"
        )

    started = (
        parse_iso_datetime(
            row["started_at"],
            "started_at",
        )
    )

    final = (
        ended_at
        or now_utc()
    )

    if final < started:
        final = started

    max_end = (
        started
        + timedelta(
            milliseconds=MAX_EVENT_MS
        )
    )

    if final > max_end:

        final = max_end

        end_reason = (
            "max_duration"
        )

    duration_ms = max(
        20,
        int(
            (
                final
                - started
            ).total_seconds()
            * 1000
        ),
    )

    if row["sample_count"] > 0:

        average = round(
            row["average_sum"]
            / row["sample_count"]
        )

    else:

        average = (
            row["last_average"]
        )

    received = iso_utc(
        now_utc()
    )

    ended_text = iso_utc(
        final
    )

    with db_connect() as conn:

        conn.execute(
            """
            UPDATE hug_sessions

            SET
                ended_at = ?,
                status = 'ended',
                end_reason = ?,
                updated_at = ?

            WHERE session_id = ?
            """,
            (
                ended_text,
                end_reason,
                received,
                session_id,
            ),
        )

    return insert_completed_event(
        received_at=received,

        started_at=(
            row["started_at"]
        ),

        ended_at=ended_text,

        time_source=(
            row["time_source"]
        ),

        device_id=(
            row["device_id"]
        ),

        peak=(
            row["peak_max"]
        ),

        average=average,

        duration_ms=duration_ms,

        source=(
            row["source"]
        ),

        session_id=session_id,

        end_reason=end_reason,
    )


# ============================================================
# Maintenance
# ============================================================

def cleanup_duplicate_active_sessions() -> int:
    """
    Keep only the freshest active session
    for each device.

    Older duplicates are cancelled without
    generating fake touch records.
    """

    cancelled = 0

    with db_connect() as conn:

        devices = conn.execute(
            """
            SELECT device_id

            FROM hug_sessions

            WHERE status = 'active'

            GROUP BY device_id

            HAVING COUNT(*) > 1
            """
        ).fetchall()

    for device in devices:

        with db_connect() as conn:

            rows = conn.execute(
                """
                SELECT *
                FROM hug_sessions

                WHERE
                    device_id = ?
                    AND status = 'active'

                ORDER BY
                    last_heartbeat_at DESC,
                    updated_at DESC,
                    id DESC
                """,
                (
                    device["device_id"],
                ),
            ).fetchall()

        for row in rows[1:]:

            cancel_session_without_event(
                row["session_id"],

                "superseded_duplicate",

                parse_iso_datetime(
                    row["last_heartbeat_at"],
                    "last_heartbeat_at",
                ),
            )

            cancelled += 1

    return cancelled


def expire_stale_sessions(
) -> dict[str, int]:
    """
    Confirmed hugs are closed at the last
    heartbeat.

    An unconfirmed START with no real
    heartbeat is simply abandoned, so it
    does not become a fake tap event.
    """

    cutoff = iso_utc(
        now_utc()
        - timedelta(
            seconds=(
                HEARTBEAT_TIMEOUT_SECONDS
            )
        )
    )

    with db_connect() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM hug_sessions

            WHERE
                status = 'active'
                AND last_heartbeat_at < ?
            """,
            (
                cutoff,
            ),
        ).fetchall()

    finalized = 0
    abandoned = 0

    for row in rows:

        last_heartbeat = (
            parse_iso_datetime(
                row["last_heartbeat_at"],
                "last_heartbeat_at",
            )
        )

        confirmed_ms = (
            observed_session_ms(
                row
            )
        )

        try:

            if (
                row["sample_count"] <= 1
                or confirmed_ms < HUG_MIN_MS
            ):

                cancel_session_without_event(
                    row["session_id"],

                    "heartbeat_timeout_unconfirmed",

                    last_heartbeat,
                )

                abandoned += 1

            else:

                finalize_hug_session(
                    row["session_id"],

                    ended_at=(
                        last_heartbeat
                    ),

                    end_reason=(
                        "heartbeat_timeout"
                    ),
                )

                finalized += 1

        except Exception:

            pass

    return {
        "finalized":
            finalized,

        "abandoned":
            abandoned,
    }


def run_maintenance(
) -> dict[str, int]:

    duplicates = (
        cleanup_duplicate_active_sessions()
    )

    stale = (
        expire_stale_sessions()
    )

    return {
        "duplicates_cancelled":
            duplicates,

        "stale_finalized":
            stale["finalized"],

        "stale_abandoned":
            stale["abandoned"],
    }


def ensure_one_active_session_index(
) -> None:
    """
    Database-level guard:
    even concurrent requests cannot create
    two active sessions for one device.
    """

    with db_connect() as conn:

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_one_active_session_per_device

            ON hug_sessions(device_id)

            WHERE status = 'active'
            """
        )


# ============================================================
# START
# ============================================================

def start_hug_session(
    payload: dict[str, Any],
) -> dict[str, Any]:

    run_maintenance()

    (
        device_id,
        peak,
        average,
    ) = validate_sensor_values(
        payload
    )

    raw_session_id = (
        payload.get(
            "session_id"
        )
    )

    if raw_session_id:

        session_id = (
            validate_session_id(
                raw_session_id
            )
        )

    else:

        session_id = (
            uuid.uuid4().hex
        )

    # Exact same session ID:
    # safe network retry.
    same = get_session_row(
        session_id
    )

    if same:

        result = (
            session_row_to_dict(
                same
            )
        )

        result[
            "start_result"
        ] = "idempotent_retry"

        return result

    # One active hug per physical device.
    active = (
        get_active_session_row_for_device(
            device_id
        )
    )

    if active:

        heartbeat = (
            parse_iso_datetime(
                active[
                    "last_heartbeat_at"
                ],
                "last_heartbeat_at",
            )
        )

        age_s = max(
            0.0,
            (
                now_utc()
                - heartbeat
            ).total_seconds(),
        )

        # Very recent second START:
        # double click or network retry.
        if (
            age_s
            <= DUPLICATE_START_WINDOW_SECONDS
        ):

            result = (
                session_row_to_dict(
                    active
                )
            )

            result[
                "start_result"
            ] = "reused_active_session"

            result[
                "requested_session_id"
            ] = session_id

            return result

        # Old session went quiet.
        # Do not merge two separate hugs.
        if (
            active["sample_count"] > 1
            and
            observed_session_ms(
                active
            ) >= HUG_MIN_MS
        ):

            try:

                finalize_hug_session(
                    active[
                        "session_id"
                    ],

                    ended_at=heartbeat,

                    end_reason=(
                        "superseded_by_new_start"
                    ),
                )

            except Exception:

                cancel_session_without_event(
                    active[
                        "session_id"
                    ],

                    "superseded_by_new_start",

                    heartbeat,
                )

        else:

            cancel_session_without_event(
                active[
                    "session_id"
                ],

                "superseded_unconfirmed_start",

                heartbeat,
            )

    received = now_utc()

    raw_started = (
        payload.get(
            "started_at"
        )
    )

    if raw_started is not None:

        started = (
            parse_iso_datetime(
                raw_started,
                "started_at",
            )
        )

        time_source = "device"

    else:

        started = received

        time_source = "server"

    if (
        started
        >
        received
        + timedelta(
            minutes=5
        )
    ):
        raise ValueError(
            "started_at is too far "
            "in the future"
        )

    source = str(
        payload.get(
            "source",
            "device_session",
        )
    )[:40]

    started_text = iso_utc(
        started
    )

    received_text = iso_utc(
        received
    )

    try:

        with db_connect() as conn:

            conn.execute(
                """
                INSERT INTO hug_sessions (
                    session_id,
                    device_id,

                    started_at,
                    last_heartbeat_at,
                    ended_at,

                    status,
                    end_reason,

                    peak_max,
                    average_sum,
                    sample_count,

                    last_peak,
                    last_average,

                    source,
                    time_source,

                    created_at,
                    updated_at
                )

                VALUES (
                    ?, ?,
                    ?, ?,
                    NULL,

                    'active',
                    NULL,

                    ?, ?,
                    1,

                    ?, ?,

                    ?, ?,

                    ?, ?
                )
                """,
                (
                    session_id,
                    device_id,

                    started_text,
                    received_text,

                    peak,
                    average,

                    peak,
                    average,

                    source,
                    time_source,

                    received_text,
                    received_text,
                ),
            )

    except sqlite3.IntegrityError:

        # Another concurrent START
        # may have won the race.
        active = (
            get_active_session_row_for_device(
                device_id
            )
        )

        if active:

            result = (
                session_row_to_dict(
                    active
                )
            )

            result[
                "start_result"
            ] = (
                "race_reused_active_session"
            )

            result[
                "requested_session_id"
            ] = session_id

            return result

        raise

    row = get_session_row(
        session_id
    )

    result = (
        session_row_to_dict(
            row
        )
    )

    result[
        "start_result"
    ] = "created"

    return result


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat_hug_session(
    payload: dict[str, Any],
) -> dict[str, Any]:

    run_maintenance()

    session_id = (
        validate_session_id(
            payload.get(
                "session_id"
            )
        )
    )

    (
        _,
        peak,
        average,
    ) = validate_sensor_values(
        payload
    )

    row = get_session_row(
        session_id
    )

    if not row:
        raise LookupError(
            "session not found"
        )

    if (
        row["status"]
        != "active"
    ):
        return (
            session_row_to_dict(
                row
            )
        )

    now_text = iso_utc(
        now_utc()
    )

    with db_connect() as conn:

        conn.execute(
            """
            UPDATE hug_sessions

            SET
                last_heartbeat_at = ?,

                peak_max =
                    MAX(
                        peak_max,
                        ?
                    ),

                average_sum =
                    average_sum
                    + ?,

                sample_count =
                    sample_count
                    + 1,

                last_peak = ?,
                last_average = ?,

                updated_at = ?

            WHERE
                session_id = ?
                AND status = 'active'
            """,
            (
                now_text,

                peak,
                average,

                peak,
                average,

                now_text,

                session_id,
            ),
        )

    return session_row_to_dict(
        get_session_row(
            session_id
        )
    )


# ============================================================
# END
# ============================================================

def end_hug_session(
    payload: dict[str, Any],
) -> dict[str, Any]:

    session_id = (
        validate_session_id(
            payload.get(
                "session_id"
            )
        )
    )

    raw_ended = (
        payload.get(
            "ended_at"
        )
    )

    if raw_ended is not None:

        ended_at = (
            parse_iso_datetime(
                raw_ended,
                "ended_at",
            )
        )

    else:

        ended_at = None

    return finalize_hug_session(
        session_id,

        ended_at=ended_at,

        end_reason="released",
    )


# ============================================================
# Reads
# ============================================================

def get_active_sessions(
) -> list[dict[str, Any]]:

    run_maintenance()

    now = now_utc()

    with db_connect() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM hug_sessions

            WHERE status = 'active'

            ORDER BY started_at DESC
            """
        ).fetchall()

    return [
        session_row_to_dict(
            row,
            now,
        )
        for row in rows
    ]


def get_latest_event(
) -> dict[str, Any] | None:

    run_maintenance()

    with db_connect() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM touch_events

            ORDER BY id DESC

            LIMIT 1
            """
        ).fetchone()

    return (
        row_to_event(row)
        if row
        else None
    )


def get_recent_events(
    limit: int = 10,
) -> list[dict[str, Any]]:

    run_maintenance()

    limit = max(
        1,
        min(
            int(limit),
            100,
        ),
    )

    with db_connect() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM touch_events

            ORDER BY id DESC

            LIMIT ?
            """,
            (
                limit,
            ),
        ).fetchall()

    return [
        row_to_event(
            row
        )
        for row in rows
    ]


def get_summary(
    hours: int = 24,
) -> dict[str, Any]:

    run_maintenance()

    hours = max(
        1,
        min(
            int(hours),
            24 * 30,
        ),
    )

    since = iso_utc(
        now_utc()
        - timedelta(
            hours=hours
        )
    )

    with db_connect() as conn:

        rows = conn.execute(
            """
            SELECT
                action,
                COUNT(*) AS n

            FROM touch_events

            WHERE
                COALESCE(
                    ended_at,
                    created_at
                ) >= ?

            GROUP BY action
            """,
            (
                since,
            ),
        ).fetchall()

        latest = conn.execute(
            """
            SELECT *
            FROM touch_events

            WHERE
                COALESCE(
                    ended_at,
                    created_at
                ) >= ?

            ORDER BY id DESC

            LIMIT 1
            """,
            (
                since,
            ),
        ).fetchone()

    counts = {
        row["action"]:
            row["n"]

        for row in rows
    }

    hug_actions = (
        "hug",
        "tight_hug",
        "long_hug",
        "sleep_hug",
    )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "window_hours":
            hours,

        "total_events":
            sum(
                counts.values()
            ),

        "hug_count":
            sum(
                counts.get(
                    action,
                    0,
                )
                for action
                in hug_actions
            ),

        "counts":
            counts,

        "active_hugs":
            get_active_sessions(),

        "latest":
            (
                row_to_event(
                    latest
                )
                if latest
                else None
            ),
    }


# ============================================================
# Startup
# ============================================================

init_db()

# First clean leftovers from old v4.
STARTUP_MAINTENANCE = (
    run_maintenance()
)

# After legacy duplicates are cleaned,
# add the hard SQLite guard.
ensure_one_active_session_index()


# ============================================================
# MCP
# ============================================================

mcp = MCPServer(
    "hug-bear-touch",

    instructions=(
        "Read-only touch history from a "
        "single-sensor hug plush. "

        "Long-running hugs use "
        "start/heartbeat/end sessions. "

        "Use hug_summary to see both "
        "completed events and active_hugs. "

        "Use current_hug_state if exposed "
        "by the client. "

        f"Server build: {BUILD_VERSION}."
    ),
)


@mcp.tool()
def latest_touch(
) -> dict[str, Any]:

    event = get_latest_event()

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "found":
            event is not None,

        "event":
            event,
    }


@mcp.tool()
def recent_touches(
    limit: int = 10,
) -> dict[str, Any]:

    events = (
        get_recent_events(
            limit
        )
    )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "count":
            len(events),

        "events":
            events,
    }


@mcp.tool()
def hug_summary(
    hours: int = 24,
) -> dict[str, Any]:

    return get_summary(
        hours
    )


@mcp.tool()
def was_hugged_recently(
    minutes: int = 30,
) -> dict[str, Any]:

    run_maintenance()

    minutes = max(
        1,
        min(
            int(minutes),
            7 * 24 * 60,
        ),
    )

    since = iso_utc(
        now_utc()
        - timedelta(
            minutes=minutes
        )
    )

    with db_connect() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM touch_events

            WHERE
                COALESCE(
                    ended_at,
                    created_at
                ) >= ?

                AND action IN (
                    'hug',
                    'tight_hug',
                    'long_hug',
                    'sleep_hug'
                )

            ORDER BY id DESC

            LIMIT 1
            """,
            (
                since,
            ),
        ).fetchone()

    active = (
        get_active_sessions()
    )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "hugged":
            (
                bool(row)
                or bool(active)
            ),

        "window_minutes":
            minutes,

        "active_hugs":
            active,

        "event":
            (
                row_to_event(
                    row
                )
                if row
                else None
            ),
    }


@mcp.tool()
def current_hug_state(
) -> dict[str, Any]:

    active = (
        get_active_sessions()
    )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "is_hugging_now":
            bool(active),

        "active_count":
            len(active),

        "active_hugs":
            active,

        "heartbeat_recommended_seconds":
            HEARTBEAT_RECOMMENDED_SECONDS,

        "heartbeat_timeout_seconds":
            HEARTBEAT_TIMEOUT_SECONDS,
    }


# ============================================================
# HTTP helpers
# ============================================================

async def read_json(
    request: Request,
) -> dict[str, Any]:

    try:

        return (
            await request.json()
        )

    except json.JSONDecodeError as exc:

        raise ValueError(
            "invalid JSON body"
        ) from exc


# ============================================================
# Health / version
# ============================================================

@mcp.custom_route(
    "/health",
    methods=["GET"],
)
async def health(
    _: Request,
) -> Response:

    return JSONResponse(
        {
            "ok":
                True,

            "service":
                APP_NAME,

            "server_version":
                BUILD_VERSION,

            "schema_version":
                SCHEMA_VERSION,

            "db":
                str(DB_PATH),

            "timezone":
                LOCAL_TIMEZONE,

            "active_hugs":
                len(
                    get_active_sessions()
                ),

            "heartbeat_recommended_seconds":
                HEARTBEAT_RECOMMENDED_SECONDS,

            "heartbeat_timeout_seconds":
                HEARTBEAT_TIMEOUT_SECONDS,

            "duplicate_start_window_seconds":
                DUPLICATE_START_WINDOW_SECONDS,
        }
    )


@mcp.custom_route(
    "/version",
    methods=["GET"],
)
async def version(
    _: Request,
) -> Response:

    return JSONResponse(
        {
            "ok":
                True,

            "service":
                APP_NAME,

            "server_version":
                BUILD_VERSION,

            "schema_version":
                SCHEMA_VERSION,

            "startup_maintenance":
                STARTUP_MAINTENANCE,

            "features": [
                (
                    "one active session per device "
                    "(server + SQLite unique guard)"
                ),

                "duplicate START retry protection",

                (
                    "stale unconfirmed START is "
                    "cancelled without fake touch event"
                ),

                (
                    "confirmed stale hug closes "
                    "at last heartbeat"
                ),

                "long hug / sleep hug classification",

                "24-hour session cap",

                "automatic SQLite migration",
            ],
        }
    )


# ============================================================
# Home
# ============================================================

@mcp.custom_route(
    "/",
    methods=["GET"],
)
async def home(
    _: Request,
) -> Response:

    return HTMLResponse(
        f"""
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
    Hug Bear Touch
</title>

</head>

<body
    style="
        font-family:system-ui;
        max-width:720px;
        margin:40px auto;
        padding:0 18px;
        line-height:1.7;
    "
>

<h1>
    🧸 Hug Bear Touch
</h1>

<p>
    <b>在线。</b>
    版本：{BUILD_VERSION}
</p>

<p>
    已支持：
    长抱、睡眠抱抱、心跳、
    重复 START 防护、断线兜底。
</p>

<p>
    <a href="/test">
        模拟测试
    </a>

    ·

    <a href="/dashboard">
        记录
    </a>

    ·

    <a href="/health">
        health
    </a>

    ·

    <a href="/version">
        version
    </a>
</p>

</body>

</html>
        """
    )


# ============================================================
# Old device route
# ============================================================

@mcp.custom_route(
    "/api/touch",
    methods=["POST"],
)
async def api_touch(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-device-token",
        DEVICE_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    try:

        event = (
            insert_single_event(
                await read_json(
                    request
                ),
                "device",
            )
        )

        return JSONResponse(
            {
                "ok": True,
                "event": event,
            }
        )

    except ValueError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# Simulator
# ============================================================

@mcp.custom_route(
    "/api/simulate",
    methods=["POST"],
)
async def api_simulate(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-device-token",
        DEVICE_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    try:

        event = (
            insert_single_event(
                await read_json(
                    request
                ),
                "simulate",
            )
        )

        return JSONResponse(
            {
                "ok": True,
                "event": event,
            }
        )

    except ValueError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# START API
# ============================================================

@mcp.custom_route(
    "/api/hug/start",
    methods=["POST"],
)
async def api_hug_start(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-device-token",
        DEVICE_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    try:

        session = (
            start_hug_session(
                await read_json(
                    request
                )
            )
        )

        return JSONResponse(
            {
                "ok": True,
                "session": session,
            }
        )

    except ValueError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# HEARTBEAT API
# ============================================================

@mcp.custom_route(
    "/api/hug/heartbeat",
    methods=["POST"],
)
async def api_hug_heartbeat(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-device-token",
        DEVICE_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    try:

        session = (
            heartbeat_hug_session(
                await read_json(
                    request
                )
            )
        )

        return JSONResponse(
            {
                "ok": True,
                "session": session,
            }
        )

    except ValueError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )

    except LookupError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=404,
        )


# ============================================================
# END API
# ============================================================

@mcp.custom_route(
    "/api/hug/end",
    methods=["POST"],
)
async def api_hug_end(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-device-token",
        DEVICE_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    try:

        event = (
            end_hug_session(
                await read_json(
                    request
                )
            )
        )

        return JSONResponse(
            {
                "ok": True,
                "event": event,
            }
        )

    except ValueError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )

    except LookupError as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=404,
        )


# ============================================================
# Read APIs
# ============================================================

@mcp.custom_route(
    "/api/latest",
    methods=["GET"],
)
async def api_latest(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-mcp-token",
        MCP_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    return JSONResponse(
        {
            "ok":
                True,

            "server_version":
                BUILD_VERSION,

            "schema_version":
                SCHEMA_VERSION,

            "event":
                get_latest_event(),
        }
    )


@mcp.custom_route(
    "/api/history",
    methods=["GET"],
)
async def api_history(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-mcp-token",
        MCP_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    try:

        limit = int(
            request.query_params.get(
                "limit",
                "20",
            )
        )

    except ValueError:

        limit = 20

    return JSONResponse(
        {
            "ok":
                True,

            "server_version":
                BUILD_VERSION,

            "schema_version":
                SCHEMA_VERSION,

            "active_hugs":
                get_active_sessions(),

            "events":
                get_recent_events(
                    limit
                ),
        }
    )


@mcp.custom_route(
    "/api/current",
    methods=["GET"],
)
async def api_current(
    request: Request,
) -> Response:

    if not token_ok(
        request,
        "x-mcp-token",
        MCP_TOKEN,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "unauthorized",
            },
            status_code=401,
        )

    active = (
        get_active_sessions()
    )

    return JSONResponse(
        {
            "ok":
                True,

            "server_version":
                BUILD_VERSION,

            "schema_version":
                SCHEMA_VERSION,

            "is_hugging_now":
                bool(active),

            "active_hugs":
                active,
        }
    )


# ============================================================
# Test page
# ============================================================

@mcp.custom_route(
    "/test",
    methods=["GET"],
)
async def test_page(
    _: Request,
) -> Response:

    return HTMLResponse(
        f"""
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
    模拟抱抱
</title>

<style>

body {{
    font-family: system-ui;
    max-width: 680px;
    margin: 30px auto;
    padding: 0 18px;
}}

input,
button {{
    font: inherit;
    padding: 10px;
    border-radius: 9px;
    border: 1px solid #bbb;
}}

input {{
    width: 100%;
    box-sizing: border-box;
}}

button {{
    margin: 4px 4px 4px 0;
}}

pre {{
    white-space: pre-wrap;
    word-break: break-word;
    background: #f4f4f4;
    padding: 12px;
}}

</style>

</head>

<body>

<h1>
    🧸 模拟抱抱
</h1>

<p>
    版本：{BUILD_VERSION}
</p>

<p>
    DEVICE_TOKEN
</p>

<input
    id="token"
    type="password"
    autocomplete="off"
>

<p>
    峰值 pressure
</p>

<input
    id="peak"
    type="number"
    value="3400"
>

<p>
    单次持续 ms
</p>

<input
    id="duration"
    type="number"
    value="6000"
>

<p>

<button
    id="startBtn"
    onclick="startHug()"
>
    开始长抱
</button>

<button onclick="heartbeat()">
    发送心跳
</button>

<button onclick="endHug()">
    松开并结算
</button>

<button onclick="single()">
    发送单次
</button>

</p>

<p>
    当前 session：
    <code id="sid">
        无
    </code>
</p>

<pre id="out">
等待操作…
</pre>

<script>

let sessionId = null;


async function post(
    url,
    body
) {{

    const response =
        await fetch(
            url,
            {{
                method:
                    "POST",

                headers: {{
                    "Content-Type":
                        "application/json",

                    "X-Device-Token":
                        token.value
                }},

                body:
                    JSON.stringify(
                        body
                    )
            }}
        );

    const data =
        await response.json();

    out.textContent =
        JSON.stringify(
            data,
            null,
            2
        );

    return data;
}}


function p() {{

    return Number(
        peak.value
    );
}}


function avg() {{

    return Math.round(
        p() * 0.82
    );
}}


async function single() {{

    await post(
        "/api/simulate",
        {{
            device_id:
                "simulator",

            peak:
                p(),

            average:
                avg(),

            duration_ms:
                Number(
                    duration.value
                )
        }}
    );
}}


async function startHug() {{

    if (sessionId) {{

        out.textContent =
            "当前页面已经有活动 session，"
            + "不会重复开始。";

        return;
    }}

    const requested =
        crypto
        .randomUUID()
        .replaceAll(
            "-",
            ""
        );

    const data =
        await post(
            "/api/hug/start",
            {{
                device_id:
                    "simulator",

                session_id:
                    requested,

                source:
                    "simulate_session",

                peak:
                    p(),

                average:
                    avg()
            }}
        );

    if (data.ok) {{

        // 关键：
        // 如果服务器发现这是重复 START，
        // 会返回原来的 session_id。
        sessionId =
            data.session.session_id;

        sid.textContent =
            sessionId;

        startBtn.disabled =
            true;
    }}
}}


async function heartbeat() {{

    if (!sessionId) {{

        out.textContent =
            "请先点“开始长抱”。";

        return;
    }}

    await post(
        "/api/hug/heartbeat",
        {{
            device_id:
                "simulator",

            session_id:
                sessionId,

            peak:
                p(),

            average:
                avg()
        }}
    );
}}


async function endHug() {{

    if (!sessionId) {{

        out.textContent =
            "当前页面没有活动 session。";

        return;
    }}

    const data =
        await post(
            "/api/hug/end",
            {{
                session_id:
                    sessionId
            }}
        );

    if (data.ok) {{

        sessionId =
            null;

        sid.textContent =
            "无";

        startBtn.disabled =
            false;
    }}
}}

</script>

</body>

</html>
        """
    )


# ============================================================
# Dashboard
# ============================================================

@mcp.custom_route(
    "/dashboard",
    methods=["GET"],
)
async def dashboard(
    _: Request,
) -> Response:

    return HTMLResponse(
        f"""
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
    抱抱记录
</title>

<style>

body {{
    font-family: system-ui;
    max-width: 760px;
    margin: 30px auto;
    padding: 0 18px;
}}

input,
button {{
    font: inherit;
    padding: 10px;
    border-radius: 9px;
    border: 1px solid #bbb;
}}

input {{
    width: 100%;
    box-sizing: border-box;
}}

pre {{
    white-space: pre-wrap;
    word-break: break-word;
    background: #f4f4f4;
    padding: 12px;
}}

</style>

</head>

<body>

<h1>
    🧸 抱抱记录
</h1>

<p>
    版本：{BUILD_VERSION}
</p>

<input
    id="token"
    type="password"
    placeholder="MCP_TOKEN"
    autocomplete="off"
>

<p>

<button onclick="loadData()">
    读取
</button>

</p>

<pre id="out">
尚未读取
</pre>

<script>

async function loadData() {{

    const headers = {{
        "X-MCP-Token":
            token.value
    }};

    const [
        currentResponse,
        historyResponse
    ] =
        await Promise.all(
            [
                fetch(
                    "/api/current",
                    {{
                        headers
                    }}
                ),

                fetch(
                    "/api/history?limit=20",
                    {{
                        headers
                    }}
                )
            ]
        );

    out.textContent =
        JSON.stringify(
            {{
                current_hug_state:
                    await currentResponse.json(),

                history:
                    await historyResponse.json()
            }},
            null,
            2
        );
}}

</script>

</body>

</html>
        """
    )


# ============================================================
# MCP HTTP app
# ============================================================

security = (
    TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
)

app = (
    mcp.streamable_http_app(
        streamable_http_path=(
            f"/mcp/{MCP_TOKEN}"
        ),

        json_response=True,

        stateless_http=True,

        transport_security=security,

        host="0.0.0.0",
    )
)


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,

        host="0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                "8080",
            )
        ),
    )
