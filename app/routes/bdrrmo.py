from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Barangay
from app.auth import require_role

router = APIRouter(prefix="/bdrrmo")
templates = Jinja2Templates(directory="app/templates")

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["bdrrmo"])
    if isinstance(user, RedirectResponse):
        return user

    barangay = None
    if user.get("barangay_id"):
        barangay = db.query(Barangay).filter(
            Barangay.id == user["barangay_id"]
        ).first()

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/dashboard.html",
        context={"user": user, "barangay": barangay}
    )
