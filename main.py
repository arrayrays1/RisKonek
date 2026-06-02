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

#session middleware, what makes login sessions work
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "fallback-secret-change-this")
)


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
            return RedirectResponse(url="/bdrrmo/dashboard")
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