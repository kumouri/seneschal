#!/usr/bin/env python3
"""Tests for the Samsung Health pipeline — chiefly the timezone contract, which is the whole ballgame.

Samsung stores ``start_time``/``end_time`` in UTC and the local offset separately. Reading those strings
as wall clock shifts every conclusion by 5-6 hours — exactly America/Chicago's offset — which is almost
certainly how an earlier analysis concluded Samsung was under-detecting sleep onset by "~5.5h median".
The DST tests below are the evidence that settles it, expressed as code so a future change can't quietly
re-introduce the bug.

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_health   (or)  python test_health.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import health_common as hc  # noqa: E402
import health_dashboard as hd  # noqa: E402
import health_import as hi  # noqa: E402


def _ms(y, mo, d, h, mi):
    """UTC epoch-ms for a wall-clock UTC time — for building synthetic live-feed records."""
    from datetime import timezone
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


class TestTimezoneContract(unittest.TestCase):
    def test_parse_offset(self):
        self.assertEqual(hc.parse_offset("UTC-0600"), -360)
        self.assertEqual(hc.parse_offset("UTC-0500"), -300)
        self.assertEqual(hc.parse_offset("UTC+0530"), 330)
        self.assertEqual(hc.parse_offset("UTC+0000"), 0)

    def test_parse_offset_is_forgiving(self):
        for junk in ("", None, "garbage", "UTC-XXXX"):
            self.assertEqual(hc.parse_offset(junk), 0)

    def test_stored_times_are_utc_and_offset_recovers_local(self):
        """A CST sleep at 05:37 local is stored as 11:37 UTC with UTC-0600."""
        utc = hc.parse_ts("2025-01-24 11:37:00.000")
        local = hc.to_local(utc, hc.parse_offset("UTC-0600"))
        self.assertEqual(local, datetime(2025, 1, 24, 5, 37))

    def test_summer_offset_differs_from_winter(self):
        """The same wall-clock hour maps to a different UTC instant across DST — the offset column is
        the only thing that knows, which is why we must never assume a fixed -6."""
        winter = hc.to_local(hc.parse_ts("2025-01-24 12:00:00.000"), hc.parse_offset("UTC-0600"))
        summer = hc.to_local(hc.parse_ts("2025-07-24 12:00:00.000"), hc.parse_offset("UTC-0500"))
        self.assertEqual(winter.hour, 6)
        self.assertEqual(summer.hour, 7)

    def test_parse_ts_handles_missing_millis_and_junk(self):
        self.assertEqual(hc.parse_ts("2025-01-24 11:37:00"), datetime(2025, 1, 24, 11, 37))
        self.assertIsNone(hc.parse_ts(""))
        self.assertIsNone(hc.parse_ts("not a time"))

    def test_day_time_is_already_local_midnight(self):
        self.assertEqual(hc.day_time_to_date("2025-01-23 00:00:00.000"), date(2025, 1, 23))


class TestCentralOffset(unittest.TestCase):
    """central_offset() is a deprecated shim over tz_common.us_central_offset_minutes now (the
    importer's no-offset fallback is tz_common.offset_minutes, the owner's zone). These pins keep
    the shim honest at both DST transitions — the IANA path (tzdata present) and the retained
    hand-rolled rule (bare interpreter) must both produce them."""

    def test_winter_is_cst(self):
        self.assertEqual(hc.central_offset(datetime(2025, 1, 24, 12)), -360)

    def test_summer_is_cdt(self):
        self.assertEqual(hc.central_offset(datetime(2025, 7, 24, 12)), -300)

    def test_spring_forward_boundary(self):
        # DST begins 02:00 local on the 2nd Sunday of March 2026 = Mar 8 = 08:00 UTC.
        self.assertEqual(hc.central_offset(datetime(2026, 3, 8, 7, 59)), -360)
        self.assertEqual(hc.central_offset(datetime(2026, 3, 8, 8, 0)), -300)

    def test_fall_back_boundary(self):
        # DST ends 02:00 local on the 1st Sunday of November 2025 = Nov 2. 02:00 there is CDT
        # (UTC−5) → the instant is 07:00 UTC. The old per-script formula said 08:00 — one hour
        # wrong on every fall-back day; the tz_common consolidation fixed it to match IANA.
        self.assertEqual(hc.central_offset(datetime(2025, 11, 2, 6, 59)), -300)
        self.assertEqual(hc.central_offset(datetime(2025, 11, 2, 7, 0)), -360)

    def test_epoch_ms_round_trips(self):
        self.assertEqual(hc.epoch_ms_to_utc(1747209540000), datetime(2025, 5, 14, 7, 59))


class TestSleepDay(unittest.TestCase):
    def test_night_runs_19_to_19(self):
        """An evening nap, a post-midnight main block and a late-morning fragment are ONE night. A
        midnight cut would scatter them across two, and a noon cut would bisect the morning fragment."""
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 6, 21, 33)), date(2026, 7, 6))
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 7, 3, 12)), date(2026, 7, 6))
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 7, 11, 0)), date(2026, 7, 6))

    def test_the_cut_itself_opens_a_new_night(self):
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 7, 18, 59)), date(2026, 7, 6))
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 7, 19, 0)), date(2026, 7, 7))

    def test_cut_hour_is_adjustable(self):
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 7, 6, 0), cut_hour=12), date(2026, 7, 6))
        self.assertEqual(hc.sleep_day(datetime(2026, 7, 7, 6, 0), cut_hour=0), date(2026, 7, 7))

    def test_night_start_is_the_row_edge(self):
        self.assertEqual(hc.night_start(date(2026, 7, 6)), datetime(2026, 7, 6, 19, 0))


class TestSplitAtCut(unittest.TestCase):
    def test_span_within_one_night_is_untouched(self):
        pieces = list(hd._split_at_cut(datetime(2026, 7, 7, 3, 12), datetime(2026, 7, 7, 7, 22)))
        self.assertEqual(pieces, [("2026-07-06", datetime(2026, 7, 7, 3, 12),
                                   datetime(2026, 7, 7, 7, 22))])

    def test_span_crossing_the_cut_is_split_and_conserved(self):
        a, b = datetime(2026, 7, 6, 18, 40), datetime(2026, 7, 6, 20, 10)
        pieces = list(hd._split_at_cut(a, b))
        self.assertEqual([p[0] for p in pieces], ["2026-07-05", "2026-07-06"])
        total = sum((e - s).total_seconds() for _, s, e in pieces)
        self.assertEqual(total, (b - a).total_seconds())  # no minute lost, none double-counted

    def test_multi_day_span_is_fully_tiled(self):
        a, b = datetime(2026, 7, 5, 12, 0), datetime(2026, 7, 7, 12, 0)
        pieces = list(hd._split_at_cut(a, b))
        self.assertEqual(sum((e - s).total_seconds() for _, s, e in pieces), (b - a).total_seconds())
        for (_, _, e1), (_, s2, _) in zip(pieces, pieces[1:]):
            self.assertEqual(e1, s2)  # contiguous, no gaps


class TestFormatting(unittest.TestCase):
    def test_dur(self):
        self.assertEqual(hd.dur(0), "0h 00m")
        self.assertEqual(hd.dur(409), "6h 49m")
        self.assertEqual(hd.dur(None), "—")

    def test_hhmm_wraps(self):
        self.assertEqual(hd.hhmm(1500), "01:00")
        self.assertEqual(hd.hhmm(None), "—")

    def test_median(self):
        self.assertEqual(hd.median([3, 1, 2]), 2)
        self.assertEqual(hd.median([4, 1, 2, 3]), 2.5)
        self.assertIsNone(hd.median([None, None]))


class TestEpisodes(unittest.TestCase):
    @staticmethod
    def _s(start, end):
        return {"start_local": start, "end_local": end}

    def test_near_sessions_merge_into_one_interrupted_sleep(self):
        eps = hd.episodes([self._s("2026-07-05T05:46:00", "2026-07-05T07:47:00"),
                           self._s("2026-07-05T08:08:00", "2026-07-05T11:00:00")])  # 21 min apart
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0][1], datetime(2026, 7, 5, 11, 0))

    def test_distant_sessions_stay_separate(self):
        eps = hd.episodes([self._s("2026-07-04T21:33:00", "2026-07-04T22:34:00"),
                           self._s("2026-07-05T05:46:00", "2026-07-05T07:47:00")])
        self.assertEqual(len(eps), 2)


class TestNdjsonLiveFeed(unittest.TestCase):
    """The Health Connect → desktop feed lands in the same tables as the zip importer."""

    def _db(self):
        self.tmp = tempfile.mkdtemp()
        return hc.connect(os.path.join(self.tmp, "live.db"))

    def test_sleep_hr_spo2_steps_import(self):
        conn = self._db()
        lines = [
            json.dumps({"t": "sleep_session", "uuid": "s1", "start_ms": _ms(2026, 7, 1, 6, 0),
                        "end_ms": _ms(2026, 7, 1, 11, 0), "offset_min": -300, "score": 72,
                        "stages": [["deep", _ms(2026, 7, 1, 7, 0), _ms(2026, 7, 1, 8, 0)]]}),
            json.dumps({"t": "hr", "uuid": "h1", "start_ms": _ms(2026, 7, 1, 7, 0),
                        "offset_min": -300, "bpm": 48}),
            json.dumps({"t": "spo2", "uuid": "o1", "start_ms": _ms(2026, 7, 1, 7, 0),
                        "offset_min": -300, "pct": 96}),
            json.dumps({"t": "steps", "date": "2026-07-01", "count": 8342}),
        ]
        counts = hi.import_ndjson(conn, lines)
        self.assertEqual(counts["sleep_session"], 1)
        row = conn.execute("SELECT start_local, end_local, sleep_score FROM sleep_session").fetchone()
        self.assertEqual(row["start_local"], "2026-07-01T01:00:00")   # 06:00 UTC - 5h
        self.assertEqual(row["end_local"], "2026-07-01T06:00:00")
        self.assertEqual(row["sleep_score"], 72)
        self.assertEqual(conn.execute("SELECT stage FROM sleep_stage").fetchone()["stage"], "deep")
        self.assertEqual(conn.execute("SELECT hr FROM hr_minute").fetchone()["hr"], 48)
        self.assertEqual(conn.execute("SELECT count FROM steps_daily").fetchone()["count"], 8342)
        conn.close()

    def test_bad_and_unknown_lines_are_counted_not_fatal(self):
        conn = self._db()
        counts = hi.import_ndjson(conn, ["{ not json", json.dumps({"t": "meteor_shower"}), ""])
        self.assertEqual(counts.get("bad"), 1)
        self.assertEqual(counts.get("skipped"), 1)
        conn.close()

    def test_reimport_is_idempotent(self):
        conn = self._db()
        line = [json.dumps({"t": "sleep_session", "uuid": "s1", "start_ms": _ms(2026, 7, 1, 6, 0),
                            "end_ms": _ms(2026, 7, 1, 11, 0), "offset_min": -300, "stages": []})]
        hi.import_ndjson(conn, line)
        hi.import_ndjson(conn, line)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sleep_session").fetchone()[0], 1)
        conn.close()


class TestMissedSleep(unittest.TestCase):
    """The green 'Samsung missed this' layer: still + at sleeping HR + no session, in a long-enough run."""

    def _data(self, move, hr, sessions):
        return {"move_slots": {"2026-07-01": move}, "hr_slots": {"2026-07-01": hr},
                "sessions": {"2026-07-01": sessions}}

    def test_still_and_low_hr_with_no_session_is_flagged(self):
        move = [0.2] * hd.SLOTS
        hr = [50.0] * hd.SLOTS
        got = hd.missed_slots("2026-07-01", self._data(move, hr, []), set())
        self.assertEqual(len(got), hd.SLOTS)  # the whole night qualifies

    def test_still_but_high_hr_is_not_sleep(self):
        # Sitting at the computer: still, but heart rate up. Must NOT be flagged.
        move = [0.2] * hd.SLOTS
        hr = [78.0] * hd.SLOTS
        self.assertEqual(hd.missed_slots("2026-07-01", self._data(move, hr, []), set()), [])

    def test_slots_covered_by_a_session_are_excluded(self):
        move = [0.2] * hd.SLOTS
        hr = [50.0] * hd.SLOTS
        covered = set(range(hd.SLOTS))
        self.assertEqual(hd.missed_slots("2026-07-01", self._data(move, hr, []), covered), [])

    def test_short_quiet_blip_below_min_run_is_ignored(self):
        move = [5.0] * hd.SLOTS
        hr = [78.0] * hd.SLOTS
        # a single quiet slot (3 min << MISSED_MIN_RUN) shouldn't count
        move[100] = 0.1
        hr[100] = 50.0
        self.assertEqual(hd.missed_slots("2026-07-01", self._data(move, hr, []), set()), [])


class TestSchema(unittest.TestCase):
    def test_connect_creates_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = hc.connect(os.path.join(tmp, "health.db"))
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()  # Windows won't remove the tempdir while the db handle is open
            self.assertLessEqual({"exports", "sleep_session", "sleep_stage", "heart_rate",
                                  "stress", "spo2", "skin_temp", "steps_daily",
                                  "movement", "hr_minute"}, names)

    def test_export_id_and_rejection(self):
        self.assertEqual(hc.export_id("samsunghealth_export_20260707152802.zip"),
                         "20260707152802")
        with self.assertRaises(hc.SamsungExportError):
            hc.export_id("holiday-photos.zip")


if __name__ == "__main__":
    unittest.main()
