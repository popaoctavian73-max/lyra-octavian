import os
import asyncio
import hashlib
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# Optional .env loading (does not break if python-dotenv is not installed)
try:
    from dotenv import load_dotenv  # type: ignore

    BASE_DIR__DOTENV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(BASE_DIR__DOTENV, ".env"))
except Exception:
    pass

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette import status

from .db import init_db, verify_user, add_chat, last_chats, pending_set, pending_next, pending_clear
from .auth import require_user, require_admin, redirect_to_login
from .llm_openai import answer
from .web_search import ddg_search
from .user_ui_routes import router as user_ui_router


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(BASE_DIR, "DOCS")


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        v = (os.getenv(name, "") or "").strip()
        return int(v) if v else default
    except Exception:
        return default


# ---------------------------
# Lyra Octavian = personal cognitive workbench (OpenAI-first)
# - UI routes remain intact
# - No local RAG / FAISS
# - Optional web augmentation
# - Conversation continuity via DB history
# - Server-side paging with NEXT/CONTINUE
# ---------------------------
LYRA_WEB_DEFAULT = _env_bool("LYRA_WEB_DEFAULT", False)
LYRA_WEB_MAX_RESULTS = _env_int("LYRA_WEB_MAX_RESULTS", 3)
LYRA_WEB_TIMEOUT = _env_int("LYRA_WEB_TIMEOUT", 8)
LYRA_WEB_MAX_CHARS_PER_SOURCE = _env_int("LYRA_WEB_MAX_CHARS_PER_SOURCE", 900)
LYRA_WEB_MAX_TOTAL_CHARS = _env_int("LYRA_WEB_MAX_TOTAL_CHARS", 1500)

LYRA_CHAT_PAGE_CHARS = _env_int("LYRA_CHAT_PAGE_CHARS", 1400)
LYRA_CHAT_MAX_PAGES = _env_int("LYRA_CHAT_MAX_PAGES", 0)

# How much conversation history we attach (messages). Keep bounded for cost.
LYRA_HISTORY_MESSAGES = _env_int("LYRA_HISTORY_MESSAGES", 10)

# Auto-continue when the model hits its internal output limit.
# This does NOT impose a content limit; it only prevents manual "continue" prompts.
LYRA_AUTO_CONTINUE = _env_bool("LYRA_AUTO_CONTINUE", True)
LYRA_AUTO_CONTINUE_MAX_ROUNDS = _env_int("LYRA_AUTO_CONTINUE_MAX_ROUNDS", 25)
LYRA_CONTINUE_TAIL_CHARS = _env_int("LYRA_CONTINUE_TAIL_CHARS", 1200)

# Marker appended by llm_openai.py when OpenAI indicates "incomplete" output.
LYRA_INCOMPLETE_MARKER = "[LYRA_OUTPUT_INCOMPLETE]"


class ChatRequest(BaseModel):
    query: str
    web: Optional[bool] = None  # None => default


class PublicChatRequest(BaseModel):
    query: str
    page: Optional[int] = 1
    web: Optional[bool] = None


app = FastAPI(title="LYRA")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("LYRA_SESSION_SECRET", "CHANGE_ME_DEV_SECRET"))

templates_dir = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=templates_dir)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.include_router(user_ui_router)


@app.on_event("startup")
def on_startup():
    init_db()


def _tpl_exists(name: str) -> bool:
    return os.path.exists(os.path.join(templates_dir, name))


def _render_or_fallback(request: Request, user: Dict[str, Any], candidates: List[str]) -> HTMLResponse:
    for name in candidates:
        if _tpl_exists(name):
            return templates.TemplateResponse(name, {"request": request, "user": user})

    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>LYRA (Fallback)</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body {{ font-family: Arial, sans-serif; margin: 24px; }}
      code {{ background: #f2f2f2; padding: 2px 6px; border-radius: 4px; }}
      a {{ display: inline-block; margin: 6px 0; }}
    </style>
  </head>
  <body>
    <h2>LYRA UI (Fallback)</h2>
    <p>Templates not found in <code>templates/</code>.</p>
    <p>User: <code>{user.get("username","")}</code></p>
    <hr/>
    <div>
      <a href="/user">Open User UI</a><br/>
      <a href="/api/chats">API chats</a><br/>
    </div>
  </body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = require_user(request)
    if user:
        dest = "/app" if user.get("is_admin") else "/user"
        return RedirectResponse(url=dest, status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = verify_user(username, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Login invalid."}, status_code=401)

    request.session["user"] = {"username": user["username"], "is_admin": bool(user.get("is_admin"))}
    dest = "/app" if user.get("is_admin") else "/user"
    return RedirectResponse(url=dest, status_code=status.HTTP_302_FOUND)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@app.get("/app", response_class=HTMLResponse)
def admin_app(request: Request):
    user = require_admin(request)
    if not user:
        return redirect_to_login()
    return _render_or_fallback(request, user, candidates=["app.html", "index.html", "admin.html", "dashboard.html", "user.html"])


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    user = require_admin(request)
    if not user:
        return redirect_to_login()
    return _render_or_fallback(request, user, candidates=["admin.html", "index.html", "app.html", "dashboard.html", "user.html"])


# ---------------------------
# Helpers: web formatting, paging, history pack
# ---------------------------
def _format_web_results(results: Any) -> str:
    if not results:
        return ""
    if isinstance(results, str):
        s = results.strip()
        return s[:LYRA_WEB_MAX_TOTAL_CHARS].rstrip() if LYRA_WEB_MAX_TOTAL_CHARS > 0 else s

    out: List[str] = []
    total = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        snippet = (item.get("snippet") or "").strip()

        if LYRA_WEB_MAX_CHARS_PER_SOURCE > 0 and len(snippet) > LYRA_WEB_MAX_CHARS_PER_SOURCE:
            snippet = snippet[:LYRA_WEB_MAX_CHARS_PER_SOURCE].rstrip() + "…"

        block = "\n".join([x for x in (title, url, snippet) if x]).strip()
        if not block:
            continue

        if LYRA_WEB_MAX_TOTAL_CHARS > 0:
            if total >= LYRA_WEB_MAX_TOTAL_CHARS:
                break
            remaining = LYRA_WEB_MAX_TOTAL_CHARS - total
            if len(block) > remaining:
                block = block[:max(0, remaining)].rstrip() + "\n\n[TRUNCATED_WEB]"
                out.append(block)
                break

        out.append(block)
        total += len(block) + 2

    return "\n\n".join(out).strip()



def _split_pages(text: str, page_chars: int, max_pages: int) -> List[str]:
    """
    Split text into pages for UI convenience.

    Note:
    - page_chars controls *page size* only (presentation), not total answer length.
    - max_pages <= 0 means "no cap" (do not truncate).
    """
    s = (text or "").strip()
    if not s:
        return []
    if page_chars <= 0:
        return [s]

    cap = None if max_pages <= 0 else max(1, max_pages)

    pages: List[str] = []
    i = 0
    n = len(s)
    while i < n and (cap is None or len(pages) < cap):
        pages.append(s[i : i + page_chars])
        i += page_chars

    # If a cap was configured and we hit it, we keep the remainder in a final page
    # so we do not truncate content.
    if i < n:
        pages.append(s[i:])

    return pages


def _with_paging_footer(page_text: str, page_idx: int, total_pages: int, has_more: bool) -> str:
    base = (page_text or "").rstrip()
    if total_pages <= 1:
        return base
    if has_more:
        return f"{base}\n\n---\nPage {page_idx}/{total_pages}. Type NEXT for the next page."
    return f"{base}\n\n---\nPage {page_idx}/{total_pages}."


def _is_next_command(query: str) -> bool:
    q = (query or "").strip().lower()
    return q in ("next", "continue", "more", "continuare", "urmatorul", "următorul", "2", "3", "4")


def _compact_history(username: str) -> str:
    """Return a compact conversation history string for continuity."""
    try:
        rows = last_chats(username, limit=max(4, LYRA_HISTORY_MESSAGES))
    except Exception:
        rows = []

    norm: List[Dict[str, str]] = []
    for r in rows or []:
        if isinstance(r, dict):
            role = str(r.get("role", "") or r.get("speaker", "") or "").strip().lower()
            content = str(r.get("content", "") or r.get("text", "") or "").strip()
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            role = str(r[0] or "").strip().lower()
            content = str(r[1] or "").strip()
        else:
            continue

        if role not in ("user", "assistant"):
            continue
        if not content:
            continue
        norm.append({"role": role, "content": content})

    if not norm:
        return ""

    norm = norm[-LYRA_HISTORY_MESSAGES:]

    lines: List[str] = []
    for item in norm:
        who = "User" if item["role"] == "user" else "Assistant"
        msg = item["content"].replace("\r", " ").strip()
        lines.append(f"{who}: {msg}")

    return "\n".join(lines).strip()



def _call_llm(prompt: str, web_text: str) -> str:
    # llm_openai.py retries internally on empty responses.
    return answer(prompt, [], web_results=web_text, behavior="GENERAL")


def _strip_incomplete_marker(text: str) -> str:
    return (text or "").replace(LYRA_INCOMPLETE_MARKER, "").strip()


def _auto_continue(prompt: str, web_text: str, first_chunk: str) -> str:
    """
    If the model hit its internal output cap, llm_openai.py appends LYRA_INCOMPLETE_MARKER.
    We automatically continue until the marker disappears (or safety rounds hit).
    """
    chunk = first_chunk or ""
    if not LYRA_AUTO_CONTINUE:
        return _strip_incomplete_marker(chunk)

    parts: List[str] = []
    rounds = 0

    while True:
        parts.append(_strip_incomplete_marker(chunk))

        # Stop if complete
        if LYRA_INCOMPLETE_MARKER not in (chunk or ""):
            break

        rounds += 1
        if LYRA_AUTO_CONTINUE_MAX_ROUNDS > 0 and rounds >= LYRA_AUTO_CONTINUE_MAX_ROUNDS:
            parts.append("\n\n---\n[Auto-continue stopped after safety limit. Type NEXT to continue.]")
            break

        tail = parts[-1][-max(0, LYRA_CONTINUE_TAIL_CHARS):] if LYRA_CONTINUE_TAIL_CHARS > 0 else parts[-1]
        continue_prompt = (
            f"{prompt}\n\n"
            "Continue exactly from where you stopped. Do not repeat previous text.\n"
            "Continue in the same style and structure.\n\n"
            f"Last output tail (for continuity):\n{tail}\n"
        )

        chunk = _call_llm(continue_prompt, web_text)

    return "".join(parts).strip()



# -----------------------------
# Public chat async job cache (kept for slow hardware / proxy timeouts)
# -----------------------------
_PUBLIC_JOBS_LOCK = threading.Lock()
_PUBLIC_JOBS: Dict[str, Dict[str, Any]] = {}
_PUBLIC_JOBS_TTL_SECONDS = 60 * 30
_PUBLIC_JOBS_MAX_ITEMS = 200


def _public_job_key(client_ip: str, query: str) -> str:
    base = f"{client_ip}|{(query or '').strip().lower()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _prune_public_jobs() -> None:
    now = time.time()
    with _PUBLIC_JOBS_LOCK:
        expired = [k for k, v in _PUBLIC_JOBS.items() if now - float(v.get("ts", 0.0)) > _PUBLIC_JOBS_TTL_SECONDS]
        for k in expired:
            _PUBLIC_JOBS.pop(k, None)

        if len(_PUBLIC_JOBS) > _PUBLIC_JOBS_MAX_ITEMS:
            items = sorted(_PUBLIC_JOBS.items(), key=lambda kv: float(kv[1].get("ts", 0.0)))
            for k, _ in items[: max(0, len(_PUBLIC_JOBS) - _PUBLIC_JOBS_MAX_ITEMS)]:
                _PUBLIC_JOBS.pop(k, None)


def _public_set_job(job_key: str, status: str, answer_text: str = "", web_requested: bool = False, web_used: bool = False) -> None:
    with _PUBLIC_JOBS_LOCK:
        _PUBLIC_JOBS[job_key] = {
            "ts": time.time(),
            "status": status,
            "answer": answer_text,
            "web_requested": web_requested,
            "web_used": web_used,
        }


async def _run_public_job(job_key: str, query: str, payload_web: Optional[bool]) -> None:
    try:
        effective_web = bool(payload_web) if payload_web is not None else bool(LYRA_WEB_DEFAULT)
        web_text = ""
        web_used = False
        if effective_web:
            try:
                raw = ddg_search(query, max_results=LYRA_WEB_MAX_RESULTS, timeout=LYRA_WEB_TIMEOUT)
                web_text = _format_web_results(raw)
                web_used = bool(web_text.strip())
            except Exception:
                web_text = ""
                web_used = False

        prompt = (
            "You are Lyra, a personal cognitive workbench for deep analysis and drafting.\n"
            "Be precise and helpful. Always output a written final answer.\n\n"
            f"Task: {query.strip()}\n"
        )

        first = _call_llm(prompt, web_text)
        response = _auto_continue(prompt, web_text, first)
        _public_set_job(job_key, status="done", answer_text=response, web_requested=effective_web, web_used=web_used)
    except Exception as e:
        _public_set_job(job_key, status="error", answer_text=f"internal_error: {type(e).__name__}: {e}", web_requested=False, web_used=False)


@app.post("/api/public_chat")
async def api_public_chat(payload: PublicChatRequest, request: Request):
    query = (payload.query or "").strip()
    if not query:
        return JSONResponse({"error": "query missing"}, status_code=422)

    page = int(payload.page or 1)
    if page < 1:
        return JSONResponse({"error": "page must be >= 1"}, status_code=422)

    _prune_public_jobs()
    client_ip = request.client.host if request and request.client else "unknown"
    job_key = _public_job_key(client_ip, query)

    with _PUBLIC_JOBS_LOCK:
        job = _PUBLIC_JOBS.get(job_key)

    if job is not None:
        status_ = str(job.get("status", "pending"))
        if status_ == "done":
            full = str(job.get("answer", "") or "")
            pages = _split_pages(full, LYRA_CHAT_PAGE_CHARS, LYRA_CHAT_MAX_PAGES)
            total_pages = len(pages) if pages else 1
            idx = page - 1
            if idx < 0 or idx >= total_pages:
                return JSONResponse({"error": "page out of range", "total_pages": total_pages}, status_code=422)
            has_more = (idx + 1) < total_pages
            page_text = pages[idx] if pages else full
            answer_text = _with_paging_footer(page_text, page, total_pages, has_more=has_more)
            return JSONResponse(
                {
                    "answer": answer_text,
                    "web_requested": bool(job.get("web_requested", False)),
                    "web_used": bool(job.get("web_used", False)),
                }
            )

        if status_ == "error":
            return JSONResponse({"answer": job.get("answer", "internal_error"), "web_requested": False, "web_used": False})

        return JSONResponse(
            {
                "answer": "Processing... Please resend the same question in a few seconds.",
                "web_requested": bool(payload.web) if payload.web is not None else bool(LYRA_WEB_DEFAULT),
                "web_used": False,
            }
        )

    effective_web = bool(payload.web) if payload.web is not None else bool(LYRA_WEB_DEFAULT)
    _public_set_job(job_key, status="pending", answer_text="Processing...", web_requested=effective_web, web_used=False)
    asyncio.create_task(_run_public_job(job_key, query, payload.web))
    return JSONResponse({"answer": "Processing... Please resend the same question in a few seconds.", "web_requested": effective_web, "web_used": False})


@app.post("/api/chat")
async def api_chat(payload: ChatRequest, request: Request):
    user = require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    query = (payload.query or "").strip()
    if not query:
        return JSONResponse({"error": "query missing"}, status_code=422)

    username = user["username"]

    # Paging continuation
    if _is_next_command(query):
        add_chat(username, "user", query, datetime.utcnow().isoformat())
        page_text, _has_more = pending_next(username)
        if page_text is None:
            response = "No pending output. Ask a question first."
            add_chat(username, "assistant", response, datetime.utcnow().isoformat())
            return JSONResponse({"answer": response, "web_requested": False, "web_used": False})
        add_chat(username, "assistant", page_text, datetime.utcnow().isoformat())
        return JSONResponse({"answer": page_text, "web_requested": False, "web_used": False})

    # New query invalidates previous pending continuation
    pending_clear(username)

    add_chat(username, "user", query, datetime.utcnow().isoformat())

    # Web (optional)
    effective_web = bool(payload.web) if payload.web is not None else bool(LYRA_WEB_DEFAULT)
    web_text = ""
    web_used = False
    if effective_web:
        try:
            raw = ddg_search(query, max_results=LYRA_WEB_MAX_RESULTS, timeout=LYRA_WEB_TIMEOUT)
            web_text = _format_web_results(raw)
            web_used = bool(web_text.strip())
        except Exception:
            web_text = ""
            web_used = False

    # Conversation continuity
    history = _compact_history(username)

    prompt_parts: List[str] = []
    prompt_parts.append("User task:")
    prompt_parts.append((query or "").strip())
    prompt = "\n\n".join(prompt_parts).strip()

    if history:
        prompt += f"Conversation so far (for continuity):\n{history}\n\n"
    prompt += f"Task: {query}\n"

    first = _call_llm(prompt, web_text)
    response_full = _auto_continue(prompt, web_text, first)

    # Server-side paging
    pages = _split_pages(response_full, LYRA_CHAT_PAGE_CHARS, LYRA_CHAT_MAX_PAGES)
    if len(pages) > 1:
        pending_set(username, pages, cursor=1)
        response = _with_paging_footer(pages[0], 1, len(pages), has_more=True)
    else:
        response = response_full.strip() if isinstance(response_full, str) else str(response_full)

    add_chat(username, "assistant", response, datetime.utcnow().isoformat())

    return JSONResponse({"answer": response, "web_requested": effective_web, "web_used": web_used})


@app.get("/api/chats")
def api_chats(request: Request, n: int = 30):
    user = require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = last_chats(user["username"], limit=n)
    return JSONResponse({"items": rows})


@app.post("/admin/upload")
async def admin_upload(request: Request, file: UploadFile = File(...)):
    user = require_admin(request)
    if not user:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    os.makedirs(DOCS_DIR, exist_ok=True)

    filename = os.path.basename(file.filename or "")
    if not filename:
        return JSONResponse({"error": "filename missing"}, status_code=422)

    dst = os.path.join(DOCS_DIR, filename)
    data = await file.read()
    with open(dst, "wb") as f:
        f.write(data)

    return JSONResponse({"ok": True, "saved_as": dst})


@app.post("/api/ingest")
def api_ingest(request: Request):
    user = require_admin(request)
    if not user:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    # Local ingest is disabled in the OpenAI-first Lyra Octavian setup.
    return JSONResponse({"chunks": 0, "message": "local_ingest_disabled"})
