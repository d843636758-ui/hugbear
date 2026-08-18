from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

APP_NAME = "Hug Bear Touch"
DB_PATH = Path(os.getenv("DATA_DIR", "/data")) / "touch.db"
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "")
MCP_TOKEN = os.getenv("MCP_TOKEN", "")

TAP_MAX_MS = int(os.getenv("TAP_MAX_MS", "400"))
HUG_MIN_MS = int(os.getenv("HUG_MIN_MS", "2500"))
TIGHT_HUG_PEAK = int(os.getenv("TIGHT_HUG_PEAK", "2800"))

if not DEVICE_TOKEN or not MCP_TOKEN:
    raise RuntimeError(
        "Missing DEVICE_TOKEN or MCP_TOKEN. Set both in Zeabur > Variables before starting the service."
    )
if not (24 <= len(MCP_TOKEN) <= 128) or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in MCP_TOKEN):
    raise RuntimeError(
        "MCP_TOKEN must be 24-128 URL-safe characters: letters, numbers, _ or -."
    )

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


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
                device_id TEXT NOT NULL,
                peak INTEGER NOT NULL,
                average INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                action TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'device'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_touch_events_created_at ON touch_events(created_at DESC)"
        )


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


def validate_event(payload: dict[str, Any]) -> tuple[str, int, int, int]:
    device_id = str(payload.get("device_id", "hug-bear-01"))[:64]
    try:
        peak = int(payload["peak"])
        average = int(payload.get("average", peak))
        duration_ms = int(payload["duration_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("peak, average and duration_ms must be integers") from exc

    if not 0 <= peak <= 4095:
        raise ValueError("peak must be between 0 and 4095")
    if not 0 <= average <= 4095:
        raise ValueError("average must be between 0 and 4095")
    if not 20 <= duration_ms <= 600_000:
        raise ValueError("duration_ms must be between 20 and 600000")
    return device_id, peak, average, duration_ms


def insert_event(payload: dict[str, Any], source: str = "device") -> dict[str, Any]:
    device_id, peak, average, duration_ms = validate_event(payload)
    action = classify_touch(peak, duration_ms)
    created_at = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO touch_events
                (created_at, device_id, peak, average, duration_ms, action, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (created_at, device_id, peak, average, duration_ms, action, source),
        )
        event_id = cur.lastrowid

    return {
        "id": event_id,
        "created_at": created_at,
        "device_id": device_id,
        "peak": peak,
        "average": average,
        "duration_ms": duration_ms,
        "duration_s": round(duration_ms / 1000, 2),
        "action": action,
        "action_zh": action_zh(action),
        "source": source,
    }


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "device_id": row["device_id"],
        "peak": row["peak"],
        "average": row["average"],
        "duration_ms": row["duration_ms"],
        "duration_s": round(row["duration_ms"] / 1000, 2),
        "action": row["action"],
        "action_zh": action_zh(row["action"]),
        "source": row["source"],
    }


def get_latest_event() -> dict[str, Any] | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM touch_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row_to_event(row) if row else None


def get_recent_events(limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM touch_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [row_to_event(r) for r in rows]


def get_summary(hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(int(hours), 24 * 30))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT action, COUNT(*) AS n,
                   COALESCE(MAX(peak), 0) AS max_peak,
                   COALESCE(SUM(duration_ms), 0) AS total_ms
            FROM touch_events
            WHERE created_at >= ?
            GROUP BY action
            """,
            (since,),
        ).fetchall()
        latest = conn.execute(
            "SELECT * FROM touch_events WHERE created_at >= ? ORDER BY id DESC LIMIT 1",
            (since,),
        ).fetchone()

    counts = {r["action"]: r["n"] for r in rows}
    total = sum(counts.values())
    hug_count = counts.get("hug", 0) + counts.get("tight_hug", 0)
    return {
        "window_hours": hours,
        "total_events": total,
        "hug_count": hug_count,
        "counts": counts,
        "latest": row_to_event(latest) if latest else None,
    }


def token_ok(request: Request, header: str, expected: str) -> bool:
    return request.headers.get(header, "") == expected


init_db()

mcp = MCPServer(
    "hug-bear-touch",
    instructions=(
        "Read-only touch history from a single-sensor hug plush. "
        "Use latest_touch for the newest event, recent_touches for a short timeline, "
        "hug_summary for counts, and was_hugged_recently for a simple recent-hug check."
    ),
)


@mcp.tool()
def latest_touch() -> dict[str, Any]:
    """Return the most recent touch event recorded by the hug plush."""
    event = get_latest_event()
    return {"found": event is not None, "event": event}


@mcp.tool()
def recent_touches(limit: int = 10) -> dict[str, Any]:
    """Return the most recent touch events, newest first. Limit is 1 to 100."""
    events = get_recent_events(limit)
    return {"count": len(events), "events": events}


@mcp.tool()
def hug_summary(hours: int = 24) -> dict[str, Any]:
    """Summarize touch and hug activity in the last N hours (1 to 720)."""
    return get_summary(hours)


@mcp.tool()
def was_hugged_recently(minutes: int = 30) -> dict[str, Any]:
    """Check whether a hug or tight hug happened within the last N minutes."""
    minutes = max(1, min(int(minutes), 7 * 24 * 60))
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM touch_events
            WHERE created_at >= ? AND action IN ('hug', 'tight_hug')
            ORDER BY id DESC LIMIT 1
            """,
            (since,),
        ).fetchone()
    event = row_to_event(row) if row else None
    return {"hugged": event is not None, "window_minutes": minutes, "event": event}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": APP_NAME,
            "db": str(DB_PATH),
            "thresholds": {
                "tap_max_ms": TAP_MAX_MS,
                "hug_min_ms": HUG_MIN_MS,
                "tight_hug_peak": TIGHT_HUG_PEAK,
            },
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def home(_: Request) -> Response:
    return HTMLResponse(
        """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Hug Bear Touch</title>
        <style>body{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.65}code{background:#f2f2f2;padding:2px 6px;border-radius:6px}.card{border:1px solid #ddd;border-radius:14px;padding:18px;margin:14px 0}</style>
        </head><body><h1>🧸 Hug Bear Touch</h1>
        <div class='card'><b>云端神经系统已经在线。</b><br>硬件到货前，可以先用测试页模拟抱抱数据。</div>
        <p><a href='/test'>打开模拟测试页</a> · <a href='/dashboard'>打开记录查看页</a> · <a href='/health'>健康检查</a></p>
        <p>MCP 地址格式：<code>https://你的域名/mcp/你的MCP_TOKEN</code></p>
        </body></html>"""
    )


@mcp.custom_route("/api/touch", methods=["POST"])
async def api_touch(request: Request) -> Response:
    if not token_ok(request, "x-device-token", DEVICE_TOKEN):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        event = insert_event(payload, source="device")
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "event": event})


@mcp.custom_route("/api/simulate", methods=["POST"])
async def api_simulate(request: Request) -> Response:
    if not token_ok(request, "x-device-token", DEVICE_TOKEN):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        event = insert_event(payload, source="simulate")
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "event": event})


@mcp.custom_route("/api/latest", methods=["GET"])
async def api_latest(request: Request) -> Response:
    if not token_ok(request, "x-mcp-token", MCP_TOKEN):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse({"ok": True, "event": get_latest_event()})


@mcp.custom_route("/api/history", methods=["GET"])
async def api_history(request: Request) -> Response:
    if not token_ok(request, "x-mcp-token", MCP_TOKEN):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    raw_limit = request.query_params.get("limit", "20")
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 20
    return JSONResponse({"ok": True, "events": get_recent_events(limit)})


@mcp.custom_route("/test", methods=["GET"])
async def test_page(_: Request) -> Response:
    return HTMLResponse(
        """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>模拟抱抱</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:680px;margin:34px auto;padding:0 18px}label{display:block;margin:12px 0}input,button{font:inherit;padding:10px;border-radius:9px;border:1px solid #bbb}input{width:100%;box-sizing:border-box}button{cursor:pointer;margin-right:8px}pre{white-space:pre-wrap;background:#f4f4f4;padding:14px;border-radius:10px}</style></head><body>
<h1>🧸 模拟抱抱</h1><p>DEVICE_TOKEN 只保存在这个网页当前输入框里，不会写进页面源码。</p>
<label>DEVICE_TOKEN<input id='token' type='password' autocomplete='off'></label>
<label>峰值 pressure（0~4095）<input id='peak' type='number' value='2600'></label>
<label>持续时间 ms<input id='duration' type='number' value='5800'></label>
<button onclick='send()'>发送模拟数据</button><button onclick='preset(900,180)'>轻碰</button><button onclick='preset(1800,1200)'>按住</button><button onclick='preset(2200,5000)'>抱住</button><button onclick='preset(3400,6000)'>紧紧抱住</button>
<pre id='out'>等待发送…</pre>
<script>
function preset(p,d){peak.value=p;duration.value=d}
async function send(){const t=token.value; const p=Number(peak.value), d=Number(duration.value); const r=await fetch('/api/simulate',{method:'POST',headers:{'Content-Type':'application/json','X-Device-Token':t},body:JSON.stringify({device_id:'simulator',peak:p,average:Math.round(p*0.82),duration_ms:d})}); out.textContent=JSON.stringify(await r.json(),null,2)}
</script></body></html>"""
    )


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(_: Request) -> Response:
    return HTMLResponse(
        """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>抱抱记录</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:34px auto;padding:0 18px}input,button{font:inherit;padding:10px;border-radius:9px;border:1px solid #bbb}input{width:100%;box-sizing:border-box;margin:10px 0}button{cursor:pointer}pre{white-space:pre-wrap;background:#f4f4f4;padding:14px;border-radius:10px}</style></head><body>
<h1>🧸 抱抱记录</h1><p>输入 MCP_TOKEN 后读取最近 20 条。Token 只在当前页面内使用。</p>
<input id='token' type='password' placeholder='MCP_TOKEN' autocomplete='off'><button onclick='loadData()'>读取</button><pre id='out'>尚未读取</pre>
<script>async function loadData(){const r=await fetch('/api/history?limit=20',{headers:{'X-MCP-Token':token.value}});out.textContent=JSON.stringify(await r.json(),null,2)}</script>
</body></html>"""
    )


# Zeabur terminates TLS and controls Host headers at its reverse proxy.
# The official MCP SDK docs explicitly allow disabling DNS-rebinding protection in this reverse-proxy case.
security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
app = mcp.streamable_http_app(
    streamable_http_path=f"/mcp/{MCP_TOKEN}",
    json_response=True,
    stateless_http=True,
    transport_security=security,
    host="0.0.0.0",
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
