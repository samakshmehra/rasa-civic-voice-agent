"""Small fictional routing table for the Civico demo.

OpenStreetMap resolves locations in ``lib.geocode``. This module does only one
thing: map a confirmed PIN code and complaint category to mock routing data.
Nothing returned here represents a real municipal authority or service promise.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from lib.database import project_root

ROUTING_FILENAME = "demo_routing.json"
PINCODE_PATTERN = re.compile(r"^[1-9][0-9]{5}$")


class DemoRoutingError(RuntimeError):
    """Raised when the local demo routing file is missing or malformed."""


def normalize_pincode(value: object) -> str:
    """Return one valid six-digit Indian PIN code, otherwise an empty string."""
    candidate = str(value or "").strip()
    return candidate if PINCODE_PATTERN.fullmatch(candidate) else ""


def routing_path() -> Path:
    return project_root() / "data" / "source" / ROUTING_FILENAME


@lru_cache(maxsize=1)
def load_routing() -> Dict[str, Any]:
    """Load and minimally validate the fictional routing table."""
    path = routing_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoRoutingError(f"Could not load {path}: {exc}") from exc

    if not isinstance(payload.get("version"), str):
        raise DemoRoutingError("Demo routing has no version")

    authority = payload.get("authority")
    if not isinstance(authority, dict) or not all(
        authority.get(field) for field in ("id", "name")
    ):
        raise DemoRoutingError("Demo routing has no mock authority")

    officers = payload.get("ward_officers")
    if not isinstance(officers, list) or not officers:
        raise DemoRoutingError("Demo routing has no ward officers")
    for officer in officers:
        if not isinstance(officer, dict) or not all(
            officer.get(field) for field in ("name", "contact")
        ):
            raise DemoRoutingError("Incomplete ward officer entry")

    routes = payload.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise DemoRoutingError("Demo routing has no category routes")

    for category, route in routes.items():
        if not isinstance(route, dict):
            raise DemoRoutingError(f"Invalid route for {category}")
        if not all(route.get(field) for field in ("department", "queue_id", "sla_days")):
            raise DemoRoutingError(f"Incomplete route for {category}")

    precision = payload.get("location_precision")
    if not isinstance(precision, dict):
        raise DemoRoutingError("Demo routing has no location precision table")
    levels = precision.get("levels")
    if not isinstance(levels, dict) or not levels:
        raise DemoRoutingError("Demo routing has no precision levels")
    for name, level in levels.items():
        if not isinstance(level, dict) or "attempts_before_approximate" not in level:
            raise DemoRoutingError(f"Invalid precision level {name}")
    by_category = precision.get("by_category")
    if not isinstance(by_category, dict):
        raise DemoRoutingError("Demo routing has no per-category precision")
    for category in routes:
        entry = by_category.get(category)
        if not isinstance(entry, dict) or entry.get("requires") not in levels:
            raise DemoRoutingError(f"No valid precision requirement for {category}")

    return payload


def precision_for(category: object) -> Optional[Dict[str, Any]]:
    """How precisely this category needs locating, and when to stop asking.

    The model decides what it heard; this decides whether that is good enough.
    Keeping the requirement in config rather than in the prompt means a caller
    reporting a broken streetlight is always asked which pole, and a caller
    reporting stray dogs is never asked at all — regardless of how the model
    feels about the phrasing on the day.
    """
    routing = load_routing()
    entry = routing["location_precision"]["by_category"].get(str(category or ""))
    if entry is None:
        return None
    level_name = str(entry["requires"])
    level = routing["location_precision"]["levels"][level_name]
    return {
        "category": str(category),
        "requires": level_name,
        "reason": str(entry.get("reason") or ""),
        "attempts_before_approximate": int(level["attempts_before_approximate"]),
        "ask_for": str(level.get("ask_for") or ""),
    }


def ward_for(pincode: str) -> Dict[str, str]:
    """Derive a stable fictional ward and named officer from a PIN code.

    The demo accepts any valid Indian PIN code, so a fixed lookup table is not
    possible. Deriving from the PIN keeps every answer deterministic — the same
    PIN always returns the same ward and officer, so a caller who rings twice
    is told the same thing — while staying obviously fictional.
    """
    officers = load_routing()["ward_officers"]
    officer = officers[int(pincode) % len(officers)]
    return {
        "zone": f"Demo Zone {(int(pincode) % 4) + 1}",
        "ward": f"Demo Ward {(int(pincode[-2:]) % 20) + 1}",
        "ward_officer_name": officer["name"],
        "ward_officer_contact": officer["contact"],
    }


def route_for(pincode: object, category: object) -> Optional[Dict[str, Any]]:
    """Return fictional routing for any map-confirmed six-digit PIN code."""
    normalized_pincode = normalize_pincode(pincode)
    selected_category = str(category or "").strip().lower()
    if not normalized_pincode or not selected_category:
        return None

    config = load_routing()
    route = config["routes"].get(selected_category)
    if not isinstance(route, dict):
        return None

    authority = config["authority"]
    return {
        "routing_version": config["version"],
        "routing_source": "fictional_demo_config",
        "service_area_id": f"demo-{normalized_pincode}",
        "pincode": normalized_pincode,
        "civic_body": authority["name"],
        "authority_id": authority["id"],
        "authority_name": authority["name"],
        "category": selected_category,
        **ward_for(normalized_pincode),
        **route,
    }
