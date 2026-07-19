#!/usr/bin/env python3
"""Tests for cockpit/server/health.py — the v4 health/workout/meal read layer over `state/health.db`
+ `state/meals.json` (cockpit-spec.md "Health pipeline extension (v4)"). Pure stdlib, no FastAPI
needed, so this runs unconditionally (mirrors test_governor.py/test_model_config.py).

Fixtures build `health.db` via `seneschal/scripts/health_common.connect()` — the SAME schema
`health_import.py` writes — so these tests exercise the real column shapes, not a hand-rolled guess.

Run: python -m unittest cockpit.server.test_health
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import health  # noqa: E402


def _load_health_common():
    """Load seneschal/scripts/health_common.py by explicit file path (importlib, not sys.path) — that
    directory is full of generically-named test_*.py files (test_health.py included) that would
    collide with unittest's bare-module discovery if seneschal/scripts were ever added to sys.path.
    health_common imports its siblings at module top (tz_common → identity_common), so the scripts
    dir joins sys.path ONLY for the duration of this exec, and the transiently imported siblings are
    dropped from sys.modules afterward — discovery never sees either, and the cockpit modules' own
    guarded `import tz_common` stays deterministic."""
    scripts_dir = REPO_ROOT / "seneschal" / "scripts"
    path = scripts_dir / "health_common.py"
    spec = importlib.util.spec_from_file_location("seneschal_scripts_health_common", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts_dir))
        sys.modules.pop("tz_common", None)
        sys.modules.pop("identity_common", None)
    return module


hc = _load_health_common()

# The live-feed `workouts` / `nutrition` tables are NOT part of health_common.SCHEMA in this repo
# (the feed importer that writes them hasn't been ported; the cockpit reader tolerates their absence
# in production — `available: false`). These fixtures declare the exact column shapes the reader
# queries so the grouping/rollup logic is still exercised end-to-end.
_LIVE_FEED_DDL = """
CREATE TABLE IF NOT EXISTS workouts (
    uuid TEXT PRIMARY KEY,
    start_utc TEXT,
    end_utc TEXT,
    tz_offset_min INTEGER,
    start_local TEXT,
    end_local TEXT,
    local_date TEXT,
    duration_min REAL,
    exercise_type TEXT,
    title TEXT,
    notes TEXT,
    energy_kcal REAL,
    distance_m REAL
);
CREATE TABLE IF NOT EXISTS nutrition (
    uuid TEXT PRIMARY KEY,
    start_utc TEXT,
    end_utc TEXT,
    tz_offset_min INTEGER,
    local_date TEXT,
    meal_type TEXT,
    name TEXT,
    energy_kcal REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL
);
"""


def _insert_sleep(conn, uuid, night, duration_min, efficiency=None, sleep_score=None,
                   start_local=None, end_local=None):
    conn.execute(
        "INSERT INTO sleep_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid, night, f"{night}T02:00:00", f"{night}T10:00:00", -300,
         start_local or f"{night}T02:00:00", end_local or f"{night}T10:00:00",
         duration_min, efficiency, sleep_score, None, None, None, None, None, None, None),
    )


def _insert_workout(conn, uuid, local_date, duration_min, exercise_type="running",
                     title="Run", energy_kcal=None, distance_m=None):
    conn.execute(
        "INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid, f"{local_date}T07:00:00", f"{local_date}T07:{int(duration_min):02d}:00", -300,
         f"{local_date}T07:00:00", f"{local_date}T07:{int(duration_min):02d}:00", local_date,
         duration_min, exercise_type, title, None, energy_kcal, distance_m),
    )


def _insert_nutrition(conn, uuid, local_date, meal_type, name, energy_kcal=None,
                       protein_g=None, carbs_g=None, fat_g=None, start_utc=None):
    conn.execute(
        "INSERT INTO nutrition VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uuid, start_utc or f"{local_date}T12:00:00", None, -300, local_date, meal_type, name,
         energy_kcal, protein_g, carbs_g, fat_g),
    )


class HealthDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _db(self) -> sqlite3.Connection:
        conn = hc.connect(str(self.state_dir / health.DB_FILE))
        conn.executescript(_LIVE_FEED_DDL)  # see the module-level note on these two tables
        return conn

    # --- missing / broken db, never 500-shaped ------------------------------------------------

    def test_sleep_missing_db(self):
        self.assertEqual(health.read_sleep(self.state_dir),
                          {"available": False, "nights": [], "count": 0})

    def test_workouts_missing_db(self):
        self.assertEqual(health.read_workouts(self.state_dir),
                          {"available": False, "sessions": [], "weekly": []})

    def test_nutrition_missing_db(self):
        self.assertEqual(health.read_nutrition(self.state_dir),
                          {"available": False, "days": []})

    def test_sleep_missing_table(self):
        # A db file exists but predates the schema entirely (e.g. some other sqlite file) — the
        # sleep_session table itself is missing. Must degrade like a missing db, never raise.
        conn = sqlite3.connect(str(self.state_dir / health.DB_FILE))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        self.assertEqual(health.read_sleep(self.state_dir)["available"], False)
        self.assertEqual(health.read_workouts(self.state_dir)["available"], False)
        self.assertEqual(health.read_nutrition(self.state_dir)["available"], False)

    def test_sleep_empty_tables_is_available_but_empty(self):
        self._db().close()  # schema created, zero rows
        self.assertEqual(health.read_sleep(self.state_dir),
                          {"available": True, "nights": [], "count": 0})

    # --- sleep ----------------------------------------------------------------------------------

    def test_sleep_groups_multiple_sessions_per_night(self):
        conn = self._db()
        _insert_sleep(conn, "a", "2026-07-15", 400.0, efficiency=90.0, sleep_score=85.0)
        _insert_sleep(conn, "b", "2026-07-15", 20.0, efficiency=50.0, sleep_score=40.0)  # a nap
        conn.commit()
        conn.close()

        resp = health.read_sleep(self.state_dir)
        self.assertTrue(resp["available"])
        self.assertEqual(len(resp["nights"]), 1)
        night = resp["nights"][0]
        self.assertEqual(night["night"], "2026-07-15")
        self.assertEqual(night["session_count"], 2)
        self.assertAlmostEqual(night["total_duration_min"], 420.0)
        # primary stats come from the LONGEST session, not the nap
        self.assertEqual(night["efficiency"], 90.0)
        self.assertEqual(night["sleep_score"], 85.0)

    def test_sleep_newest_night_first_and_days_limit(self):
        conn = self._db()
        for i, night in enumerate(["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13"]):
            _insert_sleep(conn, f"s{i}", night, 400.0)
        conn.commit()
        conn.close()

        resp = health.read_sleep(self.state_dir, days=2)
        self.assertEqual(resp["count"], 2)
        self.assertEqual([n["night"] for n in resp["nights"]], ["2026-07-13", "2026-07-12"])

    def test_sleep_days_clamped(self):
        conn = self._db()
        _insert_sleep(conn, "a", "2026-07-15", 400.0)
        conn.commit()
        conn.close()
        # garbage / out-of-range days values never raise — they clamp
        self.assertEqual(health.read_sleep(self.state_dir, days=0)["count"], 1)
        self.assertEqual(health.read_sleep(self.state_dir, days=None)["count"], 1)
        self.assertEqual(health.read_sleep(self.state_dir, days=99999)["count"], 1)

    # --- workouts ---------------------------------------------------------------------------------

    def test_workouts_weekly_rollup(self):
        today = health.local_today()
        conn = self._db()
        _insert_workout(conn, "w1", today.isoformat(), 30.0, energy_kcal=300.0)
        _insert_workout(conn, "w2", (today - timedelta(days=1)).isoformat(), 45.0, energy_kcal=400.0)
        conn.commit()
        conn.close()

        resp = health.read_workouts(self.state_dir, days=7)
        self.assertTrue(resp["available"])
        self.assertEqual(len(resp["sessions"]), 2)
        this_week_key = health._week_key(today)
        weekly = {w["week"]: w for w in resp["weekly"]}
        self.assertIn(this_week_key, weekly)
        self.assertEqual(weekly[this_week_key]["count"], 2)
        self.assertAlmostEqual(weekly[this_week_key]["minutes"], 75.0)
        self.assertAlmostEqual(weekly[this_week_key]["kcal"], 700.0)

    def test_workouts_outside_window_excluded(self):
        today = health.local_today()
        conn = self._db()
        _insert_workout(conn, "old", (today - timedelta(days=60)).isoformat(), 30.0)
        conn.commit()
        conn.close()
        resp = health.read_workouts(self.state_dir, days=7)
        self.assertEqual(resp["sessions"], [])

    def test_workouts_null_energy_treated_as_zero_in_rollup(self):
        today = health.local_today()
        conn = self._db()
        _insert_workout(conn, "w1", today.isoformat(), 30.0, energy_kcal=None)
        conn.commit()
        conn.close()
        resp = health.read_workouts(self.state_dir, days=7)
        wk = health._week_key(today)
        self.assertEqual(next(w for w in resp["weekly"] if w["week"] == wk)["kcal"], 0.0)

    # --- nutrition --------------------------------------------------------------------------------

    def test_nutrition_groups_by_day_with_totals_and_entries(self):
        today = health.local_today().isoformat()
        conn = self._db()
        _insert_nutrition(conn, "n1", today, "breakfast", "Oatmeal", energy_kcal=300, protein_g=10,
                           carbs_g=50, fat_g=5, start_utc=f"{today}T12:00:00")
        _insert_nutrition(conn, "n2", today, "lunch", "Salad", energy_kcal=400, protein_g=20,
                           carbs_g=30, fat_g=15, start_utc=f"{today}T18:00:00")
        conn.commit()
        conn.close()

        resp = health.read_nutrition(self.state_dir, days=1)
        self.assertTrue(resp["available"])
        self.assertEqual(len(resp["days"]), 1)
        day = resp["days"][0]
        self.assertEqual(day["date"], today)
        self.assertAlmostEqual(day["total_kcal"], 700)
        self.assertAlmostEqual(day["total_protein_g"], 30)
        self.assertEqual(len(day["entries"]), 2)

    # --- summary ----------------------------------------------------------------------------------

    def test_summary_combines_all_three_sources(self):
        today = health.local_today()
        conn = self._db()
        _insert_sleep(conn, "s1", today.isoformat(), 420.0, efficiency=88.0, sleep_score=80.0)
        _insert_workout(conn, "w1", today.isoformat(), 30.0, energy_kcal=250.0)
        _insert_nutrition(conn, "n1", today.isoformat(), "breakfast", "Eggs", energy_kcal=300,
                           protein_g=20)
        conn.commit()
        conn.close()

        resp = health.read_summary(self.state_dir)
        self.assertTrue(resp["available"])
        self.assertTrue(resp["sleep"]["available"])
        self.assertEqual(resp["sleep"]["last_night"]["night"], today.isoformat())
        self.assertTrue(resp["workouts"]["available"])
        self.assertEqual(resp["workouts"]["this_week"]["count"], 1)
        self.assertTrue(resp["nutrition"]["available"])
        self.assertEqual(resp["nutrition"]["today"]["date"], today.isoformat())

    def test_summary_all_unavailable_when_db_missing(self):
        resp = health.read_summary(self.state_dir)
        self.assertFalse(resp["available"])
        self.assertFalse(resp["sleep"]["available"])
        self.assertIsNone(resp["sleep"]["last_night"])
        self.assertFalse(resp["workouts"]["available"])
        self.assertIsNone(resp["workouts"]["this_week"])
        self.assertFalse(resp["nutrition"]["available"])
        self.assertIsNone(resp["nutrition"]["today"])


class MealsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data) -> None:
        with open(self.state_dir / health.MEALS_FILE, "w", encoding="utf-8") as fh:
            if isinstance(data, str):
                fh.write(data)
            else:
                json.dump(data, fh)

    def test_missing_file(self):
        self.assertEqual(health.read_meals(self.state_dir),
                          {"available": False, "staged_at": None, "plans": []})

    def test_corrupt_json(self):
        self._write("not json")
        self.assertEqual(health.read_meals(self.state_dir),
                          {"available": False, "staged_at": None, "plans": []})

    def test_non_dict_top_level(self):
        self._write([1, 2, 3])
        self.assertEqual(health.read_meals(self.state_dir),
                          {"available": False, "staged_at": None, "plans": []})

    def test_happy_path(self):
        self._write({
            "staged_at": "2026-07-17T22:00:00Z",
            "plans": [
                {"title": "Meal prep week", "url": "https://notion.so/abc", "summary": "protein-forward",
                 "tags": ["batch", "high-protein"]},
                {"title": "Just a title"},
            ],
        })
        resp = health.read_meals(self.state_dir)
        self.assertTrue(resp["available"])
        self.assertEqual(resp["staged_at"], "2026-07-17T22:00:00Z")
        self.assertEqual(len(resp["plans"]), 2)
        self.assertEqual(resp["plans"][0]["title"], "Meal prep week")
        self.assertEqual(resp["plans"][0]["tags"], ["batch", "high-protein"])
        self.assertEqual(resp["plans"][1]["url"], None)
        self.assertEqual(resp["plans"][1]["tags"], [])

    def test_drops_plan_entries_missing_a_title(self):
        self._write({"staged_at": None, "plans": [{"summary": "no title here"}, {"title": "Keep me"}]})
        resp = health.read_meals(self.state_dir)
        self.assertEqual([p["title"] for p in resp["plans"]], ["Keep me"])

    def test_plans_not_a_list_yields_empty(self):
        self._write({"staged_at": "x", "plans": "not-a-list"})
        resp = health.read_meals(self.state_dir)
        self.assertTrue(resp["available"])
        self.assertEqual(resp["plans"], [])


if __name__ == "__main__":
    unittest.main()
