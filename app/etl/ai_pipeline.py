"""Optional AI summary via Groq (Llama 3).

Gracefully no-ops if GROQ_API_KEY is not set or is the placeholder value
from the .env template. The Week 6 upload flow must work without AI.
"""

import os
from typing import Optional


_PLACEHOLDER_KEYS = {"", "groq_api_key_here", "your_groq_api_key", "changeme"}


def _api_key() -> Optional[str]:
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if key.lower() in _PLACEHOLDER_KEYS:
        return None
    return key or None


def is_available() -> bool:
    return _api_key() is not None


def summarize(text: str, max_chars: int = 6000) -> Optional[str]:
    """Return a short plain-English summary, or None if AI is unavailable
    or the call fails. Never raises — extraction must continue regardless.
    """
    key = _api_key()
    if not key or not text or not text.strip():
        return None

    snippet = text.strip()[:max_chars]

    try:
        from groq import Groq
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You summarize disaster-related reports for the City "
                        "Disaster Risk Reduction and Management Office. Be "
                        "concise, factual, and neutral. 3-5 sentences."
                    ),
                },
                {"role": "user", "content": snippet},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        out = resp.choices[0].message.content if resp.choices else None
        return (out or "").strip() or None
    except Exception:
        return None
