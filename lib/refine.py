"""Turn spoken words into something a map can search.

Speech recognition mangles proper nouns — "Ghaziabad" comes back as "Kazia
bath", "Esplanade" as "Ess plan aid" — and OpenStreetMap is a literal string
matcher with no tolerance for either. Something has to sit between them.

That something used to be the conversational model, asked in the skill prompt
to tidy the phrase before calling the search. It did not reliably do it: it
would answer the caller instead, or announce a cleaned query it never passed
anywhere. Anything the model must *do* rather than *say* belongs in a tool.

So the call lives here, inside the tool, on the smallest model available. It
runs every time because the tool runs every time, and its failure mode is a
deterministic fallback rather than a step that quietly does nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

MODEL = os.getenv("CIVIC_REFINE_MODEL", "gpt-5-nano")
# Correcting a mangled proper noun needs the model to actually think about it:
# at minimal effort "Kazia bath" comes back as "Kasia Bath", at medium it comes
# back as Ghaziabad. Medium costs about six seconds, which is why this runs
# only after a plain search has already failed — never on the first attempt.
REASONING_EFFORT = os.getenv("CIVIC_REFINE_EFFORT", "medium")
TIMEOUT_SECONDS = float(os.getenv("CIVIC_REFINE_TIMEOUT", "15"))

_INSTRUCTIONS = """You convert spoken Indian addresses into a short search query for OpenStreetMap.

Return ONLY the query. No explanation, no punctuation at the ends, no quotes.

Rules:
- Fix obvious speech-to-text errors in place names. "Kazia bath" is Ghaziabad.
  "Ess plan aid" is Esplanade. "Vy shali" is Vaishali.
- Expand clear abbreviations: "GK two" to "Greater Kailash 2", "MG road" to
  "Mahatma Gandhi Road".
- Drop filler and direction words: near, behind, opposite, ke paas, wali gali.
  Keep the place itself.
- Never add a city, locality or landmark the speaker did not say. If they named
  no findable place at all, return the single word NONE.
- Keep it short. A name, not a sentence."""

# The model is told to say NONE when nothing was named. Anything longer than a
# short phrase means it started explaining instead of answering.
_MAX_WORDS = 12


def _looks_invented(cleaned: str, spoken: str) -> bool:
    """True when the cleaned query shares no word stem with what was said.

    A cheap guard against the model helpfully supplying a place nobody
    mentioned. It tolerates spelling correction — "kazia" and "ghaziabad" do
    not match, so a single-token rewrite still passes only if something else
    survives — but rejects a wholesale replacement.
    """
    spoken_words = {w[:4].lower() for w in re.findall(r"\w+", spoken) if len(w) > 3}
    cleaned_words = {w[:4].lower() for w in re.findall(r"\w+", cleaned) if len(w) > 3}
    if not spoken_words or not cleaned_words:
        return False
    return not (spoken_words & cleaned_words)


async def clean_location(spoken: str, fallback: str) -> tuple[str, str]:
    """Return ``(query, source)`` for the spoken location.

    ``source`` is one of ``llm``, ``fallback`` or ``none`` — recorded so a
    surprising search can be traced back to whichever step produced its query.
    ``fallback`` is the deterministic cleanup to use when the model is
    unavailable, unhelpful, or appears to have invented a place.
    """
    spoken = str(spoken or "").strip()
    if not spoken:
        return "", "none"

    if not os.getenv("OPENAI_API_KEY"):
        return fallback, "fallback"

    try:
        import litellm

        response = await asyncio.wait_for(
            litellm.acompletion(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _INSTRUCTIONS},
                    {"role": "user", "content": spoken},
                ],
                api_key=os.getenv("OPENAI_API_KEY"),
                reasoning_effort=REASONING_EFFORT,
            ),
            timeout=TIMEOUT_SECONDS,
        )
        cleaned = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # any provider error falls back, never raises
        logger.warning("civico.refine.unavailable spoken=%r error=%s", spoken, exc)
        return fallback, "fallback"

    cleaned = cleaned.strip().strip('"').strip("'").rstrip(".")
    if not cleaned or cleaned.upper() == "NONE":
        return "", "none"
    if len(cleaned.split()) > _MAX_WORDS:
        logger.warning("civico.refine.too_long cleaned=%r", cleaned[:120])
        return fallback, "fallback"
    if _looks_invented(cleaned, spoken):
        logger.warning(
            "civico.refine.unrelated spoken=%r cleaned=%r", spoken, cleaned
        )
        return fallback, "fallback"
    return cleaned, "llm"
