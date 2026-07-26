from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional
from app.database import get_db
from app.auth import require_role
from app.services.contact_directory import build_directory_context

router = APIRouter(prefix="/staff")
templates = Jinja2Templates(directory="app/templates")

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="staff/dashboard.html",
        context={"user": user}
    )


# ══════════════════════════════════════════════════════════════════════
# CONTACT DIRECTORY — read-only, all barangays, grouped per barangay
# (shared across roles; see app/services/contact_directory.py).
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts", response_class=HTMLResponse)
def contact_directory(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    brgy: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user

    context = build_directory_context(
        db, q=q, brgy=brgy, page=page, per_page=per_page,
        directory_url="/staff/contacts", active_nav="contacts",
    )
    context["user"] = user
    return templates.TemplateResponse(
        request=request, name="shared/contact_directory.html", context=context,
    )
