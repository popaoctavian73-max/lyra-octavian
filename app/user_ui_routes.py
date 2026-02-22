from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette import status
import os, shutil, json, time, uuid

from .auth import require_user, require_admin, redirect_to_login

templates = Jinja2Templates(directory="templates")
router = APIRouter()

DOCS_DIR = "DOCS"
PUBLIC_DIR = "PUBLIC_DOCS"
PENDING_DIR = "PENDING_UPLOADS"
INBOX_DIR = "CONTACT_INBOX"

os.makedirs(PENDING_DIR, exist_ok=True)
os.makedirs(PUBLIC_DIR, exist_ok=True)
os.makedirs(INBOX_DIR, exist_ok=True)

def _safe_name(name: str) -> str | None:
    """Allow only a plain filename (no paths)."""
    if not name:
        return None
    name = name.strip().replace("\\", "/")
    if "/" in name or ".." in name:
        return None
    # Keep it simple: reject suspicious characters
    if any(c in name for c in [":", "\0"]):
        return None
    return name

def _list_files(folder: str):
    if not os.path.exists(folder):
        return []
    out = []
    for f in sorted(os.listdir(folder)):
        if f.startswith("."):
            continue
        p = os.path.join(folder, f)
        if os.path.isfile(p):
            out.append(f)
    return out

@router.get("/user", response_class=HTMLResponse)
def user(request: Request):
    u = require_user(request)
    if not u:
        return redirect_to_login()
    return templates.TemplateResponse("user.html", {"request": request, "user": u})


@router.get("/public", response_class=HTMLResponse)
def public_home(request: Request):
    """Public landing page (no auth)."""
    return templates.TemplateResponse("public.html", {"request": request})

@router.get("/public/library", response_class=HTMLResponse)
def public_library_page(request: Request):
    """Public library UI (no auth). Uses existing /api/library endpoints."""
    return templates.TemplateResponse("public_library.html", {"request": request})


@router.get("/public/chat", response_class=HTMLResponse)
def public_chat_page(request: Request):
    """Public chat UI (no auth). Uses /api/public_chat."""
    return templates.TemplateResponse("public_chat.html", {"request": request})


@router.get("/api/status")
def api_status():
    """Mic status pentru UI: online + contoare."""
    docs = len(_list_files(PUBLIC_DIR))
    pending = len(_list_files(PENDING_DIR))
    return {"ok": True, "docs": docs, "pending": pending}

# Alias compatibil cu UI-ul existent (care folosește /api/library)
@router.get("/api/library")
def library():
    return {"files": _list_files(PUBLIC_DIR)}

@router.get("/api/library/view")
def library_view(name: str):
    return public_view(name)

@router.get("/api/library/download")
def library_download(name: str):
    return public_download(name)
# -----------------------
# Public Library (Variant 2)
# -----------------------

@router.get("/api/public/list")
def public_list():
    return {"files": _list_files(PUBLIC_DIR)}

@router.get("/api/public/view")
def public_view(name: str):
    safe = _safe_name(name)
    if not safe:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    p = os.path.join(PUBLIC_DIR, safe)
    if not os.path.exists(p) or not os.path.isfile(p):
        return JSONResponse({"error": "not found"}, status_code=404)

    ext = os.path.splitext(safe.lower())[1]
    if ext not in [".txt", ".md", ".json", ".log"]:
        return JSONResponse({"error": "unsupported type"}, status_code=415)

    # Return plain text; UI can render it.
    with open(p, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8")
    except Exception:
        # best-effort decode
        text = data.decode("latin-1", errors="replace")
    return PlainTextResponse(text)

@router.get("/api/public/download")
def public_download(name: str):
    safe = _safe_name(name)
    if not safe:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    p = os.path.join(PUBLIC_DIR, safe)
    if not os.path.exists(p) or not os.path.isfile(p):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, filename=safe)

# -----------------------
# User -> Admin Inbox (internal)
# -----------------------

@router.post("/api/contact/message")
async def contact_message(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    message: str = Form(""),
    file: UploadFile | None = File(None),
):
    u = require_user(request)
    if not u:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    name = (name or "").strip()[:120]
    email = (email or "").strip()[:160]
    message = (message or "").strip()[:8000]
    if not message:
        return JSONResponse({"error": "message missing"}, status_code=422)

    ts = int(time.time())
    mid = f"{ts}_{uuid.uuid4().hex[:10]}"
    record = {
        "id": mid,
        "ts": ts,
        "from_user": u.get("username"),
        "name": name,
        "email": email,
        "message": message,
        "attachment": None,
    }

    # Optional small attachment (store in INBOX_DIR, cap size ~5MB)
    if file is not None and file.filename:
        safe = _safe_name(os.path.basename(file.filename))
        if not safe:
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            return JSONResponse({"error": "file too large"}, status_code=413)
        att_name = f"{mid}__{safe}"
        with open(os.path.join(INBOX_DIR, att_name), "wb") as f:
            f.write(data)
        record["attachment"] = att_name

    with open(os.path.join(INBOX_DIR, f"{mid}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    return {"ok": True, "id": mid}

@router.get("/api/admin/inbox")
def admin_inbox(request: Request):
    a = require_admin(request)
    if not a:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    items = []
    for fn in sorted(os.listdir(INBOX_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(INBOX_DIR, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                j = json.load(f)
            items.append({
                "id": j.get("id"),
                "ts": j.get("ts"),
                "from_user": j.get("from_user"),
                "name": j.get("name"),
                "email": j.get("email"),
                "preview": (j.get("message") or "")[:160],
                "attachment": j.get("attachment"),
            })
        except Exception:
            continue
    return {"items": items[:200]}

@router.get("/api/admin/inbox_item")
def admin_inbox_item(request: Request, id: str):
    a = require_admin(request)
    if not a:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    safe = _safe_name(id + ".json")
    if not safe:
        return JSONResponse({"error": "invalid id"}, status_code=400)
    p = os.path.join(INBOX_DIR, safe)
    if not os.path.exists(p):
        return JSONResponse({"error": "not found"}, status_code=404)
    with open(p, "r", encoding="utf-8") as f:
        j = json.load(f)
    return j

@router.get("/api/admin/inbox_download")
def admin_inbox_download(request: Request, name: str):
    a = require_admin(request)
    if not a:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    safe = _safe_name(name)
    if not safe:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    p = os.path.join(INBOX_DIR, safe)
    if not os.path.exists(p) or not os.path.isfile(p):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, filename=safe)

# -----------------------
# User upload to pending (kept)
# -----------------------

@router.post("/api/user_upload")
async def user_upload(request: Request, file: UploadFile = File(...)):
    u = require_user(request)
    if not u:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    os.makedirs(PENDING_DIR, exist_ok=True)
    filename = os.path.basename(file.filename or "")
    safe = _safe_name(filename)
    if not safe:
        return JSONResponse({"error": "invalid filename"}, status_code=400)

    dst = os.path.join(PENDING_DIR, safe)
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "saved_as": safe}
