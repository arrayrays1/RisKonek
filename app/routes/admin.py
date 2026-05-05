from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, UserRole, Barangay, AuditLog
from app.auth import require_role, hash_password
from typing import Optional

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


# ── DASHBOARD ─────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={"user": user}
    )


# ── USER LIST ─────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def user_list(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    all_users = db.query(User).order_by(User.created_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context={"user": user, "all_users": all_users}
    )


# ── CREATE USER — GET (show form) ─────────────────────────────────────

@router.get("/users/create", response_class=HTMLResponse)
def create_user_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/user_form.html",
        context={
            "user": user,
            "barangays": barangays,
            "roles": [r.value for r in UserRole],
            "edit_mode": False,
            "target_user": None,
            "error": None
        }
    )


# ── CREATE USER — POST (handle form) ─────────────────────────────────

@router.post("/users/create")
async def create_user_submit(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    contact_number: str = Form(""),
    barangay_id: Optional[int] = Form(None)
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()

    # Check username not already taken
    existing_username = db.query(User).filter(User.username == username).first()
    if existing_username:
        return templates.TemplateResponse(
            request=request,
            name="admin/user_form.html",
            context={
                "user": user,
                "barangays": barangays,
                "roles": [r.value for r in UserRole],
                "edit_mode": False,
                "target_user": None,
                "error": f"Username '{username}' is already taken."
            }
        )

    # check email not already taken
    existing_email = db.query(User).filter(User.email == email).first()
    if existing_email:
        return templates.TemplateResponse(
            request=request,
            name="admin/user_form.html",
            context={
                "user": user,
                "barangays": barangays,
                "roles": [r.value for r in UserRole],
                "edit_mode": False,
                "target_user": None,
                "error": f"Email '{email}' is already registered."
            }
        )

    # create the new user
    new_user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role=UserRole(role),
        contact_number=contact_number,
        barangay_id=barangay_id if barangay_id else None,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # log the action
    log = AuditLog(
        user_id=user["id"],
        action="created",
        target_table="users",
        target_id=new_user.id,
        description=f"Admin created new user: {username} with role: {role}"
    )
    db.add(log)
    db.commit()

    return RedirectResponse(url="/admin/users?success=User+created+successfully", status_code=302)


# ── EDIT USER — GET (show form pre-filled) ───────────────────────────

@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users", status_code=302)

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/user_form.html",
        context={
            "user": user,
            "barangays": barangays,
            "roles": [r.value for r in UserRole],
            "edit_mode": True,
            "target_user": target_user,
            "error": None
        }
    )


# ── EDIT USER — POST ─────────────────────────────────────────────────

@router.post("/users/{user_id}/edit")
async def edit_user_submit(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    role: str = Form(...),
    contact_number: str = Form(""),
    barangay_id: Optional[int] = Form(None),
    new_password: str = Form("")
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users", status_code=302)

    # Update fields
    target_user.email = email
    target_user.role = UserRole(role)
    target_user.contact_number = contact_number
    target_user.barangay_id = barangay_id if barangay_id else None

    # Only update password if a new one was provided
    if new_password.strip():
        target_user.password_hash = hash_password(new_password)

    db.commit()

    # Log the action
    log = AuditLog(
        user_id=user["id"],
        action="updated",
        target_table="users",
        target_id=user_id,
        description=f"Admin updated user: {target_user.username} (role: {role})"
    )
    db.add(log)
    db.commit()

    return RedirectResponse(url="/admin/users?success=User+updated+successfully", status_code=302)


# ── TOGGLE ACTIVE STATUS ─────────────────────────────────────────────

@router.post("/users/{user_id}/toggle")
def toggle_user_status(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    # Prevent admin from deactivating their own account
    if user_id == user["id"]:
        return RedirectResponse(url="/admin/users?error=You+cannot+deactivate+your+own+account", status_code=302)

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users", status_code=302)

    target_user.is_active = not target_user.is_active
    db.commit()

    action = "activated" if target_user.is_active else "deactivated"

    # Log the action
    log = AuditLog(
        user_id=user["id"],
        action=action,
        target_table="users",
        target_id=user_id,
        description=f"Admin {action} user: {target_user.username}"
    )
    db.add(log)
    db.commit()

    return RedirectResponse(url=f"/admin/users?success=User+{action}+successfully", status_code=302)