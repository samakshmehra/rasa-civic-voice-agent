"""SQLite mock municipal backend for the Civico voice demo."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


def project_root() -> Path:
    """Locate the project directory that owns ``data/source``.

    This cannot be derived from ``__file__``. ``rasa train`` copies ``lib/`` and
    ``tools/`` into the model archive, and ``rasa run`` unpacks that archive into
    a temp directory and executes the tools from there — but ``data/`` is not
    packaged. Resolving relative to ``__file__`` therefore points at a temp
    folder with no seed data, and the database silently comes up empty.

    Order: explicit env var, then the working directory the agent was started
    from, then the module location as a last resort.
    """
    override = os.getenv("CIVIC_PROJECT_ROOT")
    if override and (Path(override) / "data" / "source").is_dir():
        return Path(override)

    cwd = Path.cwd()
    if (cwd / "data" / "source").is_dir():
        return cwd

    return Path(__file__).resolve().parent.parent


DEMO_CITIZEN_NAME = "Ravi Kumar"
DEMO_CITIZEN_ID = "501"
DEMO_CONTACT_NUMBER = "9876543210"

# Category -> (fictional department, demo target in days).
# Kept in sync with demo_routing.json for FAQ answers without a location.
CATEGORY_ROUTING: Dict[str, Tuple[str, str]] = {
    "pothole": ("Road Maintenance", "7"),
    "water_supply": ("Water Services", "3"),
    "garbage": ("Sanitation", "2"),
    "streetlight": ("Street Lighting", "3"),
    "drainage": ("Drainage", "5"),
    "stray_animals": ("Animal Care", "5"),
}

logger = logging.getLogger(__name__)


class Database:
    """In-memory-first SQLite store seeded from ``data/source/*.json``."""

    table_definitions = {
        "citizens": {
            "create_statement": """
                CREATE TABLE IF NOT EXISTS citizens (
                    id INTEGER PRIMARY KEY,
                    citizen_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT
                )
            """,
            "insert_statement": (
                "INSERT INTO citizens (citizen_id, name, phone) VALUES (?, ?, ?)"
            ),
            "columns": ["citizen_id", "name", "phone"],
        },
        "complaints": {
            "create_statement": """
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY,
                    complaint_id TEXT UNIQUE NOT NULL,
                    citizen_id TEXT,
                    category TEXT NOT NULL,
                    description TEXT,
                    location_text TEXT,
                    raw_location_text TEXT,
                    area TEXT NOT NULL,
                    locality TEXT,
                    city TEXT,
                    state TEXT,
                    pincode TEXT,
                    osm_place_id TEXT,
                    department TEXT NOT NULL,
                    sla_days TEXT NOT NULL,
                    service_area_id TEXT,
                    authority_id TEXT,
                    authority_name TEXT,
                    zone TEXT,
                    ward TEXT,
                    routing_rule_version TEXT,
                    routing_source TEXT,
                    location_precision TEXT,
                    location_note TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolution_note TEXT,
                    latitude REAL,
                    longitude REAL
                )
            """,
            "insert_statement": (
                "INSERT INTO complaints (complaint_id, citizen_id, category, description, "
                "location_text, raw_location_text, area, locality, city, state, pincode, "
                "osm_place_id, department, sla_days, service_area_id, authority_id, "
                "authority_name, zone, ward, routing_rule_version, routing_source, "
                "location_precision, status, created_at, resolution_note, latitude, longitude) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            "columns": [
                "complaint_id",
                "citizen_id",
                "category",
                "description",
                "location_text",
                "raw_location_text",
                "area",
                "locality",
                "city",
                "state",
                "pincode",
                "osm_place_id",
                "department",
                "sla_days",
                "service_area_id",
                "authority_id",
                "authority_name",
                "zone",
                "ward",
                "routing_rule_version",
                "routing_source",
                "location_precision",
                "status",
                "created_at",
                "resolution_note",
                "latitude",
                "longitude",
            ],
        },
        "complaint_reports": {
            "create_statement": """
                CREATE TABLE IF NOT EXISTS complaint_reports (
                    id INTEGER PRIMARY KEY,
                    complaint_id TEXT NOT NULL,
                    citizen_id TEXT NOT NULL,
                    description TEXT,
                    contact_number TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (complaint_id, citizen_id),
                    FOREIGN KEY (complaint_id) REFERENCES complaints(complaint_id)
                )
            """,
            "insert_statement": (
                "INSERT INTO complaint_reports (complaint_id, citizen_id, description, "
                "contact_number, created_at) VALUES (?, ?, ?, ?, ?)"
            ),
            "columns": [
                "complaint_id",
                "citizen_id",
                "description",
                "contact_number",
                "created_at",
            ],
        },
        "handoff_tickets": {
            "create_statement": """
                CREATE TABLE IF NOT EXISTS handoff_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT UNIQUE,
                    citizen_id TEXT NOT NULL,
                    reason TEXT,
                    area TEXT,
                    contact_number TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                )
            """,
            "insert_statement": (
                "INSERT INTO handoff_tickets (ticket_id, citizen_id, reason, area, "
                "contact_number, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            "columns": [
                "ticket_id",
                "citizen_id",
                "reason",
                "area",
                "contact_number",
                "created_at",
                "status",
            ],
        },
    }

    def __init__(self, database_path: Optional[Path] = None) -> None:
        self.project_root_path = project_root()
        self.database_path = database_path or (
            self.project_root_path / "data" / "civic.db"
        )
        self.source_data_path = self.project_root_path / "data" / "source"

        if not self.database_path.exists():
            if not self.source_data_path.is_dir():
                raise FileNotFoundError(
                    "Civico seed data is unavailable. Start the agent from the "
                    "project directory or set CIVIC_PROJECT_ROOT to that directory."
                )
            # Seed in memory first, flush once, then reopen the file so every
            # later write lands on disk directly. Backing the file up onto
            # itself after a write deadlocks.
            seed = sqlite3.connect(":memory:")
            self.connection = seed
            self._configure_connection()
            self.create_schema()
            self.load_data()
            self.save_to_disk()
            seed.close()

        self.connection = sqlite3.connect(str(self.database_path), timeout=10)
        self._configure_connection()
        self.cursor = self.connection.cursor()
        self.create_schema()
        self._migrate_schema()
        self._hydrate_seed_coordinates()

    def _configure_connection(self) -> None:
        """Apply safety settings to every SQLite connection."""
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 10000")

    def create_schema(self) -> None:
        for definition in self.table_definitions.values():
            self.connection.execute(definition["create_statement"])
        self.connection.commit()

    def load_data(self) -> None:
        for source_file in sorted(self.source_data_path.glob("*.json")):
            with open(source_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            table_name = source_file.stem.lower()
            if table_name in self.table_definitions:
                self.insert_data(table_name, data)

    def insert_data(self, table_name: str, data: List[Dict[str, Any]]) -> None:
        definition = self.table_definitions[table_name]
        for row in data:
            values = tuple(row.get(column) for column in definition["columns"])
            self.connection.execute(definition["insert_statement"], values)
        self.connection.commit()

    def _migrate_schema(self) -> None:
        """Upgrade an existing demo database without deleting citizen data."""
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(complaints)").fetchall()
        }
        additions = {
            "latitude": "REAL",
            "longitude": "REAL",
            "raw_location_text": "TEXT",
            "locality": "TEXT",
            "city": "TEXT",
            "state": "TEXT",
            "pincode": "TEXT",
            "osm_place_id": "TEXT",
            "service_area_id": "TEXT",
            "authority_id": "TEXT",
            "authority_name": "TEXT",
            "zone": "TEXT",
            "ward": "TEXT",
            "routing_rule_version": "TEXT",
            "routing_source": "TEXT",
            "location_precision": "TEXT",
        }
        for name, column_type in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE complaints ADD COLUMN {name} {column_type}"
                )
        self.connection.commit()

    def _hydrate_seed_coordinates(self) -> None:
        """Backfill coordinates for seeded rows after an in-place migration."""
        source = self.source_data_path / "complaints.json"
        if not source.exists():
            return
        with open(source, "r", encoding="utf-8") as file:
            complaints = json.load(file)
        for complaint in complaints:
            latitude = complaint.get("latitude")
            longitude = complaint.get("longitude")
            if latitude is None or longitude is None:
                continue
            self.connection.execute(
                """
                UPDATE complaints
                SET area = ?, latitude = ?, longitude = ?,
                    raw_location_text = COALESCE(?, raw_location_text),
                    locality = COALESCE(?, locality),
                    city = COALESCE(?, city),
                    state = COALESCE(?, state),
                    pincode = COALESCE(?, pincode),
                    osm_place_id = COALESCE(?, osm_place_id),
                    service_area_id = COALESCE(?, service_area_id),
                    authority_id = COALESCE(?, authority_id),
                    authority_name = COALESCE(?, authority_name),
                    zone = COALESCE(?, zone),
                    ward = COALESCE(?, ward),
                    routing_rule_version = COALESCE(?, routing_rule_version),
                    routing_source = COALESCE(?, routing_source),
                    location_precision = COALESCE(?, location_precision)
                WHERE complaint_id = ?
                """,
                (
                    complaint["area"],
                    latitude,
                    longitude,
                    complaint.get("raw_location_text")
                    or complaint.get("location_text"),
                    complaint.get("locality") or complaint.get("area"),
                    complaint.get("city"),
                    complaint.get("state"),
                    complaint.get("pincode"),
                    complaint.get("osm_place_id"),
                    complaint.get("service_area_id"),
                    complaint.get("authority_id"),
                    complaint.get("authority_name"),
                    complaint.get("zone"),
                    complaint.get("ward"),
                    complaint.get("routing_rule_version"),
                    complaint.get("routing_source"),
                    complaint.get("location_precision"),
                    complaint["complaint_id"],
                ),
            )
        self.connection.commit()

    def save_to_disk(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.database_path)) as backup_db:
            self.connection.backup(backup_db)

    def run_query(
        self, query: str, parameters: Tuple = (), one_record: bool = True
    ) -> Union[Tuple, List[Tuple], None]:
        self.cursor.execute(query, parameters)
        if one_record:
            return self.cursor.fetchone()
        return self.cursor.fetchall()

    def commit(self) -> None:
        self.connection.commit()

    def create_complaint(
        self,
        *,
        citizen_id: str,
        category: str,
        description: str,
        location_text: str,
        raw_location_text: str,
        area: str,
        locality: str,
        city: str,
        state: str,
        pincode: str,
        osm_place_id: str,
        department: str,
        sla_days: str,
        service_area_id: str,
        authority_id: str,
        authority_name: str,
        zone: str,
        ward: str,
        routing_rule_version: str,
        routing_source: str,
        created_at: str,
        latitude: Optional[float],
        longitude: Optional[float],
        location_precision: str = "exact_landmark",
        location_note: str = "",
    ) -> str:
        """Atomically allocate a reference and insert a new complaint."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT MAX(CAST(SUBSTR(complaint_id, 4) AS INTEGER))
                FROM complaints WHERE complaint_id GLOB 'CIV[0-9]*'
                """
            ).fetchone()
            complaint_id = f"CIV{(int(row[0]) if row and row[0] else 1000) + 1}"
            self.connection.execute(
                """
                INSERT INTO complaints (
                    complaint_id, citizen_id, category, description, location_text,
                    raw_location_text, area, locality, city, state, pincode,
                    osm_place_id, department, sla_days, service_area_id,
                    authority_id, authority_name, zone, ward, routing_rule_version,
                    routing_source, location_precision, location_note, status,
                    created_at, resolution_note, latitude, longitude
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, 'registered', ?, '', ?, ?)
                """,
                (
                    complaint_id,
                    citizen_id,
                    category,
                    description,
                    location_text,
                    raw_location_text,
                    area,
                    locality,
                    city,
                    state,
                    pincode,
                    osm_place_id,
                    department,
                    sla_days,
                    service_area_id,
                    authority_id,
                    authority_name,
                    zone,
                    ward,
                    routing_rule_version,
                    routing_source,
                    location_precision,
                    location_note,
                    created_at,
                    latitude,
                    longitude,
                ),
            )
            self.connection.commit()
            return complaint_id
        except Exception:
            self.connection.rollback()
            raise

    def attach_report(
        self,
        *,
        complaint_id: str,
        citizen_id: str,
        description: str,
        contact_number: str,
        created_at: str,
    ) -> Tuple[bool, int]:
        """Persist a citizen's support for an existing complaint."""
        before = self.connection.total_changes
        self.connection.execute(
            """
            INSERT OR IGNORE INTO complaint_reports (
                complaint_id, citizen_id, description, contact_number, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (complaint_id, citizen_id, description, contact_number, created_at),
        )
        inserted = self.connection.total_changes > before
        row = self.connection.execute(
            "SELECT COUNT(*) FROM complaint_reports WHERE complaint_id = ?",
            (complaint_id,),
        ).fetchone()
        self.connection.commit()
        return inserted, int(row[0]) if row else 0

    def create_handoff_ticket(
        self,
        *,
        citizen_id: str,
        reason: str,
        area: str,
        contact_number: str,
        created_at: str,
    ) -> str:
        """Persist a ward-office callback ticket and return its stable reference."""
        cursor = self.connection.execute(
            """
            INSERT INTO handoff_tickets (
                citizen_id, reason, area, contact_number, created_at, status
            ) VALUES (?, ?, ?, ?, ?, 'open')
            """,
            (citizen_id, reason, area, contact_number, created_at),
        )
        ticket_id = f"WO{cursor.lastrowid:04d}"
        self.connection.execute(
            "UPDATE handoff_tickets SET ticket_id = ? WHERE id = ?",
            (ticket_id, cursor.lastrowid),
        )
        self.connection.commit()
        return ticket_id

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.connection.close()


def resolve_citizen_id(context_citizen_id: Optional[str] = None) -> str:
    """Return the active demo citizen id (defaults to 501)."""
    if context_citizen_id and str(context_citizen_id).strip():
        return str(context_citizen_id).strip()
    return DEMO_CITIZEN_ID

def routing_for(category: str) -> Optional[Tuple[str, str]]:
    """Look up (department, sla_days) for a category. Never generated."""
    if not category:
        return None
    return CATEGORY_ROUTING.get(str(category).strip().lower())
