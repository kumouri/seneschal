#!/usr/bin/env python3
"""Tests for Proteus's pipeline tools (archons/proteus/tools) — normalizers + scoring.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python, and this file
runs green under ``python -m unittest``). Pure-function tests — no network, no files beyond tmp.

Run:  python -m unittest seneschal.scripts.test_proteus_tools   (or)   python test_proteus_tools.py
"""

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "archons", "proteus", "tools"))

import fetch_jobs  # noqa: E402
import score_jobs  # noqa: E402

PROFILE = {
    "min_total_comp": 140000,
    "target_total_comp": 185000,
    "remote": "preferred",
    "remote_regions_ok": ["USA", "Worldwide"],
    "locations_ok": ["Remote", "Chicago"],
    "title_keywords_strong": ["java", "backend", "agent"],
    "title_keywords_avoid": ["junior", "frontend"],
    "seniority": ["senior", "staff"],
    "skills": {"java": 10, "spring": 8, "llm": 6},
    "preferences": {"ai agent": 8, "banking": -3},
    "dealbreakers": ["security clearance"],
}


class DealbreakerPatterns(unittest.TestCase):
    """Regressions from hunt #1 (2026-07-12): requirement-class dealbreakers + negation guard."""

    def test_citizenship_classified_language_flags(self):
        # The exact Databricks Public Sector phrasing the literal matcher missed.
        job = {"title": "Delivery Solutions Architect - Public Sector",
               "description": "Due to federal contract requirements and client site access "
                              "obligations, U.S. citizenship is required to access classified information.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertTrue(any(f.startswith("dealbreaker") for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 33.0)  # capped at 25 + possible pref boosts

    def test_negated_clearance_does_not_flag(self):
        job = {"title": "Senior Java Backend Engineer",
               "description": "Java and Spring services. No security clearance required for this role.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertEqual(scored["flags"], [])


class LocationTextCrossCheck(unittest.TestCase):
    """Regressions from hunt #1 (2026-07-12): posting text outranks the structured remote flag."""

    PROFILE = dict(PROFILE, max_onsite_days_per_week=2, relocation="no")

    def test_weekday_enumeration_overrides_remote_flag(self):
        # The Replit shape: board says remote:true, posting text says Mon/Wed/Fri in-office.
        job = {"title": "Staff Software Engineer, Agent Platform",
               "description": "This is a full-time role that can be held from our Foster City, CA "
                              "office. The hybrid role has an in-office requirement of Monday, "
                              "Wednesday, and Friday.",
               "location": "Foster City, CA", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertTrue(any("days/week onsite" in f for f in scored["flags"]))
        self.assertTrue(any("remote flag contradicted" in f for f in scored["flags"]))
        self.assertLessEqual(scored["score_breakdown"]["location"], 3.0)

    def test_numeric_days_per_week_flags(self):
        job = {"title": "Backend Engineer",
               "description": "We collaborate in office 3 days per week at our NYC headquarters.",
               "location": "New York, NY", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertTrue(any("days/week onsite" in f for f in scored["flags"]))

    def test_two_day_hybrid_within_max_does_not_flag(self):
        job = {"title": "Backend Engineer",
               "description": "Hybrid schedule: 2 days per week in office, the rest remote.",
               "location": "Springfield, MO", "remote": False}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertFalse(any("onsite" in f for f in scored["flags"]))

    def test_relocation_required_flags(self):
        job = {"title": "Platform Engineer",
               "description": "Candidates must relocate to Austin, TX within 90 days of hire.",
               "location": "Austin, TX", "remote": False}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertTrue(any("relocation required" in f for f in scored["flags"]))


class RemoteVerdicts(unittest.TestCase):
    """The relocation rule: relocation is structural, not description-text-dependent."""

    PROFILE = dict(PROFILE, max_onsite_days_per_week=2, relocation="no",
                   max_commute_minutes_one_way=90,
                   commutable_locations=["springfield", "shelbyville", "capital city"])

    def test_nyc_with_no_remote_signal_is_relocation_dealbreaker(self):
        job = {"title": "Senior Java Backend Engineer",
               "description": "Java and Spring. You will work from our office.",
               "location": "New York, NY", "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertTrue(any("relocation implied" in f for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 33.0)

    def test_nyc_hq_with_remote_mention_is_verify_not_dealbreaker(self):
        job = {"title": "Senior Software Engineer",
               "description": "This role is remote-eligible within the US.",
               "location": "New York, NY (HQ)", "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "remote?")
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))

    def test_local_office_is_commutable_not_relocation(self):
        job = {"title": "Backend Engineer", "description": "Java services.",
               "location": "Springfield, MO", "remote": False}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "commutable")
        self.assertFalse(any("relocation implied" in f for f in scored["flags"]))

    def test_structured_remote_is_true_remote(self):
        job = {"title": "Backend Engineer", "description": "Fully distributed team.",
               "location": "Remote, US", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "remote")

    def test_contradicted_remote_flag_at_noncommutable_office_is_relocation(self):
        # The Replit shape: remote:true but Mon/Wed/Fri in Foster City.
        job = {"title": "Staff Software Engineer",
               "description": "The hybrid role has an in-office requirement of Monday, Wednesday, "
                              "and Friday at our Foster City, CA office.",
               "location": "Foster City, CA", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertTrue(any("relocation implied" in f for f in scored["flags"]))


class ExperienceGaps(unittest.TestCase):
    """Experience-gap rule: a posting requiring years the owner does not have must flag."""

    PROFILE = dict(PROFILE, experience_years={
        "java": {"years": 10, "keywords": ["java"]},
        "project management": {"years": 0.9,
                               "keywords": ["project management", "project manager"]},
        "ai/agents": {"years": 2, "keywords": ["ai engineering", "llm"]},
    })

    def test_severe_pm_gap_is_dealbreaker(self):
        job = {"title": "Principal Product Manager, Agentic AI Platform",
               "description": "Requires 8-10 years of project management experience leading "
                              "cross-functional programs. Java and LLM exposure a plus.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertTrue(any(f.startswith("dealbreaker: requirement gap") for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 33.0)

    def test_years_she_clears_do_not_flag(self):
        job = {"title": "Senior Java Backend Engineer",
               "description": "5+ years of Java experience required.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertFalse(any("requirement gap" in f for f in scored["flags"]))

    def test_mild_gap_notes_without_dealbreaking(self):
        job = {"title": "AI Engineer",
               "description": "3 years of hands-on LLM and ai engineering experience.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertTrue(any(f.startswith("requirement gap (soft)") for f in scored["flags"]))
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))

    def test_unrelated_numbers_do_not_flag(self):
        job = {"title": "Backend Engineer",
               "description": "401k match, 16 weeks parental leave, 20 vacation days. Java shop.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertFalse(any("requirement gap" in f for f in scored["flags"]))

    def test_cleared_category_in_same_window_downgrades_to_soft(self):
        # "8+ yrs java ... with LLM" must NOT dealbreak on the 2-yr ai/agents tenure — the
        # requirement is really about the discipline the profile clears.
        job = {"title": "Senior AI Platform Engineer",
               "description": "8+ years of java experience building production systems, "
                              "ideally with LLM and ai engineering exposure.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertFalse(any(f.startswith("dealbreaker: requirement gap") for f in scored["flags"]))


class HtmlToText(unittest.TestCase):
    def test_strips_tags_and_collapses_whitespace(self):
        text = fetch_jobs.html_to_text("<p>Hello&nbsp; <b>world</b></p><li>item</li>")
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertIn("item", text)
        self.assertNotIn("<", text)

    def test_empty_input(self):
        self.assertEqual(fetch_jobs.html_to_text(""), "")


class Normalizers(unittest.TestCase):
    def test_greenhouse(self):
        payload = {"jobs": [{"id": 42, "title": "Senior Java Engineer",
                             "location": {"name": "Remote - US"},
                             "absolute_url": "https://x/42", "updated_at": "2026-07-10T00:00:00Z",
                             "content": "<p>Java &amp; Spring</p>"}]}
        (job,) = fetch_jobs.normalize_greenhouse("acme", payload)
        self.assertEqual(job["source"], "greenhouse")
        self.assertEqual(job["company"], "acme")
        self.assertEqual(job["external_id"], "42")
        self.assertEqual(job["location"], "Remote - US")
        self.assertIn("Java & Spring", job["description"])

    def test_lever_remote_and_salary(self):
        payload = [{"id": "abc", "text": "Backend Engineer",
                    "categories": {"location": "Remote", "commitment": "Full-time"},
                    "workplaceType": "remote", "hostedUrl": "https://x/abc",
                    "createdAt": 1751500800000,
                    "salaryRange": {"min": 150000, "max": 190000, "currency": "USD"},
                    "descriptionPlain": "Build services."}]
        (job,) = fetch_jobs.normalize_lever("acme", payload)
        self.assertTrue(job["remote"])
        self.assertEqual(job["comp_max"], 190000)
        self.assertEqual(job["employment_type"], "Full-time")

    def test_remotive_is_always_remote(self):
        payload = {"jobs": [{"id": 7, "title": "Java Dev", "company_name": "Acme",
                             "url": "https://x/7", "candidate_required_location": "USA",
                             "publication_date": "2026-07-11T08:00:00", "description": "<p>x</p>"}]}
        (job,) = fetch_jobs.normalize_remotive(payload)
        self.assertTrue(job["remote"])
        self.assertEqual(job["company"], "Acme")

    def test_smartrecruiters_with_and_without_detail(self):
        payload = {"content": [
            {"id": "744001", "name": "Staff Software Engineer, AI Platform",
             "company": {"name": "ServiceNow"},
             "location": {"city": "Santa Clara", "region": "CA", "country": "us", "remote": True},
             "releasedDate": "2026-07-01T00:00:00.000Z",
             "typeOfEmployment": {"label": "Full-time"}},
            {"id": "744002", "name": "Robotics Software Engineer", "company": {"name": "BoschGroup"},
             "location": {"city": "Pittsburgh", "country": "us"}},
        ]}
        details = {"744001": {"jobAd": {"sections": {
            "jobDescription": {"title": "Description", "text": "<p>Java, Spring, LLM agents.</p>"}}}}}
        jobs = fetch_jobs.normalize_smartrecruiters("ServiceNow", payload, details)
        self.assertEqual(len(jobs), 2)
        self.assertIn("Java, Spring, LLM agents.", jobs[0]["description"])
        self.assertTrue(jobs[0]["remote"])
        self.assertIn("jobs.smartrecruiters.com/ServiceNow/744001", jobs[0]["url"])
        self.assertEqual(jobs[1]["description"], "")  # past detail_cap → list data only

    def test_remoteok_skips_legal_notice_row(self):
        payload = [{"legal": "blah blah"},
                   {"id": 99, "position": "Senior AI Engineer", "company": "Acme",
                    "location": "Worldwide", "epoch": 1752300000,
                    "salary_min": 200000, "salary_max": 260000,
                    "description": "<p>Agents &amp; LLMs</p>", "tags": ["ai", "java"],
                    "url": "https://remoteok.com/remote-jobs/99"}]
        jobs = fetch_jobs.normalize_remoteok(payload)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["remote"])
        self.assertEqual(jobs[0]["comp_max"], 260000)
        self.assertIn("Agents & LLMs", jobs[0]["description"])

    def test_hn_hiring_top_level_only_and_company_split(self):
        comments = [
            {"objectID": "1001", "parent_id": 500, "story_id": 500, "author": "acmebot",
             "created_at": "2026-07-01T12:00:00.000Z",
             "comment_text": "Acme Robotics | Senior Software Engineer | REMOTE (US) | $210k<p>We build arms.</p>"},
            {"objectID": "1002", "parent_id": 1001, "story_id": 500, "author": "replyguy",
             "comment_text": "Is this still open?"},
        ]
        jobs = fetch_jobs.normalize_hn_hiring(comments, "500")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "Acme Robotics")
        self.assertTrue(jobs[0]["remote"])
        self.assertIn("news.ycombinator.com/item?id=1001", jobs[0]["url"])
        self.assertIn("We build arms.", jobs[0]["description"])

    def test_workday_with_and_without_detail(self):
        base = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
        payload = {"jobPostings": [
            {"title": "Senior AI Software Engineer", "externalPath": "/job/US-CA/Sr-AI-SWE_JR123",
             "locationsText": "US, CA, Santa Clara", "postedOn": "Posted 3 Days Ago"},
            {"title": "EDA Tooling Engineer", "externalPath": "/job/US-TX/EDA_JR456",
             "locationsText": "US, TX, Austin", "postedOn": "Posted 30+ Days Ago"},
        ]}
        details = {"/job/US-CA/Sr-AI-SWE_JR123": {"jobPostingInfo": {
            "jobReqId": "JR123", "location": "Santa Clara, CA", "remoteType": "Remote eligible",
            "timeType": "Full time", "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/x/JR123",
            "jobDescription": "<p>Java, Python, LLM agents, chip design.</p>",
            "postedOn": "Posted 3 Days Ago"}}}
        jobs = fetch_jobs.normalize_workday("nvidia", base, payload, details)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(jobs[0]["remote"])
        self.assertEqual(jobs[0]["external_id"], "JR123")
        self.assertIn("chip design", jobs[0]["description"])
        self.assertRegex(jobs[0]["posted_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(jobs[1]["description"], "")  # past detail_cap
        self.assertIn("myworkdayjobs.com", jobs[1]["url"])  # human-URL fallback built from base

    def test_workday_posted_parser(self):
        self.assertRegex(fetch_jobs._workday_posted_to_iso("Posted Today"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(fetch_jobs._workday_posted_to_iso("Posted Yesterday"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(fetch_jobs._workday_posted_to_iso("Posted 12 Days Ago"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsNone(fetch_jobs._workday_posted_to_iso(""))
        self.assertIsNone(fetch_jobs._workday_posted_to_iso("whenever"))

    def test_dedupe_by_url(self):
        jobs = [fetch_jobs._job(source="a", external_id="1", url="https://x/1"),
                fetch_jobs._job(source="b", external_id="2", url="https://x/1"),
                fetch_jobs._job(source="c", external_id="3", url="https://x/3")]
        self.assertEqual(len(fetch_jobs.dedupe(jobs)), 2)


class WordHit(unittest.TestCase):
    def test_word_boundaries(self):
        self.assertTrue(score_jobs._word_hit("senior java engineer", "java"))
        self.assertFalse(score_jobs._word_hit("javascript engineer", "java"))
        self.assertTrue(score_jobs._word_hit("we use spring boot daily", "spring boot"))


class CompScoring(unittest.TestCase):
    def test_at_target_gets_full_band(self):
        points, _ = score_jobs.score_comp({"comp_max": 200000}, PROFILE)
        self.assertEqual(points, 15.0)

    def test_unknown_comp_is_neutral(self):
        points, notes = score_jobs.score_comp({}, PROFILE)
        self.assertEqual(points, 8.0)
        self.assertIn("unknown", notes[0])

    def test_hourly_rate_is_annualized(self):
        self.assertEqual(score_jobs._annual(100), 208000)  # $100/hr ≈ $208k
        self.assertEqual(score_jobs._annual(12000), 144000)  # monthly → ×12

    def test_below_floor_scores_low(self):
        points, _ = score_jobs.score_comp({"comp_max": 90000}, PROFILE)
        self.assertLess(points, 8.0)


class JobScoring(unittest.TestCase):
    def _good_job(self):
        return {
            "title": "Senior Java Backend Engineer",
            "description": "Java, Spring, LLM agents. Build AI agent infrastructure.",
            "location": "Remote", "remote": True, "comp_max": 190000,
            "posted_at": "2099-01-01",  # future-proof: always counts as fresh
        }

    def test_good_match_scores_high_and_beats_bad_match(self):
        good = score_jobs.score_job(self._good_job(), PROFILE)
        bad = score_jobs.score_job(
            {"title": "Junior Frontend Developer", "description": "React and CSS.",
             "location": "Berlin, Germany", "remote": False}, PROFILE)
        self.assertGreater(good["match_percent"], 75)
        self.assertLess(bad["match_percent"], 35)
        self.assertGreater(good["match_percent"], bad["match_percent"])

    def test_dealbreaker_caps_score(self):
        job = self._good_job()
        job["description"] += " Requires active security clearance."
        scored = score_jobs.score_job(job, PROFILE)
        self.assertLessEqual(scored["match_percent"], 25.0 + 8.0)  # cap + possible pref boosts
        self.assertTrue(any("dealbreaker" in flag for flag in scored["flags"]))

    def test_preference_boost_applies(self):
        scored = score_jobs.score_job(self._good_job(), PROFILE)
        self.assertTrue(any("ai agent" in note for note in scored["score_notes"]))

    def test_clean_remote_job_has_no_flags(self):
        scored = score_jobs.score_job(self._good_job(), PROFILE)
        self.assertEqual(scored["flags"], [])

    def test_score_bounded_0_100(self):
        scored = score_jobs.score_job(self._good_job(), PROFILE)
        self.assertLessEqual(scored["match_percent"], 100.0)
        self.assertGreaterEqual(scored["match_percent"], 0.0)


if __name__ == "__main__":
    unittest.main()
