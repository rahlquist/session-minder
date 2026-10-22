"""Session Minder plugin backend.

Mounted at /api/plugins/session-minder/ by the Hermes dashboard plugin system.
All state.db access goes through SessionDB so FTS triggers and guards stay intact.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _db_path() -> Path:
    return _hermes_home() / "state.db"


def _backup_dir() -> Path:
    d = _hermes_home() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plugin_data_dir() -> Path:
    d = _hermes_home() / "plugins" / "session-minder" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _queue_db() -> Path:
    return _plugin_data_dir() / "queue.db"


def _ensure_queue():
    p = _queue_db()
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE IF NOT EXISTS pending_purge (
        session_id TEXT PRIMARY KEY,
        queued_at REAL NOT NULL,
        purge_after REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ui_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()


def _open_db(read_only: bool = False):
    from hermes_state import SessionDB
    return SessionDB(db_path=_db_path(), read_only=read_only)


def _open_archive_db() -> "SessionDB":
    from hermes_state import SessionDB
    prof = _hermes_home() / "profiles" / "archive"
    prof.mkdir(parents=True, exist_ok=True)
    db = prof / "state.db"
    return SessionDB(db_path=db, read_only=False)


def _size_of_session(db, sid: str) -> int:
    try:
        rows = db._read_all(
            "SELECT COALESCE(SUM(LENGTH(COALESCE(content,''))+LENGTH(COALESCE(tool_calls,''))+LENGTH(COALESCE(reasoning,''))+LENGTH(COALESCE(reasoning_content,''))),0) "
            "FROM messages WHERE session_id=?", [sid])
        return int(rows[0][0]) if rows else 0
    except Exception:
        return 0


def _session_is_protected(db, sid: str) -> bool:
    """A session is undeletable if the delete guard would fire."""
    try:
        conn = db._conn
        r1 = conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? LIMIT 1", (sid,)).fetchone()
        r2 = conn.execute("SELECT 1 FROM worker_executions WHERE session_id=? LIMIT 1", (sid,)).fetchone()
        return bool(r1 or r2)
    except Exception:
        return False


def _holders() -> list[dict]:
    """Processes holding state.db open (best-effort)."""
    import subprocess
    out = []
    db = str(_db_path())
    try:
        r = subprocess.run(["fuser", db], capture_output=True, text=True, timeout=5)
        pids = r.stdout.split() if r.stdout else []
        for pid in pids:
            pid = pid.strip()
            if pid.isdigit():
                try:
                    cmd = Path(f"/proc/{pid}/cmdline").read_text().replace("\x00", " ").strip()
                except Exception:
                    cmd = ""
                out.append({"pid": int(pid), "cmd": cmd})
    except Exception:
        pass
    return out


def _free_bytes(path: Path) -> int:
    st = os.statvfs(str(path.parent if path.is_file() else path))
    return st.f_bavail * st.f_frsize


def _db_size() -> int:
    p = _db_path()
    total = p.stat().st_size if p.exists() else 0
    for suf in ("-wal", "-shm"):
        sp = Path(str(p) + suf)
        if sp.exists():
            total += sp.stat().st_size
    return total


def _create_backup() -> dict:
    db = _db_path()
    backup_dir = _backup_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"state.db.backup-{ts}"
    # Free space check
    needed = _db_size() * 2
    free = _free_bytes(backup_dir)
    if free < needed:
        return {"ok": False, "error": "insufficient_space", "needed": needed, "free": free,
                "db_size": _db_size()}
    try:
        from hermes_state import SessionDB
        src = SessionDB(db_path=db, read_only=True)
        # Use sqlite backup API via the underlying connection
        conn = sqlite3.connect(str(dest))
        src_conn = src._conn
        src_conn.backup(conn)
        conn.commit()
        conn.close()
        src.close()
        # Integrity check
        c = sqlite3.connect(str(dest))
        res = c.execute("PRAGMA integrity_check").fetchone()
        c.close()
        if not res or res[0] != "ok":
            dest.unlink(missing_ok=True)
            return {"ok": False, "error": "integrity_check_failed", "detail": str(res)}
        return {"ok": True, "path": str(dest), "size": dest.stat().st_size, "ts": ts}
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


_ensure_queue()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class IdsBody(BaseModel):
    ids: list[str]
    backup_first: bool = True
    purge_mode: str = "soft"  # soft | hard


class ExportBody(BaseModel):
    session_id: str
    dest_path: str


class RestoreBody(BaseModel):
    backup_path: str
    force: bool = False


class SettingsBody(BaseModel):
    undo_mode: str = "30s"  # 30s | hold
    undo_seconds: int = 30


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
async def health():
    return {"ok": True, "db": str(_db_path())}


@router.get("/disk")
async def disk():
    return {"db_size": _db_size(), "free": _free_bytes(_backup_dir()),
            "backup_dir": str(_backup_dir())}


@router.get("/holders")
async def holders():
    return {"holders": _holders()}


@router.get("/backups")
async def backups():
    out = []
    for p in sorted(_backup_dir().glob("state.db.backup-*"), reverse=True):
        out.append({"path": str(p), "name": p.name, "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime})
    return {"backups": out}


@router.post("/backup")
async def backup():
    return _create_backup()


@router.get("/settings")
async def get_settings():
    conn = sqlite3.connect(str(_queue_db()))
    rows = conn.execute("SELECT key, value FROM ui_settings").fetchall()
    conn.close()
    s = {k: v for k, v in rows}
    return {"undo_mode": s.get("undo_mode", "30s"), "undo_seconds": int(s.get("undo_seconds", "30"))}


@router.post("/settings")
async def set_settings(body: SettingsBody):
    conn = sqlite3.connect(str(_queue_db()))
    conn.execute("INSERT OR REPLACE INTO ui_settings (key, value) VALUES ('undo_mode', ?)", (body.undo_mode,))
    conn.execute("INSERT OR REPLACE INTO ui_settings (key, value) VALUES ('undo_seconds', ?)", (str(body.undo_seconds),))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.get("/pending")
async def pending():
    conn = sqlite3.connect(str(_queue_db()))
    rows = conn.execute("SELECT session_id, queued_at, purge_after FROM pending_purge").fetchall()
    conn.close()
    return {"pending": [{"session_id": r[0], "queued_at": r[1], "purge_after": r[2]} for r in rows]}


@router.post("/cancel_purge")
async def cancel_purge(body: IdsBody):
    conn = sqlite3.connect(str(_queue_db()))
    for sid in body.ids:
        conn.execute("DELETE FROM pending_purge WHERE session_id=?", (sid,))
    conn.commit()
    conn.close()
    # un-hide
    db = _open_db()
    for sid in body.ids:
        try:
            db._conn.execute("UPDATE sessions SET hidden=0 WHERE id=?", (sid,))
            db._conn.commit()
        except Exception:
            pass
    db.close()
    return {"ok": True, "cancelled": body.ids}


@router.get("/list")
async def list_sessions(limit: int = 200, offset: int = 0, sort: str = "updated",
                       direction: str = "desc", search: str = "", include_protected: bool = False,
                       include_archived: bool = False):
    db = _open_db(read_only=True)
    try:
        sessions = db.list_sessions_rich(
            limit=limit + offset, offset=0, include_archived=include_archived,
            include_hidden=include_protected, include_children=True)
        # list_sessions_rich already filters hidden; for protected view we passed include_hidden=True
        # search_sessions may not support arbitrary; fallback filter in python
        out = []
        for s in sessions:
            if not include_archived and s.get("archived"):
                continue
            if s.get("hidden") and not include_protected:
                # hidden includes pending-purge and protected-hidden; show protected only if requested
                pass
            sid = s["id"]
            size = _size_of_session(db, sid)
            protected = _session_is_protected(db, sid)
            if (not include_protected) and protected:
                continue
            # hidden filter: skip pending-purge hidden unless protected view
            if s.get("hidden") and not (include_protected and protected):
                # still include if user explicitly wants protected? hidden pending-purge excluded
                if not include_protected:
                    continue
            title = s.get("title") or s.get("display_name") or sid
            if search and search.lower() not in str(title).lower() and search.lower() not in sid.lower():
                continue
            out.append({
                "id": sid,
                "title": title,
                "source": s.get("source"),
                "created": s.get("started_at"),
                "updated": s.get("last_activity_at") or s.get("started_at"),
                "message_count": s.get("message_count", 0),
                "size": size,
                "archived": bool(s.get("archived")),
                "pinned": bool(s.get("pinned")),
                "hidden": bool(s.get("hidden")),
                "protected": protected,
            })
        # sort
        keymap = {"created": "created", "updated": "updated", "name": "title", "size": "size"}
        k = keymap.get(sort, "updated")
        reverse = direction == "desc"
        out.sort(key=lambda x: (x[k] is None, x[k]), reverse=reverse)
        total = len(out)
        page = out[offset:offset + limit]
        return {"sessions": page, "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    db = _open_db(read_only=True)
    try:
        s = db.get_session(session_id)
        if not s:
            raise HTTPException(status_code=404, detail="not found")
        msgs = db.get_messages(session_id, include_inactive=False)
        # trim to preview (last 50)
        preview = msgs[-50:] if len(msgs) > 50 else msgs
        out_msgs = []
        for m in preview:
            out_msgs.append({
                "role": m.get("role"),
                "content": (m.get("content") or "")[:2000],
                "timestamp": m.get("timestamp"),
            })
        return {"session": s, "messages": out_msgs, "total_messages": len(msgs)}
    finally:
        db.close()


@router.post("/delete")
async def delete_sessions(body: IdsBody):
    # backup first (once per request batch)
    backup_info = None
    if body.backup_first:
        backup_info = _create_backup()
        if not backup_info.get("ok") and backup_info.get("error") == "insufficient_space":
            raise HTTPException(status_code=400, detail=backup_info)
    db = _open_db()
    results = []
    try:
        for sid in body.ids:
            try:
                db.delete_session(sid)
                results.append({"id": sid, "ok": True})
            except Exception as e:
                results.append({"id": sid, "ok": False, "error": str(e)})
    finally:
        db.close()
    return {"results": results, "backup": backup_info}


@router.post("/soft_delete")
async def soft_delete(body: IdsBody):
    """Hide sessions and queue them for purge (undo window)."""
    backup_info = None
    if body.backup_first:
        backup_info = _create_backup()
        if not backup_info.get("ok") and backup_info.get("error") == "insufficient_space":
            raise HTTPException(status_code=400, detail=backup_info)
    db = _open_db()
    queued = []
    try:
        for sid in body.ids:
            try:
                db._conn.execute("UPDATE sessions SET hidden=1 WHERE id=?", (sid,))
                db._conn.commit()
                queued.append(sid)
            except Exception as e:
                pass
    finally:
        db.close()
    # settings
    conn = sqlite3.connect(str(_queue_db()))
    import json
    settings = dict(conn.execute("SELECT key, value FROM ui_settings").fetchall())
    mode = settings.get("undo_mode", "30s")
    secs = int(settings.get("undo_seconds", "30"))
    now = time.time()
    purge_after = (now + secs) if mode == "30s" else None
    for sid in queued:
        conn.execute("INSERT OR REPLACE INTO pending_purge (session_id, queued_at, purge_after) VALUES (?,?,?)",
                     (sid, now, purge_after))
    conn.commit()
    conn.close()
    return {"queued": queued, "mode": mode, "purge_after": purge_after, "backup": backup_info}


@router.post("/purge")
async def purge(body: IdsBody):
    """Hard-delete queued (or specified) sessions, one transaction each."""
    db = _open_db()
    results = []
    try:
        for sid in body.ids:
            try:
                db.delete_session(sid)
                results.append({"id": sid, "ok": True})
            except Exception as e:
                results.append({"id": sid, "ok": False, "error": str(e)})
    finally:
        db.close()
    # remove from queue
    conn = sqlite3.connect(str(_queue_db()))
    for sid in body.ids:
        conn.execute("DELETE FROM pending_purge WHERE session_id=?", (sid,))
    conn.commit()
    conn.close()
    return {"results": results}


@router.post("/archive")
async def archive_sessions(body: IdsBody):
    backup_info = None
    if body.backup_first:
        backup_info = _create_backup()
        if not backup_info.get("ok") and backup_info.get("error") == "insufficient_space":
            raise HTTPException(status_code=400, detail=backup_info)
    db = _open_db()
    arch = _open_archive_db()
    results = []
    try:
        for sid in body.ids:
            try:
                payload = db.export_session(sid, include_inactive=True)
                if not payload:
                    results.append({"id": sid, "ok": False, "error": "not found"})
                    continue
                arch.import_sessions([payload])
                db.set_session_archived(sid, True, notify=False)
                results.append({"id": sid, "ok": True})
            except Exception as e:
                results.append({"id": sid, "ok": False, "error": str(e)})
    finally:
        db.close()
        arch.close()
    return {"results": results, "backup": backup_info}


@router.post("/export_db")
async def export_db(body: ExportBody):
    from hermes_state import SessionDB
    db = _open_db(read_only=True)
    try:
        payload = db.export_session(body.session_id, include_inactive=True)
        if not payload:
            raise HTTPException(status_code=404, detail="session not found")
    finally:
        db.close()
    dest = Path(body.dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    newdb = SessionDB(db_path=dest, read_only=False)
    try:
        newdb.import_sessions([payload])
    finally:
        newdb.close()
    return {"ok": True, "path": str(dest), "size": dest.stat().st_size}


@router.get("/export_json/{session_id}")
async def export_json(session_id: str):
    db = _open_db(read_only=True)
    try:
        payload = db.export_session(session_id, include_inactive=True)
        if not payload:
            raise HTTPException(status_code=404, detail="session not found")
        return payload
    finally:
        db.close()


@router.post("/restore")
async def restore(body: RestoreBody):
    holders = _holders()
    if holders and not body.force:
        return {"ok": False, "error": "holders", "holders": holders}
    # force: try to stop gateway
    if holders and body.force:
        import subprocess
        try:
            subprocess.run(["hermes", "gateway", "stop"], capture_output=True, text=True, timeout=20)
        except Exception:
            pass
        time.sleep(2)
        holders = _holders()
        if holders:
            return {"ok": False, "error": "holders_remain", "holders": holders}
    bp = Path(body.backup_path)
    if not bp.exists():
        raise HTTPException(status_code=404, detail="backup not found")
    # integrity check
    c = sqlite3.connect(str(bp))
    res = c.execute("PRAGMA integrity_check").fetchone()
    c.close()
    if not res or res[0] != "ok":
        raise HTTPException(status_code=400, detail=f"backup corrupt: {res}")
    db = _db_path()
    # swap
    pre = db.with_name(db.name + f".pre-restore-{int(time.time())}")
    try:
        if db.exists():
            db.rename(pre)
        # also move sidecars
        for suf in ("-wal", "-shm"):
            sp = Path(str(db) + suf)
            if sp.exists():
                sp.rename(Path(str(pre) + suf))
        shutil.copy2(bp, db)
    except Exception as e:
        # attempt rollback
        try:
            if pre.exists() and not db.exists():
                pre.rename(db)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "restored_from": str(bp), "previous": str(pre)}


import threading

def _auto_purge_loop():
    while True:
        try:
            conn = sqlite3.connect(str(_queue_db()))
            rows = conn.execute("SELECT session_id, purge_after FROM pending_purge WHERE purge_after IS NOT NULL").fetchall()
            conn.close()
            now = time.time()
            to_purge = [r[0] for r in rows if r[1] is not None and r[1] <= now]
            if to_purge:
                db = _open_db()
                try:
                    for sid in to_purge:
                        try:
                            db.delete_session(sid)
                        except Exception:
                            pass
                finally:
                    db.close()
                c = sqlite3.connect(str(_queue_db()))
                for sid in to_purge:
                    c.execute("DELETE FROM pending_purge WHERE session_id=?", (sid,))
                c.commit()
                c.close()
        except Exception:
            pass
        time.sleep(5)

threading.Thread(target=_auto_purge_loop, daemon=True).start()
