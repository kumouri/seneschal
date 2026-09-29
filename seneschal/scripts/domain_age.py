#!/usr/bin/env python3
"""Domain registration age via RDAP — one signal for an email-triage authenticity check.

A domain registered days before it starts emailing is a strong phishing signal: a lookalike
"careers" or "billing" domain stood up a week before first contact is the classic shape. RDAP
(RFC 7483, the WHOIS successor) is a plain JSON-over-HTTPS lookup with no client library needed —
`https://rdap.org/domain/<domain>` is a public bootstrap redirector that resolves to the right
registry's RDAP server, so this script never has to maintain its own IANA bootstrap table. Stdlib
only (urllib); no new dependency.

DEGRADES TO ABSENT, always: no network, a non-200, a redirect failure, or a payload this can't parse
all return `status: "unknown"` rather than raising or guessing. Callers (the triage skill) MUST
render "unknown" as UNCHECKED and never as "clean" — the absence of this signal is never evidence the
sender is legitimate. `lookup()` never raises; every failure path returns a result dict.

Usage:
  python domain_age.py example.com
  python domain_age.py example.com --json
  python domain_age.py example.com --timeout 5
  python domain_age.py example.com --rdap-base http://127.0.0.1:8080/domain   # test/mirror override

Prints a one-line render by default, `--json` for machine-readable. Exit 0 always — a lookup
failure is data for the caller to report as unchecked, not a script error.

Test coverage: test_domain_age.py drives fixture RDAP payloads plus a real localhost server
(_http_test_server.py, in this directory) -- there is no live network call anywhere in the suite.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_RDAP_BASE = "https://rdap.org/domain"
DEFAULT_TIMEOUT_SEC = 8.0
USER_AGENT = "seneschal-domain-age/1.0"


def parse_registration_date(payload: dict) -> str | None:
    """The RDAP `events` array's `registration` `eventDate`, verbatim (ISO 8601). `None` if the
    payload carries no such event — registries are not required to publish one."""
    for event in payload.get("events") or []:
        if event.get("eventAction") == "registration" and event.get("eventDate"):
            return event["eventDate"]
    return None


def age_days(iso_date: str, *, now: datetime | None = None) -> int | None:
    """Whole days between an ISO 8601 instant and `now` (UTC). `None` on anything unparseable —
    never raises, since a malformed date from a registry is still "unknown", not a crash."""
    try:
        registered = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if registered.tzinfo is None:
        registered = registered.replace(tzinfo=timezone.utc)
    reference = now if now is not None else datetime.now(timezone.utc)
    return max(0, (reference - registered).days)


def lookup(domain: str, *, timeout: float = DEFAULT_TIMEOUT_SEC,
           rdap_base: str = DEFAULT_RDAP_BASE) -> dict:
    """RDAP registration age for `domain`. Never raises — every failure path (network, HTTP,
    unparseable JSON, no registration event) returns `{"status": "unknown", ...}` rather than
    propagating, so a triage pass can call this unconditionally and still finish."""
    domain = (domain or "").strip().lower().rstrip(".")
    result = {"domain": domain, "status": "unknown", "registered_date": None,
              "age_days": None, "error": None}
    if not domain:
        result["error"] = "empty domain"
        return result

    req = urllib.request.Request(
        f"{rdap_base}/{domain}",
        headers={"Accept": "application/rdap+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        e.close()
        result["error"] = f"HTTP {e.code}"
        return result
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        result["error"] = str(e) or type(e).__name__
        return result

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        result["error"] = f"unparseable response: {e}"
        return result
    if not isinstance(payload, dict):
        result["error"] = "unparseable response: not an object"
        return result

    registered = parse_registration_date(payload)
    if not registered:
        result["error"] = "no registration event in response"
        return result

    result["status"] = "known"
    result["registered_date"] = registered
    result["age_days"] = age_days(registered)
    return result


def render(result: dict) -> str:
    if result["status"] != "known":
        return f"{result['domain']}: unknown (unchecked — {result['error']})"
    return f"{result['domain']}: registered {result['registered_date']} — {result['age_days']} days old"


def main() -> int:
    p = argparse.ArgumentParser(
        description="RDAP domain registration age (stdlib-only, fails open to 'unknown').")
    p.add_argument("domain", help="domain to look up, e.g. example.com")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC,
                    help="RDAP request timeout in seconds")
    p.add_argument("--rdap-base", default=DEFAULT_RDAP_BASE,
                    help="RDAP domain-lookup base URL (default: rdap.org's public bootstrap)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    result = lookup(args.domain, timeout=args.timeout, rdap_base=args.rdap_base)
    print(json.dumps(result) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
