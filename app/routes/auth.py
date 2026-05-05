from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import verify_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ─── LOGIN PAGE ───────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="shared/login.html",
        context={"title": "Login — RisKonek", "error": None}
    )


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()

    # Basic validation
    if not username or not password:
        return templates.TemplateResponse(
            request=request,
            name="shared/login.html",
            context={
                "title": "Login — RisKonek",
                "error": "Please enter both username and password."
            }
        )

    # Find user in database
    user = db.query(User).filter(User.username == username).first()

    # Check user exists and password matches
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="shared/login.html",
            context={
                "title": "Login — RisKonek",
                "error": "Invalid username or password."
            }
        )

    # Check account is active
    if not user.is_active:
        return templates.TemplateResponse(
            request=request,
            name="shared/login.html",
            context={
                "title": "Login — RisKonek",
                "error": "Your account has been deactivated. Contact the administrator."
            }
        )

    # Store user info in session
    request.session["user"] = {
        "id": user.id,
        "username": user.username,
        "role": user.role.value,
        "barangay_id": user.barangay_id
    }

    # Redirect to correct dashboard based on role
    role = user.role.value
    if role == "admin":
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    elif role == "bdrrmo":
        return RedirectResponse(url="/bdrrmo/dashboard", status_code=302)
    elif role == "cfau_oic":
        return RedirectResponse(url="/cfau/dashboard", status_code=302)
    elif role == "cdrrmo_staff":
        return RedirectResponse(url="/staff/dashboard", status_code=302)
    else:
        return RedirectResponse(url="/", status_code=302)


# ─── LOGOUT ───────────────────────────────────────────

@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)