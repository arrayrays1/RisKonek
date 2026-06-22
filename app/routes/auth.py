from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.database import get_db
from app.models import User, AuditLog
from app.auth import verify_password, hash_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ─── LOGIN RATE LIMITING / LOCKOUT ────────────────────
# Process-local failed-login tracker, keyed by lowercased username. Counts
# both wrong-password and unknown-username attempts. NOT shared across
# uvicorn workers or restarts — acceptable for a single-process capstone
# deployment; see the security limitations notes.

_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 15 * 60  # 15 minutes

_login_attempts: dict[str, dict] = {}

# Pre-computed once at import so the unknown-username path can perform an
# equivalent bcrypt verification (timing-attack mitigation). The plaintext
# is irrelevant — it only needs to be a real bcrypt hash to compare against.
_DUMMY_HASH = hash_password("timing-attack-mitigation-placeholder")


def _client_ip(request: Request) -> str:
    """Best-effort client IP. No X-Forwarded-For handling — behind a proxy
    this would be the proxy's address (documented limitation)."""
    return request.client.host if request.client else "unknown"


def _prune(now: datetime) -> None:
    """Bound memory: drop entries whose lock has expired, plus idle partial
    counters untouched for longer than the lockout window."""
    stale = []
    for k, rec in _login_attempts.items():
        locked_until = rec.get("locked_until")
        updated_at = rec.get("updated_at")
        if locked_until and locked_until <= now:
            stale.append(k)
        elif not locked_until and updated_at and \
                (now - updated_at).total_seconds() > _LOCKOUT_SECONDS:
            stale.append(k)
    for k in stale:
        _login_attempts.pop(k, None)


def _is_locked(key: str):
    """Return (locked: bool, seconds_remaining: int). Auto-clears an expired
    lock so the counter resets cleanly."""
    rec = _login_attempts.get(key)
    if not rec:
        return False, 0
    locked_until = rec.get("locked_until")
    if locked_until:
        now = datetime.utcnow()
        if locked_until > now:
            return True, int((locked_until - now).total_seconds())
        _login_attempts.pop(key, None)  # lock expired — reset
    return False, 0


def _register_failure(key: str) -> bool:
    """Record one failed attempt. Returns True only on the attempt that first
    trips the lock (so the lockout event is audited exactly once)."""
    now = datetime.utcnow()
    _prune(now)
    rec = _login_attempts.get(key) or {"count": 0, "locked_until": None}
    rec["count"] += 1
    rec["updated_at"] = now
    just_locked = False
    if rec["count"] >= _MAX_FAILED_ATTEMPTS and not rec.get("locked_until"):
        rec["locked_until"] = now + timedelta(seconds=_LOCKOUT_SECONDS)
        just_locked = True
    _login_attempts[key] = rec
    return just_locked


def _clear_failures(key: str) -> None:
    """Drop a username's failure record (called on successful login)."""
    _login_attempts.pop(key, None)


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

    ip = _client_ip(request)

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

    # Lockout gate — reject locked usernames BEFORE any password work. No
    # audit row here: the lockout was already recorded once when the
    # threshold was reached, so repeated attempts produce no audit spam.
    key = username.lower()
    locked, remaining = _is_locked(key)
    if locked:
        minutes = max(1, (remaining + 59) // 60)
        print(f"[auth] Rejected login for '{username}' from {ip} — "
              f"locked ({remaining}s remaining)")
        return templates.TemplateResponse(
            request=request,
            name="shared/login.html",
            context={
                "title": "Login — RisKonek",
                "error": (
                    "Too many failed attempts. Your account is temporarily "
                    f"locked. Please try again in {minutes} minute(s)."
                )
            }
        )

    # Find user in database
    user = db.query(User).filter(User.username == username).first()

    # Verify the password. The unknown-username path still runs a bcrypt
    # verify against a dummy hash so both branches do equivalent work
    # (timing-attack mitigation).
    if user:
        password_ok = verify_password(password, user.password_hash)
    else:
        verify_password(password, _DUMMY_HASH)
        password_ok = False

    # Check user exists and password matches. The user-facing message is
    # identical for both cases so we never reveal whether a username exists.
    if not password_ok:
        just_locked = _register_failure(key)
        if user:
            # Existing account, wrong password — audit it (never log the
            # attempted password, only the username and reason).
            db.add(AuditLog(
                user_id=user.id,
                action="login_failed",
                target_table="users",
                target_id=user.id,
                description=f"Failed login for '{user.username}' — "
                            f"incorrect password (from {ip})"
            ))
            if just_locked:
                db.add(AuditLog(
                    user_id=user.id,
                    action="login_lockout",
                    target_table="users",
                    target_id=user.id,
                    description=f"Account '{user.username}' locked for "
                                f"{_LOCKOUT_SECONDS // 60} minutes after "
                                f"{_MAX_FAILED_ATTEMPTS} failed attempts "
                                f"(from {ip})"
                ))
            db.commit()
        else:
            # Unknown username — no user_id to attribute it to, so no
            # AuditLog row (would violate the NOT NULL constraint and create
            # enumeration noise). Server-side log only.
            print(f"[auth] Failed login for unknown username '{username}' "
                  f"(from {ip})")
            if just_locked:
                print(f"[auth] Lockout triggered for unknown username "
                      f"'{username}' (from {ip})")
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
        # Correct credentials but the account is disabled — record the
        # attempt; valid creds on a deactivated account are worth flagging.
        db.add(AuditLog(
            user_id=user.id,
            action="login_blocked_inactive",
            target_table="users",
            target_id=user.id,
            description=f"Blocked login for '{user.username}' — "
                        f"account is deactivated (from {ip})"
        ))
        db.commit()
        return templates.TemplateResponse(
            request=request,
            name="shared/login.html",
            context={
                "title": "Login — RisKonek",
                "error": "Your account has been deactivated. Contact the administrator."
            }
        )

    # Successful login — clear the failure counter for this username.
    _clear_failures(key)

    # Record last login + audit trail
    user.last_login = datetime.utcnow()
    db.add(AuditLog(
        user_id=user.id,
        action="login",
        target_table="users",
        target_id=user.id,
        description=f"User '{user.username}' logged in (from {ip})"
    ))
    db.commit()

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
        return RedirectResponse(url="/bdrrmo/profile", status_code=302)
    elif role == "cfau_oic":
        return RedirectResponse(url="/cfau/dashboard", status_code=302)
    elif role == "cdrrmo_staff":
        return RedirectResponse(url="/staff/dashboard", status_code=302)
    else:
        return RedirectResponse(url="/", status_code=302)


# ─── LOGOUT ───────────────────────────────────────────

@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    current = request.session.get("user")
    if current:
        ip = _client_ip(request)
        db.add(AuditLog(
            user_id=current["id"],
            action="logout",
            target_table="users",
            target_id=current["id"],
            description=f"User '{current['username']}' logged out (from {ip})"
        ))
        db.commit()
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)