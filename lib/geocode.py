"""OpenStreetMap (Nominatim) landmark lookup for the Civico demo.

Citizens describe places by landmark, so the agent geocodes whatever they said
and keeps the neighbourhood name OSM returns. No API key is required.

Nominatim's usage policy caps requests at roughly one per second and requires an
identifying User-Agent, so results are cached in-process and calls are spaced.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Sequence, Tuple

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "civico-voice-civic/0.1 (Rasa Heroes beta example)"
COUNTRY_CODES = os.getenv("CIVIC_COUNTRY_CODES", "in")
# Empty by default: the caller's own words are the query. Set CIVIC_CITY_HINT
# only when an assistant serves exactly one city, and even then it is applied
# solely as a retry after the caller's own words find nothing.
CITY_HINT = os.getenv("CIVIC_CITY_HINT", "")
TIMEOUT_SECONDS = float(os.getenv("CIVIC_GEOCODE_TIMEOUT", "6"))
MIN_INTERVAL_SECONDS = 1.1

logger = logging.getLogger(__name__)

# Filler words that citizens speak but that break the geocoder. "near 100 feet
# road in Indiranagar" finds nothing; "100 feet road Indiranagar" resolves.
_FILLER = {
    "near", "nearby", "beside", "behind", "opposite", "front", "in", "at",
    "the", "a", "an", "of", "to", "my", "our", "this", "that", "here", "there",
    "side", "corner", "just", "around", "close", "next", "by", "outside",
    "ke", "ka", "ki", "paas", "peeche", "saamne", "wali", "wala", "gali",
    "mein", "me", "pe", "par", "hai", "hain", "aur",
}


def _clean(query: str) -> str:
    """Strip spoken filler so the geocoder sees a searchable phrase."""
    words = [w for w in str(query).replace(",", " ").split() if w]
    kept = [w for w in words if w.lower().strip(".") not in _FILLER]
    return " ".join(kept) if kept else str(query).strip()


_cache: Dict[Tuple[object, ...], Optional[list]] = {}
_lock = threading.Lock()
_last_call_at = 0.0


class GeocodeUnavailable(Exception):
    """Raised when the geocoding service could not be reached at all.

    Distinct from "the place was not found" — one is our problem, the other is
    the citizen's phrasing, and the agent must say different things.
    """


def _throttle() -> None:
    """Space calls out to respect the Nominatim usage policy."""
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def normalize_indian_pincode(value: object) -> str:
    """Return a six-digit Indian PIN code, or an empty string when invalid."""
    match = re.search(r"(?<!\d)([1-9]\d{5})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def lookup(
    landmark: str,
    limit: int = 3,
    *,
    viewbox: Optional[Sequence[float]] = None,
    bounded: bool = False,
) -> list:
    """Geocode a spoken landmark.

    Returns up to ``limit`` candidates with the address hierarchy, PIN code,
    OSM identifier, bounding box and coordinates needed for an auditable
    two-stage location flow. ``viewbox`` uses Nominatim's bounding-box order:
    ``[south, north, west, east]``. When ``bounded`` is true, results outside
    that confirmed broad area are rejected by Nominatim.

    Candidates are returned rather than a single best guess on purpose: the
    geocoder will confidently return the wrong city for an ambiguous landmark,
    so the citizen picks and the coordinates come from their choice.
    """
    if not landmark or not str(landmark).strip():
        return []

    cleaned = _clean(landmark)

    # Search the caller's own words first. Only if that finds nothing do we
    # retry with the configured city appended — callers routinely name their
    # own city ("vaishali ghaziabad"), and forcing a different one onto the
    # query makes every place outside it impossible to find.
    attempts = [cleaned]
    if CITY_HINT and CITY_HINT.lower() not in cleaned.lower():
        attempts.append(f"{cleaned}, {CITY_HINT}")

    for attempt_index, query in enumerate(attempts):
        found = _search(query, limit, viewbox=viewbox, bounded=bounded)
        if found or attempt_index == len(attempts) - 1:
            return found
    return []


def _search(
    query: str,
    limit: int,
    *,
    viewbox: Optional[Sequence[float]] = None,
    bounded: bool = False,
) -> list:
    """One Nominatim call, cached and throttled."""
    normalized_viewbox: Optional[Tuple[float, float, float, float]] = None
    if viewbox is not None:
        try:
            if len(viewbox) != 4:
                raise ValueError("viewbox must have four values")
            normalized_viewbox = tuple(float(value) for value in viewbox)  # type: ignore[assignment]
        except (TypeError, ValueError) as exc:
            raise ValueError("viewbox must be [south, north, west, east]") from exc

    cache_key = (query, int(limit), normalized_viewbox, bool(bounded))
    with _lock:
        if cache_key in _cache:
            return _cache[cache_key] or []

        _throttle()
        parameters = {
            "q": query,
            "format": "jsonv2",
            "limit": max(1, int(limit)),
            "addressdetails": 1,
            "countrycodes": COUNTRY_CODES,
        }
        if normalized_viewbox is not None:
            south, north, west, east = normalized_viewbox
            parameters["viewbox"] = f"{west},{north},{east},{south}"
            if bounded:
                parameters["bounded"] = 1
        url = f"{NOMINATIM_URL}?" + urllib.parse.urlencode(parameters)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("civico.geocode.unavailable query=%r error=%s", query, exc)
            raise GeocodeUnavailable(str(exc)) from exc

        candidates = []
        for item in payload:
            address = item.get("address", {}) or {}
            area = (
                address.get("suburb")
                or address.get("neighbourhood")
                or address.get("city_district")
                or address.get("town")
                or address.get("village")
                or address.get("city")
            )
            if not area:
                continue
            city = address.get("city") or address.get("state_district") or ""
            state = address.get("state") or ""
            postcode = normalize_indian_pincode(address.get("postcode"))
            # display_name is a long postal string — unusable when spoken aloud.
            place = (item.get("display_name") or "").split(",")[0].strip()
            label = ", ".join(part for part in (place, area, city) if part)
            raw_bbox = item.get("boundingbox") or []
            try:
                boundingbox = [float(value) for value in raw_bbox]
                if len(boundingbox) != 4:
                    boundingbox = []
            except (TypeError, ValueError):
                boundingbox = []
            candidates.append(
                {
                    "area": area,
                    "city": city,
                    "state": state,
                    "postcode": postcode,
                    "label": label,
                    "lat": item.get("lat"),
                    "lon": item.get("lon"),
                    "boundingbox": boundingbox,
                    "place_id": item.get("place_id"),
                    "osm_type": item.get("osm_type"),
                    "osm_id": item.get("osm_id"),
                    "category": item.get("category") or item.get("class"),
                    "place_type": item.get("type"),
                }
            )

        _cache[cache_key] = candidates
        return candidates
