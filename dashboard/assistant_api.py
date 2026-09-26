"""dashboard/assistant_api.py — read endpoints for the assistant's own state.

Kept in its own module so `dashboard/server.py` needs two lines added rather
than a hundred: build the router, include it, and the phone gets everything the
brain knows.

Auth reuses the dashboard's existing token check, which is passed in. The phone
*page* itself is served without auth because it is only a shell — every piece
of real data behind it requires a token, so an unauthenticated visitor learns
nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
PHONE_PAGE = Path(__file__).resolve().parent / "static" / "jarvis_phone.html"

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    _FASTAPI_OK = True
except ImportError:                                        # pragma: no cover
    _FASTAPI_OK = False


def _safe(fn: Callable[..., Any], *args, **kwargs) -> Any:
    """Never let one broken subsystem turn into a 500 on the phone."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:                                # noqa: BLE001
        return {"error": f"{getattr(fn, '__name__', 'call')} failed: {exc}"}


def build_router(auth: Callable[[Request], bool], port: int = 8000, enable_phone_page: bool = True):
    """Return an APIRouter, or None when FastAPI is unavailable."""
    if not _FASTAPI_OK:
        return None

    router = APIRouter()

    def _unauthorised() -> JSONResponse:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    @router.get("/phone", response_class=HTMLResponse)
    async def phone_page():
        """Mobile page: mic, attention digest, handoffs, Jarvis's recent speech."""
        if PHONE_PAGE.exists():
            return FileResponse(str(PHONE_PAGE), media_type="text/html")
        return HTMLResponse(
            "<h1>Phone page missing</h1><p>dashboard/static/jarvis_phone.html was not found.</p>",
            status_code=404,
        )

    @router.get("/assistant/state")
    async def assistant_state(req: Request):
        if not auth(req):
            return _unauthorised()
        from core.brain import get_brain
        from core import phone_bridge, senses
        brain = get_brain()
        return JSONResponse({
            "brain": _safe(brain.status),
            "phone": _safe(phone_bridge.state),
            "pairing": _safe(phone_bridge.pairing_info, port),
            "readings": _safe(senses.SensesHub(persist=False).snapshot),
        })

    @router.get("/assistant/digest")
    async def assistant_digest(req: Request, limit: int = 15):
        if not auth(req):
            return _unauthorised()
        from core.brain import get_brain
        return JSONResponse({
            "digest": _safe(get_brain().engine.peek_digest, limit),
        })

    @router.get("/assistant/activity")
    async def assistant_activity(req: Request, limit: int = 25, category: Optional[str] = None):
        if not auth(req):
            return _unauthorised()
        from core import activity_log
        return JSONResponse({
            "entries": _safe(activity_log.recent, limit, category),
            "stats": _safe(activity_log.stats),
        })

    @router.get("/assistant/phone/pull")
    async def phone_pull(req: Request, limit: int = 10, mark: bool = False):
        """What the phone should show now. Reading does not consume unless asked."""
        if not auth(req):
            return _unauthorised()
        from core import phone_bridge
        return JSONResponse({
            "items": _safe(phone_bridge.pull_for_phone, limit, mark),
            "state": _safe(phone_bridge.state),
        })

    @router.post("/assistant/phone/note")
    async def phone_note(req: Request):
        """A note typed or dictated on the phone, kept for continuity."""
        if not auth(req):
            return _unauthorised()
        from core import phone_bridge
        try:
            body = await req.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty note"}, status_code=400)
        return JSONResponse({"ok": True, "item": _safe(phone_bridge.inbox_append, text, "phone")})

    @router.post("/assistant/phone/ack")
    async def phone_ack(req: Request):
        """The phone confirms it has shown specific items."""
        if not auth(req):
            return _unauthorised()
        from core import phone_bridge
        try:
            body = await req.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        ids = (body or {}).get("ids") or []
        return JSONResponse({"ok": True, "detail": _safe(phone_bridge.ack, list(ids))})

    @router.post("/assistant/complete")
    async def assistant_complete(req: Request):
        """Mark a handoff finished (used by the phone, or by voice on request)."""
        if not auth(req):
            return _unauthorised()
        from core import phone_bridge
        try:
            body = await req.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        return JSONResponse({
            "ok": True,
            "detail": _safe(phone_bridge.complete,
                            str((body or {}).get("id") or ""),
                            str((body or {}).get("result") or "")),
        })

    return router
