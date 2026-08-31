"""Deterministic tests for the Civico demo boundary."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import geocode
from lib.authority import (
    load_routing,
    normalize_pincode,
    precision_for,
    route_for,
)
from lib.database import Database


class DemoRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        load_routing.cache_clear()

    def test_valid_map_confirmed_pin_uses_fictional_route(self) -> None:
        route = route_for("560095", "garbage")

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route["authority_name"], "Civico Demo Municipal Services")
        self.assertEqual(route["department"], "Sanitation")
        self.assertEqual(route["service_area_id"], "demo-560095")
        self.assertEqual(route["routing_source"], "fictional_demo_config")

    def test_all_six_demo_categories_are_configured(self) -> None:
        self.assertEqual(
            set(load_routing()["routes"]),
            {
                "pothole",
                "water_supply",
                "garbage",
                "streetlight",
                "drainage",
                "stray_animals",
            },
        )

    def test_invalid_pin_or_category_is_rejected(self) -> None:
        self.assertEqual(normalize_pincode("12345"), "")
        self.assertIsNone(route_for("12345", "garbage"))
        self.assertIsNone(route_for("560095", "fire"))

    def test_every_route_names_a_ward_officer(self) -> None:
        route = route_for("560095", "garbage")
        assert route is not None
        for field in ("ward", "zone", "ward_officer_name", "ward_officer_contact"):
            self.assertTrue(route[field], f"{field} must not be empty")

    def test_ward_officer_is_stable_for_the_same_pincode(self) -> None:
        # A caller who rings twice about the same area must be told the same
        # officer both times, so this has to be derived, never random.
        first = route_for("201010", "pothole")
        second = route_for("201010", "drainage")
        assert first is not None and second is not None
        self.assertEqual(first["ward"], second["ward"])
        self.assertEqual(first["ward_officer_name"], second["ward_officer_name"])
        self.assertEqual(
            first["ward_officer_contact"], second["ward_officer_contact"]
        )

    def test_ward_officer_comes_from_the_configured_roster(self) -> None:
        roster = {
            (officer["name"], officer["contact"])
            for officer in load_routing()["ward_officers"]
        }
        for pincode in ("560095", "201010", "110092", "400001"):
            route = route_for(pincode, "garbage")
            assert route is not None
            self.assertIn(
                (route["ward_officer_name"], route["ward_officer_contact"]),
                roster,
                "officer must come from demo_routing.json, never invented",
            )


class OpenStreetMapTests(unittest.TestCase):
    def setUp(self) -> None:
        geocode._cache.clear()

    def test_spoken_filler_is_removed_without_inventing_a_place(self) -> None:
        self.assertEqual(
            geocode._clean("near the 100 Feet Road in Indiranagar"),
            "100 Feet Road Indiranagar",
        )

    @patch("lib.geocode.urllib.request.urlopen")
    @patch("lib.geocode.MIN_INTERVAL_SECONDS", 0)
    def test_nominatim_response_becomes_a_confirmable_candidate(self, urlopen) -> None:
        payload = [
            {
                "place_id": 42,
                "osm_type": "way",
                "osm_id": 99,
                "lat": "12.9349",
                "lon": "77.6121",
                "boundingbox": ["12.93", "12.94", "77.60", "77.62"],
                "display_name": "Forum South Bengaluru, Adugodi, Bengaluru",
                "category": "place",
                "type": "mall",
                "address": {
                    "suburb": "Adugodi",
                    "city": "Bengaluru",
                    "state": "Karnataka",
                    "postcode": "560029",
                },
            }
        ]
        response = io.StringIO(json.dumps(payload))
        urlopen.return_value.__enter__.return_value = response

        candidates = geocode.lookup("Forum South Bengaluru")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["postcode"], "560029")
        self.assertEqual(candidates[0]["area"], "Adugodi")
        self.assertEqual(candidates[0]["label"], "Forum South Bengaluru, Adugodi, Bengaluru")
        request = urlopen.call_args.args[0]
        self.assertIn("nominatim.openstreetmap.org/search", request.full_url)
        self.assertIn("countrycodes=in", request.full_url)


class DatabaseTests(unittest.TestCase):
    def test_seed_data_loads_into_a_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "civic.db") as db:
                count = db.run_query("SELECT COUNT(*) FROM complaints")
                seeded_route = db.run_query(
                    "SELECT authority_name, routing_source FROM complaints "
                    "WHERE complaint_id = 'CIV0999'"
                )

        self.assertEqual(count[0], 6)
        self.assertEqual(
            seeded_route,
            ("Civico Demo Municipal Services", "fictional_demo_config"),
        )


class LocationPrecisionTests(unittest.TestCase):
    """The half of the location decision that the model does not get a vote on."""

    def setUp(self) -> None:
        load_routing.cache_clear()

    def test_every_routed_category_has_a_precision_requirement(self) -> None:
        for category in load_routing()["routes"]:
            self.assertIsNotNone(
                precision_for(category), f"{category} has no precision rule"
            )

    def test_a_streetlight_needs_a_more_exact_spot_than_stray_animals(self) -> None:
        # The same vague phrase has to behave differently by category: a repair
        # crew needs one pole, an animal-catching team works a whole area.
        streetlight = precision_for("streetlight")
        strays = precision_for("stray_animals")
        assert streetlight is not None and strays is not None
        self.assertEqual(streetlight["requires"], "exact")
        self.assertEqual(strays["requires"], "area")
        self.assertGreater(
            streetlight["attempts_before_approximate"],
            strays["attempts_before_approximate"],
        )

    def test_area_level_categories_never_interrogate_the_caller(self) -> None:
        strays = precision_for("stray_animals")
        assert strays is not None
        self.assertEqual(strays["attempts_before_approximate"], 0)

    def test_every_requirement_runs_out_so_filing_is_never_blocked(self) -> None:
        # The anti-dead-end guarantee: no category may demand a mappable
        # landmark forever, or the map's ignorance becomes the citizen's problem.
        for category in load_routing()["routes"]:
            requirement = precision_for(category)
            assert requirement is not None
            self.assertLessEqual(
                requirement["attempts_before_approximate"],
                3,
                f"{category} would trap the caller in a search loop",
            )

    def test_unknown_category_has_no_precision_rule(self) -> None:
        self.assertIsNone(precision_for("fire"))
        self.assertIsNone(precision_for(""))


class ApproximateComplaintTests(unittest.TestCase):
    def test_a_complaint_can_be_filed_without_an_exact_map_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "civic.db") as db:
                complaint_id = db.create_complaint(
                    citizen_id="501",
                    category="garbage",
                    description="Rubbish piling up for a week",
                    location_text="near the pole outside our lane",
                    raw_location_text="near the pole outside our lane",
                    area="Vaishali",
                    locality="Vaishali",
                    city="Ghaziabad",
                    state="Uttar Pradesh",
                    pincode="201010",
                    osm_place_id="",
                    department="Sanitation",
                    sla_days="2",
                    service_area_id="demo-201010",
                    authority_id="civico-demo-services",
                    authority_name="Civico Demo Municipal Services",
                    zone="Demo Zone 3",
                    ward="Demo Ward 11",
                    routing_rule_version="demo-v1",
                    routing_source="fictional_demo_config",
                    created_at="2026-08-29",
                    latitude=28.6400,
                    longitude=77.3400,
                    location_precision="locality_approximate",
                    location_note="near the pole outside our lane",
                )
                stored = db.run_query(
                    "SELECT location_precision, location_note FROM complaints "
                    "WHERE complaint_id = ?",
                    (complaint_id,),
                )

        self.assertEqual(stored[0], "locality_approximate")
        self.assertEqual(stored[1], "near the pole outside our lane")

    def test_an_exact_complaint_still_records_exact_precision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "civic.db") as db:
                precision = db.run_query(
                    "SELECT location_precision FROM complaints "
                    "WHERE complaint_id = 'CIV1001'"
                )
        self.assertEqual(precision[0], "exact_landmark")


class FakeMemory:
    def __init__(self, **values):
        self.values = dict(values)

    def set(self, field, value):
        self.values[field] = value

    def get(self, field, default=None):
        return self.values.get(field, default)


class FakeContext:
    """Minimal stand-in for ToolContext so tools can be driven directly."""

    def __init__(self, **values):
        self.memory = FakeMemory(**values)


def confirmed_area(category, **overrides):
    values = {
        "category": category,
        "broad_location_confirmed": "True",
        "broad_latitude": 28.6496701,
        "broad_longitude": 77.339575,
        "broad_bounding_box": [28.62, 28.67, 77.31, 77.36],
        "pincode": "201001",
        "locality": "Vaishali",
        "city": "Ghaziabad",
        "exact_attempts": 0,
        "location_asks": 0,
    }
    values.update(overrides)
    return FakeContext(**values)


class LocateIncidentTests(unittest.TestCase):
    """The location flow, driven exactly as the agent drives it."""

    def call(self, context, spoken="", cleaned=""):
        from tools.civic import locate_incident

        return asyncio.run(
            locate_incident(
                spoken_location=spoken, cleaned_query=cleaned, context=context
            )
        ).llm_response

    def test_area_level_problems_settle_without_any_questions(self) -> None:
        # Stray dogs move. Asking which gate they are near wastes the call.
        context = confirmed_area("stray_animals")
        result = self.call(context, spoken="dogs around our lane")

        self.assertEqual(result["status"], "settled_approximate")
        self.assertEqual(
            context.memory.get("location_precision"), "locality_approximate"
        )
        self.assertTrue(context.memory.get("location_settled"))

    def test_unmappable_phrase_is_questioned_before_it_is_accepted(self) -> None:
        context = confirmed_area("streetlight")
        result = self.call(context, spoken="outside our lane")

        self.assertEqual(result["status"], "ask_once")
        self.assertIn("shop", result["ask_about"])
        self.assertIsNone(context.memory.get("location_settled"))

    def test_repeated_unmappable_answers_still_end_in_a_filed_location(self) -> None:
        # The anti-dead-end guarantee. A caller who genuinely cannot name a
        # landmark must not be trapped in a loop.
        context = confirmed_area("streetlight")
        seen = []
        for _ in range(8):
            result = self.call(context, spoken="just the pole outside our lane")
            seen.append(result["status"])
            if result["status"] == "settled_approximate":
                break

        self.assertIn("settled_approximate", seen, f"never settled: {seen}")
        self.assertEqual(
            context.memory.get("location_precision"), "locality_approximate"
        )
        self.assertIn(
            "just the pole outside our lane", context.memory.get("location_note")
        )

    def test_the_useful_description_survives_a_later_unhelpful_answer(self) -> None:
        # "there is nothing nearby" is an answer to our question, not a
        # description of the spot. It must not replace the real one.
        context = confirmed_area("streetlight")
        self.call(context, spoken="near the pole outside our lane")
        self.call(context, spoken="there is nothing nearby, no shop at all")

        note = context.memory.get("location_note")
        self.assertIn("near the pole outside our lane", note)

    def test_a_streetlight_is_asked_more_often_than_garbage(self) -> None:
        def questions_before_settling(category):
            context = confirmed_area(category)
            for turn in range(1, 10):
                if self.call(context, spoken="outside our lane")["status"] == (
                    "settled_approximate"
                ):
                    return turn
            return 99

        self.assertGreater(
            questions_before_settling("streetlight"),
            questions_before_settling("garbage"),
        )

    def test_nothing_is_located_before_the_area_is_confirmed(self) -> None:
        context = FakeContext(category="pothole")
        result = self.call(context, spoken="near the market", cleaned="market")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "need_area_first")

    def test_a_map_match_is_offered_as_a_choice_never_auto_picked(self) -> None:
        context = confirmed_area("pothole")
        match = [
            {
                "lat": "28.6496701",
                "lon": "77.339575",
                "postcode": "201001",
                "area": "Vaishali",
                "city": "Ghaziabad",
                "label": "Vaishali Metro Station, Vaishali, Ghaziabad",
                "place_id": 1,
                "osm_type": "node",
                "osm_id": 7,
            }
        ]
        with patch("tools.civic.geocode_lookup", return_value=match):
            result = self.call(
                context, spoken="near vaishali metro", cleaned="Vaishali Metro Station"
            )

        self.assertEqual(result["status"], "choose")
        self.assertEqual(result["option_count"], 1)
        self.assertIsNone(context.memory.get("location_settled"))

    def test_an_unreachable_map_does_not_stop_the_complaint(self) -> None:
        context = confirmed_area("pothole")
        with patch(
            "tools.civic.geocode_lookup", side_effect=geocode.GeocodeUnavailable("down")
        ):
            result = self.call(
                context, spoken="near the big gate", cleaned="big gate"
            )

        self.assertEqual(result["status"], "settled_approximate")
        self.assertTrue(result.get("geocoder_unavailable"))
        self.assertTrue(context.memory.get("location_settled"))

    def test_a_result_outside_the_confirmed_pincode_is_discarded(self) -> None:
        # The failure that sent a Ghaziabad complaint to Vijayawada.
        context = confirmed_area("streetlight")
        far_away = [
            {
                "lat": "16.5",
                "lon": "80.6",
                "postcode": "520001",
                "area": "Patamata Lanka",
                "city": "Vijayawada",
                "label": "Electric Pole, Patamata Lanka, Vijayawada",
                "place_id": 2,
                "osm_type": "node",
                "osm_id": 9,
            }
        ]
        with patch("tools.civic.geocode_lookup", return_value=far_away):
            result = self.call(context, spoken="the pole", cleaned="electric pole")

        self.assertNotEqual(result.get("status"), "choose")
        self.assertIsNone(context.memory.get("latitude"))


if __name__ == "__main__":
    unittest.main()
