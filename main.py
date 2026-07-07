from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from app.database import engine
import app.models as models
import os
import secrets
from dotenv import load_dotenv

load_dotenv()

# creates all database tables on startup
models.Base.metadata.create_all(bind=engine)


# ─── CSRF PROTECTION ──────────────────────────────────────────────────
# Synchroniser-token pattern. A random token is minted once per session and
# stored in the (signed) session cookie. Every state-changing request must
# echo it back in a `csrf_token` form field; a missing or mismatched token is
# rejected with 403 before the route handler runs. GET/HEAD/OPTIONS are safe
# methods and only ensure the token exists so it can be rendered into forms.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


async def csrf_protect(request: Request):
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token

    if request.method not in _CSRF_SAFE_METHODS:
        form = await request.form()
        submitted = form.get("csrf_token")
        if not submitted or not secrets.compare_digest(str(submitted), token):
            raise HTTPException(
                status_code=403,
                detail="CSRF token missing or invalid. Please reload the page and try again.",
            )


# Applied to every route (including the standalone ones below) via the
# app-level dependency list.
app = FastAPI(title="RisKonek", dependencies=[Depends(csrf_protect)])

# Fail closed: never sign sessions with a hardcoded fallback secret. A missing
# SECRET_KEY is a configuration error, not something to silently work around.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Define it in your .env before starting RisKonek."
    )

# COOKIE_SECURE / HTTPS toggle. Default False so local HTTP and OWASP ZAP
# testing work; set COOKIE_SECURE=true in .env when deploying over HTTPS so
# session cookies get the Secure flag and HSTS is sent.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes")

# session middleware, what makes login sessions work
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",        # mitigates CSRF-style cross-site cookie sending
    https_only=COOKIE_SECURE,  # Secure flag only when serving over HTTPS
    max_age=60 * 60 * 8,    # 8-hour session lifetime
)

# ─── CONTENT SECURITY POLICY ──────────────────────────────────────────
# The host allowlist mirrors the CDN/tile hosts actually referenced by the
# templates: jsDelivr (Bootstrap, Bootstrap Icons, Chart.js), unpkg (Leaflet),
# and OpenStreetMap tiles.
#
# script-src: NO 'unsafe-inline'. Every inline <script> carries a per-request
#   nonce (request.state.csp_nonce, rendered into the templates) and all inline
#   on* event handlers have been removed in favour of delegated listeners in
#   /static/js/rk-forms.js. Per the CSP spec, the presence of a nonce makes a
#   browser ignore 'unsafe-inline' for this directive anyway, so leaving it out
#   is what actually closes the ZAP "script-src unsafe-inline" finding.
#
# style-src: 'unsafe-inline' is RETAINED (phase 1). The templates still rely on
#   ~225 inline style="" attributes, which CSP nonces/hashes CANNOT cover (they
#   apply only to <style>/<script> elements, never to style attributes). Adding
#   a style nonce here would make the browser ignore 'unsafe-inline' and break
#   every one of those attributes. Removing it is deferred to a dedicated CSS
#   refactor pass — see SECURITY_CSP_STYLE_REPORT.md.
#
# {nonce} is substituted per request below.
CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
    "img-src 'self' data: https://unpkg.com https://*.tile.openstreetmap.org; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add baseline security headers to every response (OWASP ZAP baseline).

    A fresh CSP nonce is minted per request and exposed on request.state so the
    Jinja templates can stamp it onto every inline <script>. The same value is
    written into the Content-Security-Policy header, so only first-party inline
    scripts we generated this request are allowed to execute.
    """
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce

    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Lock down powerful browser features the app never uses.
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = CSP_TEMPLATE.format(nonce=nonce)
    # HSTS is only meaningful over HTTPS; sending it on plain HTTP is ignored by
    # browsers and can cause issues, so gate it on the HTTPS/secure-cookie flag.
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ─── CROSS-ORIGIN (CORS) POSTURE ──────────────────────────────────────
# RisKonek is a single-origin, server-rendered app: the browser only ever talks
# to its own origin, so NO CORSMiddleware is registered and NO
# Access-Control-Allow-Origin header is ever emitted. That absence is the secure
# state ZAP's "Cross-Domain Misconfiguration" check looks for — the alert fires
# on a permissive `Access-Control-Allow-Origin: *`, which this app never sends.
# Do NOT add a wildcard ACAO here; if a separate front-end is ever introduced,
# register CORSMiddleware with an explicit origin allowlist (never "*") and
# allow_credentials=True so session cookies keep working.


app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

from app.routes.auth import router as auth_router
from app.routes.admin import router as admin_router
from app.routes.bdrrmo import router as bdrrmo_router
from app.routes.cfau import router as cfau_router
from app.routes.staff import router as staff_router
from app.routes.uploads import router as uploads_router

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(bdrrmo_router)
app.include_router(cfau_router)
app.include_router(staff_router)
app.include_router(uploads_router)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = request.session.get("user")

    if user:
        role = user["role"]
        if role == "admin":
            return RedirectResponse(url="/admin/dashboard")
        elif role == "bdrrmo":
            return RedirectResponse(url="/bdrrmo/profile")
        elif role == "cfau_oic":
            return RedirectResponse(url="/cfau/dashboard")
        elif role == "cdrrmo_staff":
            return RedirectResponse(url="/staff/dashboard")

    return templates.TemplateResponse(
    request=request,
    name="shared/home.html",
    context={"title": "RisKonek"
    })

@app.get("/health")
def health_check():
    return {"status": "RisKonek is running.", "database": "connected"}

@app.get("/unauthorized", response_class=HTMLResponse)
def unauthorized(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="shared/unauthorized.html",
        context={"title": "Access Denied"
    })