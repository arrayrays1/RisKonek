from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.auth import require_role

router = APIRouter(prefix="/cfau")
templates = Jinja2Templates(directory="app/templates")

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_role(request, ["cfau_oic"])
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="cfau/dashboard.html",
        context={"user": user}
    )