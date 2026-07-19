#!/usr/bin/env python3
"""Tests for Proteus's pipeline tools (archons/proteus/tools) — normalizers + scoring.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python, and this file
runs green under ``python -m unittest``). Pure-function tests — no network, no files beyond tmp.

Run:  python -m unittest seneschal.scripts.test_proteus_tools   (or)   python test_proteus_tools.py
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "archons", "proteus", "tools"))

import fetch_jobs  # noqa: E402
import proteus_comp  # noqa: E402
import proteus_paths  # noqa: E402
import register_workup  # noqa: E402
import score_jobs  # noqa: E402
import source_tiers  # noqa: E402

NOW_TS = 1784000000.0  # fixed clock for retention tests — never the wall clock

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
    "dealbreakers": [],
    "us_citizen": True,
    "clearance_eligible": True,
}


class ClearanceAndCitizenship(unittest.TestCase):
    """The profile marks the owner a US citizen & clearance-ELIGIBLE. Citizenship-required = fine;
    obtainable-clearance = target; only an ACTIVE clearance the owner doesn't hold blocks a
    day-1 start."""

    def test_citizenship_required_is_fine_for_a_citizen(self):
        job = {"title": "Software Engineer - Public Sector",
               "description": "Due to federal contract requirements, U.S. citizenship is required.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))

    def test_able_to_obtain_clearance_is_a_target_not_a_blocker(self):
        job = {"title": "Software Engineer", "location": "Springfield, MO", "remote": False,
               "description": "Must be a US citizen with the ability to obtain a security clearance."}
        scored = score_jobs.score_job(job, dict(PROFILE, commutable_locations=["springfield"]))
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))
        self.assertTrue(any("obtainable" in f for f in scored["flags"]))

    def test_active_clearance_required_is_a_dealbreaker(self):
        job = {"title": "Software Engineer",
               "description": "Requires an active TS/SCI clearance to start. Must currently hold a "
                              "Top Secret clearance.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertTrue(any(f.startswith("dealbreaker") and "ACTIVE" in f for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 33.0)

    def test_bare_clearance_mention_is_soft_flag_not_dealbreaker(self):
        job = {"title": "Software Engineer",
               "description": "This role supports a program that involves a security clearance.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))
        self.assertTrue(any("clearance" in f for f in scored["flags"]))

    def test_not_clearance_eligible_profile_still_dealbreaks(self):
        job = {"title": "Software Engineer", "description": "Ability to obtain a security clearance.",
               "location": "Remote", "remote": True}
        scored = score_jobs.score_job(job, dict(PROFILE, clearance_eligible=False))
        self.assertTrue(any(f.startswith("dealbreaker") for f in scored["flags"]))

    def test_negated_clearance_does_not_flag(self):
        # comp_* is set so this stays a genuinely clean fixture: an unposted comp now (correctly)
        # raises its own flag, which would otherwise break this assertion for an unrelated reason.
        job = {"title": "Senior Java Backend Engineer",
               "description": "Java and Spring services. No security clearance required for this role.",
               "location": "Remote", "remote": True, "comp_min": 150000, "comp_max": 190000}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertEqual(scored["flags"], [])


class LocationTextCrossCheck(unittest.TestCase):
    """Regressions from hunt #1: posting text outranks the structured remote flag."""

    PROFILE = dict(PROFILE, max_onsite_days_per_week=2, relocation="no")

    def test_weekday_enumeration_overrides_remote_flag(self):
        # The shape: board says remote:true, posting text says Mon/Wed/Fri in-office.
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


class TitleAndSkillsScoring(unittest.TestCase):
    """Recall rebuild: exact target-title roles were buried by the keyword-count-only path."""

    PROFILE = dict(PROFILE, titles=["Forward Deployed Engineer", "AI Architect",
                                    "Senior Java Backend Engineer"],
                   title_keywords_avoid=["junior", "ruby"])

    def test_exact_target_title_scores_near_max(self):
        # The bug: "Forward Deployed Engineer" (one keyword) scored 10/30 and buried FDE roles.
        pts, notes = score_jobs.score_title({"title": "Forward Deployed Engineer"}, self.PROFILE)
        self.assertGreaterEqual(pts, 28.0)
        self.assertTrue(any("target-title" in n for n in notes))

    def test_top_tier_title_beats_lower_tier(self):
        top, _ = score_jobs.score_title({"title": "Forward Deployed Engineer"}, self.PROFILE)
        low, _ = score_jobs.score_title({"title": "Senior Java Backend Engineer"}, self.PROFILE)
        self.assertGreaterEqual(top, low)

    def test_strong_target_title_flags_express_when_clean(self):
        job = {"title": "Forward Deployed Engineer", "description": "Java, LLM agents.",
               "location": "Remote", "remote": True}
        self.assertEqual(score_jobs.strong_target_title(job, self.PROFILE), "Forward Deployed Engineer")

    def test_ruby_title_disqualifies_express(self):
        job = {"title": "Forward Deployed Engineer (Ruby)", "description": "Rails shop.",
               "location": "Remote", "remote": True}
        self.assertIsNone(score_jobs.strong_target_title(job, self.PROFILE))

    def test_generic_swe_target_not_express(self):
        # "Staff Software Engineer" reduces to {software, engineer} — matches everything, so it must
        # NOT trigger the express lane (a New-Grad SWE role shouldn't read as a target-title match).
        prof = dict(self.PROFILE, titles=["Staff Software Engineer", "Forward Deployed Engineer"])
        self.assertIsNone(score_jobs.strong_target_title({"title": "Software Engineer, New Grad"}, prof))
        self.assertEqual(
            score_jobs.strong_target_title({"title": "Forward Deployed Engineer"}, prof),
            "Forward Deployed Engineer")

    def test_skills_not_penalized_by_long_profile(self):
        # Same job, but the second profile has many MORE skills — score must NOT drop.
        job = {"title": "Engineer", "description": "java spring llm agentic mcp python rest"}
        lean = {"skills": {"java": 10, "llm": 9, "agentic": 9, "mcp": 8, "spring": 9}}
        fat = {"skills": dict(lean["skills"], **{f"skill{i}": 3 for i in range(30)})}
        lean_pts, _ = score_jobs.score_skills(job, lean)
        fat_pts, _ = score_jobs.score_skills(job, fat)
        self.assertGreaterEqual(lean_pts, 25.0)          # a strong cluster saturates high
        self.assertAlmostEqual(lean_pts, fat_pts, delta=0.5)  # long list doesn't dilute it


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

    def test_foreign_only_remote_is_not_us_eligible(self):
        for loc in ("Remote - India", "London", "Frankfurt am Main", "Remote - EMEA",
                    "New Taipei City", "Toronto, Canada"):
            job = {"title": "Software Engineer", "remote": True, "location": loc,
                   "description": "Build things."}
            scored = score_jobs.score_job(job, self.PROFILE)
            self.assertTrue(any("not US work-eligible" in f for f in scored["flags"]), loc)
            self.assertLessEqual(scored["match_percent"], 33.0, loc)

    def test_us_and_worldwide_remote_stay_eligible(self):
        for loc in ("Remote - US", "Remote (US/Canada)", "San Francisco, Remote - US",
                    "USA - Springfield, MO", "Remote - Worldwide", "Reston, VA", "Remote", ""):
            job = {"title": "AI Engineer", "remote": True, "location": loc, "description": "LLM agents."}
            scored = score_jobs.score_job(job, self.PROFILE)
            self.assertFalse(any("not US work-eligible" in f for f in scored["flags"]), loc)

    def test_structured_remote_is_true_remote(self):
        job = {"title": "Backend Engineer", "description": "Fully distributed team.",
               "location": "Remote, US", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "remote")

    def test_onsite_city_in_title_text_is_relocation(self):
        # The HN shape: no structured location, but the title reads
        # "…| San Francisco, CA | Full-time | ONSITE | $150K-$210K" — it used to alert as "unclear".
        job = {"title": "Full Stack Software Engineer | San Francisco, CA | Full-time | ONSITE | $150K-$210K + equity",
               "description": "We build web apps. Come join us in our SF office.",
               "location": None, "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertTrue(any(f.startswith("dealbreaker") and "relocation" in f for f in scored["flags"]))
        self.assertTrue(any("San Francisco" in f for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 33.0)  # capped — never alerts

    def test_stray_remote_word_in_body_does_not_rescue_onsite_post(self):
        # A "remote" mention in the BODY (not a structured flag) must not create a false remote read
        # for an explicitly-ONSITE non-commutable role — the realistic HN shape (remote=None).
        job = {"title": "Senior Engineer | Austin, TX | ONSITE",
               "description": "Onsite in Austin. We value remote-first tooling though.",
               "location": None, "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")

    def test_effective_locations_pulls_city_state_from_title(self):
        locs = score_jobs.effective_locations({"title": "Eng | Seattle, WA | ONSITE", "description": ""})
        self.assertIn("Seattle, WA", locs)

    def test_workplace_type_hybrid_noncommutable_is_relocation(self):
        # End-to-end: workplace_type Hybrid at SF → relocation, never "true remote".
        job = {"title": "Staff Software Engineer", "location": "San Francisco, New York",
               "remote": False, "workplace_type": "Hybrid", "description": "Build backend systems."}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")

    def test_workplace_type_hybrid_commutable_is_commutable(self):
        job = {"title": "Software Engineer", "location": "Springfield, MO",
               "remote": False, "workplace_type": "Hybrid", "description": "Backend."}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "commutable")

    def test_genuine_remote_with_in_office_perk_mention_stays_remote(self):
        # Precision guard: a real remote flag + an incidental "in-office" word must NOT become relocation.
        job = {"title": "Senior AI Engineer", "remote": True,
               "description": "Fully remote team. In-office perks available if you visit HQ in NYC.",
               "location": "Remote, US"}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "remote")
        self.assertFalse(any("relocation" in f for f in scored["flags"]))

    def test_contradicted_remote_flag_at_noncommutable_office_is_relocation(self):
        # The shape: remote:true but Mon/Wed/Fri in Foster City.
        job = {"title": "Staff Software Engineer",
               "description": "The hybrid role has an in-office requirement of Monday, Wednesday, "
                              "and Friday at our Foster City, CA office.",
               "location": "Foster City, CA", "remote": True}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertTrue(any("relocation implied" in f for f in scored["flags"]))

    def test_location_string_hybrid_noncommutable_is_relocation(self):
        # The Greenhouse shape: the workplace type is baked into the LOCATION STRING
        # ("Hybrid - San Francisco, New York City") with remote + workplace_type both null. The old
        # read consulted only workplace_type + prose, so it scored high and got worked up. The lone
        # "remote" here ("a remote and distributed team") describes the team, not the role.
        job = {"title": "Senior Software Engineer, AI Gateway",
               "description": "You will collaborate with a remote and distributed team of engineers.",
               "location": "Hybrid - San Francisco, New York City", "remote": None, "workplace_type": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertTrue(any(f.startswith("dealbreaker") and "relocation" in f for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 33.0)

    def test_location_string_onsite_keyword_is_relocation(self):
        job = {"title": "Software Engineer", "description": "Join our team building things.",
               "location": "On-site - Austin, TX", "remote": None, "workplace_type": None}
        self.assertEqual(score_jobs.score_job(job, self.PROFILE)["remote_verdict"], "relocation")

    def test_location_hybrid_or_remote_stays_remote(self):
        # Guard: "Hybrid or Remote" NAMES a real remote option, so the location keyword must NOT
        # force relocation — the "or Remote" wins.
        job = {"title": "Principal Solutions Engineer", "description": "Support enterprise customers.",
               "location": "Hybrid or Remote", "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "remote")
        self.assertFalse(any("relocation" in f for f in scored["flags"]))

    def test_boilerplate_remote_word_does_not_rescue_named_city(self):
        # The shape: location "San Francisco", remote null, and the only "remote" is a benefits line
        # ("flexibility in terms of remote work") — boilerplate in nearly every JD. It must not
        # rescue a named non-commutable office to a high-scoring "remote?".
        job = {"title": "Senior Software Engineer, Observability",
               "description": "Strong benefits and flexibility in terms of remote work. Come build with us.",
               "location": "San Francisco", "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertLessEqual(scored["match_percent"], 33.0)

    def test_all_remote_company_phrase_at_city_is_verify_not_relocation(self):
        # The broadening: a genuinely all-remote company ("all of our roles are remote") tagged with a
        # bare US city and no structured remote flag stays a soft "remote?" (verify), not relocation.
        job = {"title": "Backend Engineer",
               "description": "All of our roles are remote; we work asynchronously across the US.",
               "location": "Austin, TX", "remote": None}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(scored["remote_verdict"], "remote?")
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))


class ExperienceGaps(unittest.TestCase):
    """Experience-gap rule: a posting requiring years the profile does not have must flag."""

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

    def test_years_the_profile_clears_do_not_flag(self):
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

    def test_ashby_hybrid_overrides_isremote_and_parses_comp(self):
        # The Ashby bug: isRemote=true but workplaceType=Hybrid in SF/NY, comp in the tier summary.
        payload = {"jobs": [{"id": "h1", "title": "Staff Software Engineer, Backend Platform",
                             "location": "San Francisco", "isRemote": True, "workplaceType": "Hybrid",
                             "secondaryLocations": [{"location": "New York"}],
                             "compensation": {"compensationTierSummary": "$238K – $290K • Offers Equity • Offers Bonus"},
                             "jobUrl": "https://jobs.ashbyhq.com/acme/h1"}]}
        (job,) = fetch_jobs.normalize_ashby("acme", payload)
        self.assertFalse(job["remote"])                 # Hybrid wins over isRemote=true
        self.assertEqual(job["workplace_type"], "Hybrid")
        self.assertEqual(job["comp_min"], 238000)
        self.assertEqual(job["comp_max"], 290000)
        self.assertIn("New York", job["location"])      # secondary locations folded in

    def test_ashby_genuine_remote_stays_remote(self):
        payload = {"jobs": [{"id": "r1", "title": "AI Engineer", "location": "Remote, US",
                             "isRemote": True, "workplaceType": "Remote", "compensation": {}}]}
        (job,) = fetch_jobs.normalize_ashby("acme", payload)
        self.assertTrue(job["remote"])

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

    def test_hn_comp_parsed_and_onsite_not_remote(self):
        comments = [{"objectID": "9", "parent_id": 5, "story_id": 5, "author": "co",
                     "comment_text": "Acme | Full Stack | San Francisco, CA | ONSITE | $150K-$210K + equity<p>details</p>"}]
        (job,) = fetch_jobs.normalize_hn_hiring(comments, "5")
        self.assertEqual(job["comp_min"], 150000)
        self.assertEqual(job["comp_max"], 210000)
        self.assertIsNone(job["remote"])   # ONSITE marker → not remote despite pipe tags

    def test_parse_comp_range_variants(self):
        self.assertEqual(fetch_jobs.parse_comp_range("$150K-$210K + equity"), (150000, 210000))
        self.assertEqual(fetch_jobs.parse_comp_range("$150,000 to $210,000"), (150000, 210000))
        self.assertEqual(fetch_jobs.parse_comp_range("no pay listed"), (None, None))

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

    def test_rippling_merges_list_and_detail(self):
        listing = [{"uuid": "u1", "name": "ASIC Design Verification Engineer",
                    "url": "https://ats.rippling.com/acmechips/jobs/u1",
                    "workLocation": {"label": "Remote (United States)"}}]
        details = {"u1": {"description": {"company": "<p>We build chips.</p>",
                                          "role": "<p>Verify RTL with Python and SystemVerilog.</p>"},
                          "workLocations": ["Remote (United States)"],
                          "employmentType": {"id": "Salaried, full-time"},
                          "createdOn": "2026-04-24T14:02:01.393000-07:00",
                          "payRangeDetails": [{"min": 180000, "max": 240000}]}}
        (job,) = fetch_jobs.normalize_rippling("acmechips", listing, details)
        self.assertEqual(job["source"], "rippling")
        self.assertEqual(job["company"], "acmechips")
        self.assertTrue(job["remote"])
        self.assertIn("Verify RTL", job["description"])
        self.assertEqual(job["comp_min"], 180000)
        self.assertEqual(job["comp_max"], 240000)
        self.assertEqual(job["posted_at"], "2026-04-24T14:02:01")

    def test_rippling_without_detail_still_lists(self):
        listing = [{"uuid": "u2", "name": "Software Engineer", "url": "https://x/u2",
                    "workLocation": {"label": "San Jose, CA"}}]
        (job,) = fetch_jobs.normalize_rippling("acmechips", listing, {})
        self.assertEqual(job["title"], "Software Engineer")
        self.assertEqual(job["location"], "San Jose, CA")
        self.assertEqual(job["description"], "")

    def test_workday_with_and_without_detail(self):
        base = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeExternalCareerSite"
        payload = {"jobPostings": [
            {"title": "Senior AI Software Engineer", "externalPath": "/job/US-CA/Sr-AI-SWE_JR123",
             "locationsText": "US, CA, Santa Clara", "postedOn": "Posted 3 Days Ago"},
            {"title": "EDA Tooling Engineer", "externalPath": "/job/US-TX/EDA_JR456",
             "locationsText": "US, TX, Austin", "postedOn": "Posted 30+ Days Ago"},
        ]}
        details = {"/job/US-CA/Sr-AI-SWE_JR123": {"jobPostingInfo": {
            "jobReqId": "JR123", "location": "Santa Clara, CA", "remoteType": "Remote eligible",
            "timeType": "Full time", "externalUrl": "https://acme.wd5.myworkdayjobs.com/x/JR123",
            "jobDescription": "<p>Java, Python, LLM agents, chip design.</p>",
            "postedOn": "Posted 3 Days Ago"}}}
        jobs = fetch_jobs.normalize_workday("acme", base, payload, details)
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


GERMAN_DESC = ("Die Musterfirma GmbH begleitet als Bildungsanbieter mit IT-Spezialisierung "
               "deutschlandweit Menschen bei ihrer Weiterbildung und beruflichen Neuorientierung. "
               "Du hast Lust auf eine spannende Herausforderung? Du möchtest Teil eines dynamischen "
               "Teams sein und den digitalen Wandel aktiv mitgestalten? Wir freuen uns auf deine "
               "Bewerbung.")


class TestForeignLanguageEligibility(unittest.TestCase):
    """A German-language role in Heidelberg once pinged as a high-scoring "true remote" US match.

    Three separate leaks let it through; these lock each one shut.
    """

    def test_german_posting_is_not_us_eligible(self):
        # the actual miss shape: bare city (no country) + German description
        job = {"title": "Senior Software Engineer / ML Engineer (m/w/x) im Bildungsumfeld / Remote",
               "location": "Heidelberg", "description": GERMAN_DESC, "remote": True}
        self.assertFalse(score_jobs.us_work_eligible(job))
        self.assertEqual(score_jobs.posting_language_foreign(job), "German")

    def test_german_posting_flags_the_language_as_the_reason(self):
        job = {"title": "Entwickler (m/w/d)", "location": "Heidelberg", "description": GERMAN_DESC}
        flags = score_jobs.us_eligibility_flags(job, {"us_citizen": True})
        self.assertTrue(flags)
        self.assertIn("written in German", flags[0])

    def test_dach_gender_notation_alone_is_enough(self):
        """(m/w/d)/(m/w/x) is legally standard in DACH ads and appears in no US posting."""
        for title in ("Elektrotechniker (m/w/d)", "Engineer (m/w/x)", "Developer (w/m/d)",
                      "Ingenieur (gn)"):
            self.assertEqual(score_jobs.posting_language_foreign({"title": title, "description": ""}),
                             "German", title)

    def test_english_posting_is_not_mistaken_for_german(self):
        job = {"title": "Senior Software Engineer",
               "description": "We are building the premier technology platform. You will die-cast "
                              "components and ship code. Our team is remote across the US."}
        self.assertIsNone(score_jobs.posting_language_foreign(job))
        self.assertTrue(score_jobs.us_work_eligible(job))

    def test_bare_german_city_is_caught_even_in_english(self):
        # An English-language posting at a bare German city slipped both the city list and language
        for city in ("Heidelberg", "Karlsruhe", "Stuttgart", "Cologne", "Düsseldorf", "Hannover"):
            self.assertFalse(score_jobs.us_work_eligible({"location": city, "description": "English text"}),
                             f"{city} should read as foreign")

    def test_lowercase_iso_country_code_is_not_a_us_state(self):
        """', de' is Germany but also Delaware; ', in' India but also Indiana."""
        for loc in ("Munich, de", "bengaluru, in", "Tel Aviv, il", "Dresden, SN, de",
                    "Bangalore, Karnataka, in"):
            self.assertFalse(score_jobs.us_work_eligible({"location": loc, "description": "English"}),
                             f"{loc!r} should read as foreign")

    def test_real_us_states_still_pass(self):
        for loc in ("Austin, TX", "Boston, MA", "Reston, VA", "Mountain View, CA", "Atlanta, GA",
                    "Huntsville, AL", "Denver, CO", "Chicago, IL", "Bethesda, MD", "Omaha, NE"):
            self.assertTrue(score_jobs.us_work_eligible({"location": loc, "description": "English"}),
                            f"{loc!r} is a US location")

    def test_us_cities_with_foreign_namesakes_still_pass(self):
        """The uppercase 'City, ST' short-circuit is what protects these."""
        for loc in ("Manchester, NH", "Vienna, VA", "Berlin, CT", "Paris, TX", "Dublin, OH",
                    "Frankfort, KY", "Stuttgart, AR"):
            self.assertTrue(score_jobs.us_work_eligible({"location": loc, "description": "English"}),
                            f"{loc!r} is in the US")

    def test_lowercase_us_location_still_passes(self):
        # a feed emitting lowercase mustn't be rejected — the foreign list only rejects on a hit.
        # 'ma' is also Morocco's ISO code, so this guards the 2-part case specifically.
        self.assertTrue(score_jobs.us_work_eligible({"location": "boston, ma", "description": "English"}))
        self.assertTrue(score_jobs.us_work_eligible({"location": "san diego, ca", "description": "English"}))

    def test_city_region_country_triplet_reads_as_foreign(self):
        """'Renningen, BW, de' is in Germany — an English ad in an unlisted town."""
        for loc in ("Renningen, BW, de", "Stuhr, NIEDERSACHSEN, de", "Toronto, ON, ca",
                    "Some Town, Region, fr"):
            self.assertFalse(score_jobs.us_work_eligible({"location": loc, "description": "English"}),
                             f"{loc!r} should read as foreign")

    def test_us_triplets_are_not_mistaken_for_countries(self):
        for loc in ("Austin, TX, US", "New York, NY, USA", "Chicago, IL, Dallas, TX, Austin, TX"):
            self.assertTrue(score_jobs.us_work_eligible({"location": loc, "description": "English"}),
                            f"{loc!r} is a US location")

    def test_multi_site_posting_offering_us_still_passes(self):
        loc = "London, UK; Ontario, CAN; Remote-Friendly, United States; San Francisco, CA"
        self.assertTrue(score_jobs.us_work_eligible({"location": loc, "description": "English"}))

    def test_german_role_scores_as_a_dealbreaker(self):
        job = {"title": "Senior Software Engineer / ML Engineer (m/w/x) im Bildungsumfeld / Remote",
               "location": "Heidelberg", "description": GERMAN_DESC, "remote": True,
               "posted_at": _posted(0)}
        scored = score_jobs.score_job(job, dict(PROFILE, us_citizen=True))
        self.assertTrue(any(f.startswith("dealbreaker: not US work-eligible") for f in scored["flags"]))
        self.assertLessEqual(scored["match_percent"], 25.0)   # dealbreaker caps the total


def _posted(days_ago: float) -> str:
    """An ISO post date `days_ago` days in the past."""
    import datetime as _dt
    return (_dt.datetime.now() - _dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")


class TestAgeDecay(unittest.TestCase):
    """A dead posting is worthless regardless of match — age multiplies the total.

    Regression: the additive 0-5 recency band floored at 1.0, so a 1905-day-old posting
    still scored exactly 60.0% and sat on the alert threshold.
    """

    def test_fresh_posting_is_not_penalised(self):
        mult, tier, notes, flags = score_jobs.age_decay({"posted_at": _posted(3)})
        self.assertEqual(mult, 1.0)
        self.assertEqual(tier, "fresh")
        self.assertEqual(flags, [])

    def test_grace_period_boundary_is_full_score(self):
        mult, tier, _, _ = score_jobs.age_decay({"posted_at": _posted(29)})
        self.assertEqual(mult, 1.0)
        self.assertEqual(tier, "fresh")

    def test_decay_is_exponential_after_grace(self):
        # one half-life past the grace period → about half
        one_hl = score_jobs.AGE_GRACE_DAYS + score_jobs.AGE_HALF_LIFE_DAYS
        mult, _, _, _ = score_jobs.age_decay({"posted_at": _posted(one_hl)})
        self.assertAlmostEqual(mult, 0.5, delta=0.03)
        # two half-lives → about a quarter (halving again, not linear)
        two_hl = score_jobs.AGE_GRACE_DAYS + 2 * score_jobs.AGE_HALF_LIFE_DAYS
        mult2, _, _, _ = score_jobs.age_decay({"posted_at": _posted(two_hl)})
        self.assertAlmostEqual(mult2, 0.25, delta=0.03)

    def test_decay_is_monotonic(self):
        prev = 1.1
        for days in (0, 30, 60, 90, 180, 365, 730, 1905):
            mult, _, _, _ = score_jobs.age_decay({"posted_at": _posted(days)})
            self.assertLessEqual(mult, prev, f"decay must never rise ({days}d)")
            prev = mult

    def test_ancient_posting_is_annihilated_and_flagged(self):
        mult, tier, _, flags = score_jobs.age_decay({"posted_at": _posted(1905)})
        self.assertEqual(tier, "ancient")
        self.assertLess(mult, 0.001)
        self.assertTrue(any(f.startswith("stale:") for f in flags))

    def test_unknown_date_is_left_alone_not_punished(self):
        mult, tier, _, flags = score_jobs.age_decay({})
        self.assertEqual(mult, 1.0)
        self.assertEqual(tier, "unknown")
        self.assertEqual(flags, [])

    def test_tiers(self):
        for days, want in ((5, "fresh"), (30, "fresh"), (60, "recent"), (90, "recent"),
                           (120, "stale"), (180, "stale"), (300, "old"), (365, "old"),
                           (400, "ancient"), (1905, "ancient")):
            self.assertEqual(score_jobs.age_tier(days), want, f"{days}d")
        self.assertEqual(score_jobs.age_tier(None), "unknown")

    def test_no_cliff_across_a_tier_boundary(self):
        """Smooth decay: 89d vs 91d must not jump, even though the tier label changes."""
        a, _, _, _ = score_jobs.age_decay({"posted_at": _posted(89)})
        b, _, _, _ = score_jobs.age_decay({"posted_at": _posted(91)})
        self.assertLess(abs(a - b), 0.02)

    def test_score_job_emits_age_fields(self):
        job = {"title": "Forward Deployed Engineer", "description": "python agentic",
               "posted_at": _posted(300), "location": "Remote - US"}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertEqual(scored["age_tier"], "old")     # 300d: past 180 stale, within 365
        self.assertIsInstance(scored["age_days"], int)
        self.assertGreater(scored["age_days"], 280)

    def test_ancient_job_scores_far_below_a_fresh_twin(self):
        """Same role, only the post date differs — the ancient twin must collapse."""
        base = {"title": "Forward Deployed Engineer",
                "description": "python agentic llm remote", "location": "Remote - US"}
        fresh = score_jobs.score_job(dict(base, posted_at=_posted(2)), PROFILE)
        ancient = score_jobs.score_job(dict(base, posted_at=_posted(1905)), PROFILE)
        self.assertGreater(fresh["match_percent"], 20.0)      # fixture profile is minimal
        self.assertLess(ancient["match_percent"], 1.0)
        self.assertGreater(fresh["match_percent"], ancient["match_percent"] * 50)

    def test_decay_applies_after_preference_boosts(self):
        """An ancient posting's preference boosts must not rescue it."""
        job = {"title": "Agentic AI Engineer", "description": "agentic llm python",
               "posted_at": _posted(1200), "location": "Remote - US"}
        scored = score_jobs.score_job(job, PROFILE)
        self.assertLess(scored["match_percent"], 5.0)


class CompFromText(unittest.TestCase):
    """Pay-transparency ranges are prose at the foot of a JD, and MAX_DESC_CHARS used to truncate them
    away before anything read them. Precision over recall: a bogus range silently re-inflates the exact
    score this exists to fix."""

    def test_greenhouse_disclosure_tail(self):
        low, high, note = proteus_comp.comp_from_text(
            "At Acme, we welcome everyone. The range of pay for this role is: "
            "$105,910 — $160,200. This is base salary.")
        self.assertEqual((low, high), (105910.0, 160200.0))
        self.assertIn("105,910", note)

    def test_k_suffix_and_carry_across(self):
        self.assertEqual(proteus_comp.comp_from_text("Annual Salary: $250K – $485K")[:2],
                         (250000.0, 485000.0))

    def test_to_form(self):
        self.assertEqual(
            proteus_comp.comp_from_text("The base pay range for this position is $180,000 to $230,000 per year.")[:2],
            (180000.0, 230000.0))

    def test_hourly_is_annualized(self):
        low, high, note = proteus_comp.comp_from_text("Compensation: $60 - $75 per hour.")
        self.assertEqual((low, high), (124800.0, 156000.0))
        self.assertIn("hourly", note)

    def test_last_contextual_range_wins(self):
        # Disclosure blocks sit at the bottom; earlier figures are boilerplate.
        low, high, _ = proteus_comp.comp_from_text(
            "Salary at our last company was $90,000 - $100,000. ... "
            "The pay range for this role is $180,000 - $230,000.")
        self.assertEqual((low, high), (180000.0, 230000.0))

    def test_ignores_funding_and_revenue(self):
        for text in ("We raised a $500M Series C and grew ARR from $2M to $8M.",
                     "Our platform saves customers $1.2M - $4.5M annually.",
                     "Equity: 0.05% - 0.15% of the company.",
                     "Founded in 2019 - 2020 by two engineers.",
                     ""):
            self.assertEqual(proteus_comp.comp_from_text(text), (None, None, None), text[:40])

    def test_requires_a_salary_word_nearby(self):
        # Same numbers, no salary context → not a pay band.
        self.assertEqual(proteus_comp.comp_from_text("Tickets sold: $105,910 — $160,200."),
                         (None, None, None))


class CompSurvivesTruncation(unittest.TestCase):
    """The truncation bug end-to-end: a JD longer than MAX_DESC_CHARS with its pay range at the
    bottom stored `comp unknown` and scored a free +8/15 — better than disclosing honestly."""

    def _long_jd(self):
        body = "<p>" + ("We are hiring. Responsibilities include building agents. " * 260) + "</p>"
        return body + "<p>The range of pay for this role is: $105,910 &mdash; $160,200.</p>"

    def test_greenhouse_recovers_comp_past_the_cap(self):
        payload = {"jobs": [{"id": 1, "title": "Applied AI Specialist",
                             "location": {"name": "Remote - US"}, "absolute_url": "https://x/y",
                             "updated_at": "2026-07-10", "content": self._long_jd()}]}
        job = fetch_jobs.normalize_greenhouse("acme", payload)[0]
        self.assertGreater(len(fetch_jobs.html_to_text(self._long_jd())), fetch_jobs.MAX_DESC_CHARS)
        self.assertEqual(len(job["description"]), fetch_jobs.MAX_DESC_CHARS)  # cache still bounded
        self.assertEqual((job["comp_min"], job["comp_max"]), (105910.0, 160200.0))  # ...comp survives
        self.assertEqual(job["comp_currency"], "USD")

    def test_structured_comp_is_not_overwritten(self):
        job = fetch_jobs._job(title="x", description="The pay range is $10,000 - $20,000 per year.",
                              comp_min=200000, comp_max=300000)
        self.assertEqual((job["comp_min"], job["comp_max"]), (200000, 300000))

    def test_score_comp_falls_back_to_text_for_already_cached_rows(self):
        job = {"title": "AI Engineer", "description": "The pay range for this role is $80,000 - $95,000 per year."}
        points, notes = score_jobs.score_comp(job, PROFILE)   # floor 140k → below floor
        self.assertLess(points, 8.0)                          # ...so it must score WORSE than "unknown"
        self.assertTrue(any("below floor" in n for n in notes))
        self.assertTrue(any("parsed from posting text" in n for n in notes))


class CompUnknownIsFlagged(unittest.TestCase):
    def test_unknown_comp_raises_a_flag_but_stays_neutral(self):
        job = {"title": "AI Engineer", "description": "No numbers here.", "location": "Remote - US"}
        self.assertEqual(score_jobs.score_comp(job, PROFILE)[0], 8.0)   # honest: we don't know
        flags = score_jobs.comp_unknown_flags(job, PROFILE)             # ...but it says so
        self.assertTrue(flags and flags[0].startswith("comp:"))

    def test_known_comp_is_not_flagged(self):
        job = {"title": "AI Engineer", "comp_min": 150000, "comp_max": 190000}
        self.assertEqual(score_jobs.comp_unknown_flags(job, PROFILE), [])

    def test_only_warns_on_roles_that_reach_the_funnel(self):
        # Unconditional, this fires on most of the board — wallpaper, not a signal. A role that
        # would never be worked up has nothing for the warning to protect.
        strong = {"title": "Senior Backend Engineer, AI Agents", "company": "x", "location": "Remote - US",
                  "remote": True, "posted_at": "2026-07-16", "description": "java spring llm ai agent " * 30}
        weak = {"title": "Warehouse Associate", "company": "x", "location": "Remote - US",
                "remote": True, "posted_at": "2026-07-16", "description": "lifting boxes"}
        strong_scored = score_jobs.score_job(strong, PROFILE)
        weak_scored = score_jobs.score_job(weak, PROFILE)
        self.assertGreaterEqual(strong_scored["match_percent"], score_jobs.COMP_WARN_FLOOR)
        self.assertLess(weak_scored["match_percent"], score_jobs.COMP_WARN_FLOOR)
        self.assertTrue(any(f.startswith("comp:") for f in strong_scored["flags"]))
        self.assertFalse(any(f.startswith("comp:") for f in weak_scored["flags"]))


class TitleAvoidIsADealbreaker(unittest.TestCase):
    """An avoid-listed role once sat high on the board: a -15 deduction is out-earned by a strong
    skills match plus preference boosts. An avoid-word in the title is a dealbreaker instead."""

    def test_avoid_word_in_title_flags_dealbreaker(self):
        flags = score_jobs.title_avoid_flags({"title": "Staff Product Manager – AI Gateway"},
                                             {"title_keywords_avoid": ["product manager"]})
        self.assertTrue(flags[0].startswith("dealbreaker:"))
        self.assertIn("product manager", flags[0])

    def test_caps_the_total_score(self):
        # The real shape: a strong skills match + preference boosts that used to out-earn the -15
        # deduction. PROFILE's own avoid list is ["junior","frontend"], so this needs one that
        # actually carries "product manager".
        profile = dict(PROFILE, title_keywords_avoid=["product manager"])
        job = {"title": "Staff Product Manager, AI Platform", "company": "x", "location": "Remote - US",
               "remote": True, "posted_at": "2026-07-16", "comp_min": 200000, "comp_max": 260000,
               "description": "java spring llm ai agent " * 40}
        self.assertGreater(score_jobs.score_job(job, dict(profile, title_keywords_avoid=[]))["match_percent"], 60.0)
        scored = score_jobs.score_job(job, profile)   # ...same job, now with the avoid word live
        self.assertTrue(any(f.startswith("dealbreaker: title avoid-word") for f in scored["flags"]))
        self.assertLess(scored["match_percent"], 60.0)   # below the alert bar, whatever else it hits

    def test_clean_title_unaffected(self):
        self.assertEqual(score_jobs.title_avoid_flags({"title": "Senior Backend Engineer"}, PROFILE), [])

    def test_matches_title_only_not_the_description(self):
        # A JD that merely mentions frontend work is not a frontend job.
        self.assertEqual(
            score_jobs.title_avoid_flags({"title": "AI Engineer", "description": "some frontend work"},
                                         {"title_keywords_avoid": ["frontend"]}), [])


class AvoidCompaniesAndLegalRisk(unittest.TestCase):
    """`avoid_companies`, `avoid_patterns` and `post_employment_constraints` sat in profile.json
    referenced by NOTHING — so a named avoid-company scored like any other employer, and a
    consultancy competing with the owner's employer reached the top of the board unflagged despite
    the profile's rule saying competitors get flagged. The two rules have deliberately different
    strengths because the profile writes them that way."""

    PROFILE = dict(PROFILE, avoid_companies=["Globex"],
                   avoid_patterns=["Tech-consulting/staffing firms competing with Initech — "
                                   "12-to-18-month non-compete exposure; don't auto-drop, FLAG."],
                   post_employment_constraints={"employer": "Initech", "non_compete": "§XII: 12 months"})

    def test_avoid_company_is_a_dealbreaker(self):
        flags = score_jobs.avoid_company_flags({"company": "Globex"}, self.PROFILE)
        self.assertTrue(flags[0].startswith("dealbreaker:"))

    def test_avoid_company_matches_the_company_not_the_prose(self):
        # A JD that name-drops a competitor is not a job at one.
        self.assertEqual(score_jobs.avoid_company_flags(
            {"company": "acme", "description": "we compete with Globex"}, self.PROFILE), [])

    def test_consultancy_is_flagged_not_dropped(self):
        # The profile's rule is explicit: "don't auto-drop, FLAG ... for the owner's call."
        job = {"company": "Catalyst", "title": "Lead AI Engineer", "location": "Remote - US",
               "remote": True, "posted_at": "2026-07-16", "comp_min": 220000, "comp_max": 260000,
               "description": "We ship working systems alongside our clients' engineering teams. "
                              "python llm agentic ai agent " * 10}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertTrue(any(f.startswith("legal-risk:") for f in scored["flags"]))
        self.assertFalse(any(f.startswith("dealbreaker") for f in scored["flags"]))  # NOT dropped
        # The invariant that matters: flagging must not move the number. Same job, rule off vs on.
        unflagged = score_jobs.score_job(job, dict(self.PROFILE, avoid_patterns=[]))
        self.assertEqual(scored["match_percent"], unflagged["match_percent"])

    def test_the_flag_quotes_the_actual_clause(self):
        flags = score_jobs.legal_risk_flags({"company": "x"}, self.PROFILE,
                                            "we work alongside our clients")
        self.assertIn("Initech", flags[0])
        self.assertIn("§XII: 12 months", flags[0])

    def test_product_companies_are_not_flagged(self):
        # Tuned against a live board: "our clients" and "professional services" fire on every B2B
        # company — including a top-target Forward-Deployed Engineer posting. Hundreds of false
        # flags → a couple dozen real ones.
        for desc in ("We serve our clients with a great product.",
                     "Comprehensive benefits including professional services support.",
                     "Our customers love the platform."):
            self.assertEqual(score_jobs.legal_risk_flags({"company": "acme"}, self.PROFILE, desc), [], desc)

    def test_no_avoid_patterns_configured_means_no_flag(self):
        self.assertEqual(score_jobs.legal_risk_flags(
            {"company": "x"}, {"avoid_patterns": []}, "alongside our clients"), [])


class RegionLockedRemote(unittest.TestCase):
    """Two bugs, both found from one HN posting:
    `Kinxshn | Forward Deployed Engineer - backend - python | REMOTE (Europe) | Full time | Funded`
    scored as US-eligible with a full 15/15 remote and no flag.

    (a) `_FOREIGN_MARK` knew EMEA and every European country but NOT "europe"/"eu", so a role located
        plainly "Europe" passed. (b) Every region check read `job['location']` — which is None on HN
        posts, where the region is a pipe field in the TITLE. With an exact target title it was one
        `remote` verdict from the express lane pushing an alert."""

    # `titles` carries a distinctive target because the express-lane test turns on it — that is the
    # path that would have pushed the alert.
    PROFILE = dict(PROFILE, us_citizen=True, remote="preferred",
                   titles=["Forward Deployed Engineer", "Artificial Intelligence Engineer"],
                   remote_regions_ok=["USA", "United States", "US", "North America", "Worldwide"])

    def test_foreign_marker_knows_the_continent(self):
        for term in ("europe", "eu", "emea", "germany"):
            self.assertTrue(score_jobs._FOREIGN_MARK.search(term), term)

    def test_plain_europe_location_is_not_us_eligible(self):
        for loc in ("Europe", "Remote - Europe", "Remote (Europe)", "EMEA"):
            self.assertFalse(score_jobs.us_work_eligible({"location": loc, "title": "Engineer"}), loc)

    def test_region_read_from_an_hn_title_when_there_is_no_location(self):
        job = {"location": None, "remote": True,
               "title": "Kinxshn | Forward Deployed Engineer - backend - python | REMOTE (Europe) | Full time"}
        self.assertEqual(score_jobs.remote_region_claim(job), "Europe")
        self.assertFalse(score_jobs.us_work_eligible(job))

    def test_us_and_worldwide_billings_still_pass(self):
        for title in ("Acme | Engineer | REMOTE (US) | Full time",
                      "Acme | Engineer | REMOTE (Worldwide) | Full time",
                      "Acme | Engineer | REMOTE (North America) | Full time"):
            self.assertTrue(score_jobs.us_work_eligible({"location": None, "title": title}), title)

    def test_silence_still_gets_the_benefit_of_the_doubt(self):
        # No location and no REMOTE(X) billing must stay eligible — most US posts look like this.
        self.assertTrue(score_jobs.us_work_eligible(
            {"location": None, "title": "Senior Backend Engineer", "description": "python"}))

    def test_the_description_is_not_scanned_for_continents(self):
        # "our European customers" is not a region lock. Scanning prose would flag half the board —
        # the same over-firing that made the first cut of legal_risk_flags useless.
        self.assertTrue(score_jobs.us_work_eligible(
            {"location": None, "title": "Backend Engineer",
             "description": "You'll support our European customers and EMEA expansion."}))

    def test_location_band_docks_a_title_only_region_lock(self):
        job = {"location": None, "remote": True, "title": "X | Engineer | REMOTE (Europe) | Full time"}
        points, notes = score_jobs.score_location(job, self.PROFILE)
        self.assertEqual(points, 3.0)
        self.assertTrue(any("region-locked" in n for n in notes))

    def test_the_flag_names_the_region_not_none(self):
        # It printed "region-locked to 'None'" on exactly the postings it exists for.
        job = {"location": None, "remote": True, "title": "X | Engineer | REMOTE (Europe) | Full time"}
        flag = score_jobs.us_eligibility_flags(job, self.PROFILE)[0]
        self.assertIn("Europe", flag)
        self.assertNotIn("'None'", flag)

    def test_it_kills_the_express_lane(self):
        # The express lane bypasses the score bar (hunt_cycle), so an exact target title + a `remote`
        # verdict would have pushed a Europe-only role straight to an alert.
        job = {"company": "kinxshn", "location": None, "remote": True, "posted_at": "2026-07-16",
               "title": "Kinxshn | Forward Deployed Engineer | REMOTE (Europe) | Full time",
               "description": "python backend agentic"}
        scored = score_jobs.score_job(job, self.PROFILE)
        self.assertEqual(score_jobs.strong_target_title(job, self.PROFILE), "Forward Deployed Engineer")
        self.assertTrue(any(f.startswith("dealbreaker: not US work-eligible") for f in scored["flags"]))
        self.assertLess(scored["match_percent"], 60.0)


class MisleadingRemote(unittest.TestCase):
    """The trick: boards reward the word 'remote', so postings wear it. A posting can state
    remote=False AND workplace_type=Hybrid in its own ATS fields, then bill itself
    'San Francisco, Remote' — and used to score a full 15/15 remote. Relocation is the profile's
    one hard no."""

    def _job(self, **kw):
        base = {"title": "MTS, Desktop Apps", "company": "acme", "posted_at": "2026-07-16",
                "location": "San Francisco, Remote", "description": "build desktop apps"}
        base.update(kw)
        return base

    def test_contradiction_detected_from_workplace_type(self):
        note = score_jobs.remote_contradiction(self._job(workplace_type="Hybrid", remote=False))
        self.assertIn("misleading:", note)
        self.assertIn("workplace_type=hybrid", note)
        self.assertIn("remote=false", note)

    def test_contradiction_from_workplace_type_alone(self):
        self.assertIsNotNone(score_jobs.remote_contradiction(self._job(workplace_type="Onsite")))

    def test_honest_remote_posting_is_not_flagged(self):
        self.assertIsNone(score_jobs.remote_contradiction(
            self._job(location="Remote - US", remote=True, workplace_type="Remote")))

    def test_no_remote_claim_no_contradiction(self):
        # An honestly-onsite posting isn't misleading — it's just onsite.
        self.assertIsNone(score_jobs.remote_contradiction(
            self._job(location="San Francisco, CA", workplace_type="Hybrid", remote=False)))

    def test_the_word_no_longer_beats_the_fields_in_scoring(self):
        honest = score_jobs.score_location(self._job(location="Remote - US", remote=True), PROFILE)[0]
        tricky = score_jobs.score_location(self._job(workplace_type="Hybrid", remote=False), PROFILE)[0]
        self.assertEqual(honest, 15.0)
        self.assertLess(tricky, honest)   # the contradiction costs it the free full marks

    def test_structured_hybrid_trick_is_relocation(self):
        # A structured Hybrid/Onsite type at a non-commutable place, billed 'Remote', is the trick:
        # believe the employer's own dropdown — it's relocation-class, not a softer 'verify' than an
        # honest hybrid at the same place would get.
        verdict = score_jobs.classify_remote(self._job(workplace_type="Hybrid", remote=False),
                                             PROFILE, "build desktop apps")
        self.assertEqual(verdict, "relocation")

    def test_remote_false_alone_still_downgrades_to_verify(self):
        # No structured onsite type — only remote=False contradicting the 'Remote' location word. A
        # genuine remote option may still exist, so this stays 'remote?' (verify), NOT relocation:
        # the escalation is targeted to the authoritative Hybrid/Onsite dropdown.
        verdict = score_jobs.classify_remote(self._job(remote=False), PROFILE, "build desktop apps")
        self.assertEqual(verdict, "remote?")

    def test_flag_reaches_the_scored_job(self):
        scored = score_jobs.score_job(self._job(workplace_type="Hybrid", remote=False), PROFILE)
        self.assertTrue(any(f.startswith("misleading:") for f in scored["flags"]))


class SourceTiers(unittest.TestCase):
    def test_known_tiers(self):
        self.assertEqual(source_tiers.tier_of("greenhouse"), "S")
        self.assertEqual(source_tiers.tier_of("Ashby"), "S")          # case-insensitive
        self.assertEqual(source_tiers.tier_of("remotive"), "A")
        self.assertEqual(source_tiers.tier_of("jsearch"), "B")

    def test_unknown_source_is_neutral_A(self):
        self.assertEqual(source_tiers.tier_of("brand_new_board"), "A")
        self.assertEqual(source_tiers.tier_of(None), "A")

    def test_rank_order(self):
        self.assertGreater(source_tiers.tier_rank("greenhouse"), source_tiers.tier_rank("remotive"))
        self.assertGreater(source_tiers.tier_rank("remotive"), source_tiers.tier_rank("jsearch"))


class CrossPostingDedup(unittest.TestCase):
    def _job(self, source, company, title, url):
        return {"source": source, "company": company, "title": title, "url": url, "external_id": url[-1]}

    def test_aggregator_repost_folds_into_direct_board(self):
        jobs = [self._job("greenhouse", "Acme", "Software Engineer", "https://g/1"),
                self._job("adzuna", "Acme", "Software Engineer", "https://j/x")]
        out = fetch_jobs.dedupe(jobs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "greenhouse")            # the direct board survives
        self.assertEqual(out[0]["also_sources"], ["adzuna"])        # the aggregator still credited

    def test_aggregator_only_role_survives(self):
        out = fetch_jobs.dedupe([self._job("adzuna", "Remote Zest Jobs", "Staff AI Engineer", "https://j/y")])
        self.assertEqual(len(out), 1)                               # no direct dup → kept
        self.assertIsNone(out[0].get("also_sources"))

    def test_repeated_aggregator_reposts_collapse(self):
        jobs = [self._job("adzuna", "Remote Zest Jobs", "Staff AI Engineer", "https://j/y"),
                self._job("adzuna", "Remote Zest Jobs", "Staff AI Engineer", "https://j/z")]
        self.assertEqual(len(fetch_jobs.dedupe(jobs)), 1)

    def test_two_same_title_reqs_on_one_direct_board_stay_distinct(self):
        # Two genuinely different reqs at Acme, same title, both on greenhouse — must NOT merge.
        jobs = [self._job("greenhouse", "Acme", "Software Engineer", "https://g/1"),
                self._job("greenhouse", "Acme", "Software Engineer", "https://g/2")]
        self.assertEqual(len(fetch_jobs.dedupe(jobs)), 2)

    def test_exact_url_dup_still_dropped(self):
        j = self._job("greenhouse", "Acme", "Software Engineer", "https://g/1")
        self.assertEqual(len(fetch_jobs.dedupe([j, dict(j)])), 1)


class GhostPenalties(unittest.TestCase):
    def _job(self, **kw):
        base = {"company": "Acme", "title": "AI Engineer", "description": "Remote agentic AI, Java, LLM.",
                "location": "Remote", "remote": True, "posted_at": "2026-07-16"}
        base.update(kw)
        return base

    def test_aggregator_company_penalized_and_flagged(self):
        clean = score_jobs.score_job(self._job(company="Acme Labs"), PROFILE)["match_percent"]
        agg = score_jobs.score_job(self._job(company="Remote Zest Jobs"), PROFILE)
        self.assertLess(agg["match_percent"], clean)
        self.assertTrue(any(f.startswith("aggregator/recruiter") for f in agg["flags"]))

    def test_real_employer_not_flagged_as_aggregator(self):
        for name in ("CVS Health", "Airbnb", "Campbell Soup Company", "Corelight", "Anthropic"):
            scored = score_jobs.score_job(self._job(company=name), PROFILE)
            self.assertFalse(any(f.startswith("aggregator/recruiter") for f in scored["flags"]),
                             f"{name} wrongly flagged as an aggregator")

    def test_evergreen_penalized_and_flagged(self):
        fresh = score_jobs.score_job(self._job(company="Acme Labs"), PROFILE)["match_percent"]
        old = score_jobs.score_job(self._job(company="Acme Labs", days_listed=60), PROFILE)
        self.assertLess(old["match_percent"], fresh)
        self.assertTrue(any(f.startswith("evergreen") for f in old["flags"]))

    def test_recent_listing_not_evergreen(self):
        scored = score_jobs.score_job(self._job(company="Acme Labs", days_listed=10), PROFILE)
        self.assertFalse(any(f.startswith("evergreen") for f in scored["flags"]))


class RegisterWorkup(unittest.TestCase):
    """Proteus registers each workup via register_workup.py (Bash, granted) rather than Editing the
    existing manifest, which the headless runtime intermittently denies. Append + de-dupe + preserve shape."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manifest = pathlib.Path(self.tmp) / "workups.json"
        # register_workup binds MANIFEST_FILE at import time — save + restore it, or a leaked path
        # points a later test (or a real run) at this deleted tmpdir.
        self._saved = register_workup.MANIFEST_FILE
        register_workup.MANIFEST_FILE = self.manifest

    def tearDown(self):
        register_workup.MANIFEST_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_then_update_dedupes_by_slug(self):
        register_workup.register({"slug": "acme-swe", "company": "Acme", "title": "SWE", "match": 80})
        register_workup.register({"slug": "beta-swe", "company": "Beta", "title": "SWE"})
        count, action = register_workup.register(
            {"slug": "acme-swe", "company": "Acme", "title": "SWE", "match": 90})
        wk = json.loads(self.manifest.read_text(encoding="utf-8"))["workups"]
        self.assertEqual(count, 2)  # updated in place, not duplicated
        self.assertEqual(action, "updated")
        self.assertEqual(next(w for w in wk if w["slug"] == "acme-swe")["match"], 90)

    def test_preserves_the_dict_wrapper(self):
        self.manifest.write_text(json.dumps({"_comment": "keep me", "workups": []}), encoding="utf-8")
        register_workup.register({"slug": "s1", "company": "C", "title": "T"})
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["_comment"], "keep me")  # wrapper + sibling keys preserved
        self.assertEqual(len(data["workups"]), 1)

    def test_dedupe_falls_back_to_jd_url_when_no_slug(self):
        # queue-reconciled entries can lack a slug; _key falls back to jd_url so they still de-dupe.
        register_workup.register({"jd_url": "https://x/1", "company": "X", "title": "T", "match": 50})
        count, action = register_workup.register(
            {"jd_url": "https://x/1", "company": "X", "title": "T", "match": 70})
        self.assertEqual((count, action), (1, "updated"))


class RawBoardsStayOutOfOut(unittest.TestCase):
    """`out/` is what a run produces FOR the owner; `state/` is machinery. The charter's Phase 1
    hardcodes `--out .../out/<run-date>/jobs.json`, so every delegated run filled out/ with raw
    scored boards — tens of MB of them sitting beside the resumes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Save EVERY module global these tests touch — a leaked path would silently point a later
        # test (or a real run) at a deleted tmpdir.
        self._saved = (proteus_paths.OUT_DIR, proteus_paths.STATE_DIR, proteus_paths.RUNS_DIR,
                       proteus_paths.STABLE_DIR, proteus_paths._LEGACY_MOVES)
        proteus_paths.OUT_DIR = pathlib.Path(self.tmp) / "out"
        proteus_paths.STATE_DIR = pathlib.Path(self.tmp) / "state"
        proteus_paths.RUNS_DIR = proteus_paths.STATE_DIR / "runs"
        proteus_paths.STABLE_DIR = pathlib.Path(self.tmp) / "stable" / "proteus"
        proteus_paths._LEGACY_MOVES = ()   # default: only the sweeps under test run

    def tearDown(self):
        (proteus_paths.OUT_DIR, proteus_paths.STATE_DIR, proteus_paths.RUNS_DIR,
         proteus_paths.STABLE_DIR, proteus_paths._LEGACY_MOVES) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_raw_boards_redirect_to_state_runs(self):
        for name in ("jobs.json", "scored.json"):
            src = proteus_paths.OUT_DIR / "2026-07-16" / name
            self.assertEqual(proteus_paths.resolve_raw_artifact(src),
                             proteus_paths.RUNS_DIR / "2026-07-16" / name)

    def test_deliverables_are_left_alone(self):
        for rel in ("2026-07-16/resume-1-acme.md", "digests/2026-07-16.md", "site/data.js",
                    "workups.json"):
            src = proteus_paths.OUT_DIR / rel
            self.assertEqual(proteus_paths.resolve_raw_artifact(src), src, rel)

    def test_a_path_already_in_state_is_untouched(self):
        src = proteus_paths.RUNS_DIR / "2026-07-16" / "jobs.json"
        self.assertEqual(proteus_paths.resolve_raw_artifact(src), src)

    def test_migration_moves_boards_and_keeps_deliverables(self):
        run = proteus_paths.OUT_DIR / "2026-07-14"
        run.mkdir(parents=True)
        (run / "jobs.json").write_text("{}")
        (run / "scored.json").write_text("{}")
        (run / "candidate-batch.md").write_text("# a deliverable")
        moved = proteus_paths.migrate_legacy_state()
        self.assertEqual(sorted(moved), ["runs/2026-07-14/jobs.json", "runs/2026-07-14/scored.json"])
        self.assertEqual([p.name for p in run.iterdir()], ["candidate-batch.md"])  # deliverable stays
        self.assertTrue((proteus_paths.RUNS_DIR / "2026-07-14" / "jobs.json").exists())

    def test_migration_is_idempotent(self):
        run = proteus_paths.OUT_DIR / "2026-07-14"
        run.mkdir(parents=True)
        (run / "jobs.json").write_text("{}")
        self.assertTrue(proteus_paths.migrate_legacy_state())
        self.assertEqual(proteus_paths.migrate_legacy_state(), [])   # a second pass is a no-op

    def test_migration_never_clobbers_an_existing_state_file(self):
        run = proteus_paths.OUT_DIR / "2026-07-14"
        run.mkdir(parents=True)
        (run / "jobs.json").write_text("OLD")
        target = proteus_paths.RUNS_DIR / "2026-07-14" / "jobs.json"
        target.parent.mkdir(parents=True)
        target.write_text("NEW")
        proteus_paths.migrate_legacy_state()
        self.assertEqual(target.read_text(), "NEW")   # the newer state file wins

    def test_ledger_migrates_out_of_the_tracked_stable(self):
        # The ledger was the one part of stable/ that churned: demiurge appends on every delegation,
        # which left the live checkout permanently dirty (arming the pull-failed reload bug) and put
        # the full request/response text into git history. It belongs with the runtime churn.
        stable = pathlib.Path(self.tmp) / "stable" / "proteus"
        stable.mkdir(parents=True)
        (stable / "ledger.jsonl").write_text('{"task_id": "t1"}\n')
        (stable / "charter.md").write_text("# stays put")   # real stable data must NOT move
        proteus_paths.STABLE_DIR = stable
        proteus_paths._LEGACY_MOVES = ((stable / "ledger.jsonl", proteus_paths.STATE_DIR / "ledger.jsonl"),)

        moved = proteus_paths.migrate_legacy_state()
        self.assertIn("ledger.jsonl", moved)
        self.assertFalse((stable / "ledger.jsonl").exists())
        self.assertEqual((proteus_paths.STATE_DIR / "ledger.jsonl").read_text(), '{"task_id": "t1"}\n')
        self.assertTrue((stable / "charter.md").exists())   # spec/charter/evals/record stay tracked

    def test_ledger_migration_never_clobbers_a_newer_state_copy(self):
        stable = pathlib.Path(self.tmp) / "stable" / "proteus"
        stable.mkdir(parents=True)
        (stable / "ledger.jsonl").write_text("OLD\n")
        proteus_paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (proteus_paths.STATE_DIR / "ledger.jsonl").write_text("NEW — has entries the stable copy lacks\n")
        proteus_paths.STABLE_DIR = stable
        proteus_paths._LEGACY_MOVES = ((stable / "ledger.jsonl", proteus_paths.STATE_DIR / "ledger.jsonl"),)

        proteus_paths.migrate_legacy_state()
        # The live ledger is append-only history — a half-finished migration must never eat it.
        self.assertEqual((proteus_paths.STATE_DIR / "ledger.jsonl").read_text(),
                         "NEW — has entries the stable copy lacks\n")

    def _aged_run(self, name, days_old):
        run = proteus_paths.RUNS_DIR / name
        run.mkdir(parents=True)
        board = run / "jobs.json"
        board.write_text("{}")
        old = NOW_TS - days_old * 86400
        os.utime(board, (old, old))
        return run

    def test_prune_drops_boards_past_retention_and_keeps_fresh_ones(self):
        stale = self._aged_run("2026-07-01", days_old=30)
        fresh = self._aged_run("2026-07-16", days_old=1)
        removed = proteus_paths.prune_runs(days=7, now=NOW_TS)
        self.assertEqual(removed, ["2026-07-01"])
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_prune_ages_on_the_newest_file_so_a_live_run_is_safe(self):
        # A board still being written must never be pruned out from under the run producing it.
        run = self._aged_run("2026-07-01", days_old=30)      # an old jobs.json...
        (run / "scored.json").write_text("{}")               # ...but scored.json just landed
        self.assertEqual(proteus_paths.prune_runs(days=7, now=NOW_TS), [])
        self.assertTrue(run.exists())

    def test_prune_disabled_with_zero(self):
        self._aged_run("2026-07-01", days_old=999)
        self.assertEqual(proteus_paths.prune_runs(days=0, now=NOW_TS), [])

    def test_prune_on_missing_dir_is_a_noop(self):
        self.assertEqual(proteus_paths.prune_runs(days=7, now=NOW_TS), [])

    def test_retention_default_is_a_week(self):
        # 24h would delete the board before the owner has necessarily read the digest it produced.
        self.assertEqual(proteus_paths.RUNS_RETENTION_DAYS, 7)

    def test_empty_run_dir_is_swept_but_populated_one_survives(self):
        empty = proteus_paths.OUT_DIR / "2026-07-12b"
        empty.mkdir(parents=True)
        (empty / "jobs.json").write_text("{}")
        keep = proteus_paths.OUT_DIR / "site"
        keep.mkdir(parents=True)
        (keep / "data.js").write_text("x")
        proteus_paths.migrate_legacy_state()
        self.assertFalse(empty.exists())              # held only churn → gone
        self.assertTrue((keep / "data.js").exists())  # a real deliverable → untouched


if __name__ == "__main__":
    unittest.main()
