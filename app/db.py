import os, sqlite3, hashlib, json, time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "app.db")

def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS chatlog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts TEXT NOT NULL
    )""")

    # NEW: pending paged outputs (server-side, not in session cookie)
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_output (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        cursor INTEGER NOT NULL DEFAULT 0,
        updated_ts INTEGER NOT NULL
    )""")
    cur.execute("""CREATE INDEX IF NOT EXISTS idx_pending_output_user ON pending_output(username)""")

    conn.commit()

    cur.execute("SELECT id FROM users WHERE username = ?", ("admin",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
            ("admin", _hash_pw("admin")),
        )
        conn.commit()
    conn.close()

def verify_user(username: str, password: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username, password_hash, is_admin FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    u, h, is_admin = row
    if _hash_pw(password) != h:
        return None
    return {"username": u, "is_admin": bool(is_admin)}

def add_chat(username: str, role: str, content: str, ts: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chatlog (username, role, content, ts) VALUES (?, ?, ?, ?)",
        (username, role, content, ts),
    )
    conn.commit()
    conn.close()

def last_chats(username: str, limit: int = 40):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content, ts FROM chatlog WHERE username=? ORDER BY id DESC LIMIT ?",
        (username, limit),
    )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r, "content": c, "ts": ts} for (r, c, ts) in rows]

# ============================
# NEW: Paging helpers (NEXT)
# ============================

def _now_ts() -> int:
    return int(time.time())

def pending_clear(username: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_output WHERE username=?", (username,))
    conn.commit()
    conn.close()

def pending_set(username: str, pages: list[str], cursor: int = 0) -> None:
    """
    Store pages for a user. Overwrites previous pending output for that user.
    pages: list of strings (each page already formatted)
    cursor: index of next page to serve
    """
    payload = {"pages": pages}
    payload_json = json.dumps(payload, ensure_ascii=False)
    ts = _now_ts()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_output WHERE username=?", (username,))
    cur.execute(
        "INSERT INTO pending_output (username, payload_json, cursor, updated_ts) VALUES (?, ?, ?, ?)",
        (username, payload_json, int(cursor), ts),
    )
    conn.commit()
    conn.close()

def pending_next(username: str) -> tuple[str | None, bool]:
    """
    Returns (page_text, has_more).
    If no pending, returns (None, False).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, payload_json, cursor FROM pending_output WHERE username=? ORDER BY id DESC LIMIT 1",
        (username,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return (None, False)

    pid, payload_json, cursor = row
    try:
        payload = json.loads(payload_json or "{}") or {}
        pages = payload.get("pages") or []
        if not isinstance(pages, list):
            pages = []
    except Exception:
        pages = []

    cursor = int(cursor or 0)
    if cursor < 0:
        cursor = 0

    if cursor >= len(pages):
        # nothing left -> clear
        cur.execute("DELETE FROM pending_output WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        return (None, False)

    page = str(pages[cursor] or "")
    cursor2 = cursor + 1
    has_more = cursor2 < len(pages)

    if has_more:
        cur.execute(
            "UPDATE pending_output SET cursor=?, updated_ts=? WHERE id=?",
            (cursor2, _now_ts(), pid),
        )
    else:
        cur.execute("DELETE FROM pending_output WHERE id=?", (pid,))

    conn.commit()
    conn.close()
    return (page, has_more)
