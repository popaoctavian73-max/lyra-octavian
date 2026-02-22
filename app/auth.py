from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette import status
from urllib.parse import quote

def require_user(request: Request):
    return request.session.get("user")

def require_admin(request: Request):
    u = require_user(request)
    if not u or not u.get("is_admin"):
        return None
    return u

def redirect_to_login(next_path: str | None = None):
    """Redirect to login page and preserve where the user wanted to go."""
    if next_path:
        # only allow local paths
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        return RedirectResponse(url=f"/?next={quote(next_path)}", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
