"""Print the seeded demo data."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.database import CATEGORY_ROUTING, Database  # noqa: E402


def main() -> None:
    db = Database()

    print("\n=== Demo citizen ===")
    for citizen_id, name, phone in (
        db.run_query("SELECT citizen_id, name, phone FROM citizens", one_record=False)
        or []
    ):
        print(f"  {name}  (id {citizen_id})  phone {phone}")

    print("\n=== Fictional category routing (demo table) ===")
    for category, (department, sla_days) in CATEGORY_ROUTING.items():
        print(f"  {category:<15} -> {department:<15} {sla_days} working days")

    print("\n=== Seeded complaints ===")
    for cid, category, area, status, location_text, created_at in (
        db.run_query(
            """
            SELECT complaint_id, category, area, status, location_text, created_at
            FROM complaints ORDER BY complaint_id
            """,
            one_record=False,
        )
        or []
    ):
        print(f"  {cid}  {category:<13} {area:<16} {status:<12} {location_text}  ({created_at})")

    print("\n=== Locations are looked up live on OpenStreetMap ===")
    print("  Say a locality and landmark; the citizen confirms each map result.")
    print("  Departments and target days are mock values from demo_routing.json.")

    print("\n=== Try these ===")
    print('  "pothole on 100 feet road in Indiranagar"  -> duplicate of CIV1001 fires')
    print('  "garbage near Forum Mall Koramangala"      -> clean new complaint')
    print('  "behind Anand Sweets"                      -> 3 real matches, you pick')
    print('  "status of C I V one zero zero four"       -> resolved path')
    print('  "status of C I V one zero zero five"       -> rejected path')
    print("\n  make demo-failure   -> submit fails on purpose, exercises on_failure\n")


if __name__ == "__main__":
    main()
