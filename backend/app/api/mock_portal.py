"""Development-only procurement portal used to verify the Mode A Playwright worker end-to-end
(login → upload slots → submit → OTP pause → confirmation). Disabled when APP_ENV=production."""
from __future__ import annotations

import random
import secrets
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/mock-portal", include_in_schema=False)
_SESSIONS: dict[str, dict] = {}
PAGE = "<html><head><title>Mock eProcurement Portal</title><style>body{{font-family:sans-serif;max-width:720px;margin:40px auto}}label{{display:block;margin:8px 0}}</style></head><body>{}</body></html>"


@router.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return PAGE.format("<h1>Supplier login</h1><form method=post action='/mock-portal/login'><label>Username <input name=username></label>"
                       "<label>Password <input type=password name=password></label><button type=submit>Log in</button></form>")


@router.post("/login")
def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    sid = secrets.token_hex(8)
    _SESSIONS[sid] = {"user": username, "files": {}}
    r = RedirectResponse("/mock-portal/home", status_code=303)
    r.set_cookie("mp", sid)
    return r


@router.get("/home", response_class=HTMLResponse)
def home() -> str:
    return PAGE.format("<h1>Welcome</h1><p>Open a tender to submit.</p>")


@router.get("/tenders/{ref}", response_class=HTMLResponse)
def tender_page(ref: str) -> str:
    slots = "".join(f"<label>{n} <input type=file name={k}></label>" for k, n in
                    [("sbd1", "SBD 1"), ("sbd31", "SBD 3.1"), ("sbd4", "SBD 4"), ("sbd61", "SBD 6.1"), ("sbd62", "SBD 6.2"), ("supporting", "Supporting documents")])
    return PAGE.format(f"<h1>Submit bid: {ref}</h1><form method=post enctype=multipart/form-data action='/mock-portal/tenders/{ref}/submit'>"
                       f"{slots}<button id=submit-bid type=submit>Submit bid</button></form>")


@router.post("/tenders/{ref}/submit", response_class=HTMLResponse)
async def submit(ref: str, request: Request) -> str:
    form = await request.form()
    sid = request.cookies.get("mp", "")
    files = {k: (v.filename, len(await v.read())) for k, v in form.items() if hasattr(v, "filename") and v.filename}
    _SESSIONS.setdefault(sid, {})["files"] = files
    _SESSIONS[sid]["ref"] = ref
    return PAGE.format(f"<h1>One-time PIN</h1><p>An OTP was sent to your registered cellphone. {len(files)} file(s) received.</p>"
                       f"<form method=post action='/mock-portal/tenders/{ref}/otp'><label>OTP <input name=otp></label>"
                       "<button id=confirm-otp type=submit>Confirm submission</button></form>")


@router.post("/tenders/{ref}/otp", response_class=HTMLResponse)
def otp(ref: str, request: Request, otp: str = Form(...)) -> str:
    if not otp.strip().isdigit():
        return PAGE.format("<h1>Invalid OTP</h1>")
    files = _SESSIONS.get(request.cookies.get("mp", ""), {}).get("files", {})
    reference = f"ETR-{datetime.now().year}-{random.randint(100000, 999999)}"
    listing = "".join(f"<li>{n} ({s:,} bytes)</li>" for n, s in files.values())
    return PAGE.format(f"<div id=confirmation><h1>Submission received</h1><p>Reference number: <b>{reference}</b></p>"
                       f"<p>Tender {ref} · {datetime.now().isoformat(timespec='seconds')}</p><ul>{listing}</ul></div>")
