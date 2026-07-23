"""
The numbers produced by engine.py are the product; this layer is an OPTIONAL
plain-language briefing on top of them. It must never break a simulation:
callers wrap explain_simulation() in try/except and render the numeric report
regardless.

Security: the API key is read from the environment only. It is NEVER hardcoded,
NEVER logged, and NEVER placed in an exception message or anything returned to
the browser.

Model: llama-3.3-70b-versatile — a single, low-volume, user-facing briefing per
run, so we favour prose quality over the throughput the ETL layer needs (that
one uses the smaller llama-3.1-8b-instant). Both are on Groq's free tier.
"""

import json
import os
from typing import Optional

import markdown as _markdown
import nh3
from dotenv import load_dotenv

# main.py loads .env at startup; load again here so the module is safe to import
# and use in isolation (tests, scripts) too. Idempotent.
load_dotenv()

MODEL = "llama-3.3-70b-versatile"
REQUEST_TIMEOUT_SECONDS = 20

# Placeholder values from the .env template count as "no key" (mirrors
# app/etl/ai_pipeline.py so both AI paths behave identically).
_PLACEHOLDER_KEYS = {"", "groq_api_key_here", "your_groq_api_key", "changeme"}

_SYSTEM_PROMPT = (
    "You are a disaster-preparedness planning assistant. Using ONLY the "
    "structured facts provided, write a briefing: summarize readiness, name the "
    "biggest operational concerns in priority order, and explain the resource "
    "gaps. Do NOT invent, recalculate, or change any number. Do NOT make "
    "deployment decisions — recommendations are advisory; CDRRMO planners retain "
    "final authority."
)


# The ONLY HTML tags allowed to survive from a model response. Everything else
# (headings, links, images, scripts, tables, raw HTML the model might emit) is
# stripped. This is the security boundary — the markdown step is convenience,
# nh3 is what makes the output safe to render.
_ALLOWED_TAGS = {"p", "br", "strong", "em", "b", "i", "ul", "ol", "li"}


def to_safe_html(md_text: str) -> str:
    """Convert a markdown briefing to a SANITIZED HTML fragment.

    Two steps: render markdown -> HTML, then run nh3 with a strict tag allowlist
    and NO permitted attributes. Because nh3 sanitizes afterwards, even raw
    <script>/<img onerror=...>/<a href=...> injected by the model cannot survive
    — disallowed tags are dropped and their text is unwrapped, all attributes
    are removed. The result is safe to mark |safe in the template.
    """
    if not md_text or not md_text.strip():
        return ""
    html = _markdown.markdown(md_text)
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes={})


def _api_key() -> Optional[str]:
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if key.lower() in _PLACEHOLDER_KEYS:
        return None
    return key or None


# Initialise the client ONCE at module level. If the key is absent or the SDK
# fails to construct, _client stays None and explain_simulation() reports the
# feature as unavailable — without ever exposing why to the browser.
_client = None
_key = _api_key()
if _key:
    try:
        from groq import Groq
        _client = Groq(api_key=_key)
    except Exception:
        _client = None


def is_available() -> bool:
    """True only when a usable key and client exist."""
    return _client is not None


def explain_simulation(result: dict) -> str:
    """Return a plain-language briefing for ONE simulation result.

    The AI receives only this scenario's facts (the `result` dict), so it cannot
    reference other barangays. Raises on any failure (no key, API error,
    timeout, empty response) so the caller can fall back to the numeric report;
    the raised message never contains the API key.
    """
    if _client is None:
        raise RuntimeError("AI briefing unavailable: Groq client not configured.")

    facts = json.dumps(result, default=str)

    resp = _client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": facts},
        ],
        temperature=0.2,
        max_tokens=450,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    text = (resp.choices[0].message.content if resp.choices else "") or ""
    text = text.strip()
    if not text:
        raise RuntimeError("AI briefing unavailable: empty response.")
    return text
