#!/usr/bin/env python3
"""Tests for Proteus's scorer — the screens that decide what reaches the owner's phone.

Stdlib only (score_jobs imports argparse/json/os/re/sys/time + its siblings proteus_comp and
source_tiers), so this runs in a plain-python CI job with no venv. Run:

  python -m unittest discover -s archons/proteus/tools -p "test_*.py"

The cost of a miss here is asymmetric: a bad alert interrupts the owner's day and erodes trust in the
whole feed, and a silently-broken screen is invisible until the owner notices the noise. So the
screens are pinned against the SHAPES of real false positives (a foreign lock laundered by an
"Anywhere" stamp, a non-employment "portfolio project", a mirror of a dead req) — with neutral
fixtures: the profile below is a test profile, never the owner's gitignored ``profile.json``.

Every call that does age math passes ``NOW`` (a pinned epoch), so no test depends on the wall clock.
"""
from __future__ import annotations

import calendar
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import score_jobs as sj  # noqa: E402
import source_tiers  # noqa: E402

# A pinned instant (2026-09-01T12:00:00Z) — every fixture's age is measured against this.
NOW = float(calendar.timegm((2026, 9, 1, 12, 0, 0)))


def _profile(**over) -> dict:
    """A neutral test profile exercising every screen these tests touch."""
    profile = {
        "us_citizen": True,
        "remote": "required",
        "remote_regions_ok": ["USA", "United States", "Americas", "Worldwide"],
        "titles": ["Forward Deployed Engineer", "AI Solutions Architect", "Robotics Software Engineer"],
        "title_keywords_strong": ["agentic", "ai", "llm", "platform"],
        "title_keywords_avoid": ["intern", "internship", "bootcamp", "apprenticeship", "unpaid",
                                 "portfolio project", "capstone"],
        "seniority": ["senior", "staff", "principal"],
        "skills": {"python": 8, "llm": 9},
        "preferences": {"examplecorp": 6, "initech": 5},
        "locations_ok": ["Remote", "Springfield", "Shelbyville"],
        "commutable_locations": ["springfield", "shelbyville"],
        "max_onsite_days_per_week": 2,
        "max_commute_minutes_one_way": 60,
        "relocation": "no",
        "onsite_cap_exempt_title_words": ["robotics", "robotic", "robot", "robots"],
    }
    profile.update(over)
    return profile


def _score(job: dict, profile: dict | None = None, **kw) -> dict:
    return sj.score_job(job, profile if profile is not None else _profile(), now=NOW, **kw)


class USWorkEligibleVagueLocation(unittest.TestCase):
    """`us_work_eligible` — a content-free aggregator location must not launder a foreign lock."""

    def test_foreign_lock_behind_anywhere_is_not_eligible(self):
        # The shape: the employer bills the region in the title and posts a separate "| US | Remote"
        # listing; this is the Ireland twin, and "Anywhere" is the aggregator's stamp, not the claim.
        job = {"title": "Staff AI Engineer - Platform | Ireland | Remote", "company": "Acme",
               "location": "Anywhere", "remote": True}
        self.assertFalse(sj.us_work_eligible(job))

    def test_the_flag_is_dealbreaker_class_so_it_blocks_the_alert(self):
        # hunt_cycle.py gates on `str(flag).startswith("dealbreaker")` — the prefix is the contract.
        job = {"title": "Staff AI Engineer | Ireland | Remote", "location": "Anywhere", "remote": True}
        flags = sj.us_eligibility_flags(job, _profile())
        self.assertTrue(flags, "a foreign-locked posting must produce a flag")
        self.assertTrue(flags[0].startswith("dealbreaker"), flags[0])

    def test_us_twin_of_the_same_role_stays_eligible(self):
        job = {"title": "Staff AI Engineer | US | Remote", "location": "Anywhere", "remote": True}
        self.assertTrue(sj.us_work_eligible(job))
        self.assertEqual(sj.us_eligibility_flags(job, _profile()), [])

    def test_plain_anywhere_posting_is_untouched(self):
        job = {"title": "Agentic AI Engineer", "location": "Anywhere", "remote": True}
        self.assertTrue(sj.us_work_eligible(job))

    def test_multi_site_listing_including_the_us_still_counts(self):
        job = {"title": "Member of Technical Staff",
               "location": "San Francisco, Remote (United States), London, Ireland", "remote": True}
        self.assertTrue(sj.us_work_eligible(job))

    def test_multi_region_billing_naming_the_americas_stays_eligible(self):
        job = {"title": "Product Manager - Security (EMEA/AMER)", "location": "Anywhere",
               "remote": True}
        self.assertTrue(sj.us_work_eligible(job))

    def test_other_vague_stamps_behave_the_same(self):
        for loc in ("Remote", "Worldwide", "Global", "N/A", "Multiple Locations"):
            with self.subTest(location=loc):
                job = {"title": "Senior Engineer | Germany | Remote", "location": loc, "remote": True}
                self.assertFalse(sj.us_work_eligible(job), f"{loc!r} should not launder Germany")


class TitleLocationSegments(unittest.TestCase):
    """A location piped into the TITLE with an empty structured location."""

    def test_pipe_segment_naming_a_foreign_country_is_screened(self):
        job = {"title": "Software Engineer - Monitoring | Germany | Remote", "location": ""}
        self.assertFalse(sj.us_work_eligible(job))
        flag = sj.us_eligibility_flags(job, _profile())[0]
        self.assertIn("Germany", flag)

    def test_us_positive_segment_wins(self):
        job = {"title": "Software Engineer | Remote (US) | Germany", "location": ""}
        self.assertTrue(sj.us_work_eligible(job))

    def test_a_real_structured_location_disables_the_segment_read(self):
        job = {"title": "Engineer | Germany", "location": "Austin, TX"}
        self.assertEqual(sj.title_location_segments(job), [])


class ForeignDomain(unittest.TestCase):
    """Where the posting was PUBLISHED — scrutinise, tie-break, never override."""

    def test_vanity_tlds_are_never_foreign(self):
        for url in ("https://boards.greenhouse.io/acme/jobs/1", "https://jobs.lever.co/acme/x",
                    "https://acme.ai/careers/1", "https://acme.io/jobs"):
            with self.subTest(url=url):
                self.assertIsNone(sj.foreign_domain({"url": url}))

    def test_longest_suffix_wins(self):
        self.assertEqual(sj.foreign_domain({"url": "https://www.jobboard.co.uk/j/1"}), ".co.uk")

    def test_no_location_plus_country_domain_is_a_dealbreaker(self):
        job = {"title": "Backend Engineer", "location": "", "url": "https://www.jobboard.co.uk/j/1"}
        self.assertFalse(sj.us_work_eligible(job))
        flag = sj.us_eligibility_flags(job, _profile())[0]
        self.assertTrue(flag.startswith("dealbreaker"))
        self.assertIn(".co.uk", flag)

    def test_a_stated_us_location_wins_but_is_flagged_for_scrutiny(self):
        job = {"title": "Backend Engineer", "location": "Remote, United States",
               "url": "https://www.jobboard.co.uk/j/1"}
        self.assertTrue(sj.us_work_eligible(job))
        flags = sj.foreign_domain_flags(job)
        self.assertTrue(flags and flags[0].startswith("origin:"), flags)


class NonEmploymentListings(unittest.TestCase):
    """Postings that aren't jobs at all — screened via profile.title_keywords_avoid."""

    def test_portfolio_project_is_dealbreakered(self):
        job = {"title": "Remote: Agentic AI Workflows - 8-Week Portfolio Project",
               "company": "Acme", "location": "Anywhere", "remote": True}
        flags = sj.title_avoid_flags(job, _profile())
        self.assertTrue(flags, "an 8-week portfolio project is not a job")
        self.assertTrue(flags[0].startswith("dealbreaker"), flags[0])

    def test_other_non_employment_shapes(self):
        for title in ("AI Engineering Bootcamp", "Software Apprenticeship - AI Platform",
                      "Backend Engineering Internship", "Unpaid AI Research Assistant",
                      "Data Engineering Capstone Program"):
            with self.subTest(title=title):
                self.assertTrue(sj.title_avoid_flags({"title": title}, _profile()),
                                f"{title!r} is not employment")

    def test_real_ai_titles_are_not_caught(self):
        # The reason these are phrases, not bare words: bare "project"/"training" would eat these.
        for title in ("Member of Technical Staff (Inference & Training Platform)",
                      "Staff AI Engineer, Project Infrastructure",
                      "Forward Deployed Engineer", "Agentic AI Engineer", "AI Solutions Architect"):
            with self.subTest(title=title):
                self.assertEqual(sj.title_avoid_flags({"title": title}, _profile()), [])


class TitleOnsite(unittest.TestCase):
    """An ONSITE word in the TITLE outranks a structured remote flag."""

    def test_onsite_title_contradicts_a_remote_flag(self):
        job = {"title": "Onsite AI Client Partner - Enterprise Architect", "location": "Anywhere",
               "remote": True}
        self.assertEqual(sj.title_onsite(job), "onsite")
        self.assertIsNotNone(sj.remote_contradiction(job))
        self.assertNotEqual(sj.classify_remote(job, _profile(), job["title"].lower()), "remote")

    def test_remote_or_onsite_title_is_not_a_contradiction(self):
        job = {"title": "Engineer (Remote or Onsite)", "remote": True}
        self.assertIsNone(sj.title_onsite(job))
        self.assertIsNone(sj.remote_contradiction(job))


def _flag_prefixes(scored: dict) -> list[str]:
    return [str(f).split(":", 1)[0] for f in scored.get("flags") or []]


def _has_source_risk(scored: dict) -> bool:
    return any(str(f).startswith("source-risk:") for f in scored.get("flags") or [])


def _has_aggregator(scored: dict) -> bool:
    return any(str(f).startswith("aggregator/recruiter:") for f in scored.get("flags") or [])


class SourceGhostRisk(unittest.TestCase):
    """The source axis — 'did this posting arrive as somebody else's copy?'"""

    BASE = {
        "title": "Senior Software Engineer",
        "company": "Northwind Systems",
        "description": "Build backend services in Python.",
        "location": "Remote (United States)",
        "remote": True,
    }

    def _score(self, **over) -> dict:
        return _score({**self.BASE, **over})

    def test_high_risk_source_docks_and_flags(self):
        high = self._score(source="adzuna")
        self.assertTrue(_has_source_risk(high), high["flags"])
        self.assertEqual(high["ghost_risk"], "high")

    def test_ats_native_source_is_untouched(self):
        for source in ("greenhouse", "ashby", "lever", "workday", "smartrecruiters", "rippling"):
            with self.subTest(source=source):
                scored = self._score(source=source)
                self.assertFalse(_has_source_risk(scored), f"{source} must not be flagged")
                self.assertEqual(scored["ghost_risk"], "low")

    def test_the_dock_is_exactly_the_knob(self):
        direct = self._score(source="greenhouse")
        mirror = self._score(source="adzuna")
        self.assertAlmostEqual(direct["match_percent"] - mirror["match_percent"],
                               sj.SOURCE_GHOST_PENALTY, places=1)

    def test_hn_hiring_is_carved_out_and_stays_carved_out(self):
        """hn_hiring is tier B for *directness* but every HN posting is typed by the hiring
        employer — the opposite of ghosty. Re-lumping HN in with the open aggregators because they
        share a directness tier would punish HN for their sins. This test exists to stop that."""
        self.assertEqual(source_tiers.tier_of("hn_hiring"), "B")
        self.assertEqual(source_tiers.ghost_risk("hn_hiring"), "low")
        hn = self._score(source="hn_hiring")
        direct = self._score(source="greenhouse")
        self.assertFalse(_has_source_risk(hn), hn["flags"])
        self.assertEqual(hn["match_percent"], direct["match_percent"])

    def test_unknown_source_is_the_neutral_middle(self):
        for source in ("", None, "some_new_board"):
            with self.subTest(source=source):
                scored = self._score(source=source)
                self.assertEqual(scored["ghost_risk"], "medium")
                self.assertFalse(_has_source_risk(scored))
                self.assertEqual(scored["match_percent"],
                                 self._score(source="greenhouse")["match_percent"])

    def test_curated_aggregators_are_the_neutral_middle_too(self):
        for source in ("remotive", "remoteok", "arbeitnow"):
            with self.subTest(source=source):
                scored = self._score(source=source)
                self.assertEqual(scored["ghost_risk"], "medium")
                self.assertFalse(_has_source_risk(scored))

    def test_a_staffing_front_via_an_aggregator_is_docked_once_not_twice(self):
        clean = self._score(source="greenhouse")
        both = self._score(source="adzuna", company="Acme Staffing Partners")
        self.assertAlmostEqual(clean["match_percent"] - both["match_percent"],
                               sj.AGGREGATOR_PENALTY, places=1,
                               msg="the two ghost penalties must not stack")
        self.assertLess(sj.SOURCE_GHOST_PENALTY, sj.AGGREGATOR_PENALTY)

    def test_both_flags_still_fire_when_the_penalty_is_absorbed(self):
        both = self._score(source="adzuna", company="Acme Staffing Partners")
        self.assertTrue(_has_aggregator(both), both["flags"])
        self.assertTrue(_has_source_risk(both), both["flags"])

    def test_the_flag_is_not_dealbreaker_class_so_it_never_hides_a_role(self):
        scored = self._score(source="adzuna")
        self.assertNotIn("dealbreaker", _flag_prefixes(scored))
        self.assertGreater(scored["match_percent"], 0.0)


def _springfield_robotics(**kw) -> dict:
    """A robotics req at an office inside the test commute zone, hybrid, comp posted."""
    base = {"title": "Robotics Software Engineer", "company": "Initech Robotics",
            "location": "Springfield, IL", "remote": False, "posted_at": "2026-08-30",
            "comp_min": 210000.0, "comp_max": 250000.0,
            "description": "Build motion-planning and perception software in Python and C++. "
                           "This is a hybrid role: 3 days per week in office at our Springfield site."}
    base.update(kw)
    return base


def _exemption_flag(scored: dict) -> str | None:
    return next((f for f in scored["flags"] if f.startswith("onsite-cap exemption")), None)


def _days_cap_flag(scored: dict) -> str | None:
    return next((f for f in scored["flags"] if "over the" in f and "-day max" in f
                 and not f.startswith("onsite-cap exemption")), None)


class OnsiteCapExemption(unittest.TestCase):
    """`onsite_cap_exempt_title_words`: the days cap stops screening for the listed title family and
    the role is surfaced WITH ITS DAYS-PER-WEEK STATED — surfacing only, never a ranking bonus and
    never anything said on the owner's behalf."""

    def test_no_list_means_no_exemption(self):
        scored = _score(_springfield_robotics(), _profile(onsite_cap_exempt_title_words=[]))
        self.assertIsNone(_exemption_flag(scored))
        self.assertIsNotNone(_days_cap_flag(scored))

    def test_an_in_range_exempt_role_is_surfaced_with_its_days(self):
        scored = _score(_springfield_robotics())
        flag = _exemption_flag(scored)
        self.assertIsNotNone(flag, scored["flags"])
        self.assertIn("onsite days/week: 3", flag)
        self.assertEqual(scored["remote_verdict"], "commutable")
        self.assertIsNone(_days_cap_flag(scored), "the days cap must not also fire")
        self.assertGreater(scored["score_breakdown"]["location"], 3.0)

    def test_a_non_exempt_role_in_range_is_still_screened(self):
        scored = _score(_springfield_robotics(title="Senior Java Backend Engineer"))
        self.assertIsNone(_exemption_flag(scored), scored["flags"])
        self.assertIsNotNone(_days_cap_flag(scored), scored["flags"])
        self.assertLessEqual(scored["score_breakdown"]["location"], 3.0)

    def test_an_exempt_role_beyond_the_zone_is_still_rejected(self):
        """The exemption buys a role past the DAYS cap only, never past geography."""
        job = _springfield_robotics(location="Boston, MA",
                                    description="Onsite in Boston, 3 days per week, humanoid robotics.")
        scored = _score(job)
        self.assertEqual(scored["remote_verdict"], "relocation")
        self.assertTrue(any(f.startswith("dealbreaker") for f in scored["flags"]), scored["flags"])
        self.assertIsNone(_exemption_flag(scored))

    def test_the_relocation_flag_quotes_the_profile_ceiling(self):
        job = {"title": "Robotics Software Engineer", "location": "Austin, TX", "remote": False,
               "description": "Onsite at our Austin office building humanoid platforms."}
        flag = next(f for f in _score(job)["flags"] if "relocation implied" in f)
        self.assertIn("~60-min commute zone", flag)

    def test_an_rpa_posting_does_not_get_the_exemption(self):
        """THE TRAP: "Robotic Process Automation" is enterprise workflow software."""
        rpa = _springfield_robotics(
            title="Robotics Process Automation Developer", company="Acme Consulting",
            description="Build UiPath and Blue Prism bots to automate back-office workflows. "
                        "Hybrid: 3 days per week in office at our Springfield site.")
        scored = _score(rpa)
        self.assertIsNone(_exemption_flag(scored), scored["flags"])
        self.assertIsNotNone(_days_cap_flag(scored), scored["flags"])

    def test_robot_framework_is_not_robotics_either(self):
        self.assertIsNone(sj.onsite_exempt_role(
            {"title": "Senior QA Engineer, Robot Framework",
             "description": "Own our Robot Framework suites. Hybrid, 3 days per week in office."},
            _profile()))

    def test_the_veto_reads_the_description_too(self):
        self.assertIsNone(sj.onsite_exempt_role(
            {"title": "Robotics Developer", "description": "Automate invoicing with UiPath (RPA)."},
            _profile()))

    def test_the_word_in_boilerplate_prose_is_not_an_exempt_role(self):
        job = _springfield_robotics(
            title="Senior Java Backend Engineer",
            description="Our customers work across robotics, healthcare and fintech. Hybrid: "
                        "3 days per week in office at our Springfield site.")
        scored = _score(job)
        self.assertIsNone(_exemption_flag(scored), scored["flags"])
        self.assertIsNotNone(_days_cap_flag(scored), scored["flags"])

    def test_word_boundaries_mean_robotics_does_not_match_robotic(self):
        self.assertFalse(sj._word_hit("robotic process automation", "robotics"))
        self.assertTrue(sj._word_hit("robotics software engineer", "robotics"))
        self.assertEqual(sj.onsite_exempt_role({"title": "Robotic Systems Engineer",
                                                "description": "ROS"}, _profile()), "robotic")

    def test_no_stated_days_renders_not_stated(self):
        job = _springfield_robotics(
            description="Hybrid role based in our Springfield office. You will build perception "
                        "and controls software for mobile robots.")
        flag = _exemption_flag(_score(job))
        self.assertIsNotNone(flag)
        self.assertIn("not stated", flag)
        self.assertNotRegex(flag, r"onsite days/week: \d")

    def test_the_days_number_is_never_invented(self):
        self.assertEqual(sj.onsite_days_statement("hybrid role in our springfield office"),
                         "onsite days/week: not stated")
        self.assertIn("onsite days/week: 3",
                      sj.onsite_days_statement("hybrid: 3 days per week in office"))

    def test_a_fully_remote_exempt_role_gets_no_schedule_noise(self):
        job = _springfield_robotics(location="Remote", remote=True,
                                    description="Fully remote robotics role. Build perception software.")
        self.assertIsNone(_exemption_flag(_score(job)))

    def test_five_days_is_never_ranked_above_two(self):
        five = _score(_springfield_robotics(
            description="Robotics role, 5 days per week in office at our Springfield site."))
        two = _score(_springfield_robotics(
            description="Robotics role, 2 days per week in office at our Springfield site."))
        self.assertLessEqual(five["match_percent"], two["match_percent"])

    def test_the_flag_refuses_to_speak_for_the_owner(self):
        flag = _exemption_flag(_score(_springfield_robotics(
            description="Robotics role, 5 days per week in office at our Springfield site.")))
        self.assertIn("NOT a sign-off", flag)
        self.assertIn("never represent them as open to it", flag)

    def test_the_exemption_flag_is_not_dealbreaker_class(self):
        self.assertNotIn("dealbreaker", _flag_prefixes(_score(_springfield_robotics())))

    def test_a_billed_remote_exempt_role_still_reports_the_contradiction(self):
        flag = _exemption_flag(_score(_springfield_robotics(remote=True)))
        self.assertIsNotNone(flag)
        self.assertIn("structured remote flag contradicted by posting text", flag)

    def test_the_gate_asks_geography_through_the_verdict_s_own_code(self):
        self.assertTrue(sj.commutable_office({"location": "Springfield, IL"}, _profile()))
        self.assertFalse(sj.commutable_office({"location": "Boston, MA"}, _profile()))


class PreferenceWordBoundaries(unittest.TestCase):
    """`preferences` keys are matched as WORDS — a substring match would fire everywhere."""

    def test_a_company_key_does_not_match_inside_a_longer_word(self):
        _, notes = sj.preference_boost("initechnology and examplecorporate synergy",
                                       {"preferences": {"initech": 5, "examplecorp": 6}})
        self.assertEqual(notes, [])

    def test_the_company_key_still_hits(self):
        _, notes = sj.preference_boost("join initech to build compilers", {"preferences": {"initech": 5}})
        self.assertIn("initech +5", " ".join(notes))

    def test_the_boost_saturates(self):
        one, _ = sj.preference_boost("a role at examplecorp", {"preferences": {"examplecorp": 6}})
        self.assertAlmostEqual(one, 5.3, places=1)
        two, _ = sj.preference_boost("examplecorp and initech",
                                     {"preferences": {"examplecorp": 6, "initech": 5}})
        self.assertAlmostEqual(two, 7.5, places=1)
        self.assertLess(two, one * 2, "stacking must saturate, not sum")


class AgeMathIsInjectable(unittest.TestCase):
    """`now` pins every age computation — no test reads the wall clock."""

    def test_age_days_from_the_pinned_instant(self):
        self.assertEqual(int(sj.job_age_days({"posted_at": "2026-08-22"}, NOW)), 10)

    def test_decay_tier_is_deterministic(self):
        _, tier, _, flags = sj.age_decay({"posted_at": "2025-06-01"}, NOW)
        self.assertEqual(tier, "ancient")
        self.assertTrue(flags)


class CompanyIntel(unittest.TestCase):
    """The curated ledger plus the pending overlay, with owner entries protected in the live score."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.tracked = os.path.join(self.dir, "company-intel.json")
        self.pending = os.path.join(self.dir, "pending.jsonl")

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_files_cost_only_the_tilt(self):
        self.assertEqual(sj.load_company_intel(self.tracked, self.pending), {})

    def test_a_pending_entry_tilts_immediately_and_says_so(self):
        import json
        self._write(self.pending, json.dumps({"company": "Acme", "adjust": -5, "tags": ["layoffs"],
                                              "source": "work-up acme"}) + "\n")
        intel = sj.load_company_intel(self.tracked, self.pending)
        points, notes, flags = sj.company_intel_adjustment("acme", intel)
        self.assertEqual(points, -5.0)
        self.assertIn("[pending review]", notes[0])
        self.assertIn("[pending review]", flags[0])

    def test_a_truncated_trailing_line_is_survived(self):
        import json
        self._write(self.pending, json.dumps({"company": "Acme", "adjust": 3}) + "\n{\"company\": \"Glo")
        intel = sj.load_company_intel(self.tracked, self.pending)
        self.assertIn("Acme", intel["companies"])

    def test_overlay_replaces_in_place_case_insensitively(self):
        import json
        self._write(self.tracked, json.dumps({"companies": {"Acme Corp": {"adjust": -2,
                                                                          "source": "work-up x"}}}))
        self._write(self.pending, json.dumps({"company": "acme corp", "adjust": -9}) + "\n")
        intel = sj.load_company_intel(self.tracked, self.pending)
        self.assertEqual(list(intel["companies"]), ["Acme Corp"])
        self.assertEqual(intel["companies"]["Acme Corp"]["adjust"], -9)

    def test_a_weakening_proposal_never_softens_an_owner_entry_in_the_live_score(self):
        import json
        self._write(self.tracked, json.dumps({"companies": {"Globex": {
            "adjust": -18, "tags": ["layoffs"], "note": "owner's call",
            "source": "owner: seeded"}}}))
        self._write(self.pending, json.dumps({"company": "Globex", "adjust": -2}) + "\n")
        intel = sj.load_company_intel(self.tracked, self.pending)
        points, _, _ = sj.company_intel_adjustment("globex", intel)
        self.assertEqual(points, -18.0)

    def test_the_clamp_holds(self):
        points, _, _ = sj.company_intel_adjustment(
            "acme", {"companies": {"Acme": {"adjust": -999}}})
        self.assertEqual(points, -sj.INTEL_ADJUST_MAX)

    def test_score_job_applies_the_intel(self):
        base = {"title": "Senior Software Engineer", "company": "Acme",
                "description": "Python services.", "location": "Remote (United States)",
                "remote": True, "source": "greenhouse"}
        plain = _score(base)
        docked = _score(base, intel={"companies": {"Acme": {"adjust": -10, "tags": ["layoffs"]}}})
        self.assertAlmostEqual(plain["match_percent"] - docked["match_percent"], 10.0, places=1)


if __name__ == "__main__":
    unittest.main()
