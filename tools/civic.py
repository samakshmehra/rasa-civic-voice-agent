"""Shared civic complaint tools available via import_tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import sqlite3
from datetime import date
from typing import Any, Optional

from rasa.mantle.tools.decorator import ToolContext, tool
from rasa.mantle.tools.result import ToolResult

from lib.authority import (
    DemoRoutingError,
    normalize_pincode,
    precision_for as authority_precision_for,
    route_for as authority_route_for,
)
from lib.database import (
    Database,
    routing_for,
)
from lib.refine import clean_location
from lib.geocode import (
    TIMEOUT_SECONDS as GEOCODE_TIMEOUT_SECONDS,
    _clean as geocode_clean,
    GeocodeUnavailable,
    lookup as geocode_lookup,
    normalize_indian_pincode,
)

STATUS_LABELS = {
    "registered": "registered, not yet assigned",
    "assigned": "assigned to a field team",
    "in_progress": "work in progress",
    "resolved": "marked resolved",
    "rejected": "rejected",
}


def _memory(context: Optional[ToolContext], field: str) -> Optional[str]:
    value = _memory_value(context, field)
    return str(value) if value is not None else None


def _citizen_id(context: Optional[ToolContext]) -> Optional[str]:
    """Resolve the caller from the number they gave, if they are known.

    A civic line has no accounts, so most callers are anonymous. Returns None
    rather than falling back to a demo identity — a complaint filed by a
    stranger must not be attributed to somebody else.
    """
    known = _memory(context, "citizen_id")
    if known:
        return str(known)

    number = _memory(context, "contact_number")
    if not number:
        return None
    digits = "".join(ch for ch in str(number) if ch.isdigit())[-10:]
    if len(digits) < 10:
        return None
    with Database() as db:
        row = db.run_query(
            "SELECT citizen_id FROM citizens WHERE phone = ?", (digits,)
        )
    return str(row[0]) if row else None


def _reporter_key(context: Optional[ToolContext]) -> Optional[str]:
    """Return a stable key for known and anonymous callers after phone capture."""
    citizen_id = _citizen_id(context)
    if citizen_id:
        return citizen_id
    digits = "".join(
        character for character in str(_memory(context, "contact_number") or "")
        if character.isdigit()
    )[-10:]
    return f"phone:{digits}" if len(digits) == 10 else None


def _memory_value(context: Optional[ToolContext], field: str) -> Any:
    if context is None:
        return None
    return context.memory.get(field)


def _memory_float(context: Optional[ToolContext], field: str) -> Optional[float]:
    value = _memory_value(context, field)
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _candidate_list(context: Optional[ToolContext], field: str) -> Optional[list]:
    """Read a JSON/list candidate memory value without trusting its shape."""
    raw = _memory_value(context, field)
    if not raw:
        return None
    try:
        candidates = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(candidates, list) or not all(
        isinstance(candidate, dict) for candidate in candidates
    ):
        return None
    return candidates


def _selected_candidate(candidates: list, option: int) -> Optional[dict]:
    """Return a one-based candidate selection; zero/negative are invalid."""
    try:
        selected = int(option)
    except (TypeError, ValueError):
        return None
    if selected < 1 or selected > len(candidates):
        return None
    candidate = candidates[selected - 1]
    return candidate if isinstance(candidate, dict) else None


def _clear_memory_fields(context: Optional[ToolContext], fields: tuple[str, ...]) -> None:
    if context is not None:
        for field in fields:
            context.memory.set(field, None)


ROUTING_MEMORY_FIELDS = (
    "service_area_id",
    "authority_id",
    "authority_name",
    "zone",
    "ward",
    "department",
    "sla_days",
    "routing_rule_version",
    "routing_source",
)

EXACT_AND_DOWNSTREAM_FIELDS = (
    "exact_landmark_raw",
    "location_settled",
    "location_precision",
    "location_summary",
    "exact_location_candidates",
    "exact_location_confirmed",
    "location_text",
    "area",
    "latitude",
    "longitude",
    "osm_place_id",
    "duplicate_found",
    "existing_complaint_id",
    "duplicate_decision",
    "override_reason",
    "details_verified",
) + ROUTING_MEMORY_FIELDS

BROAD_AND_DOWNSTREAM_FIELDS = (
    "broad_location_candidates",
    "broad_location_confirmed",
    "broad_latitude",
    "broad_longitude",
    "broad_bounding_box",
    "broad_osm_place_id",
    "locality",
    "city",
    "state",
    "pincode",
) + EXACT_AND_DOWNSTREAM_FIELDS


def _expanded_bounding_box(candidate: dict, minimum_span: float = 0.04) -> list[float]:
    """Return [south,north,west,east], expanding tiny POI boxes for local search."""
    raw = candidate.get("boundingbox") or []
    try:
        south, north, west, east = (float(value) for value in raw)
    except (TypeError, ValueError):
        latitude = float(candidate["lat"])
        longitude = float(candidate["lon"])
        half = minimum_span / 2
        return [latitude - half, latitude + half, longitude - half, longitude + half]

    latitude = float(candidate.get("lat") or (south + north) / 2)
    longitude = float(candidate.get("lon") or (west + east) / 2)
    lat_span = max(north - south, minimum_span)
    lon_span = max(east - west, minimum_span)
    return [
        latitude - lat_span / 2,
        latitude + lat_span / 2,
        longitude - lon_span / 2,
        longitude + lon_span / 2,
    ]


def _speak_reference(reference: str) -> str:
    """Return a stable voice rendering such as 'C I V one zero zero one'."""
    digit_words = {
        "0": "zero",
        "1": "one",
        "2": "two",
        "3": "three",
        "4": "four",
        "5": "five",
        "6": "six",
        "7": "seven",
        "8": "eight",
        "9": "nine",
    }
    spoken = []
    for character in str(reference).upper():
        if character.isdigit():
            spoken.append(digit_words[character])
        elif character.isalpha():
            spoken.append(character)
    return " ".join(spoken)


def _normalize_complaint_id(reference: str) -> str:
    """Normalize typed or spoken references into the canonical CIV#### form."""
    aliases = {
        "c": "C",
        "see": "C",
        "i": "I",
        "eye": "I",
        "v": "V",
        "vee": "V",
        "zero": "0",
        "oh": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }
    tokens = re.findall(r"[a-z]+|\d+", str(reference).lower())
    normalized = "".join(aliases.get(token, token.upper()) for token in tokens)
    return re.sub(r"[^A-Z0-9]", "", normalized)


def _distance_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Calculate great-circle distance between two WGS84 points."""
    earth_radius_meters = 6_371_000
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_meters * math.asin(math.sqrt(haversine))


def _duplicate_radius_meters() -> float:
    try:
        return max(25.0, float(os.getenv("CIVIC_DUPLICATE_RADIUS_METERS", "300")))
    except ValueError:
        return 300.0


@tool(
    description=(
        "Record which of the six civic problems the caller is reporting. Call "
        "this as soon as you know, before asking anything about location. "
        "Pass one of: pothole, water_supply, garbage, streetlight, drainage, "
        "stray_animals."
    )
)
async def record_problem_category(
    category: str = "", context: ToolContext = None
) -> ToolResult:
    """Record the problem category, as a tool call rather than a field write.

    This was an `llm_settable` field the skill asked the model to set. The model
    reliably *said* it had — "I'll register the streetlight complaint" — without
    ever writing it, and the whole flow then ran with no category: the location
    step started before the locality was confirmed, and the caller was asked
    what kind of problem it was after they had already described it twice.

    A tool call either happens or it does not, and the result comes back in the
    transcript. That difference has mattered everywhere it has been tried here.
    """
    normalized = str(category or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "street_light": "streetlight",
        "streetlights": "streetlight",
        "water": "water_supply",
        "water_supply_issue": "water_supply",
        "stray_animal": "stray_animals",
        "strays": "stray_animals",
        "potholes": "pothole",
        "garbage_collection": "garbage",
        "sewage": "drainage",
    }
    normalized = aliases.get(normalized, normalized)

    try:
        requirement = authority_precision_for(normalized)
    except DemoRoutingError as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "demo_routing_unavailable",
                "detail": str(exc)[:200],
            }
        )

    if requirement is None:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "unsupported_category",
                "heard": category,
                "supported": [
                    "pothole",
                    "water_supply",
                    "garbage",
                    "streetlight",
                    "drainage",
                    "stray_animals",
                ],
                "hint": (
                    "Say this line only handles those six problems and offer the "
                    "ward office. Do not guess a category on their behalf."
                ),
            }
        )

    if context is not None:
        context.memory.set("category", normalized)
        # The recording notice is gated on this flag. Setting it here rather
        # than asking the model to means it never announces "I need to note the
        # recording notice first" as though it were a step in the conversation.
        context.memory.set("recording_notice_played", True)

    return ToolResult(
        llm_response={
            "ok": True,
            "category": normalized,
            "precision_required": requirement["requires"],
            "hint": (
                "Now ask where it is, broadly: their locality with the city, or "
                "their six-digit PIN code. Either one on its own is enough — do "
                "not insist on both. Pass whatever they say to "
                "resolve_broad_location; the map supplies the PIN code. Do not "
                "ask for a landmark yet."
            ),
        }
    )


@tool(description="Clear location and routing state before starting a new complaint.")
async def reset_complaint_context(context: ToolContext = None) -> ToolResult:
    """Prevent a second complaint in one call from reusing stale project memory.

    This must NOT be a plain unconditional wipe. The model can invoke the block
    this belongs to directly (via the engine's own activate/block mechanism),
    independent of whatever skill prose is currently gated on or off — so a
    reactivation can happen even after a location has already been resolved for
    the complaint in progress. If that reset runs unconditionally, it silently
    destroys area/coordinates/routing moments before submit_complaint, which
    then fails with "incomplete_complaint" — a confusing, misleading error for
    what is really a spurious re-entry, not a real municipal outage.

    A location already being resolved (``area`` set) with no complaint filed
    yet (``complaint_id`` unset) means the current, still-open attempt is being
    re-entered — not a citizen asking to start a second complaint. Only wipe
    when this is genuinely fresh: no location yet, or the previous complaint in
    this call already completed.
    """
    attempt_started = any(
        _memory_value(context, field)
        for field in (
            "raw_location",
            "broad_location_query",
            "broad_location_candidates",
            "broad_location_confirmed",
            "exact_landmark_raw",
            "exact_location_candidates",
            "exact_location_confirmed",
        )
    )
    already_completed = bool(_memory(context, "complaint_id"))

    if attempt_started and not already_completed:
        return ToolResult(
            llm_response={
                "ok": True,
                "skipped": True,
                "reason": "already_in_progress",
                "hint": (
                    "The location for this complaint is already resolved — do "
                    "not ask for it again. Continue with the next unfinished "
                    "step instead of restarting."
                ),
            }
        )

    if context is not None:
        for field in (
            "raw_location",
            "broad_location_query",
            "broad_location_candidates",
            "broad_location_confirmed",
            "broad_latitude",
            "broad_longitude",
            "broad_bounding_box",
            "broad_osm_place_id",
            "locality",
            "location_text",
            "exact_landmark_raw",
            "exact_location_candidates",
            "exact_location_confirmed",
            "area",
            "city",
            "state",
            "pincode",
            "osm_place_id",
            "latitude",
            "longitude",
            "complaint_id",
            "spoken_complaint_id",
            "service_area_id",
            "authority_id",
            "authority_name",
            "zone",
            "ward",
            "department",
            "sla_days",
            "routing_rule_version",
            "routing_source",
            "description",
            "duplicate_found",
            "existing_complaint_id",
            "duplicate_decision",
            "override_reason",
            "contact_confirmed",
            "details_verified",
        ):
            context.memory.set(field, None)
    return ToolResult(llm_response={"ok": True})


@tool(
    description=(
        "Look up a caller by the phone number they gave, so a returning caller "
        "can be greeted by the name already on file. Call this only after the "
        "caller has said their number. There are no accounts on a civic line — "
        "an unknown number is completely normal, not an error."
    )
)
async def lookup_caller(
    phone: str = "", context: ToolContext = None
) -> ToolResult:
    """Match a spoken phone number against people who have called before."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) < 10:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "incomplete_number",
                "digits_heard": len(digits),
                "hint": "Read back what you heard and ask them to repeat it.",
            }
        )

    digits = digits[-10:]
    with Database() as db:
        row = db.run_query(
            "SELECT citizen_id, name, phone FROM citizens WHERE phone = ?",
            (digits,),
        )

    if context is not None:
        context.memory.set("contact_number", digits)
        context.memory.set("known_caller", bool(row))
        if row:
            context.memory.set("citizen_id", str(row[0]))
            context.memory.set("citizen_name", row[1])

    if not row:
        # Not an error. Most callers are new.
        return ToolResult(
            llm_response={
                "ok": True,
                "known_caller": False,
                "contact_number": digits,
                "hint": "New caller. Do not ask for a name — just carry on.",
            }
        )

    return ToolResult(
        llm_response={
            "ok": True,
            "known_caller": True,
            "citizen_name": row[1],
            "contact_number": digits,
        }
    )


def _osm_reference(candidate: dict) -> str:
    osm_type = str(candidate.get("osm_type") or "").strip()
    osm_id = str(candidate.get("osm_id") or "").strip()
    if osm_type and osm_id:
        return f"{osm_type}:{osm_id}"
    return str(candidate.get("place_id") or "")


@tool(
    description=(
        "Find broad locality/PIN candidates on OpenStreetMap. This only proposes "
        "an area; the citizen must choose and confirm a result."
    )
)
async def resolve_broad_location(
    location: str = "", context: ToolContext = None
) -> ToolResult:
    """Resolve a broad service area while retaining raw speech for audit."""
    raw_location = location or _memory(context, "raw_location") or ""
    if not raw_location.strip():
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "no_broad_location",
                "hint": "Ask for a locality, city, or six-digit PIN code.",
            }
        )

    # Any correction invalidates the old exact point, duplicate decision and
    # authority immediately. A failed new lookup must never leave stale routing
    # available for submission.
    _clear_memory_fields(context, BROAD_AND_DOWNSTREAM_FIELDS)
    if context is not None:
        context.memory.set("raw_location", raw_location)
        context.memory.set("broad_location_query", raw_location)

    explicit_pincode = normalize_indian_pincode(raw_location)
    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(geocode_lookup, raw_location),
            timeout=GEOCODE_TIMEOUT_SECONDS + 1,
        )
    except (GeocodeUnavailable, asyncio.TimeoutError) as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "geocoder_unavailable",
                "detail": str(exc)[:200],
                "hint": "The map service is unavailable. Do not invent a location.",
            }
        )

    normalized_candidates = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_pincode = normalize_pincode(candidate.get("postcode"))
        if explicit_pincode and candidate_pincode and candidate_pincode != explicit_pincode:
            continue
        # A PIN spoken by the caller is useful for rejecting contradictory map
        # results, but it is not proof that a result with no postcode belongs
        # to that PIN. Keep missing map metadata missing and make confirmation
        # ask for a better result instead of silently copying the user's digits.
        enriched = dict(candidate)
        enriched["postcode"] = candidate_pincode
        normalized_candidates.append(enriched)

    if not normalized_candidates:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "broad_location_not_found",
                "raw_location": raw_location,
                "hint": "Ask for the locality together with its city or PIN code.",
            }
        )

    if context is not None:
        context.memory.set("broad_location_candidates", normalized_candidates)
        context.memory.set("broad_location_confirmed", False)

    options = [
        {
            "option": index,
            "locality": candidate.get("area") or "",
            "city": candidate.get("city") or "",
            "state": candidate.get("state") or "",
            "pincode": candidate.get("postcode") or "not returned",
        }
        for index, candidate in enumerate(normalized_candidates, start=1)
    ]
    return ToolResult(
        llm_response={
            "ok": True,
            "option_count": len(options),
            "options": options,
            "hint": (
                "Read the locality, city and PIN code. The citizen must confirm "
                "one option before any exact-landmark search."
            ),
        }
    )


@tool(
    description=(
        "Confirm the broad OpenStreetMap service-area candidate selected by the "
        "citizen. Requires a one-based option number and a valid six-digit PIN."
    )
)
async def confirm_broad_location(
    option: int, context: ToolContext = None
) -> ToolResult:
    candidates = _candidate_list(context, "broad_location_candidates")
    chosen = _selected_candidate(candidates or [], option)
    if not candidates or chosen is None:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "bad_broad_option",
                "option": option,
                "option_count": len(candidates or []),
                "hint": "Ask the citizen to choose one of the listed areas.",
            }
        )

    pincode = normalize_pincode(chosen.get("postcode"))
    if not pincode:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "pincode_required",
                "hint": (
                    "The map result has no PIN code. Ask the citizen for the six-digit "
                    "PIN and search the locality again with it."
                ),
            }
        )

    try:
        broad_latitude = float(chosen["lat"])
        broad_longitude = float(chosen["lon"])
        broad_bbox = _expanded_bounding_box(chosen)
    except (KeyError, TypeError, ValueError):
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "bad_broad_candidate",
                "hint": "Search for the broad location again.",
            }
        )

    locality = str(chosen.get("area") or "").strip()
    city = str(chosen.get("city") or "").strip()
    state = str(chosen.get("state") or "").strip()
    osm_reference = _osm_reference(chosen)
    if context is not None:
        for field, value in {
            "locality": locality,
            "city": city,
            "state": state,
            "pincode": pincode,
            "broad_latitude": broad_latitude,
            "broad_longitude": broad_longitude,
            "broad_bounding_box": broad_bbox,
            "broad_osm_place_id": osm_reference,
            "broad_location_confirmed": True,
        }.items():
            context.memory.set(field, value)

    return ToolResult(
        llm_response={
            "ok": True,
            "locality": locality,
            "city": city,
            "state": state,
            "pincode": pincode,
            "broad_latitude": broad_latitude,
            "broad_longitude": broad_longitude,
            "bounding_box": broad_bbox,
            "osm_reference": osm_reference,
            "location_precision": "broad_only",
            "hint": "Now ask for the exact problem landmark inside this area.",
        }
    )


# --- Locating the incident ---------------------------------------------------
#
# One tool, called once per caller turn. It cleans, searches, counts and — when
# the map cannot help — settles the location itself.
#
# An earlier version split this across three tools and asked the model to
# classify the location first ("searchable" / "needs_more" / "good_as_vague"),
# read the returned action, and dispatch accordingly. That put protocol in the
# model's hands, and every live failure came from there: it announced a verdict
# it had not earned, narrated settling the location without calling the tool,
# or re-asked the caller without calling anything at all. None of those
# decisions ever needed the model. How precisely a department needs an incident
# located, whether the search budget is spent, and when to stop asking are all
# knowable from configuration and state.
#
# What is left for the model is the one thing only it can do: turn spoken words
# into a searchable phrase. It passes what the caller said and its best cleanup;
# everything else happens here.


def _attempts(context: Optional[ToolContext]) -> int:
    """Failed map searches — the precision budget."""
    try:
        return int(float(_memory_value(context, "exact_attempts") or 0))
    except (TypeError, ValueError):
        return 0


def _asks(context: Optional[ToolContext]) -> int:
    """Questions asked about the exact spot — the loop-breaker."""
    try:
        return int(float(_memory_value(context, "location_asks") or 0))
    except (TypeError, ValueError):
        return 0


def _remember_note(context: Optional[ToolContext], spoken: str) -> str:
    """Build up what the caller said about the spot, rather than replacing it.

    The last thing someone says about a location is usually the least useful —
    "there is nothing nearby" is an answer to our question, not a description
    of where the problem is. Overwriting on every turn threw away "near the
    pole outside our lane" and handed the field officer the refusal instead.
    Keeping each distinct thing they said preserves the useful part.
    """
    spoken = str(spoken or "").strip().rstrip(".")
    existing = str(_memory(context, "location_note") or "").strip()
    if not spoken:
        return existing
    parts = [part.strip() for part in existing.split(";") if part.strip()]
    lowered = {part.lower() for part in parts}
    if spoken.lower() not in lowered and not any(
        spoken.lower() in part or part in spoken.lower() for part in lowered
    ):
        parts.append(spoken)
    note = "; ".join(parts)[:300]
    if context is not None:
        context.memory.set("location_note", note)
    return note


def _settle_approximate(
    context: Optional[ToolContext], note: str, requirement: dict
) -> dict:
    """Place the complaint at the confirmed locality, keeping the caller's words.

    A paper complaint has always been "the pothole behind the Supertech gate"
    with no coordinates at all, and field officers have always found those.
    Refusing to file without a map pin would make this worse than the form it
    replaces.
    """
    latitude = _memory_float(context, "broad_latitude")
    longitude = _memory_float(context, "broad_longitude")
    pincode = normalize_pincode(_memory(context, "pincode"))
    locality = _memory(context, "locality") or ""

    if context is not None:
        for field, value in {
            "location_text": note,
            "location_note": note,
            "area": locality or _memory(context, "city") or pincode,
            "latitude": latitude,
            "longitude": longitude,
            "osm_place_id": "",
            "exact_location_confirmed": False,
            "location_settled": True,
            "location_precision": "locality_approximate",
            "location_summary": (
                f"logged to {locality or pincode} from your description, "
                f'"{note}"'
            ),
        }.items():
            context.memory.set(field, value)

    return {
        "ok": True,
        "status": "settled_approximate",
        "locality": locality,
        "pincode": pincode,
        "note": note,
        "precision_required": requirement["requires"],
        "hint": (
            "Tell them plainly it is logged to the locality with their own "
            "description, and move on to the rest of the complaint. Do not "
            "imply an exact point was captured, and do not ask about the "
            "location again."
        ),
    }


def _options_line(payload: dict) -> str:
    """Render the map choices as one speakable line for the prompt."""
    options = payload.get("options") or []
    return "; ".join(
        f"{o['option']}) {o.get('place') or o.get('locality') or ''}" for o in options
    )


def _publish_location_status(
    context: Optional[ToolContext], payload: dict
) -> dict:
    """Mirror the outcome into memory so the skill can branch on it.

    The post-write hook is engine-invoked rather than part of an LLM exchange,
    so its return value is not something the skill can rely on reading. Writing
    the status and the pending question into memory means the prose can react
    to them the same way it reacts to any other state.
    """
    if context is not None:
        context.memory.set("location_status", str(payload.get("status") or ""))
        context.memory.set("location_ask", str(payload.get("ask_about") or ""))
        context.memory.set("location_options", _options_line(payload))
    return payload


async def _maybe_await(value):
    """Await *value* when it is awaitable, so send() works either way."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _search_scoped(
    query: str,
    pincode: str,
    broad_bbox: list,
    locality: str,
    city: str,
) -> list:
    """Search inside the confirmed area, keeping only results that belong to it.

    Raises GeocodeUnavailable / TimeoutError to the caller — an unreachable map
    is handled differently from a map that simply found nothing.
    """
    scope = " ".join(part for part in (query, locality, city, pincode) if part)
    candidates = await asyncio.wait_for(
        asyncio.to_thread(geocode_lookup, scope, viewbox=broad_bbox, bounded=True),
        timeout=GEOCODE_TIMEOUT_SECONDS + 1,
    )

    south, north, west, east = broad_bbox
    scoped = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            latitude = float(candidate["lat"])
            longitude = float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (south <= latitude <= north and west <= longitude <= east):
            continue
        candidate_pincode = normalize_pincode(candidate.get("postcode"))
        if candidate_pincode and candidate_pincode != pincode:
            continue
        enriched = dict(candidate)
        enriched["confirmed_pincode"] = pincode
        scoped.append(enriched)
    return scoped


async def _locate(
    spoken_location: str,
    cleaned_query: str,
    context: Optional[ToolContext],
) -> ToolResult:
    """Resolve the incident spot, or settle it at locality level trying.

    Returns one ``status`` telling the caller-facing model exactly what to do
    next, so there is never a decision for it to get wrong:

    ``choose``               read the options out, then confirm_incident_location
    ``ask_once``             ask for one named detail, then call this again
    ``settled_approximate``  done — say so honestly and move on
    """
    if _memory(context, "broad_location_confirmed") != "True":
        return ToolResult(
            llm_response={
                "ok": False,
                "status": "need_area_first",
                "hint": "Confirm the locality and PIN code before locating the incident.",
            }
        )

    category = _memory(context, "category") or ""
    try:
        requirement = authority_precision_for(category)
    except DemoRoutingError as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "status": "demo_routing_unavailable",
                "detail": str(exc)[:200],
            }
        )
    if requirement is None:
        return ToolResult(
            llm_response={
                "ok": False,
                "status": "category_not_set",
                "hint": "Establish which of the six problems this is first.",
            }
        )

    spoken = str(spoken_location or "").strip()
    note = _remember_note(context, spoken)
    # Speech-to-text mangles proper nouns and the geocoder is a literal
    # matcher, so something has to sit between them. That call happens here,
    # inside the tool, rather than being asked of the conversational model —
    # a tool runs every time, a prompt instruction does not.
    # The first search uses a plain, instant cleanup. Correcting a mishearing
    # costs several seconds of model time, so it is spent only once that search
    # has come back empty — at which point the caller is waiting anyway.
    query = str(cleaned_query or "").strip() or geocode_clean(spoken)
    query_source = "caller" if cleaned_query else "plain"
    allowance = requirement["attempts_before_approximate"]

    if context is not None:
        context.memory.set("precision_required", requirement["requires"])
        context.memory.set("location_query", query)
        context.memory.set("location_query_source", query_source)

    # Some problems are area-level by nature. Asking a caller to pinpoint stray
    # dogs wastes their time and ours.
    if requirement["requires"] == "area" and note:
        return ToolResult(llm_response=_settle_approximate(context, note, requirement))

    attempts = _attempts(context)
    asks = _asks(context)
    # One effort budget, spent by anything that costs the caller a turn.
    exhausted = attempts + asks >= allowance

    if not query:
        if exhausted and note:
            return ToolResult(
                llm_response=_settle_approximate(context, note, requirement)
            )
        asks += 1
        if context is not None:
            context.memory.set("location_asks", asks)
        return ToolResult(
            llm_response={
                "ok": True,
                "status": "ask_once",
                "ask_about": requirement["ask_for"],
                "why": requirement["reason"],
            }
        )

    pincode = normalize_pincode(_memory(context, "pincode"))
    raw_bbox = _memory_value(context, "broad_bounding_box")
    try:
        broad_bbox = json.loads(raw_bbox) if isinstance(raw_bbox, str) else raw_bbox
        broad_bbox = [float(value) for value in broad_bbox]
        if len(broad_bbox) != 4:
            raise ValueError("bad bounding box")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ToolResult(
            llm_response={
                "ok": False,
                "status": "bad_broad_search_scope",
                "hint": "Resolve the broad location again.",
            }
        )

    # A new landmark invalidates any previous point and everything derived from
    # it, while keeping the already confirmed broad area.
    _clear_memory_fields(context, EXACT_AND_DOWNSTREAM_FIELDS)
    if context is not None:
        context.memory.set("exact_landmark_raw", query)

    locality = _memory(context, "locality") or ""
    city = _memory(context, "city") or ""
    try:
        scoped = await _search_scoped(query, pincode, broad_bbox, locality, city)
        if not scoped and query_source == "plain":
            # Nothing found on the caller's own words. Before giving up, spend
            # the model time on a mishearing — "Kazia bath" is Ghaziabad, and
            # the geocoder will never work that out. Only worth the seconds now
            # that a plain search has already failed.
            #
            # That correction plus a second map call runs to about ten seconds.
            # On a phone line that is a long silence, so say something first —
            # a deterministic line, sent immediately, not a turn the model has
            # to decide to take.
            sender = getattr(context, "send", None)
            if callable(sender):
                await _maybe_await(sender("Let me check that spelling."))
            corrected, source = await clean_location(spoken, query)
            if corrected and corrected.lower() != query.lower():
                query, query_source = corrected, source
                if context is not None:
                    context.memory.set("location_query", query)
                    context.memory.set("location_query_source", query_source)
                scoped = await _search_scoped(
                    query, pincode, broad_bbox, locality, city
                )
    except (GeocodeUnavailable, asyncio.TimeoutError) as exc:
        if note:
            settled = _settle_approximate(context, note, requirement)
            settled["geocoder_unavailable"] = True
            settled["hint"] = (
                "The map service is unavailable, so this is logged to the "
                "locality with their description. Say so plainly and carry on."
            )
            return ToolResult(llm_response=settled)
        return ToolResult(
            llm_response={
                "ok": False,
                "status": "geocoder_unavailable",
                "detail": str(exc)[:200],
                "hint": "Ask them to describe the spot in their own words.",
            }
        )

    if not scoped:
        attempts += 1
        if context is not None:
            context.memory.set("exact_attempts", attempts)
        if attempts + asks >= allowance and note:
            settled = _settle_approximate(context, note, requirement)
            settled["searched_and_failed"] = attempts
            return ToolResult(llm_response=settled)
        return ToolResult(
            llm_response={
                "ok": True,
                "status": "ask_once",
                "ask_about": requirement["ask_for"],
                "not_found": query,
                "attempts_so_far": attempts,
                "hint": (
                    "That place is not on the map inside the confirmed area. "
                    "Say so briefly and ask for one different nearby place. "
                    "Then call this tool again with whatever they reply — "
                    "including 'there is nothing nearby', which is a location "
                    "answer, not small talk. Only this tool can settle the "
                    "location. Never widen the search outside the confirmed "
                    "area to force a match."
                ),
            }
        )

    if context is not None:
        context.memory.set("exact_location_candidates", scoped)
        context.memory.set("exact_location_confirmed", False)

    return ToolResult(
        llm_response={
            "ok": True,
            "status": "choose",
            "option_count": len(scoped),
            "options": [
                {
                    "option": index,
                    "place": candidate.get("label") or "",
                    "locality": candidate.get("area") or locality,
                    "pincode": pincode,
                }
                for index, candidate in enumerate(scoped, start=1)
            ],
            "hint": (
                "Read the options back briefly in plain speech and ask which "
                "one it is. Never pick for them, even with one result, and "
                "never read a raw map label aloud. Then call "
                "confirm_incident_location with their choice."
            ),
        }
    )




@tool(
    description=(
        "Work out where inside the confirmed area the problem is. Prefer "
        "setting spoken_location, which runs this automatically; call it "
        "directly only if that write was rejected."
    )
)
async def locate_incident(
    spoken_location: str = "",
    cleaned_query: str = "",
    context: ToolContext = None,
) -> ToolResult:
    """Direct entry point. The post-write hook is the normal path."""
    result = await _locate(spoken_location, cleaned_query, context)
    _publish_location_status(context, result.llm_response)
    return result


@tool(
    description=(
        "Confirm the exact incident candidate inside the confirmed PIN-code area. "
        "Only this tool may set the final complaint coordinates."
    )
)
async def confirm_incident_location(
    option: int, context: ToolContext = None
) -> ToolResult:
    candidates = _candidate_list(context, "exact_location_candidates")
    chosen = _selected_candidate(candidates or [], option)
    if not candidates or chosen is None:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "bad_exact_option",
                "option": option,
                "option_count": len(candidates or []),
            }
        )

    confirmed_pincode = normalize_pincode(_memory(context, "pincode"))
    candidate_pincode = normalize_pincode(
        chosen.get("postcode") or chosen.get("confirmed_pincode")
    )
    if not confirmed_pincode or candidate_pincode != confirmed_pincode:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "pincode_mismatch",
                "expected_pincode": confirmed_pincode,
                "candidate_pincode": candidate_pincode,
                "hint": "Do not confirm this point; search within the confirmed PIN again.",
            }
        )

    try:
        latitude = float(chosen["lat"])
        longitude = float(chosen["lon"])
    except (KeyError, TypeError, ValueError):
        return ToolResult(llm_response={"ok": False, "error": "bad_exact_candidate"})

    raw_landmark = _memory(context, "exact_landmark_raw") or str(
        chosen.get("label") or ""
    )
    area = str(chosen.get("area") or _memory(context, "locality") or "").strip()
    city = str(chosen.get("city") or _memory(context, "city") or "").strip()
    state = str(chosen.get("state") or _memory(context, "state") or "").strip()
    osm_reference = _osm_reference(chosen)
    if context is not None:
        for field, value in {
            "location_text": raw_landmark,
            "area": area,
            "city": city,
            "state": state,
            "pincode": confirmed_pincode,
            "latitude": latitude,
            "longitude": longitude,
            "osm_place_id": osm_reference,
            "exact_location_confirmed": True,
            "location_settled": True,
            "location_precision": "exact_landmark",
            "location_summary": f"pinned to {chosen.get('label') or raw_landmark}",
        }.items():
            context.memory.set(field, value)

    return ToolResult(
        llm_response={
            "ok": True,
            "place": chosen.get("label") or raw_landmark,
            "area": area,
            "city": city,
            "state": state,
            "pincode": confirmed_pincode,
            "latitude": latitude,
            "longitude": longitude,
            "osm_reference": osm_reference,
            "location_precision": "exact_landmark",
        }
    )


@tool(
    description=(
        "Check whether an open complaint of the same category already exists in "
        "the immediate area. Call before collecting full details, so the citizen "
        "is not asked for information twice."
    )
)
async def find_nearby_complaints(context: ToolContext = None) -> ToolResult:
    """Look for an open complaint of this category within the configured radius."""
    category = _memory(context, "category")
    latitude = _memory_float(context, "latitude")
    longitude = _memory_float(context, "longitude")
    location_settled = _memory(context, "location_settled") == "True"

    if not category or latitude is None or longitude is None or not location_settled:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "missing_coordinates_or_category",
                "hint": "Confirm the exact mapped incident point and problem type first.",
            }
        )

    with Database() as db:
        rows = db.run_query(
            """
            SELECT complaint_id, description, location_text, area, status,
                   created_at, latitude, longitude
            FROM complaints
            WHERE category = ?
              AND status IN ('registered', 'assigned', 'in_progress')
              AND latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY created_at DESC
            """,
            (category,),
            one_record=False,
        )

    radius = _duplicate_radius_meters()
    matches = []
    for cid, description, location_text, area, status, created_at, lat, lon in rows or []:
        distance = _distance_meters(latitude, longitude, float(lat), float(lon))
        if distance > radius:
            continue
        try:
            age_days = max(0, (date.today() - date.fromisoformat(created_at)).days)
        except (TypeError, ValueError):
            age_days = None
        matches.append(
            {
                "complaint_id": cid,
                "spoken_complaint_id": _speak_reference(cid),
                "description": description,
                "location_text": location_text,
                "area": area,
                "status": status,
                "created_at": created_at,
                "age_days": age_days,
                "distance_meters": round(distance),
            }
        )

    if context is not None:
        context.memory.set("duplicate_found", bool(matches))
        if matches:
            context.memory.set("existing_complaint_id", matches[0]["complaint_id"])
            context.memory.set(
                "spoken_complaint_id", matches[0]["spoken_complaint_id"]
            )

    return ToolResult(
        llm_response={
            "ok": True,
            "duplicate_found": bool(matches),
            "radius_meters": radius,
            "match_count": len(matches),
            "matches": matches,
        }
    )


@tool(
    description=(
        "Persist the citizen's report against the existing nearby complaint they "
        "selected. Call only after they explicitly choose to attach rather than "
        "file a separate complaint."
    )
)
async def attach_to_existing_complaint(
    context: ToolContext = None,
) -> ToolResult:
    """Record a supporting report instead of merely claiming it was attached."""
    complaint_id = _memory(context, "existing_complaint_id")
    if not complaint_id:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "no_existing_complaint",
                "hint": "Run duplicate detection and let the citizen choose first.",
            }
        )

    reporter_key = _reporter_key(context)
    contact_number = "".join(
        character for character in str(_memory(context, "contact_number") or "")
        if character.isdigit()
    )[-10:]
    if not reporter_key or len(contact_number) != 10:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "contact_required",
                "hint": "Collect and validate a callback number before attaching.",
            }
        )

    try:
        with Database() as db:
            exists = db.run_query(
                "SELECT status FROM complaints WHERE complaint_id = ?",
                (complaint_id,),
            )
            if not exists:
                return ToolResult(
                    llm_response={
                        "ok": False,
                        "error": "complaint_not_found",
                        "complaint_id": complaint_id,
                    }
                )
            if exists[0] not in {"registered", "assigned", "in_progress"}:
                return ToolResult(
                    llm_response={
                        "ok": False,
                        "error": "complaint_not_open",
                        "complaint_id": complaint_id,
                        "status": exists[0],
                    }
                )
            inserted, report_count = db.attach_report(
                complaint_id=complaint_id,
                citizen_id=reporter_key,
                description=_memory(context, "description") or "",
                contact_number=contact_number,
                created_at=date.today().isoformat(),
            )
    except sqlite3.Error as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "municipal_system_unavailable",
                "detail": str(exc)[:200],
            }
        )

    spoken_complaint_id = _speak_reference(complaint_id)
    if context is not None:
        context.memory.set("complaint_id", complaint_id)
        context.memory.set("spoken_complaint_id", spoken_complaint_id)

    return ToolResult(
        llm_response={
            "ok": True,
            "complaint_id": complaint_id,
            "spoken_complaint_id": spoken_complaint_id,
            "report_added": inserted,
            "already_attached": not inserted,
            "report_count": report_count,
        }
    )


@tool(
    description=(
        "Look up the fictional department and target time for a demo category. "
        "New complaint flows use route_to_authority after map confirmation. Never "
        "present these demo targets as official commitments."
    )
)
async def route_to_department(
    category: str = "", context: ToolContext = None
) -> ToolResult:
    """Legacy deterministic category -> department + demo-target lookup."""
    selected_category = category or _memory(context, "category") or ""
    routing = routing_for(selected_category)

    if not routing:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "unknown_category",
                "category": selected_category,
                "hint": "Ask the citizen to pick one of the six known problem types.",
            }
        )

    department, sla_days = routing
    if context is not None:
        context.memory.set("department", department)
        context.memory.set("sla_days", sla_days)

    return ToolResult(
        llm_response={
            "ok": True,
            "category": selected_category,
            "department": department,
            "sla_days": sla_days,
        }
    )


@tool(
    description=(
        "Deterministically route a complaint from its confirmed six-digit PIN "
        "code and category. Values come from a small fictional demo table; never "
        "accept an LLM-supplied department or target time."
    )
)
async def route_to_authority(context: ToolContext = None) -> ToolResult:
    category = _memory(context, "category") or ""
    pincode = normalize_pincode(_memory(context, "pincode"))
    latitude = _memory_float(context, "latitude")
    longitude = _memory_float(context, "longitude")
    location_settled = _memory(context, "location_settled") == "True"

    routing_fields = (
        "service_area_id",
        "authority_id",
        "authority_name",
        "zone",
        "ward",
        "department",
        "sla_days",
        "routing_rule_version",
        "routing_source",
    )

    def clear_stale_route() -> None:
        if context is not None:
            for field in routing_fields:
                context.memory.set(field, None)

    if not pincode or not category or latitude is None or longitude is None or not location_settled:
        clear_stale_route()
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "incomplete_location_for_routing",
                "hint": (
                    "A confirmed six-digit PIN, exact landmark coordinates and "
                    "complaint category are required before routing."
                ),
            }
        )

    try:
        route = authority_route_for(pincode, category)
    except DemoRoutingError as exc:
        clear_stale_route()
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "demo_routing_unavailable",
                "detail": str(exc)[:200],
            }
        )

    if route is None:
        clear_stale_route()
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "unsupported_category",
                "pincode": pincode,
                "category": category,
                "hint": "Ask the citizen to choose one of the six demo categories.",
            }
        )

    memory_values = {
        "service_area_id": route["service_area_id"],
        "authority_id": route["authority_id"],
        "authority_name": route["authority_name"],
        "zone": route.get("zone") or "",
        "ward": route.get("ward") or "",
        "ward_officer_name": route.get("ward_officer_name") or "",
        "ward_officer_contact": route.get("ward_officer_contact") or "",
        "department": route["department"],
        "sla_days": str(route["sla_days"]),
        "routing_rule_version": route["routing_version"],
        "routing_source": route["routing_source"],
    }
    if context is not None:
        for field, value in memory_values.items():
            context.memory.set(field, value)

    return ToolResult(
        llm_response={
            "ok": True,
            "pincode": pincode,
            "category": category,
            "civic_body": route["civic_body"],
            "authority_name": route["authority_name"],
            "department": route["department"],
            "zone": route.get("zone") or "",
            "ward": route.get("ward") or "",
            "ward_officer_name": route.get("ward_officer_name") or "",
            "ward_officer_contact": route.get("ward_officer_contact") or "",
            "queue_id": route.get("queue_id") or "",
            "sla_days": str(route["sla_days"]),
            "routing_rule_version": route["routing_version"],
            "routing_source": route["routing_source"],
            "demo": True,
            "hint": "Use these exact fictional values and describe the target as a demo value.",
        }
    )


@tool(
    description=(
        "File the complaint with the local demo system and return its reference "
        "number. Only call once the summary has been read back and confirmed."
    )
)
async def submit_complaint(context: ToolContext = None) -> ToolResult:
    """Persist the complaint. Returns ok: False when the municipal system is down."""
    # Deliberate failure switch so the on_failure lever is demoable on demand.
    if os.getenv("CIVIC_FORCE_SUBMIT_FAILURE") == "1":
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "municipal_system_unavailable",
                "hint": "The demo system is not responding. Do not retry silently.",
            }
        )

    category = _memory(context, "category")
    area = _memory(context, "area")
    pincode = normalize_pincode(_memory(context, "pincode"))
    latitude = _memory_float(context, "latitude")
    longitude = _memory_float(context, "longitude")
    reporter_key = _reporter_key(context)
    location_settled = _memory(context, "location_settled") == "True"
    location_precision = _memory(context, "location_precision") or "exact_landmark"
    location_note = _memory(context, "location_note") or ""
    details_verified = _memory(context, "details_verified") == "True"

    if not (
        category
        and area
        and pincode
        and latitude is not None
        and longitude is not None
        and location_settled
        and details_verified
        and reporter_key
    ):
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "incomplete_complaint",
                "hint": (
                    "Category, a settled location (exact or approximate), PIN code, "
                    "callback number and citizen-approved summary are all required."
                ),
            }
        )

    try:
        route = authority_route_for(pincode, category)
    except DemoRoutingError as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "demo_routing_unavailable",
                "detail": str(exc)[:200],
            }
        )
    if route is None:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "demo_route_unavailable",
                "pincode": pincode,
                "hint": "Nothing was registered. Ask for one of the six demo categories.",
            }
        )

    department = str(route["department"])
    sla_days = str(route["sla_days"])

    try:
        with Database() as db:
            complaint_id = db.create_complaint(
                citizen_id=reporter_key,
                category=category,
                description=_memory(context, "description") or "",
                location_text=_memory(context, "location_text") or "",
                raw_location_text=_memory(context, "raw_location") or "",
                area=area,
                locality=_memory(context, "locality") or area,
                city=_memory(context, "city") or "",
                state=_memory(context, "state") or "",
                pincode=pincode,
                osm_place_id=_memory(context, "osm_place_id") or "",
                department=department,
                sla_days=sla_days,
                service_area_id=str(route["service_area_id"]),
                authority_id=str(route["authority_id"]),
                authority_name=str(route["authority_name"]),
                zone=str(route.get("zone") or ""),
                ward=str(route.get("ward") or ""),
                routing_rule_version=str(route["routing_version"]),
                routing_source=str(route["routing_source"]),
                created_at=date.today().isoformat(),
                latitude=latitude,
                longitude=longitude,
                location_precision=location_precision,
                location_note=location_note,
            )
    except sqlite3.Error as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "municipal_system_unavailable",
                "detail": str(exc)[:200],
                "hint": "Nothing was registered. Do not retry silently.",
            }
        )

    spoken_complaint_id = _speak_reference(complaint_id)
    if context is not None:
        context.memory.set("complaint_id", complaint_id)
        context.memory.set("spoken_complaint_id", spoken_complaint_id)
        context.memory.set("service_area_id", route["service_area_id"])
        context.memory.set("authority_id", route["authority_id"])
        context.memory.set("authority_name", route["authority_name"])
        context.memory.set("zone", route.get("zone") or "")
        context.memory.set("ward", route.get("ward") or "")
        context.memory.set("department", department)
        context.memory.set("sla_days", sla_days)
        context.memory.set("routing_rule_version", route["routing_version"])
        context.memory.set("routing_source", route["routing_source"])

    return ToolResult(
        llm_response={
            "ok": True,
            "complaint_id": complaint_id,
            "spoken_complaint_id": spoken_complaint_id,
            "department": department,
            "authority_name": route["authority_name"],
            "zone": route.get("zone") or "",
            "ward": route.get("ward") or "",
            "sla_days": sla_days,
            "routing_rule_version": route["routing_version"],
            "location_precision": location_precision,
            "status": "registered",
        }
    )


@tool(
    description=(
        "Look up the current status of a complaint by its reference number, "
        "for example CIV1001."
    )
)
async def get_complaint_status(
    complaint_id: str = "", context: ToolContext = None
) -> ToolResult:
    """Return status, department, and resolution note for one complaint."""
    reference = _normalize_complaint_id(
        complaint_id or _memory(context, "complaint_id") or ""
    )

    if not reference:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "no_complaint_id",
                "hint": "Ask for the complaint number, or offer to search by location.",
            }
        )

    citizen_id = _reporter_key(context)
    with Database() as db:
        row = db.run_query(
            """
            SELECT c.complaint_id, c.category, c.description, c.location_text,
                   c.area, c.department, c.sla_days, c.status, c.created_at,
                   c.resolution_note
            FROM complaints AS c
            WHERE c.complaint_id = ?
              AND (
                  c.citizen_id = ?
                  OR EXISTS (
                      SELECT 1 FROM complaint_reports AS r
                      WHERE r.complaint_id = c.complaint_id AND r.citizen_id = ?
                  )
              )
            """,
            (reference, citizen_id, citizen_id),
        )

    if not row:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "complaint_not_found",
                "complaint_id": reference,
                "hint": "Read the number back and check it, or search by location.",
            }
        )

    (
        cid,
        category,
        description,
        location_text,
        area,
        department,
        sla_days,
        status,
        created_at,
        resolution_note,
    ) = row

    if context is not None:
        context.memory.set("complaint_id", cid)
        context.memory.set("spoken_complaint_id", _speak_reference(cid))
        context.memory.set("area", area)
        context.memory.set("department", department)
        context.memory.set("sla_days", sla_days)
        context.memory.set("status", status)

    return ToolResult(
        llm_response={
            "ok": True,
            "complaint_id": cid,
            "spoken_complaint_id": _speak_reference(cid),
            "category": category,
            "description": description,
            "location_text": location_text,
            "area": area,
            "department": department,
            "sla_days": sla_days,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "created_at": created_at,
            "resolution_note": resolution_note or "",
        }
    )


@tool(
    description=(
        "List the caller's own complaints from their phone number, for when "
        "they cannot remember a reference. Ask for the number, pass the digits."
    )
)
async def find_my_complaints(phone: str = "", context: ToolContext = None) -> ToolResult:
    """Look up a caller's complaints by phone number.

    This replaced a lookup that first geocoded a spoken neighbourhood. That
    version had no confirmed area to scope to, so an unmappable phrase came
    back as somewhere in another state and was read out as the caller's own
    complaint. A phone number is what a civic line actually has on file, needs
    no map, and cannot resolve to the wrong city.
    """
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) < 10:
        digits = "".join(
            ch for ch in str(_memory(context, "contact_number") or "") if ch.isdigit()
        )
    if len(digits) < 10:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "incomplete_number",
                "digits_heard": len(digits),
                "hint": "Read back what you heard and ask them to repeat it.",
            }
        )
    digits = digits[-10:]

    with Database() as db:
        citizen = db.run_query(
            "SELECT citizen_id FROM citizens WHERE phone = ?", (digits,)
        )
        keys = [str(citizen[0])] if citizen else []
        keys.append(f"phone:{digits}")
        placeholders = ",".join("?" for _ in keys)
        rows = db.run_query(
            f"""
            SELECT c.complaint_id, c.category, c.location_text, c.status, c.created_at
            FROM complaints AS c
            WHERE c.citizen_id IN ({placeholders})
               OR EXISTS (
                   SELECT 1 FROM complaint_reports AS r
                   WHERE r.complaint_id = c.complaint_id
                     AND r.citizen_id IN ({placeholders})
               )
            ORDER BY c.created_at DESC
            """,
            tuple(keys) * 2,
            one_record=False,
        )

    complaints = [
        {
            "complaint_id": cid,
            "spoken_complaint_id": _speak_reference(cid),
            "category": category,
            "location_text": location_text,
            "status": status,
            "created_at": created_at,
        }
        for cid, category, location_text, status, created_at in rows or []
    ]

    return ToolResult(
        llm_response={
            "ok": True,
            "complaint_count": len(complaints),
            "complaints": complaints,
            "hint": (
                "Read back each one briefly — problem, where, and status — and "
                "ask which they mean. Nothing found is normal, not an error."
            ),
        }
    )


@tool(description="Raise a callback ticket with the ward office for a human officer.")
async def create_handoff_ticket(context: ToolContext = None) -> ToolResult:
    """Create and persist a mock ward-office callback ticket."""
    area = _memory(context, "area") or "your local"
    contact_number = _memory(context, "contact_number") or ""
    reporter_key = _reporter_key(context)
    if not reporter_key:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "contact_required",
                "hint": "Ask for and validate a callback number first.",
            }
        )
    try:
        with Database() as db:
            ticket_id = db.create_handoff_ticket(
                citizen_id=reporter_key,
                reason=_memory(context, "handoff_reason") or "",
                area=area,
                contact_number=contact_number,
                created_at=date.today().isoformat(),
            )
    except sqlite3.Error as exc:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": "municipal_system_unavailable",
                "detail": str(exc)[:200],
            }
        )

    spoken_ticket_id = _speak_reference(ticket_id)
    if context is not None:
        context.memory.set("ticket_id", ticket_id)
        context.memory.set("spoken_ticket_id", spoken_ticket_id)

    return ToolResult(
        llm_response={
            "ok": True,
            "ticket_id": ticket_id,
            "spoken_ticket_id": spoken_ticket_id,
            "area": area,
            "contact_number": contact_number,
        }
    )
