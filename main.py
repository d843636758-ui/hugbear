from __future__ import annotations

import json
import os
import sqlite3
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
BUILD_VERSION = "2026-08-18-time-v3"
SCHEMA_VERSION = 3

DB_PATH = Path(os.getenv("DATA_DIR", "/data")) / "touch.db"
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "")
MCP_TOKEN = os.getenv("MCP_TOKEN", "")

LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "Asia/Shanghai")
LOCAL_TZ = ZoneInfo(LOCAL_TIMEZONE)

TAP_MAX_MS = int(os.getenv("TAP_MAX_MS", "400"))
HUG_MIN_MS = int(os.getenv("HUG_MIN_MS", "2500"))
TIGHT_HUG_PEAK = int(os.getenv("TIGHT_HUG_PEAK", "2800"))


# ============================================================
# Startup checks
# ============================================================

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
# Database
# ============================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS touch_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                time_source TEXT NOT NULL DEFAULT 'server_derived',
                device_id TEXT NOT NULL,
                peak INTEGER NOT NULL,
                average INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                action TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'device'
            )
            """
        )

        # 兼容旧版数据库：
        # 如果以前已经创建过 touch_events，
        # 自动补上新的时间字段，不删除旧记录。
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(touch_events)"
            ).fetchall()
        }

        if "started_at" not in columns:
            conn.execute(
                "ALTER TABLE touch_events "
                "ADD COLUMN started_at TEXT"
            )

        if "ended_at" not in columns:
            conn.execute(
                "ALTER TABLE touch_events "
                "ADD COLUMN ended_at TEXT"
            )

        if "time_source" not in columns:
            conn.execute(
                """
                ALTER TABLE touch_events
                ADD COLUMN time_source TEXT
                NOT NULL DEFAULT 'server_derived'
                """
            )

        # 老记录没有 ended_at 时，
        # 先把原 created_at 当成结束时间。
        conn.execute(
            """
            UPDATE touch_events
            SET ended_at = created_at
            WHERE ended_at IS NULL
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_touch_events_created_at
            ON touch_events(created_at DESC)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_touch_events_ended_at
            ON touch_events(ended_at DESC)
            """
        )


# ============================================================
# Touch classification
# ============================================================

def classify_touch(peak: int, duration_ms: int) -> str:
    if duration_ms < TAP_MAX_MS:
        return "tap"

    if duration_ms >= HUG_MIN_MS and peak >= TIGHT_HUG_PEAK:
        return "tight_hug"

    if duration_ms >= HUG_MIN_MS:
        return "hug"

    return "press"


def action_zh(action: str) -> str:
    return {
        "tap": "轻碰",
        "press": "按住",
        "hug": "抱住",
        "tight_hug": "紧紧抱住",
    }.get(action, action)


# ============================================================
# Time helpers
# ============================================================

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

    # 如果设备以后传来的时间没有时区，
    # 暂时按 UTC 解释。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def local_time_fields(
    started_at: str,
    ended_at: str,
    received_at: str,
) -> dict[str, Any]:
    start_dt = parse_iso_datetime(
        started_at,
        "started_at",
    )
    end_dt = parse_iso_datetime(
        ended_at,
        "ended_at",
    )
    received_dt = parse_iso_datetime(
        received_at,
        "received_at",
    )

    start_local = start_dt.astimezone(LOCAL_TZ)
    end_local = end_dt.astimezone(LOCAL_TZ)
    received_local = received_dt.astimezone(LOCAL_TZ)

    return {
        "timezone": LOCAL_TIMEZONE,

        "started_at_local":
            start_local.isoformat(),

        "ended_at_local":
            end_local.isoformat(),

        "received_at_local":
            received_local.isoformat(),

        "started_at_text":
            start_local.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "ended_at_text":
            end_local.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "received_at_text":
            received_local.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "time_range_text": (
            f"{start_local.strftime('%Y-%m-%d %H:%M:%S')}"
            f" → "
            f"{end_local.strftime('%H:%M:%S')}"
        ),
    }


def resolve_event_times(
    payload: dict[str, Any],
    duration_ms: int,
) -> tuple[str, str, str, str]:

    # Zeabur 真正收到这条数据的时刻。
    received_dt = datetime.now(timezone.utc)

    raw_started = payload.get("started_at")
    raw_ended = payload.get("ended_at")

    # 如果设备已经给结束时间，就相信设备。
    # 目前模拟器没给，则服务器收到时间视作松开时间。
    if raw_ended is not None:
        ended_dt = parse_iso_datetime(
            raw_ended,
            "ended_at",
        )
    else:
        ended_dt = received_dt

    # 如果设备已经给开始时间，就直接使用。
    # 否则根据结束时间 - 持续时长倒推。
    if raw_started is not None:
        started_dt = parse_iso_datetime(
            raw_started,
            "started_at",
        )
    else:
        started_dt = (
            ended_dt
            - timedelta(milliseconds=duration_ms)
        )

    if started_dt > ended_dt:
        raise ValueError(
            "started_at must be earlier than "
            "or equal to ended_at"
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
        time_source = "server_derived"

    return (
        iso_utc(received_dt),
        iso_utc(started_dt),
        iso_utc(ended_dt),
        time_source,
    )


# ============================================================
# Validation
# ============================================================

def validate_event(
    payload: dict[str, Any],
) -> tuple[str, int, int, int]:

    device_id = str(
        payload.get(
            "device_id",
            "hug-bear-01",
        )
    )[:64]

    try:
        peak = int(payload["peak"])

        average = int(
            payload.get(
                "average",
                peak,
            )
        )

        duration_ms = int(
            payload["duration_ms"]
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            "peak, average and duration_ms "
            "must be integers"
        ) from exc

    if not 0 <= peak <= 4095:
        raise ValueError(
            "peak must be between 0 and 4095"
        )

    if not 0 <= average <= 4095:
        raise ValueError(
            "average must be between 0 and 4095"
        )

    if not 20 <= duration_ms <= 600_000:
        raise ValueError(
            "duration_ms must be between "
            "20 and 600000"
        )

    return (
        device_id,
        peak,
        average,
        duration_ms,
    )


# ============================================================
# Event insert / serialization
# ============================================================

def insert_event(
    payload: dict[str, Any],
    source: str = "device",
) -> dict[str, Any]:

    (
        device_id,
        peak,
        average,
        duration_ms,
    ) = validate_event(payload)

    action = classify_touch(
        peak,
        duration_ms,
    )

    (
        received_at,
        started_at,
        ended_at,
        time_source,
    ) = resolve_event_times(
        payload,
        duration_ms,
    )

    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO touch_events
            (
                created_at,
                started_at,
                ended_at,
                time_source,
                device_id,
                peak,
                average,
                duration_ms,
                action,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )

        event_id = cur.lastrowid

    event = {
        "id": event_id,

        "schema_version":
            SCHEMA_VERSION,

        "server_version":
            BUILD_VERSION,

        # created_at 为兼容旧版本保留。
        # 它现在等价于 received_at。
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
            action_zh(action),

        "source":
            source,
    }

    event.update(
        local_time_fields(
            started_at,
            ended_at,
            received_at,
        )
    )

    return event


def row_to_event(
    row: sqlite3.Row,
) -> dict[str, Any]:

    keys = set(row.keys())

    received_at = row["created_at"]

    if (
        "ended_at" in keys
        and row["ended_at"]
    ):
        ended_at = row["ended_at"]
    else:
        ended_at = received_at

    if (
        "started_at" in keys
        and row["started_at"]
    ):
        started_at = row["started_at"]

    else:
        ended_dt = parse_iso_datetime(
            ended_at,
            "ended_at",
        )

        started_at = iso_utc(
            ended_dt
            - timedelta(
                milliseconds=row[
                    "duration_ms"
                ]
            )
        )

    if (
        "time_source" in keys
        and row["time_source"]
    ):
        time_source = row["time_source"]
    else:
        time_source = "server_derived"

    event = {
        "id":
            row["id"],

        "schema_version":
            SCHEMA_VERSION,

        "server_version":
            BUILD_VERSION,

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
            row["device_id"],

        "peak":
            row["peak"],

        "average":
            row["average"],

        "duration_ms":
            row["duration_ms"],

        "duration_s":
            round(
                row["duration_ms"] / 1000,
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
# Queries
# ============================================================

def get_latest_event() -> dict[str, Any] | None:
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
            (limit,),
        ).fetchall()

    return [
        row_to_event(row)
        for row in rows
    ]


def get_summary(
    hours: int = 24,
) -> dict[str, Any]:

    hours = max(
        1,
        min(
            int(hours),
            24 * 30,
        ),
    )

    since = (
        datetime.now(timezone.utc)
        - timedelta(hours=hours)
    ).isoformat()

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                action,
                COUNT(*) AS n,
                COALESCE(
                    MAX(peak),
                    0
                ) AS max_peak,
                COALESCE(
                    SUM(duration_ms),
                    0
                ) AS total_ms

            FROM touch_events

            WHERE
                COALESCE(
                    ended_at,
                    created_at
                ) >= ?

            GROUP BY action
            """,
            (since,),
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
            (since,),
        ).fetchone()

    counts = {
        row["action"]: row["n"]
        for row in rows
    }

    total = sum(
        counts.values()
    )

    hug_count = (
        counts.get("hug", 0)
        + counts.get(
            "tight_hug",
            0,
        )
    )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "window_hours":
            hours,

        "total_events":
            total,

        "hug_count":
            hug_count,

        "counts":
            counts,

        "latest":
            row_to_event(latest)
            if latest
            else None,
    }


# ============================================================
# Token helper
# ============================================================

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
# Initialize DB
# ============================================================

init_db()


# ============================================================
# MCP server
# ============================================================

mcp = MCPServer(
    "hug-bear-touch",

    instructions=(
        "Read-only touch history from a "
        "single-sensor hug plush. "

        "Use latest_touch for the newest event, "
        "recent_touches for a short timeline, "
        "hug_summary for counts, and "
        "was_hugged_recently for a simple "
        "recent-hug check. "

        "Each event includes started_at, "
        "ended_at, duration, and "
        "Asia/Shanghai local-time fields. "

        f"Server build: {BUILD_VERSION}."
    ),
)


# ============================================================
# MCP tools
# ============================================================

@mcp.tool()
def latest_touch() -> dict[str, Any]:
    """
    Return the most recent touch event
    recorded by the hug plush.
    """

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
def latest_touch_with_time() -> dict[str, Any]:
    """
    Return the newest touch with explicit
    start/end/local-time fields.

    Use this to verify time-aware v3.
    """

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
    """
    Return the most recent touch events,
    newest first.

    Limit is 1 to 100.
    """

    events = get_recent_events(limit)

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
def recent_touches_with_time(
    limit: int = 10,
) -> dict[str, Any]:
    """
    Return recent touches with explicit
    start/end/local-time fields.

    Limit is 1 to 100.
    """

    events = get_recent_events(limit)

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
    """
    Summarize touch and hug activity
    in the last N hours.

    Hours is 1 to 720.
    """

    return get_summary(hours)


@mcp.tool()
def was_hugged_recently(
    minutes: int = 30,
) -> dict[str, Any]:
    """
    Check whether a hug or tight hug
    happened within the last N minutes.
    """

    minutes = max(
        1,
        min(
            int(minutes),
            7 * 24 * 60,
        ),
    )

    since = (
        datetime.now(timezone.utc)
        - timedelta(minutes=minutes)
    ).isoformat()

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
                    'tight_hug'
                )

            ORDER BY id DESC
            LIMIT 1
            """,
            (since,),
        ).fetchone()

    event = (
        row_to_event(row)
        if row
        else None
    )

    return {
        "server_version":
            BUILD_VERSION,

        "schema_version":
            SCHEMA_VERSION,

        "hugged":
            event is not None,

        "window_minutes":
            minutes,

        "event":
            event,
    }


# ============================================================
# HTTP routes
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

            "thresholds": {
                "tap_max_ms":
                    TAP_MAX_MS,

                "hug_min_ms":
                    HUG_MIN_MS,

                "tight_hug_peak":
                    TIGHT_HUG_PEAK,
            },
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

            "time_fields": [
                "started_at",
                "ended_at",
                "received_at",
                "started_at_text",
                "ended_at_text",
                "time_range_text",
            ],

            "mcp_time_tools": [
                "latest_touch_with_time",
                "recent_touches_with_time",
            ],
        }
    )


@mcp.custom_route(
    "/",
    methods=["GET"],
)
async def home(
    _: Request,
) -> Response:

    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">

    <meta
        name="viewport"
        content="width=device-width,initial-scale=1"
    >

    <title>Hug Bear Touch</title>

    <style>
        body {
            font-family:
                system-ui,
                -apple-system,
                sans-serif;

            max-width: 760px;
            margin: 40px auto;
            padding: 0 20px;
            line-height: 1.65;
        }

        code {
            background: #f2f2f2;
            padding: 2px 6px;
            border-radius: 6px;
        }

        .card {
            border: 1px solid #ddd;
            border-radius: 14px;
            padding: 18px;
            margin: 14px 0;
        }
    </style>
</head>

<body>

<h1>🧸 Hug Bear Touch</h1>

<div class="card">
    <b>云端神经系统已经在线。</b>
    <br>
    版本：2026-08-18-time-v3
    <br>
    硬件到货前，可以先用测试页模拟抱抱数据。
</div>

<p>
    <a href="/test">
        打开模拟测试页
    </a>
    ·
    <a href="/dashboard">
        打开记录查看页
    </a>
    ·
    <a href="/health">
        健康检查
    </a>
    ·
    <a href="/version">
        版本检查
    </a>
</p>

<p>
    MCP 地址格式：
    <code>
        https://你的域名/mcp/你的MCP_TOKEN
    </code>
</p>

</body>
</html>
        """
    )


# ============================================================
# Device API
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
        payload = await request.json()

        event = insert_event(
            payload,
            source="device",
        )

    except (
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )

    return JSONResponse(
        {
            "ok": True,
            "event": event,
        }
    )


# ============================================================
# Simulator API
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
        payload = await request.json()

        event = insert_event(
            payload,
            source="simulate",
        )

    except (
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )

    return JSONResponse(
        {
            "ok": True,
            "event": event,
        }
    )


# ============================================================
# Read API
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
            "ok": True,
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

    raw_limit = request.query_params.get(
        "limit",
        "20",
    )

    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 20

    return JSONResponse(
        {
            "ok": True,
            "server_version":
                BUILD_VERSION,
            "schema_version":
                SCHEMA_VERSION,
            "events":
                get_recent_events(limit),
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
        """
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

body {
    font-family:
        system-ui,
        -apple-system,
        sans-serif;

    max-width: 680px;
    margin: 34px auto;
    padding: 0 18px;
}

label {
    display: block;
    margin: 12px 0;
}

input,
button {
    font: inherit;
    padding: 10px;
    border-radius: 9px;
    border: 1px solid #bbb;
}

input {
    width: 100%;
    box-sizing: border-box;
}

button {
    cursor: pointer;
    margin-right: 8px;
    margin-bottom: 8px;
}

pre {
    white-space: pre-wrap;
    word-break: break-word;
    background: #f4f4f4;
    padding: 14px;
    border-radius: 10px;
}

</style>

</head>

<body>

<h1>
    🧸 模拟抱抱
</h1>

<p>
    DEVICE_TOKEN 只保存在这个网页当前输入框里，
    不会写进页面源码。
</p>

<p>
    发送成功后会返回：
    开始时间、结束时间、持续时长和东八区显示。
</p>

<label>
    DEVICE_TOKEN

    <input
        id="token"
        type="password"
        autocomplete="off"
    >
</label>

<label>
    峰值 pressure（0~4095）

    <input
        id="peak"
        type="number"
        value="2600"
    >
</label>

<label>
    持续时间 ms

    <input
        id="duration"
        type="number"
        value="5800"
    >
</label>

<button onclick="send()">
    发送模拟数据
</button>

<button onclick="preset(900,180)">
    轻碰
</button>

<button onclick="preset(1800,1200)">
    按住
</button>

<button onclick="preset(2200,5000)">
    抱住
</button>

<button onclick="preset(3400,6000)">
    紧紧抱住
</button>

<pre id="out">
等待发送…
</pre>

<script>

function preset(p, d) {
    peak.value = p;
    duration.value = d;
}


async function send() {

    const t = token.value;

    const p = Number(
        peak.value
    );

    const d = Number(
        duration.value
    );

    const response = await fetch(
        "/api/simulate",
        {
            method: "POST",

            headers: {
                "Content-Type":
                    "application/json",

                "X-Device-Token":
                    t,
            },

            body: JSON.stringify(
                {
                    device_id:
                        "simulator",

                    peak:
                        p,

                    average:
                        Math.round(
                            p * 0.82
                        ),

                    duration_ms:
                        d,
                }
            ),
        }
    );

    const data =
        await response.json();

    out.textContent =
        JSON.stringify(
            data,
            null,
            2
        );
}

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
        """
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

body {
    font-family:
        system-ui,
        -apple-system,
        sans-serif;

    max-width: 760px;
    margin: 34px auto;
    padding: 0 18px;
}

input,
button {
    font: inherit;
    padding: 10px;
    border-radius: 9px;
    border: 1px solid #bbb;
}

input {
    width: 100%;
    box-sizing: border-box;
    margin: 10px 0;
}

button {
    cursor: pointer;
}

pre {
    white-space: pre-wrap;
    word-break: break-word;
    background: #f4f4f4;
    padding: 14px;
    border-radius: 10px;
}

</style>

</head>

<body>

<h1>
    🧸 抱抱记录
</h1>

<p>
    输入 MCP_TOKEN 后读取最近 20 条。
    Token 只在当前页面内使用。
</p>

<input
    id="token"
    type="password"
    placeholder="MCP_TOKEN"
    autocomplete="off"
>

<button onclick="loadData()">
    读取
</button>

<pre id="out">
尚未读取
</pre>

<script>

async function loadData() {

    const response = await fetch(
        "/api/history?limit=20",
        {
            headers: {
                "X-MCP-Token":
                    token.value,
            },
        }
    );

    const data =
        await response.json();

    out.textContent =
        JSON.stringify(
            data,
            null,
            2
        );
}

</script>

</body>

</html>
        """
    )


# ============================================================
# MCP HTTP application
# ============================================================

# Zeabur 会在反向代理层终止 HTTPS，
# 所以这里允许由代理处理 Host / TLS。
security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

app = mcp.streamable_http_app(
    streamable_http_path=(
        f"/mcp/{MCP_TOKEN}"
    ),

    json_response=True,

    stateless_http=True,

    transport_security=security,

    host="0.0.0.0",
)


# ============================================================
# Start
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
