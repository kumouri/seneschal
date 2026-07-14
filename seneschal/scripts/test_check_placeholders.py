"""Tests for the UUID placeholder CI guard."""
import unittest

import check_placeholders as cp


class TestPlaceholderPatterns(unittest.TestCase):
    def test_sanctioned_placeholders_pass(self):
        for u in (
            "00000000-0000-0000-0000-000000000000",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-0000000000ff",
            "00000000000000000000000000000002",
        ):
            self.assertTrue(cp.PLACEHOLDER.match(u.lower()), u)

    def test_real_shaped_uuids_fail(self):
        for u in (
            "12345678-1234-1234-1234-123456789abc",
            "00000000-0000-0000-0001-000000000000",  # non-zero in a leading group
            "deadbeefdeadbeefdeadbeefdeadbeef",
        ):
            self.assertFalse(cp.PLACEHOLDER.match(u.lower()), u)

    def test_normalize_compact_form(self):
        self.assertEqual(
            cp.normalize("DEADBEEFDEADBEEFDEADBEEFDEADBEEF"),
            "deadbeef-dead-beef-dead-beefdeadbeef",
        )
        self.assertEqual(
            cp.normalize("12345678-1234-1234-1234-123456789ABC"),
            "12345678-1234-1234-1234-123456789abc",
        )

    def test_compact_regex_ignores_longer_hex_runs(self):
        # sha256 digests (64 hex chars) must not be flagged as compact UUIDs
        sha = "a" * 64
        self.assertEqual(cp.COMPACT.findall(sha), [])
        self.assertEqual(len(cp.COMPACT.findall(f"x {'b' * 32} y")), 1)

    def test_hyphenated_regex_finds_uuid_in_line(self):
        line = "collection://12345678-1234-1234-1234-123456789abc trailing"
        self.assertEqual(
            cp.HYPHENATED.findall(line), ["12345678-1234-1234-1234-123456789abc"]
        )


if __name__ == "__main__":
    unittest.main()
