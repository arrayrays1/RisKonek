# RisKonek

A web-based **Disaster Decision Support System (DSS)** for the City Disaster Risk Reduction and Management Office (CDRRMO) of **San Pedro, Laguna**. RisKonek consolidates barangay profiles, population and hazard data, critical-facility mapping, incident and inventory tracking, and AI-assisted document processing into a single role-based platform.

Built with **FastAPI**, **Jinja2** templates, and **SQLAlchemy** over a Medallion-style data lakehouse.

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [User Roles](#user-roles)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Environment Variables](#environment-variables)
- [Seeding Demo Data](#seeding-demo-data)
- [Running the App](#running-the-app)
- [Database & Migrations](#database--migrations)
- [AI Document Processing](#ai-document-processing)
- [Data Model](#data-model)
- [Project Structure](#project-structure)

---

## Features

- **Barangay profiles** — risk levels, officials, hazard types, and emergency contacts for all 27 barangays of San Pedro.
- **GIS facility map** — critical facilities (evacuation centers, clinics, schools, hospitals, staging areas) plotted on an interactive Leaflet map.
- **Inventory management** — relief resources and equipment with restock thresholds, expiry alerts, and serviceability review workflows.
- **Incident & operational reporting** — disaster event logging and CFAU post-incident reports.
- **AI-assisted uploads** — PDF/Excel/CSV reports flow through an ETL pipeline with optional AI summarization.
- **Audit trail** — append-only upload history and a system-wide action audit log.
- **Role-based access control** — four distinct user roles, each with a scoped dashboard.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI + Uvicorn (ASGI) |
| Templating | Jinja2 (server-rendered HTML) |
| Frontend | Bootstrap 5, Bootstrap Icons, Chart.js, Leaflet (via CDN) |
| ORM / DB | SQLAlchemy (sync) — SQLite by default, PostgreSQL-ready |
| Auth / sessions | Starlette `SessionMiddleware` + `passlib` password hashing |
| Document ETL | pdfplumber, PyMuPDF, pandas, openpyxl |
| AI summarization | Groq (Llama 3.1) — optional |

---

## User Roles

| Role | Login lands on | Scope |
|------|----------------|-------|
| `admin` | `/admin/dashboard` | Full system administration |
| `cfau_oic` | `/cfau/dashboard` | City Fire Auxiliary Unit operations |
| `bdrrmo` | `/bdrrmo/profile` | Barangay-scoped (tied to one barangay) |
| `cdrrmo_staff` | `/staff/dashboard` | CDRRMO logistics / staff operations |

---

## Prerequisites

- **Python 3.11+**
- **pip** and **venv** (bundled with Python)
- A **Groq API key** *(optional — only needed for AI summarization)*

---

## Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd riskonek

# 2. Create and activate a virtual environment
python -m venv venv

#    Windows (PowerShell)
venv\Scripts\Activate.ps1
#    macOS / Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env file (see Environment Variables below)
#    then generate a SECRET_KEY:
python -c "import secrets; print(secrets.token_hex(32))"

# 5. Seed demonstration data (creates the SQLite DB and test accounts)
python seed.py

# 6. Run the app
uvicorn main:app --reload
```

Then open <http://127.0.0.1:8000>.

> **Note:** This repository does not yet ship a `requirements.txt`. Until one is committed, install the core dependencies directly:
>
> ```bash
> pip install fastapi uvicorn[standard] jinja2 sqlalchemy python-dotenv \
>     passlib itsdangerous python-multipart pdfplumber pymupdf \
>     pandas openpyxl groq
> ```
>
> After your environment is working, freeze it so others can reproduce it:
>
> ```bash
> pip freeze > requirements.txt
> ```

---

## Environment Variables

RisKonek reads configuration from a `.env` file in the project root (loaded via `python-dotenv`). Create one with the following keys:

```ini
# REQUIRED — signs session cookies. The app refuses to start without it.
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=your-generated-secret-here

# Database connection string. Defaults to a local SQLite file.
DATABASE_URL=sqlite:///./instance/riskonek.db

# OPTIONAL — enables AI summarization of uploaded reports.
# Leave as a placeholder to disable AI cleanly (uploads still work).
GROQ_API_KEY=groq_api_key_here

# Set to true ONLY when serving over HTTPS (production). Adds the Secure
# flag to session cookies and sends HSTS. Keep false for local HTTP testing.
COOKIE_SECURE=false
```

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `SECRET_KEY` | **Yes** | — | Signs session cookies; app raises `RuntimeError` if missing |
| `DATABASE_URL` | No | `sqlite:///./instance/riskonek.db` | SQLAlchemy connection string |
| `GROQ_API_KEY` | No | placeholder | Groq API key for AI summaries |
| `COOKIE_SECURE` | No | `false` | Toggles Secure cookies + HSTS for HTTPS |

---

## Seeding Demo Data

`seed.py` populates the database with **San Pedro demonstration data** — 27 barangays (real 2020 PSA population figures), population records, historical incidents, relief resources, equipment, critical facilities, and one test user per role.

```bash
python seed.py
```

It is **idempotent** — existing records are skipped, so it is safe to re-run. To start completely fresh, delete `instance/riskonek.db` first, then run it again.

If the CDRRMO critical-facilities Excel dataset is present, facilities are imported from it; otherwise the script falls back to jittered demo facilities.

**Test accounts** (all use password `password123`):

| Username | Role | Lands on |
|----------|------|----------|
| `admin` | admin | `/admin/dashboard` |
| `cfau_oic` | cfau_oic | `/cfau/dashboard` |
| `bdrrmo1` | bdrrmo (Cuyab) | `/bdrrmo/profile` |
| `staff1` | cdrrmo_staff | `/staff/dashboard` |

> All seeded data is for demonstration only — will replace with actual CDRRMO records before deployment.

---

## Running the App

```bash
# Development (auto-reload)
uvicorn main:app --reload

# Specify host/port
uvicorn main:app --host 0.0.0.0 --port 8000

# Production-style (multiple workers, no reload)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

Useful routes:

- `/` — redirects logged-in users to their role dashboard, otherwise shows the home page
- `/login` — login page
- `/health` — JSON health check
- `/unauthorized` — access-denied page

### Security headers

Every response carries baseline OWASP hardening: a Content-Security-Policy locked to the CDN/tile hosts actually used (jsDelivr, unpkg, OpenStreetMap), plus `X-Frame-Options`, `X-Content-Type-Options`, and `Referrer-Policy`. HSTS is sent only when `COOKIE_SECURE=true`. Sessions are signed, `SameSite=Lax`, and expire after 8 hours.

---

## Database & Migrations

- On startup, `main.py` calls `models.Base.metadata.create_all(bind=engine)`, which **creates new tables** but **never alters existing ones**.
- When columns are added to existing tables, use the idempotent scripts in `scripts/`. Each one is guarded by existence checks and is safe to run repeatedly:

```bash
python scripts/migrate_facility_status.py
python scripts/migrate_upload_lifecycle.py
python scripts/migrate_cfau_operations.py
python scripts/migrate_bdrrmo_week9.py
python scripts/migrate_equipment_archive.py
python scripts/migrate_repair_scheduling.py
```

> If you are starting from a fresh database, you do not need the migration scripts — `create_all()` builds the current schema for you.

To switch to PostgreSQL (or another DB), set `DATABASE_URL` accordingly and install the appropriate driver (e.g. `pip install psycopg2-binary`).

---

## AI Document Processing

Uploaded PDF/Excel/CSV reports flow through a lightweight ETL pipeline in `app/etl/`:

1. **Extract** — `extract_pdf.py` (pdfplumber → PyMuPDF fallback) and `extract_excel.py` (pandas/openpyxl) pull raw text/tables.
2. **Summarize (work in progress)** — `ai_pipeline.py` sends extracted text to **Groq (Llama 3.1)** for a concise plain-English summary.
3. **Structure & store** — results are saved to `UploadedReport` (`ai_summary`, `extracted_data` JSON) and progress through the upload lifecycle.

**AI is fully optional.** If `GROQ_API_KEY` is unset or a placeholder, summarization silently no-ops and uploads still work — extraction never fails because AI is unavailable.

---

## Data Model

Core tables defined in `app/models.py`:

| Table | Purpose |
|-------|---------|
| `barangays` | Barangay profile, risk level, officials, hazard types, emergency contacts |
| `users` | Accounts across the 4 roles, scoped to a barangay where applicable |
| `populations` | Census snapshots per barangay |
| `facilities` | Critical facilities with map coordinates and operational status |
| `incidents` | Disaster events (type, severity, casualties, affected families) |
| `resources` | Relief resource inventory (food, medicine, water, etc.) |
| `equipment` | Vehicles, generators, radios, and other equipment |
| `equipment_reports` | Serviceability reports with review workflow |
| `incident_reports` | CFAU post-incident operational reports |
| `uploaded_reports` | Uploaded documents + AI summary + extracted data |
| `upload_history` | Append-only, immutable trail of upload edits/lifecycle |
| `audit_logs` | System-wide action audit trail |

Helpers `log_action(...)` and `add_upload_history(...)` (in `models.py`) record audit and upload-history entries.

---

## Project Structure

```
riskonek/
├── main.py                 # FastAPI app, middleware, security headers, root routes
├── seed.py                 # Demonstration data seeder
├── app/
│   ├── database.py         # SQLAlchemy engine, session, get_db dependency
│   ├── models.py           # ORM models + audit/history helpers
│   ├── auth.py             # Password hashing
│   ├── routes/             # auth, admin, bdrrmo, cfau, staff, uploads routers
│   ├── etl/                # Document extraction + AI pipeline
│   ├── analytics/          # Risk-level simulator
│   ├── utils/              # Geo coords, facility importer
│   ├── templates/          # Jinja2 templates
│   └── static/             # CSS / JS / assets
├── scripts/                # Idempotent column-add migrations
└── instance/              # SQLite database (created on first run)
```
