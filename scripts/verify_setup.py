"""Pre-flight diagnostics for the Civico voice civic agent."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

OK = "\033[92m✓\033[0m"
WARN = "\033[93m!\033[0m"
FAIL = "\033[91m✗\033[0m"

failures = 0
warnings = 0


def check(label: str, passed: bool, hint: str = "", fatal: bool = True) -> None:
    global failures, warnings
    if passed:
        print(f"  {OK} {label}")
        return
    if fatal:
        failures += 1
        print(f"  {FAIL} {label}")
    else:
        warnings += 1
        print(f"  {WARN} {label}")
    if hint:
        print(f"      {hint}")


print("\nCivico — pre-flight check\n")

print("Environment")
check(
    "RASA_LICENSE set",
    bool(os.getenv("RASA_LICENSE")),
    "Request one: https://rasa.com/rasa-pro-developer-edition-license-key-request/",
)
check(
    "OPENAI_API_KEY set",
    bool(os.getenv("OPENAI_API_KEY")),
    "https://platform.openai.com/api-keys",
)
check(
    "DEEPGRAM_API_KEY set",
    bool(os.getenv("DEEPGRAM_API_KEY")),
    "Voice only — text still works without it. https://console.deepgram.com/",
    fatal=False,
)

print("\nProject files")
for name in (
    "agent.yml",
    "integrations.yml",
    "endpoints.yml",
    "memory.yml",
    "responses.yml",
):
    check(name, (PROJECT_ROOT / name).exists())

print("\nSkills")
skills_dir = PROJECT_ROOT / "skills"
skills = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
check(f"{len(skills)} skills found: {', '.join(skills)}", bool(skills))
for skill in skills:
    check(
        f"skills/{skill}/skill.md",
        (skills_dir / skill / "skill.md").exists(),
    )

print("\nCALM v1 leftovers (should be absent)")
for name in ("config.yml", "domain.yml"):
    check(
        f"no {name}",
        not (PROJECT_ROOT / name).exists(),
        f"This is a Skills project — delete {name}.",
    )

print("\nDemo data")
try:
    from lib.database import Database

    with Database() as db:
        complaints = db.run_query("SELECT COUNT(*) FROM complaints")
        check(f"{complaints[0]} seeded complaints", complaints[0] > 0)

        statuses = {
            row[0]
            for row in db.run_query(
                "SELECT DISTINCT status FROM complaints", one_record=False
            )
            or []
        }
        geocoded = db.run_query(
            """
            SELECT COUNT(*) FROM complaints
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            """
        )
        tables = {
            row[0]
            for row in db.run_query(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
                one_record=False,
            )
            or []
        }
    check(
        f"all 5 statuses seeded ({', '.join(sorted(statuses))})",
        len(statuses) >= 5,
        "Some if: branches will not be demoable.",
        fatal=False,
    )
    check(f"{geocoded[0]} complaints have coordinates", geocoded[0] > 0)
    check(
        "attachment and handoff tables available",
        {"complaint_reports", "handoff_tickets"}.issubset(tables),
    )
except Exception as exc:  # noqa: BLE001
    check("demo database loads", False, str(exc))

print("\nGeocoder (OpenStreetMap)")
try:
    from lib.geocode import lookup

    hits = lookup("100 feet road Indiranagar")
    check(f"Nominatim reachable ({len(hits)} match(es))", bool(hits),
          "Location lookup needs internet. Everything else works offline.",
          fatal=False)
except Exception as exc:  # noqa: BLE001
    check("Nominatim reachable", False, str(exc)[:120], fatal=False)

if os.getenv("CIVIC_FORCE_SUBMIT_FAILURE") == "1":
    print(f"\n  {WARN} CIVIC_FORCE_SUBMIT_FAILURE=1 — submit_complaint will fail on purpose")

print()
if failures:
    print(f"{FAIL} {failures} check(s) failed.\n")
    sys.exit(1)
if warnings:
    print(f"{WARN} {warnings} warning(s), nothing fatal. Next: make train\n")
else:
    print(f"{OK} All checks passed. Next: make train\n")
