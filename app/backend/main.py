"""
FastAPI backend — powered by gemini-cli subprocess.
Session history (conversation memory) is managed by gemini-cli.
Display turns (sidebar) are stored in our own sessions.db.
"""

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent.parent.parent
load_dotenv(BASE_DIR / ".env")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
GEMINI_BIN   = "/opt/homebrew/bin/gemini"  # full path avoids PATH lookup issues

import logging
import sys
sys.path.append(str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nyai")

# ---------------------------------------------------------------------------
# Display DB  (session titles + turn history for sidebar)
# ---------------------------------------------------------------------------

DISPLAY_DB_PATH = BASE_DIR / "sessions.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DISPLAY_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id                TEXT PRIMARY KEY,
            title             TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            turns             TEXT NOT NULL DEFAULT '[]',
            gemini_session_id TEXT
        )
    """)
    # Migrate: add gemini_session_id column if it didn't exist before
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN gemini_session_id TEXT")
    except Exception:
        pass   # column already present
    conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Legal Research API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_title(message: str) -> str:
    t = message.strip().split("\n")[0]
    return t[:72] + "…" if len(t) > 72 else t


def tool_summary(tool_name: str, args: dict) -> str:
    # gemini-cli prefixes MCP tool names with "mcp_<server>_"
    # Normalize: strip "mcp_legal_" prefix so display logic works uniformly
    name = tool_name.replace("mcp_legal_", "")
    if name == "search_cases":
        return f'Searched "{args.get("query", "")}"'
    if name == "read_judgment":
        return f'Read {args.get("path", "")} ({args.get("section", "all")})'
    if name == "find_citations":
        return f'Citations for "{args.get("case_name", "")}"'
    if name == "get_cases_by_statute":
        s = args.get("section", "")
        return f'Statute: {args.get("act", "")}' + (f" §{s}" if s else "")
    if name in ("google_search", "web_search_agent", "googleSearch"):
        return f'Web search: "{args.get("query", args.get("q", args.get("request", "")))}"'
    if name == "grep_judgments":
        return f'Grep: "{args.get("pattern", "")}"'
    if name == "get_context_around":
        return f'Context: {args.get("path", "")}'
    return tool_name


async def _gemini_stream(message: str, gemini_session_id: Optional[str], model: Optional[str] = None):
    """
    Spawn gemini-cli and yield parsed stream-json event dicts line by line.

    Stream-json event types emitted by gemini-cli:
      init        – {"type":"init","session_id":"..."}
      tool_use    – {"type":"tool_use","tool_name":"...","parameters":{...}}
      tool_result – {"type":"tool_result","tool_name":"...","status":"success","output":"..."}
      message     – {"type":"message","content":"...","delta":true}  (streaming chunks)
      result      – {"type":"result","stats":{...}}  (terminal event)
    """
    base_cmd = [
        GEMINI_BIN,
        "--prompt", message,
        "--output-format", "stream-json",
        "--yolo",
        "--model", model or GEMINI_MODEL,
    ]

    # If a gemini_session_id is provided, try --resume first.
    # Resume failures are fast (<1s, no assistant output) so we probe without
    # real-time yield. On failure we fall back to a fresh session and stream live.
    if gemini_session_id:
        probe_cmd = base_cmd + ["--resume", gemini_session_id]
        log.info("gemini probe --resume %s", gemini_session_id)
        probe = await asyncio.create_subprocess_exec(
            *probe_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(BASE_DIR),
        )
        probe_events = []
        assert probe.stdout is not None
        async for raw in probe.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                probe_events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        probe_rc = await probe.wait()
        got_assistant = any(
            e.get("type") == "message" and e.get("role") != "user"
            for e in probe_events
        )
        if probe_rc == 0 and got_assistant:
            log.info("gemini resume succeeded (%d events)", len(probe_events))
            for evt in probe_events:
                yield evt
            return
        stderr_data = await probe.stderr.read()
        log.warning("gemini resume failed rc=%d events=%d — retrying fresh. stderr: %s",
                    probe_rc, len(probe_events), stderr_data.decode(errors="replace")[-300:])

    # Fresh session (no --resume) — stream live
    cmd = base_cmd
    log.info("gemini fresh session cmd: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(BASE_DIR),
    )

    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
            t = evt.get("type")
            if t == "result":
                log.info("gemini RESULT: %s", json.dumps(evt))
            log.debug("gemini event type=%s role=%s", t, evt.get("role"))
            yield evt
        except json.JSONDecodeError:
            log.warning("gemini non-json: %s", line[:200])
            continue

    rc = await proc.wait()
    if rc != 0:
        stderr = await proc.stderr.read()
        log.error("gemini-cli exit %d stderr: %s", rc, stderr.decode(errors="replace")[-500:])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    model: Optional[str] = None   # overrides GEMINI_MODEL env var


class ChatResponse(BaseModel):
    session_id: str
    response: str
    tools_used: list[dict]
    context_tokens: int


class SessionListItem(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int


class SessionDetail(BaseModel):
    id: str
    title: str
    created_at: str
    turns: list[dict]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "model": GEMINI_MODEL, "backend": "gemini-cli", "version": "debug-1"}


@app.get("/debug/stream")
async def debug_stream():
    """Raw dump of every gemini-cli event for a test prompt — for debugging."""
    events = []
    async for evt in _gemini_stream("say hi in one word", None):
        events.append(evt)
    return {"events": events}


@app.get("/debug/sync")
def debug_sync():
    """Synchronous subprocess test — isolates async issues."""
    import subprocess
    result = subprocess.run(
        ["gemini", "--prompt", "say hi in one word", "--output-format", "stream-json", "--yolo"],
        capture_output=True, text=True, cwd=str(BASE_DIR), stdin=subprocess.DEVNULL,
    )
    return {
        "rc": result.returncode,
        "stdout_lines": result.stdout.strip().splitlines(),
        "stderr_tail": result.stderr.strip().splitlines()[-10:],
    }


@app.get("/sessions", response_model=list[SessionListItem])
def list_sessions():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at, turns FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [
        SessionListItem(
            id=r["id"],
            title=r["title"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            turn_count=len(json.loads(r["turns"])),
        )
        for r in rows
    ]


@app.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT id, title, created_at, turns FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetail(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        turns=json.loads(row["turns"]),
    )


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    # Note: gemini-cli session history is stored in ~/.gemini/history/
    # and is not deleted here — that's fine, it will expire per retention policy.
    return {"status": "deleted"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Non-streaming chat endpoint — waits for full response."""
    conn = get_db()

    if req.session_id:
        row = conn.execute(
            "SELECT id, title, turns, gemini_session_id FROM sessions WHERE id = ?",
            (req.session_id,),
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Session not found")
        session_id      = row["id"]
        turns           = json.loads(row["turns"])
        gemini_sess_id  = row["gemini_session_id"]
    else:
        session_id     = str(uuid.uuid4())
        title          = make_title(req.message)
        turns          = []
        gemini_sess_id = None
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, turns, gemini_session_id) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, title, now_iso(), now_iso(), "[]", None),
        )
        conn.commit()

    conn.close()

    full_text      = ""
    tool_calls: list[dict] = []
    new_gemini_id  = gemini_sess_id

    async for event in _gemini_stream(req.message, gemini_sess_id, req.model):
        t = event.get("type")
        if t == "init":
            new_gemini_id = event.get("session_id", new_gemini_id)
        elif t == "tool_use":
            tname        = event.get("tool_name", "")
            args         = event.get("parameters", {})
            display_name = tname.replace("mcp_legal_", "")
            tool_calls.append({"tool": display_name, "summary": tool_summary(tname, args)})
        elif t == "message" and event.get("role") != "user":
            full_text += event.get("content", "")

    turn = {
        "id":                str(uuid.uuid4()),
        "user_message":      req.message,
        "assistant_message": full_text,
        "tools_used":        tool_calls,
        "timestamp":         now_iso(),
    }
    turns.append(turn)

    db = get_db()
    db.execute(
        "UPDATE sessions SET updated_at=?, turns=?, gemini_session_id=? WHERE id=?",
        (now_iso(), json.dumps(turns), new_gemini_id, session_id),
    )
    db.commit()
    db.close()

    return ChatResponse(
        session_id=session_id,
        response=full_text,
        tools_used=tool_calls,
        context_tokens=len(full_text) // 4,
    )


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE streaming endpoint — emits tool progress and text chunks in real time."""

    conn = get_db()

    is_new = False
    if req.session_id:
        row = conn.execute(
            "SELECT id, title, turns, gemini_session_id FROM sessions WHERE id = ?",
            (req.session_id,),
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Session not found")
        session_id     = row["id"]
        title          = row["title"]
        turns          = json.loads(row["turns"])
        gemini_sess_id = row["gemini_session_id"]
    else:
        session_id     = str(uuid.uuid4())
        title          = make_title(req.message)
        turns          = []
        gemini_sess_id = None
        is_new         = True
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, turns, gemini_session_id) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, title, now_iso(), now_iso(), "[]", None),
        )
        conn.commit()

    conn.close()

    async def generate():
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'title': title, 'is_new': is_new})}\n\n"

        full_text      = ""
        tool_calls: list[dict] = []
        new_gemini_id  = gemini_sess_id
        last_tool_name = ""   # track most-recent tool for tool_result events

        try:
            event_count = 0
            async for event in _gemini_stream(req.message, gemini_sess_id, req.model):
                event_count += 1
                t = event.get("type")
                log.debug("stream event #%d type=%s", event_count, t)

                if t == "init":
                    new_gemini_id = event.get("session_id", new_gemini_id)

                elif t == "tool_use":
                    tname          = event.get("tool_name", "")
                    args           = event.get("parameters", {})
                    last_tool_name = tname
                    display_name   = tname.replace("mcp_legal_", "")
                    summary        = tool_summary(tname, args)
                    tool_calls.append({"tool": display_name, "summary": summary})
                    yield f"data: {json.dumps({'type': 'tool_start', 'tool': display_name, 'summary': summary})}\n\n"

                elif t == "tool_result":
                    # tool_result has tool_id but not tool_name; use last_tool_name
                    tname = event.get("tool_name", last_tool_name)
                    yield f"data: {json.dumps({'type': 'tool_done', 'tool': tname.replace('mcp_legal_', '')})}\n\n"

                elif t == "message":
                    role  = event.get("role", "")
                    chunk = event.get("content", "")
                    log.debug("  message role=%r chunk_len=%d", role, len(chunk))
                    if role != "user" and chunk:
                        full_text += chunk
                        yield f"data: {json.dumps({'type': 'text', 'chunk': chunk})}\n\n"

                elif t == "result" and event.get("status") == "error":
                    err_msg = event.get("error", {}).get("message", "Unknown error")
                    log.error("gemini result error: %s", err_msg)
                    yield f"data: {json.dumps({'type': 'error', 'message': err_msg})}\n\n"
                    return

            log.info("stream done: events=%d text_len=%d tools=%d", event_count, len(full_text), len(tool_calls))

        except Exception as exc:
            log.exception("stream error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            return

        # Persist display turn
        turn = {
            "id":                str(uuid.uuid4()),
            "user_message":      req.message,
            "assistant_message": full_text,
            "tools_used":        tool_calls,
            "timestamp":         now_iso(),
        }
        db = get_db()
        db.execute(
            "UPDATE sessions SET updated_at=?, turns=?, gemini_session_id=? WHERE id=?",
            (now_iso(), json.dumps(turns + [turn]), new_gemini_id, session_id),
        )
        db.commit()
        db.close()

        yield f"data: {json.dumps({'type': 'complete', 'context_tokens': len(full_text) // 4})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


@app.get("/search")
def search(q: str, year_from: int = None, year_to: int = None, top_k: int = 10):
    from agent.tools import search_cases
    return search_cases(q, year_from=year_from, year_to=year_to, top_k=top_k)


# Serve frontend
FRONTEND_DIR = BASE_DIR / "app" / "frontend"
if FRONTEND_DIR.exists():
    @app.get("/")
    def serve_landing():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/login")
    def serve_login():
        return FileResponse(str(FRONTEND_DIR / "login.html"))

    @app.get("/app")
    def serve_app():
        return FileResponse(str(FRONTEND_DIR / "app.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
