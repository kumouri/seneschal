#!/usr/bin/env python3
"""Tests for the fetch layer — the Welcome to the Jungle source, the mirror fold it introduced, and
employer attribution from an apply URL's ATS org.

The heaviest tests are on what a normalizer EMITS — what location string comes out, and whether the
scorer's US screen can read it — because the costliest fetch-layer bug shape is a normalizer that
leaves `location` empty (or unreadable) and lets a foreign req alert as a US match.

Pure functions only; nothing here touches the network or the wall clock. Fixtures are neutral
(Acme / ExampleCorp / Initech / Globex), never real employers.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_jobs as fj  # noqa: E402
import hunt_cycle as hc  # noqa: E402
import score_jobs as sj  # noqa: E402
import source_tiers  # noqa: E402


def _wttj_row(**over) -> dict:
    """One row shaped like the /api/v3/organizations/<slug>/jobs payload."""
    row = {
        "name": "Machine Learning Infrastructure Engineer",
        "status": "published",
        "remote": "unknown",
        "reference": "00000000-0000-0000-0000-000000000000",
        "slug": "machine-learning-infrastructure-engineer_new-york_abcd1234",
        "updated_at": "2026-08-06T00:05:17.489752Z",
        "published_at": "2026-08-06T00:05:00Z",
        "contract_type": "full_time",
        "organization": {"name": "ExampleCorp", "slug": "examplecorp"},
        "office": {"city": "New York", "country_code": "US"},
        "offices": [{"city": "New York", "country_code": "US"}],
        "salary_period": "yearly",
        "salary_min": 270000.0,
        "salary_max": 320000.0,
        "salary_currency": "USD",
    }
    row.update(over)
    return row


def _payload(*rows) -> dict:
    return {"data": list(rows), "metadata": {"total": len(rows), "page": 1,
                                             "per_page": 30, "page_count": 1}}


class WttjNormalizer(unittest.TestCase):
    def test_core_fields(self):
        job, = fj.normalize_wttj("examplecorp", _payload(_wttj_row()))
        self.assertEqual(job["source"], "welcometothejungle")
        self.assertEqual(job["company"], "ExampleCorp")
        self.assertEqual(job["title"], "Machine Learning Infrastructure Engineer")
        self.assertEqual(job["external_id"], "00000000-0000-0000-0000-000000000000")
        self.assertEqual(job["posted_at"], "2026-08-06T00:05:00Z")
        self.assertEqual(job["employment_type"], "full_time")
        self.assertTrue(job["url"].startswith(
            "https://www.welcometothejungle.com/en/companies/examplecorp/jobs/"))

    def test_structured_salary_is_carried_through(self):
        job, = fj.normalize_wttj("examplecorp", _payload(_wttj_row()))
        self.assertEqual((job["comp_min"], job["comp_max"], job["comp_currency"]),
                         (270000, 320000, "USD"))

    def test_non_yearly_salary_is_annualized(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(
            salary_period="monthly", salary_min=10000, salary_max=None)))
        self.assertEqual(job["comp_min"], 120000)
        self.assertIsNone(job["comp_max"])

    def test_unknown_salary_period_is_dropped_not_guessed(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(salary_period="per_sprint")))
        self.assertIsNone(job["comp_min"])
        self.assertIsNone(job["comp_max"])

    def test_zero_salary_is_not_a_salary(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(salary_min=0, salary_max=0)))
        self.assertIsNone(job["comp_min"])
        self.assertIsNone(job["comp_currency"])

    def test_unpublished_reqs_are_skipped(self):
        jobs = fj.normalize_wttj("x", _payload(_wttj_row(status="archived"),
                                               _wttj_row(slug="b_austin_z")))
        self.assertEqual(len(jobs), 1)

    def test_remote_enum_maps_to_flag_and_workplace_type(self):
        cases = {
            "fulltime": (True, "Remote"),
            "partial": (False, "Hybrid"),
            "punctual": (False, "Hybrid"),
            "no": (False, "Onsite"),
            "unknown": (None, None),
        }
        for value, (remote, workplace) in cases.items():
            with self.subTest(remote=value):
                job, = fj.normalize_wttj("x", _payload(_wttj_row(remote=value)))
                self.assertEqual(job["remote"], remote)
                self.assertEqual(job["workplace_type"], workplace)

    def test_list_rows_carry_no_description(self):
        """List-only is the hourly contract. If this ever starts returning prose, the lazy-hydrate
        design has silently changed and the request-budget argument needs redoing."""
        job, = fj.normalize_wttj("x", _payload(_wttj_row()))
        self.assertEqual(job["description"], "")
        self.assertIsNone(job["apply_url"])


class WttjLocationIsReadableByTheScorer(unittest.TestCase):
    """Asserted end-to-end through `us_work_eligible`, not just its formatting."""

    def test_us_office_renders_a_country_the_screen_recognizes(self):
        job, = fj.normalize_wttj("examplecorp", _payload(_wttj_row()))
        self.assertEqual(job["location"], "New York, United States")
        self.assertTrue(sj.us_work_eligible(job))

    def test_french_office_is_screened_out(self):
        job, = fj.normalize_wttj("globex", _payload(_wttj_row(
            office={"city": "Paris", "country_code": "FR"},
            offices=[{"city": "Paris", "country_code": "FR"}])))
        self.assertEqual(job["location"], "Paris, France")
        self.assertFalse(sj.us_work_eligible(job))

    def test_obscure_foreign_city_still_screened_via_the_country_name(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(
            office={"city": "Louviers", "country_code": "FR"},
            offices=[{"city": "Louviers", "country_code": "FR"}])))
        self.assertIn("France", job["location"])
        self.assertFalse(sj.us_work_eligible(job))

    def test_multi_office_role_prefers_its_US_offices(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(offices=[
            {"city": "Paris", "country_code": "FR"},
            {"city": "New York", "country_code": "US"},
        ])))
        self.assertEqual(job["location"], "New York, United States")
        self.assertTrue(sj.us_work_eligible(job))

    def test_all_foreign_offices_are_all_reported(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(offices=[
            {"city": "Paris", "country_code": "FR"},
            {"city": "Berlin", "country_code": "DE"},
        ])))
        self.assertIn("France", job["location"])
        self.assertIn("Germany", job["location"])
        self.assertFalse(sj.us_work_eligible(job))

    def test_missing_offices_is_not_a_crash(self):
        job, = fj.normalize_wttj("x", _payload(_wttj_row(office=None, offices=[])))
        self.assertIsNone(job["location"])


class WttjSourceClassification(unittest.TestCase):
    def test_tier_a_not_s(self):
        """A mirror of an employer req, not the employer's own board. Calling it S would stop dedupe
        folding it (S rows are deliberately never merged) and show every overlapping job twice."""
        self.assertEqual(source_tiers.tier_of("welcometothejungle"), "A")

    def test_ghost_risk_low_because_dead_is_cheap_to_prove(self):
        self.assertEqual(source_tiers.ghost_risk("welcometothejungle"), "low")

    def test_it_is_not_penalized_like_an_open_aggregator(self):
        base = {"title": "Senior Software Engineer", "company": "Northwind Systems",
                "description": "Build backend services in Python.",
                "location": "Remote (United States)", "remote": True}
        profile = {"us_citizen": True, "skills": {}, "preferences": {}}
        direct = sj.score_job({**base, "source": "greenhouse"}, profile, now=0.0)
        mirror = sj.score_job({**base, "source": "welcometothejungle"}, profile, now=0.0)
        self.assertEqual(mirror["match_percent"], direct["match_percent"])

    def test_it_is_registered_as_a_mirror(self):
        self.assertIn("welcometothejungle", source_tiers.MIRROR_SOURCES)


class MirrorFold(unittest.TestCase):
    """A WTTJ row for a req a direct board already carries collapses into that row — and, unlike the
    tier-B aggregator fold, may give something back."""

    def _direct(self, **over):
        job = {"source": "greenhouse", "company": "ExampleCorp", "title": "ML Infra Engineer",
               "url": "https://boards.greenhouse.io/examplecorp/jobs/1", "comp_min": None,
               "comp_max": None, "comp_currency": None, "remote": None, "workplace_type": None,
               "employment_type": None, "posted_at": "2026-08-01"}
        job.update(over)
        return job

    def _mirror(self, **over):
        job = {"source": "welcometothejungle", "company": "ExampleCorp",
               "title": "ML Infra Engineer",
               "url": "https://www.welcometothejungle.com/en/companies/examplecorp/jobs/x",
               "comp_min": 270000, "comp_max": 320000, "comp_currency": "USD",
               "remote": True, "workplace_type": "Remote", "employment_type": "full_time",
               "posted_at": "2026-08-06"}
        job.update(over)
        return job

    def test_mirror_folds_into_the_direct_row(self):
        out = fj.dedupe([self._direct(), self._mirror()])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "greenhouse")
        self.assertIn("welcometothejungle", out[0]["also_sources"])

    def test_the_fold_fills_in_comp_the_direct_board_left_blank(self):
        out = fj.dedupe([self._direct(), self._mirror()])
        self.assertEqual((out[0]["comp_min"], out[0]["comp_max"]), (270000, 320000))
        self.assertEqual(out[0]["comp_source"], "welcometothejungle")

    def test_the_fold_never_overwrites_comp_the_direct_board_already_had(self):
        out = fj.dedupe([self._direct(comp_min=100000, comp_max=150000), self._mirror()])
        self.assertEqual((out[0]["comp_min"], out[0]["comp_max"]), (100000, 150000))

    def test_a_mirror_with_no_direct_row_survives_alone(self):
        out = fj.dedupe([self._mirror(company="Globex", title="Security Engineer")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "welcometothejungle")

    def test_two_direct_rows_are_still_never_merged_with_each_other(self):
        a = self._direct(url="https://boards.greenhouse.io/examplecorp/jobs/1")
        b = self._direct(url="https://boards.greenhouse.io/examplecorp/jobs/2")
        self.assertEqual(len(fj.dedupe([a, b])), 2)

    def test_the_same_req_on_two_wttj_profiles_collapses(self):
        a = self._mirror(url="https://www.welcometothejungle.com/en/companies/examplecorp/jobs/x")
        b = self._mirror(url="https://www.welcometothejungle.com/en/companies/examplecorp-2/jobs/y")
        self.assertEqual(len(fj.dedupe([a, b])), 1)

    def test_an_aggregator_repost_still_folds_after_the_mirror_pass(self):
        agg = {"source": "adzuna", "company": "ExampleCorp", "title": "ML Infra Engineer",
               "url": "https://example.invalid/j/1"}
        out = fj.dedupe([self._direct(), self._mirror(), agg])
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0]["also_sources"]), {"adzuna", "welcometothejungle"})

    def test_a_mirror_can_be_the_survivor_an_aggregator_folds_into(self):
        agg = {"source": "adzuna", "company": "Globex", "title": "Security Engineer",
               "url": "https://example.invalid/j/2"}
        out = fj.dedupe([self._mirror(company="Globex", title="Security Engineer"), agg])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "welcometothejungle")
        self.assertIn("adzuna", out[0]["also_sources"])


class DiscoveryUrlParsing(unittest.TestCase):
    """wttj_discover reads locations out of sitemap slugs. Getting the segment split wrong is how a
    role NAMED 'us-partnerships' would read as a US location."""

    def setUp(self):
        import wttj_discover
        self.wd = wttj_discover

    def test_us_city_segment_counts(self):
        self.assertTrue(self.wd.is_us_listing("security-engineer_palo-alto_b7xpvebe"))
        self.assertTrue(self.wd.is_us_listing("director-of-legal-operations_washington_lfsfiqnc"))

    def test_foreign_city_does_not(self):
        self.assertFalse(self.wd.is_us_listing("manager-de-rayon-h-f_saint-vallier"))
        self.assertFalse(self.wd.is_us_listing("senior-risk-manager_london_tu2ia3fq"))

    def test_the_role_segment_is_never_read_as_a_location(self):
        self.assertFalse(self.wd.is_us_listing("us-partnerships-lead_paris"))

    def test_slug_with_no_location_segment(self):
        self.assertFalse(self.wd.is_us_listing("expression-of-interest"))


# --------------------------------------------------------------- employer attribution
#
# The regression shape these guard: an aggregator row naming a job-curation board as the "company",
# while its apply link is the real employer's req on Ashby. These tests exist so the correction
# never silently stops — and never fires on anything but ATS evidence.

ASHBY_REQ = "https://jobs.ashbyhq.com/examplecorp/00000000-0000-0000-0000-000000000000"
REPOST = "https://www.reposter.example/join/forward-deployed-engineer-1a2b3c"


class AtsHostParsing(unittest.TestCase):
    """`ats_hosts` reads the org out of a URL, or declines. Declining must be the safe default."""

    def setUp(self):
        import ats_hosts
        self.ats = ats_hosts

    def test_ashby_path_names_the_org(self):
        self.assertEqual(self.ats.employer_org(ASHBY_REQ), ("ashby", "examplecorp"))

    def test_greenhouse_lever_smartrecruiters_workable(self):
        cases = {
            "https://job-boards.greenhouse.io/acme/jobs/4012345": ("greenhouse", "acme"),
            "https://boards.greenhouse.io/globex/jobs/9": ("greenhouse", "globex"),
            "https://jobs.lever.co/initech/abc-123": ("lever", "initech"),
            "https://jobs.smartrecruiters.com/Umbrella/744000012345": ("smartrecruiters", "umbrella"),
            "https://apply.workable.com/hooli/j/DEADBEEF/": ("workable", "hooli"),
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.ats.employer_org(url), want)

    def test_workday_org_is_the_tenant_subdomain_not_the_pod(self):
        self.assertEqual(
            self.ats.employer_org("https://globex.wd5.myworkdayjobs.com/en-US/GlobexCareers/job/x"),
            ("workday", "globex"))
        self.assertIsNone(self.ats.employer_org("https://wd5.myworkdayjobs.com/en-US/site/job/x"))

    def test_an_unrecognised_host_yields_nothing(self):
        for url in (REPOST, "https://curator.example/jobs/examplecorp-fde",
                    "https://www.linkedin.com/jobs/view/4012345678",
                    "https://acme.example/careers/job?4703779006&board=acme&gh_jid=4703779006"):
            with self.subTest(url=url):
                self.assertIsNone(self.ats.employer_org(url))

    def test_routing_segments_are_not_read_as_an_org(self):
        self.assertIsNone(self.ats.employer_org("https://apply.workable.com/j/DEADBEEF"))

    def test_junk_input_is_not_a_crash(self):
        for url in ("", None, "not a url", "javascript:alert(1)", "https://", "mailto:x@y.z"):
            with self.subTest(url=url):
                self.assertIsNone(self.ats.employer_org(url))

    def test_names_agree_across_casing_punctuation_and_legal_suffixes(self):
        self.assertTrue(self.ats.names_agree("ExampleCorp", "examplecorp"))
        self.assertTrue(self.ats.names_agree("ExampleCorp, Inc.", "examplecorp"))
        self.assertTrue(self.ats.names_agree("Initech Robotics", "initech-robotics"))
        self.assertFalse(self.ats.names_agree("Curatorly", "examplecorp"))
        self.assertFalse(self.ats.names_agree("", "examplecorp"))

    def test_display_name_is_derived_and_says_so_about_casing(self):
        self.assertEqual(self.ats.display_name("initech-robotics"), "Initech Robotics")
        self.assertEqual(self.ats.display_name("examplecorp"), "Examplecorp")


class EmployerAttribution(unittest.TestCase):
    """`attribute_employers` corrects from the ATS org, or leaves the feed's value alone."""

    def _agg(self, **over):
        job = {"source": "adzuna", "company": "Curatorly",
               "title": "Forward Deployed Engineer", "url": REPOST, "apply_url": ASHBY_REQ}
        job.update(over)
        return job

    def test_the_regression_the_curator_is_replaced_by_the_ats_org(self):
        job = self._agg()
        self.assertEqual(fj.attribute_employers([job]), 1)
        self.assertEqual(job["company"], "Examplecorp")

    def test_the_feeds_claim_is_kept_not_dropped(self):
        job = self._agg()
        fj.attribute_employers([job])
        self.assertEqual(job["company_reported"], "Curatorly")
        self.assertEqual(job["company_source"], "apply-url")
        self.assertEqual((job["apply_ats"], job["apply_org"]), ("ashby", "examplecorp"))

    def test_the_negative_case_an_unknown_host_changes_nothing(self):
        job = self._agg(apply_url="https://aggregator.example/c/Curatorly/Job/FDE")
        self.assertEqual(fj.attribute_employers([job]), 0)
        self.assertEqual(job["company"], "Curatorly")
        for key in ("company_reported", "company_source", "apply_ats", "apply_org"):
            self.assertNotIn(key, job)

    def test_no_apply_url_falls_back_to_the_posting_url(self):
        job = self._agg(apply_url=None, url=ASHBY_REQ)
        self.assertEqual(fj.attribute_employers([job]), 1)
        self.assertEqual(job["company"], "Examplecorp")

    def test_a_feed_that_already_had_it_right_is_left_completely_alone(self):
        job = self._agg(company="ExampleCorp, Inc.")
        self.assertEqual(fj.attribute_employers([job]), 0)
        self.assertEqual(job["company"], "ExampleCorp, Inc.")
        self.assertNotIn("company_source", job)

    def test_a_direct_board_row_is_never_re_attributed(self):
        job = {"source": "greenhouse", "company": "ExampleCorp PBC", "title": "ML Infra Engineer",
               "url": "https://boards.greenhouse.io/examplecorp/jobs/1"}
        self.assertEqual(fj.attribute_employers([job]), 0)
        self.assertEqual(job["company"], "ExampleCorp PBC")

    def test_an_exact_name_already_in_hand_beats_the_slug_derived_one(self):
        direct = {"source": "ashby", "company": "ExampleCorp", "title": "Backend Engineer",
                  "url": "https://jobs.ashbyhq.com/examplecorp/aaa"}
        reposted = self._agg()
        fj.attribute_employers([direct, reposted])
        self.assertEqual(reposted["company"], "ExampleCorp")

    def test_a_blinded_listing_with_no_employer_at_all_is_named(self):
        job = self._agg(company="")
        self.assertEqual(fj.attribute_employers([job]), 1)
        self.assertEqual(job["company"], "Examplecorp")
        self.assertIsNone(job["company_reported"])

    def test_correction_runs_before_dedupe_so_the_repost_folds(self):
        direct = {"source": "ashby", "company": "ExampleCorp", "title": "Forward Deployed Engineer",
                  "url": "https://jobs.ashbyhq.com/examplecorp/1b26f0a2"}
        reposted = self._agg()
        fj.attribute_employers([direct, reposted])
        out = fj.dedupe([direct, reposted])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "ashby")
        self.assertIn("adzuna", out[0]["also_sources"])

    def test_the_correction_is_flagged_by_the_scorer_not_silently_applied(self):
        job = self._agg()
        fj.attribute_employers([job])
        scored = sj.score_job(dict(job, description="Python, LLM agents."), {}, now=0.0)
        flag = next((f for f in scored["flags"] if f.startswith("employer corrected:")), None)
        self.assertIsNotNone(flag, scored["flags"])
        self.assertIn("Curatorly", flag)
        self.assertIn("examplecorp", flag)

    def test_an_uncorrected_job_gets_no_such_flag(self):
        scored = sj.score_job({"source": "adzuna", "company": "Curatorly", "title": "FDE",
                               "url": REPOST, "description": "Python."}, {}, now=0.0)
        self.assertFalse([f for f in scored["flags"] if f.startswith("employer corrected:")])


class HydrationShortlist(unittest.TestCase):
    """`hunt_cycle.hydration_shortlist` — the per-cycle detail-request budget."""

    def _row(self, score, **over):
        row = {"source": "welcometothejungle", "description": "", "match_percent": score,
               "flags": [], "remote_verdict": "remote", "age_tier": "fresh"}
        row.update(over)
        return row

    def test_the_list_length_is_the_budget(self):
        rows = [self._row(90.0 - i) for i in range(40)]
        self.assertEqual(len(hc.hydration_shortlist(rows, 60.0, limit=25)), 25)
        self.assertEqual(hc.hydration_shortlist(rows, 60.0, limit=-5), [])

    def test_only_textless_wttj_rows_under_consideration(self):
        rows = [self._row(90.0, source="greenhouse"), self._row(90.0, description="has text"),
                self._row(90.0, flags=["dealbreaker: x"]), self._row(10.0)]
        self.assertEqual(hc.hydration_shortlist(rows, 60.0), [])

    def test_express_rows_outrank_high_scorers(self):
        express = self._row(5.0, target_title="Forward Deployed Engineer")
        rows = [self._row(95.0) for _ in range(3)] + [express]
        self.assertIs(hc.hydration_shortlist(rows, 60.0, limit=1)[0], express)


class ApplyLinkRendering(unittest.TestCase):
    def test_a_distinct_apply_link_is_shown(self):
        lines = hc.link_block("https://mirror.example/j/1", ASHBY_REQ)
        self.assertEqual(len(lines), 2)
        self.assertIn(ASHBY_REQ, lines[1])

    def test_the_same_link_under_another_spelling_is_not(self):
        lines = hc.link_block("https://jobs.example/j/1?utm_source=x", "https://jobs.example/j/1/")
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
