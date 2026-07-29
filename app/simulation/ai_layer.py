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
    "final authority.\n\n"
    "Reply with a single JSON object with exactly these keys:\n"
    '  "briefing_markdown" — the briefing described above, as markdown '
    "(Briefing, Readiness, Biggest Operational Concerns, Resource Gaps). Do NOT "
    "put suggested actions or the closing advisory note in this field.\n"
    '  "suggested_actions" — a list of 0 to 5 objects, each with "action" (the '
    'suggestion), "basis" (the facts it rests on) and "priority" '
    '("high", "medium" or "low").\n'
    '  "advisory_note" — one short closing sentence restating that this '
    "briefing is advisory and that CDRRMO planners retain final authority.\n\n"
    "Rules for suggested_actions. After summarizing the readiness status, "
    "operational concerns and resource gaps, generate a SEPARATE list of "
    "practical planning considerations. Every action must be directly supported "
    "by the provided facts, and must add planning value rather than merely "
    "restating a gap: say what planners may review, verify, coordinate, "
    "prioritize or evaluate about it. Use cautious advisory wording (Consider, "
    "Evaluate, Review, Assess, Prioritize, Verify, Coordinate, Explore); never "
    "present an action as an approved decision or a command such as deploy, "
    "purchase, dispatch, request, evacuate, transfer, close or approve. Do NOT "
    "invent missing quantities, equipment conditions, barangay risk levels, "
    "partner agencies, agreements, deadlines, seasons, budgets, procurement "
    "sources or any other operational fact. Only mention maintenance, repair or "
    "servicing when the facts explicitly report units under repair or out of "
    "service — a shortage on its own NEVER implies that existing units are "
    "under repair. Refer to mutual aid or resource sharing only as an option "
    "whose feasibility may be evaluated, never as an existing agreement, and "
    "never name an LGU, agency or partner that is not in the facts. Do not "
    "imply a season, an upcoming event, a date or a deadline; the planning "
    "horizon in the facts is the only time context. Avoid near-duplicate "
    "actions. Return at most five actions, and return an empty list when the "
    "facts support none."
)

# Hard ceiling on how many suggested actions are ever kept or displayed.
MAX_SUGGESTED_ACTIONS = 5

# Shown when the model returns no usable advisory note (or none at all).
DEFAULT_ADVISORY_NOTE = (
    "Note: The CDRRMO planners retain final authority for deployment decisions. "
    "This briefing is advisory in nature."
)

_VALID_PRIORITIES = {"high", "medium", "low"}


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


def validate_actions(raw) -> list:
    """Filter a model-supplied suggested_actions value down to what is safe to
    render. Supplemental content: anything unusable is DROPPED, never raised, so
    a bad list can't take the briefing down with it.

    Accepts a plain string or an object with a non-empty `action`. `basis` and
    `priority` are optional; an unrecognised priority is discarded rather than
    displayed. The result is capped at MAX_SUGGESTED_ACTIONS.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            action, basis, priority = item, "", ""
        elif isinstance(item, dict):
            action = item.get("action")
            basis = item.get("basis")
            priority = item.get("priority")
        else:
            continue
        if not isinstance(action, str) or not action.strip():
            continue
        basis = basis.strip() if isinstance(basis, str) else ""
        priority = priority.strip().lower() if isinstance(priority, str) else ""
        out.append({
            "action": action.strip(),
            "basis": basis,
            "priority": priority if priority in _VALID_PRIORITIES else "",
        })
        if len(out) >= MAX_SUGGESTED_ACTIONS:
            break
    return out


def parse_briefing(raw_text: str) -> dict:
    """Split ONE model response into its briefing / actions / advisory parts.

    Returns {"briefing_markdown", "suggested_actions", "advisory_note"} and
    never raises. When the response is not a JSON object (an older-style plain
    markdown reply, or truncated JSON) the whole text is treated as the briefing
    and the supplemental parts come back empty — i.e. exactly the pre-existing
    behaviour.
    """
    text = (raw_text or "").strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return {"briefing_markdown": text, "suggested_actions": [], "advisory_note": ""}

    briefing = data.get("briefing_markdown")
    note = data.get("advisory_note")
    return {
        "briefing_markdown": briefing.strip() if isinstance(briefing, str) else "",
        "suggested_actions": validate_actions(data.get("suggested_actions")),
        "advisory_note": note.strip() if isinstance(note, str) else "",
    }


def load_saved_actions(json_text) -> dict:
    """Re-read the supplemental parts frozen into SavedScenario.ai_actions_json.

    Same shape as parse_briefing() minus the briefing (that column holds the
    already-sanitized HTML). Never raises — a NULL, legacy or corrupt column
    simply yields no actions.
    """
    try:
        data = json.loads(json_text or "")
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return {"suggested_actions": [], "advisory_note": ""}
    note = data.get("advisory_note")
    return {
        "suggested_actions": validate_actions(data.get("suggested_actions")),
        "advisory_note": note.strip() if isinstance(note, str) else "",
    }


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

    The response is a JSON object (see _SYSTEM_PROMPT); callers run it through
    parse_briefing(), which degrades to plain markdown if it is not.
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
        # Raised from 450 now that the same call also returns the suggested
        # actions — a truncated reply would be unparseable JSON.
        max_tokens=900,
        response_format={"type": "json_object"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    text = (resp.choices[0].message.content if resp.choices else "") or ""
    text = text.strip()
    if not text:
        raise RuntimeError("AI briefing unavailable: empty response.")
    return text
