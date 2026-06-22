from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from app.database import engine
import app.models as models
import os
from dotenv import load_dotenv

load_dotenv()

# creates all database tables on startup
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="RisKonek")

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

# Content-Security-Policy host allowlist mirrors the CDN/tile hosts actually
# referenced by the templates: jsDelivr (Bootstrap, Bootstrap Icons, Chart.js),
# unpkg (Leaflet), and OpenStreetMap tiles. 'unsafe-inline' is required because
# the dashboard and Leaflet markers use inline <script> and style attributes.
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
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
    """Add baseline security headers to every response (OWASP ZAP baseline)."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = CSP_POLICY
    # HSTS is only meaningful over HTTPS; sending it on plain HTTP is ignored by
    # browsers and can cause issues, so gate it on the HTTPS/secure-cookie flag.
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


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