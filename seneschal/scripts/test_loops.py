#!/usr/bin/env python3
"""Tests for `loops.py` — the work-item register.

**What this suite is guarding, in one sentence each.** The refusals in `loops.py` are not input
validation: each one is a mechanism, and each is the kind of thing a later edit removes while every
other test stays green.

* **`TerminalStateIsTheMembershipTest`** — *can you name the event that would close this?* decides
  whether something is a work item, and the schema refusing without it **is** the test. A default, a
  `--no-terminal-state`, or an auto-filled `"unknown"` deletes it.
* **`OwnerOnlyWrites`** — the owner's three writes need a **distinct mutation that does not exist on
  the assistant's side**, not a shared one that checks its caller. These read the module's own
  SOURCE, because the property is an ABSENCE and no input can demonstrate one. **A `set --as-owner`
  flag would pass every behavioural test in this file**, so the source scan is the only shape that
  can catch it.
* **`TheGuardedWrite`** — `state/carry-over.md` gets a generated region appended below its
  hand-written content. Each of the four refusals gets a test, plus the post-write restore, plus the
  byte-exact survival of the hand-written half, plus line-ending preservation.
* **`TheScopeLine`** — a partial pull must stop being silent and become arithmetic that does not add
  up. The counts and the filter statement are the product, not decoration.
* **`TheDormantOverlay`** — every projection must apply the overlay **or say it did not**, and the
  render threshold is unset. The suite pins that a threshold-less render SAYS SO.
* **`TheTripwireIsNotAQuota`** — a cap that drops a record destroys the only evidence it was ever
  open.
* **`TheVocabularyIsRuled`** — `retired` is gone and `dormant` is never stored. A later edit putting
  either back is a vocabulary change, not tidying up.
* **`TheProjectionIsGated`** — the register -> Notion Tasks projection is Notion-backend-only and a
  no-op until `outbox_common` provides the `task_status` op.

Every test passes an explicit `--state-dir`: a state-defaulting flag turns an old test into a live
writer against the daemon's own store. `Base` also pins `loops.store_backend` to `notion` so no test
reads the real `store/config.json`; `TheProjectionIsGated` exercises the real resolver against temp
files.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loops  # noqa: E402
import memory_write  # noqa: E402
import outbox_common as ob  # noqa: E402 — the register -> Notion Tasks projection's forward tests

try:  # the CI guard against truncating state/ writes lands in a later wave
    import check_state_writes  # noqa: E402
except ImportError:
    check_state_writes = None


def _slurp(path: str, mode: str = "r", **kwargs):
    """Read a whole file and close it (no ResourceWarning from a dangling handle)."""
    if "b" not in mode:
        kwargs.setdefault("encoding", "utf-8")
    with io.open(path, mode, **kwargs) as fh:
        return fh.read()


LOOPS_SOURCE = _slurp(loops.__file__)

#: The `task_status` outbox op (and `task_status_key`) lands in wave 26; until then the projection is
#: a no-op and every test that asserts an enqueue skips.
HAS_TASK_STATUS = hasattr(ob, "task_status_key")
TASK_STATUS_SEAM = "the task_status outbox op lands in wave 26"

HAND_WRITTEN = (
    "# Carry-over — the assistant's running open loops\n"
    "\n"
    "## Open follow-ups (the assistant's self-tasks)\n"
    "\n"
    "- **2026-08-02 (Chat):** a hand-written line nothing may touch — µ ✅ 🧷\n"
)

GOOD = dict(
    text="Add a toggle to collapse recently-finished jobs in the cockpit's Jobs panel",
    audience="owner",
    terminal_state="the toggle ships or the owner says drop it",
    whose_move="assistant",
    next_action="add the collapse handler to the Jobs panel",
    kind="enhancement",
    priority="normal",
)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        backend = mock.patch.object(loops, "store_backend", return_value="notion")
        backend.start()
        self.addCleanup(backend.stop)

    def seed_carry_over(self, text: str = HAND_WRITTEN, newline: str = "\n") -> str:
        path = loops.carry_over_path(self.state)
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace("\n", newline))
        return path

    def add(self, **over):
        kwargs = dict(GOOD)
        kwargs.update(over)
        return loops.add(self.state, **kwargs)

    def read(self, path: str) -> bytes:
        return _slurp(path, "rb")

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = loops.main(["--state-dir", self.state, *argv])
        return code, out.getvalue(), err.getvalue()

    def seed_tasks_pointer(self, item_id: str, notion_page_id: str) -> None:
        """Write `owi-migration-map.json` directly — the correlation an importer would leave behind.
        Only the shape `loops._tasks_notion_page_id` reads matters: a `tasks:<page id>` key ->
        `{"loop_id": …}`."""
        path = os.path.join(self.state, "owi-migration-map.json")
        data = {"schema": "seneschal.owi-migration-map/1",
                "entries": {f"tasks:{notion_page_id}": {"loop_id": item_id, "surface": "tasks",
                                                         "migrated_at": "2026-09-14T15:18:00+00:00"}}}
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def outbox_entry(self, page_id: str, status: str):
        conn = ob.connect(self.state)
        try:
            return ob.get_by_key(conn, ob.task_status_key(page_id, status))
        finally:
            conn.close()


# --------------------------------------------------------------------------- the membership test

class TerminalStateIsTheMembershipTest(Base):
    """*"A writer who cannot fill it has been told by the schema that the item is not a work item."*
    The refusal IS the membership test."""

    def test_add_refuses_without_a_terminal_state(self):
        for missing in (None, "", "   "):
            with self.subTest(missing=missing):
                with self.assertRaises(loops.LoopsError) as ctx:
                    self.add(terminal_state=missing)
                self.assertIn("membership test", str(ctx.exception))
                self.assertIn("notes", str(ctx.exception))

    def test_nothing_is_written_when_the_membership_test_is_refused(self):
        with self.assertRaises(loops.LoopsError):
            self.add(terminal_state=None)
        self.assertFalse(os.path.exists(loops.store_path(self.state)))

    def test_the_cli_makes_it_a_required_flag_too(self):
        # argparse exits 2 on a missing required flag; the point is that there is no default and no
        # opt-out, so the test asserts BOTH doors rather than trusting the library one alone.
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                loops.main(["--state-dir", self.state, "add", "--text", "x",
                            "--audience", "owner", "--whose-move", "assistant",
                            "--next-action", "x", "--kind", "chore", "--priority", "low"])
        self.assertNotIn("--no-terminal-state", LOOPS_SOURCE)


class TheMandatoryOwiFields(Base):
    """MANDATORY on write, with `unknown` LEGAL. Both halves matter — the schema refuses to let the
    field be forgotten, and backfill costs nothing because an existing row is honestly `unknown`
    rather than falsely attributed."""

    def test_whose_move_and_next_action_are_refused_when_absent(self):
        with self.assertRaises(loops.LoopsError):
            self.add(whose_move=None)
        with self.assertRaises(loops.LoopsError):
            self.add(next_action="")

    def test_unknown_is_a_legal_value_on_every_one_of_them(self):
        item = self.add(whose_move="unknown", next_action="unknown",
                        kind="unknown", priority="unknown")
        self.assertEqual(item["whose_move"], "unknown")
        self.assertEqual(item["next_action"], "unknown")
        self.assertEqual(item["kind_assistant"], "unknown")
        self.assertEqual(item["priority_assistant"], "unknown")

    def test_an_out_of_vocabulary_value_is_refused_and_says_the_vocabulary_is_ruled(self):
        for field, bad in (("whose_move", "someone"), ("kind", "feature"),
                           ("priority", "p1"), ("audience", "both")):
            with self.subTest(field=field):
                with self.assertRaises(loops.LoopsError) as ctx:
                    self.add(**{field: bad})
                self.assertIn("RULED", str(ctx.exception))

    def test_the_eight_owi_fields_are_all_on_a_new_record(self):
        item = self.add()
        for field in ("whose_move", "next_action", "blocked_by", "last_touched",
                      "kind_assistant", "kind_owner", "priority_assistant", "priority_owner"):
            self.assertIn(field, item, field)


# --------------------------------------------------------------------------- the owner's three

class OwnerOnlyWrites(Base):
    """The three writes that are the owner's alone, enforced by the SCHEMA.

    *"The assistant sets the owner's priority"* and *"the assistant declares a thing abandoned"* must
    have **no code path to travel**, rather than being refused at the door. **A caller check inside a
    shared mutation is NOT this**, and neither is a `set` verb with an `--as-owner` flag — both would
    pass every behavioural test in this file, which is why these read the source."""

    ASSISTANT_VERBS = ("add", "carry", "hold", "start", "observe", "mark_observation_complete",
                       "resolve", "drop", "update", "raise_item")
    OWNER_FIELDS = ("kind_owner", "priority_owner")

    #: The scan is over the AST and never over the file's PROSE: `drop`'s docstring names `abandoned`
    #: **in the sentence that refuses it**, and `drop`'s own refusal message quotes the word back to
    #: the caller. A string search would go red on the documentation of the rule it is checking. So
    #: the question asked here is the precise one: **does any statement in an assistant-side verb
    #: ASSIGN one of these?**
    def _fn(self, name: str):
        import ast
        tree = ast.parse(LOOPS_SOURCE)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"loops.py has no top-level function {name}")

    def _subscript_assignments(self, name: str):
        """`(key, value_node)` for every `something["key"] = value` in the function."""
        import ast
        out = []
        for node in ast.walk(self._fn(name)):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    out.append((target.slice.value, node.value))
        return out

    def _dict_literal_keys(self, name: str):
        """`{key: value_node}` for every dict literal in the function — `add`'s record."""
        import ast
        out = {}
        for node in ast.walk(self._fn(name)):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        out[k.value] = v
        return out

    def test_no_assistant_side_verb_assigns_either_of_the_owners_two_fields(self):
        for verb in self.ASSISTANT_VERBS:
            for key, _ in self._subscript_assignments(verb):
                self.assertNotIn(key, self.OWNER_FIELDS,
                                 f"{verb} assigns {key} — the code path must not exist")

    def test_add_initialises_the_owners_to_none_and_never_from_the_assistants_value(self):
        """The assistant's value may never pre-fill, seed, default to, or overwrite the owner's, and
        the ONE place it could is the record literal `add` builds."""
        import ast
        keys = self._dict_literal_keys("add")
        for field in self.OWNER_FIELDS:
            self.assertIn(field, keys, f"add's record is missing {field}")
            value = keys[field]
            self.assertTrue(isinstance(value, ast.Constant) and value.value is None,
                            f"add sets {field} to something other than a literal None — "
                            f"an unset value is DATA and pre-filling it fabricates assent")

    def test_no_assistant_side_verb_can_write_the_abandoned_status(self):
        import ast
        for verb in self.ASSISTANT_VERBS:
            for key, value in self._subscript_assignments(verb):
                if key != "status":
                    continue
                self.assertTrue(isinstance(value, ast.Constant),
                                f"{verb} writes a non-literal status — it must be readable here")
                self.assertNotEqual(value.value, "abandoned",
                                    f"{verb} writes `abandoned` — it is the OWNER'S ALONE")

    def test_add_opens_a_record_as_open_and_nothing_else(self):
        import ast
        status = self._dict_literal_keys("add").get("status")
        self.assertTrue(isinstance(status, ast.Constant) and status.value == "open",
                        "a new work item opens as `open`")

    def test_only_the_three_dedicated_mutations_write_the_three_owner_only_things(self):
        """The complement of the tests above, so the scan cannot pass by the fields having moved
        somewhere this suite does not look."""
        import ast
        writers = {"kind_owner": set(), "priority_owner": set(), "status:abandoned": set()}
        tree = ast.parse(LOOPS_SOURCE)
        for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if not (isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)):
                        continue
                    key = target.slice.value
                    if key in ("kind_owner", "priority_owner"):
                        writers[key].add(fn.name)
                    if (key == "status" and isinstance(node.value, ast.Constant)
                            and node.value.value == "abandoned"):
                        writers["status:abandoned"].add(fn.name)
        self.assertEqual(writers["kind_owner"], {"owner_kind"})
        self.assertEqual(writers["priority_owner"], {"owner_priority"})
        self.assertEqual(writers["status:abandoned"], {"owner_abandon"})

    def test_the_assistants_verbs_expose_no_argparse_option_reaching_the_owners_fields(self):
        for verb in ("add", "carry", "hold", "start", "observe", "resolve", "drop", "update"):
            code, out, err = self.cli_help(verb)
            self.assertEqual(code, 0)
            for banned in ("--as-owner", "--owner", "--kind-owner", "--priority-owner",
                           "--owner-kind", "--owner-priority", "--abandon", "--status"):
                self.assertNotIn(banned, err + out, f"{verb} exposes {banned}")

    def cli_help(self, verb):
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                loops.main([verb, "--help"])
        except SystemExit as exc:
            return int(exc.code or 0), out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_there_is_no_shared_set_verb(self):
        # The exact shape refused: a single `set` verb with an `--as-owner` flag, or a caller check
        # inside a shared mutation.
        self.assertNotIn('sub.add_parser("set"', LOOPS_SOURCE)
        self.assertNotIn("as_owner", LOOPS_SOURCE)

    def test_the_three_dedicated_mutations_do_write_them(self):
        item = self.add()
        loops.owner_kind(self.state, item_id=item["id"], kind="decision")
        loops.owner_priority(self.state, item_id=item["id"], priority="high")
        got = loops.get(loops.load(self.state), item["id"])
        self.assertEqual(got["kind_owner"], "decision")
        self.assertEqual(got["priority_owner"], "high")

    def test_owner_abandon_is_terminal_and_takes_no_reason(self):
        item = self.add()
        loops.owner_abandon(self.state, item_id=item["id"])
        self.assertEqual(loops.get(loops.load(self.state), item["id"])["status"], "abandoned")
        # `--because` is impossible by construction: the absence of a reason is the whole content of
        # the value. The CLI must not accept one — argparse exits 2 on the unknown flag.
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                loops.main(["--state-dir", self.state, "owner-abandon", item["id"],
                            "--because", "it rotted"])

    def test_an_unset_owner_field_is_data_and_is_never_prefilled_from_the_assistants(self):
        item = self.add(kind="bug", priority="critical")
        self.assertIsNone(item["kind_owner"])
        self.assertIsNone(item["priority_owner"])
        # And an absent key reads identically to an explicit null, so a record written either way
        # means the same thing.
        self.assertTrue(loops.unset(item, "priority_owner"))
        self.assertTrue(loops.unset({}, "priority_owner"))

    def test_the_render_never_falls_back_to_the_assistants_value_for_the_owners(self):
        # A rendering that shows one priority column is the same merge performed at the last moment.
        item = self.add(priority="high", kind="bug")
        loops.carry(self.state, item_id=item["id"], because="still live")
        region = loops.render_region(loops.load(self.state))
        self.assertIn("priority: high", region)
        self.assertNotIn("OWNER priority", region)
        loops.owner_priority(self.state, item_id=item["id"], priority="low")
        region = loops.render_region(loops.load(self.state))
        self.assertIn("OWNER priority: low", region)


class DropAndAbandonAreDifferentClaims(Base):
    """`dropped` means *someone decided* and carries the reason; `abandoned` means *it is over and
    nobody ever decided*. Recording a decision nobody made permanently pollutes the count of real
    drops."""

    def test_drop_requires_a_reason(self):
        item = self.add()
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.drop(self.state, item_id=item["id"], because=None)
        self.assertIn("SOMEONE DECIDED", str(ctx.exception))

    def test_drop_sets_a_status_and_does_not_remove_the_record(self):
        # "Not in the carry-over" never means "destroy it", and terminal records are kept so the
        # pile rate stays re-measurable.
        item = self.add()
        loops.drop(self.state, item_id=item["id"], because="superseded by a newer item")
        store = loops.load(self.state)
        self.assertIn(item["id"], store["items"])
        self.assertEqual(store["items"][item["id"]]["status"], "dropped")


class StartMovesALiveRecordToInProgress(Base):
    """`start()` writes `in_progress` — the row actually being worked."""

    def test_start_sets_in_progress_and_touches(self):
        item = self.add()
        earlier = (loops._parse_stamp(item["last_touched"]) - timedelta(days=1)).isoformat(
            timespec="seconds")
        store = loops.load(self.state)
        store["items"][item["id"]]["last_touched"] = earlier
        loops.save(store, self.state)
        got = loops.start(self.state, item_id=item["id"])
        self.assertEqual(got["status"], "in_progress")
        self.assertNotEqual(got["last_touched"], earlier)

    def test_start_refuses_on_each_terminal_status(self):
        for verb, kwargs in ((loops.resolve, {"because": "shipped"}),
                             (loops.drop, {"because": "superseded"}),
                             (loops.owner_abandon, {})):
            item = self.add()
            verb(self.state, item_id=item["id"], **kwargs)
            with self.assertRaises(loops.LoopsError) as ctx:
                loops.start(self.state, item_id=item["id"])
            self.assertIn("terminal", str(ctx.exception))

    def test_resolve_from_in_progress_is_an_ordinary_transition(self):
        item = self.add()
        loops.start(self.state, item_id=item["id"])
        got = loops.resolve(self.state, item_id=item["id"], because="shipped")
        self.assertEqual(got["status"], "done")

    def test_held_and_paused_records_may_also_start(self):
        item = self.add()
        loops.hold(self.state, item_id=item["id"], because="unverifiable")
        got = loops.start(self.state, item_id=item["id"])
        self.assertEqual(got["status"], "in_progress")

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_start_forwards_in_progress(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.start(self.state, item_id=item["id"])
        self.assertIsNotNone(self.outbox_entry("page-1", "In Progress"))

    def test_forward_false_enqueues_nothing(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.start(self.state, item_id=item["id"], forward=False)
        self.assertFalse(os.path.exists(ob.db_path(self.state)))

    def test_because_is_optional_and_lands_in_notes(self):
        item = self.add()
        got = loops.start(self.state, item_id=item["id"], because="picked this up")
        self.assertEqual(got["notes"], "picked this up")

    def test_cli_start_round_trips(self):
        item = self.add()
        code, out, _ = self.cli("start", item["id"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "in_progress")

    def test_cli_start_refuses_a_terminal_record(self):
        item = self.add()
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        code, _, err = self.cli("start", item["id"])
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)


class ObserveRequiresAGate(Base):
    """`observation` is the one status that REQUIRES a machine-checkable exit condition, refused
    without one (`observation-gate-spec.md`)."""

    def test_observe_refuses_without_requires(self):
        item = self.add()
        for bad in (None, [], "not-a-list"):
            with self.assertRaises(loops.LoopsError) as ctx:
                loops.observe(self.state, item_id=item["id"], requires=bad)
            self.assertIn("gate", str(ctx.exception))

    def test_observe_refuses_an_unknown_requirement_type(self):
        item = self.add()
        with self.assertRaises(loops.LoopsError):
            loops.observe(self.state, item_id=item["id"],
                          requires=[{"type": "vibes", "days": 1}])

    def test_observe_refuses_a_requirement_missing_its_own_required_field(self):
        item = self.add()
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.observe(self.state, item_id=item["id"], requires=[{"type": "min_elapsed"}])
        self.assertIn("days", str(ctx.exception))

    def test_observe_sets_status_and_stamps_the_gate(self):
        item = self.add()
        got = loops.observe(self.state, item_id=item["id"],
                            requires=[{"type": "min_elapsed", "days": 14}])
        self.assertEqual(got["status"], "observation")
        self.assertEqual(got["gate"]["kind"], "observation")
        self.assertIsNotNone(got["gate"]["started_at"])
        self.assertIsNone(got["gate"]["met_at"])
        self.assertEqual(got["gate"]["requires"], [{"type": "min_elapsed", "days": 14}])

    def test_observe_refuses_on_a_terminal_record(self):
        item = self.add()
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.observe(self.state, item_id=item["id"],
                          requires=[{"type": "min_elapsed", "days": 1}])
        self.assertIn("terminal", str(ctx.exception))

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_observe_forwards_observation(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.observe(self.state, item_id=item["id"], requires=[{"type": "min_elapsed", "days": 1}])
        self.assertIsNotNone(self.outbox_entry("page-1", "Paused"))

    def test_cli_observe_round_trips(self):
        item = self.add()
        code, out, _ = self.cli("observe", item["id"],
                                "--requires", '[{"type": "min_elapsed", "days": 1}]')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "observation")

    def test_cli_observe_refuses_bad_json(self):
        item = self.add()
        code, _, err = self.cli("observe", item["id"], "--requires", "not json")
        self.assertEqual(code, 2)
        self.assertIn("JSON", err)


class MinElapsedSince(Base):
    """`started_at` is always stamped NOW by `observe()`, so a gate for evidence that predates its
    own attachment could never read met. `since` gives that ONE requirement its own clock start;
    `started_at` is untouched."""

    def test_a_backdated_since_is_accepted_and_stored_verbatim(self):
        item = self.add()
        since = "2026-08-20"
        got = loops.observe(self.state, item_id=item["id"],
                            requires=[{"type": "min_elapsed", "days": 14, "since": since}])
        self.assertEqual(got["gate"]["requires"][0]["since"], since)
        # `started_at` still means "when this gate was attached" — never backdated itself.
        self.assertNotEqual(got["gate"]["started_at"][:10], since)

    def test_a_future_since_is_refused(self):
        item = self.add()
        future = (loops._now() + timedelta(days=5)).date().isoformat()
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.observe(self.state, item_id=item["id"],
                          requires=[{"type": "min_elapsed", "days": 14, "since": future}])
        self.assertIn("future", str(ctx.exception))

    def test_an_unparseable_since_is_refused(self):
        item = self.add()
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.observe(self.state, item_id=item["id"],
                          requires=[{"type": "min_elapsed", "days": 14, "since": "not-a-date"}])
        self.assertIn("since", str(ctx.exception))

    def test_omitting_since_behaves_exactly_as_before(self):
        item = self.add()
        got = loops.observe(self.state, item_id=item["id"],
                            requires=[{"type": "min_elapsed", "days": 14}])
        self.assertNotIn("since", got["gate"]["requires"][0])

    def test_since_on_a_non_min_elapsed_requirement_is_not_validated(self):
        # `since` is min_elapsed-specific; a stray `since` on another type is inert, not a refusal —
        # `observation_gate.py` never reads it off anything but `min_elapsed`.
        item = self.add()
        got = loops.observe(self.state, item_id=item["id"],
                            requires=[{"type": "manual", "note": "?", "since": "not-a-date"}])
        self.assertEqual(got["status"], "observation")


class MarkObservationComplete(Base):
    """`mark_observation_complete()` — the one mutation `observation_gate.py`'s scanner calls, and
    only after finding a gate fully met. Not on `loops.py`'s own CLI."""

    def _observed(self, **gate_kwargs):
        item = self.add()
        return loops.observe(self.state, item_id=item["id"],
                             requires=[{"type": "min_elapsed", "days": 1}], **gate_kwargs)

    def test_refuses_off_a_non_observation_record(self):
        item = self.add()
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.mark_observation_complete(self.state, item_id=item["id"])
        self.assertIn("observation", str(ctx.exception))

    def test_sets_observation_complete_and_stamps_met_at(self):
        item = self._observed()
        got = loops.mark_observation_complete(self.state, item_id=item["id"])
        self.assertEqual(got["status"], "observation-complete")
        self.assertIsNotNone(got["gate"]["met_at"])

    def test_whose_move_defaults_to_both(self):
        item = self._observed()
        got = loops.mark_observation_complete(self.state, item_id=item["id"])
        self.assertEqual(got["whose_move"], "both")

    def test_whose_move_on_complete_overrides_the_default(self):
        item = self._observed(whose_move_on_complete="owner")
        got = loops.mark_observation_complete(self.state, item_id=item["id"])
        self.assertEqual(got["whose_move"], "owner")

    def test_last_touched_equals_met_at_when_nothing_follows_up(self):
        # `observation_gate`'s staleness read depends on this equality holding immediately after
        # the flip.
        item = self._observed()
        got = loops.mark_observation_complete(self.state, item_id=item["id"])
        self.assertEqual(got["last_touched"], got["gate"]["met_at"])

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_forwards_observation_complete(self):
        item = self._observed()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.mark_observation_complete(self.state, item_id=item["id"])
        self.assertIsNotNone(self.outbox_entry("page-1", "Not Started"))

    def test_it_is_not_on_the_cli(self):
        self._observed()
        with self.assertRaises(SystemExit):
            self.cli("mark-observation-complete", "whatever")


class ObservationCompleteRendersLikeInProgress(Base):
    """`project()`/`default_query()` treat a freshly-resurfaced gate as worth leading with, the same
    tier as `in_progress`; `observation` itself stays withheld, same as `held`/`paused`."""

    def test_observation_is_withheld_from_the_projection(self):
        item = self.add()
        loops.observe(self.state, item_id=item["id"], requires=[{"type": "min_elapsed", "days": 1}])
        store = loops.load(self.state)
        view = loops.project(store)
        self.assertEqual(view["shown"], [])
        self.assertIn("status=observation", view["withheld"])

    def test_observation_complete_is_shown_and_sorted_first(self):
        older = self.add(text="an ordinary open item")
        loops.carry(self.state, item_id=older["id"], because="still relevant")
        gated = self.add(text="a gate just closed")
        loops.observe(self.state, item_id=gated["id"],
                      requires=[{"type": "min_elapsed", "days": 1}])
        loops.mark_observation_complete(self.state, item_id=gated["id"])
        store = loops.load(self.state)
        view = loops.project(store)
        ids = [item["id"] for item in view["shown"]]
        self.assertEqual(ids[0], gated["id"])

    def test_observation_complete_is_not_terminal(self):
        self.assertNotIn("observation-complete", loops.TERMINAL_STATUSES)

    def test_moving_on_from_observation_complete_is_an_ordinary_transition(self):
        item = self.add()
        loops.observe(self.state, item_id=item["id"], requires=[{"type": "min_elapsed", "days": 1}])
        loops.mark_observation_complete(self.state, item_id=item["id"])
        got = loops.start(self.state, item_id=item["id"])
        self.assertEqual(got["status"], "in_progress")


# --------------------------------------------------------------------------- the store

class TheStore(Base):
    def test_a_missing_store_is_empty_and_a_malformed_one_is_a_refusal(self):
        self.assertEqual(loops.load(self.state)["items"], {})
        with io.open(loops.store_path(self.state), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.load(self.state)
        self.assertIn("REFUSING", str(ctx.exception))

    def test_a_wrong_shape_is_refused_rather_than_overwritten(self):
        with io.open(loops.store_path(self.state), "w", encoding="utf-8") as fh:
            fh.write('{"schema": "seneschal.open-loops/1"}')
        with self.assertRaises(loops.LoopsError):
            loops.load(self.state)

    @unittest.skipIf(check_state_writes is None, "check_state_writes lands in wave 29")
    def test_the_store_is_written_through_memory_write_and_never_open_w(self):
        """The check is run through **`check_state_writes.py` itself** rather than by grepping for
        `open(`: a second spelling of *"is this a truncating write?"* drifts from the one that gates
        CI, and this module's own docstrings name the forbidden call in the sentences refusing it —
        where a string search goes red on the documentation of the rule it is checking."""
        findings, _ = check_state_writes.scan_source(LOOPS_SOURCE, "seneschal/scripts/loops.py")
        self.assertEqual([f.symbol for f in findings], [])
        self.assertIn("memory_write.write_text", LOOPS_SOURCE)

    def test_ids_do_not_collide_on_one_day(self):
        a = self.add()
        b = self.add()
        self.assertNotEqual(a["id"], b["id"])

    def test_an_explicit_id_is_never_silently_reused(self):
        self.add(item_id="loop-20260901-x")
        with self.assertRaises(loops.LoopsError):
            self.add(item_id="loop-20260901-x")


class TheVocabularyIsRuled(Base):
    def test_nine_values_are_stored_and_ten_are_rendered(self):
        self.assertEqual(len(loops.STORED_STATUSES), 9)
        self.assertEqual(len(loops.RENDERED_STATUSES), 10)
        self.assertIn("in_progress", loops.STORED_STATUSES)
        self.assertIn("observation", loops.STORED_STATUSES)
        self.assertIn("observation-complete", loops.STORED_STATUSES)
        self.assertNotIn("dormant", loops.STORED_STATUSES)
        self.assertIn("dormant", loops.RENDERED_STATUSES)

    def test_retired_is_gone_and_putting_it_back_is_a_vocabulary_change(self):
        # This test is the thing that makes restoring it a conversation instead of a commit.
        self.assertNotIn("retired", loops.STORED_STATUSES)
        self.assertNotIn("retired", loops.RENDERED_STATUSES)

    def test_blocked_is_not_a_status(self):
        # A blocked item's status is `open`; being blocked is what `blocked_by` says and who it waits
        # on is what `whose_move` says.
        self.assertNotIn("blocked", loops.RENDERED_STATUSES)

    def test_there_is_no_status_assistant_or_status_owner(self):
        item = self.add()
        self.assertNotIn("status_assistant", item)
        self.assertNotIn("status_owner", item)

    def test_seven_kinds_and_five_priorities_no_more(self):
        self.assertEqual(len(loops.KINDS), 7)
        self.assertEqual(len(loops.PRIORITIES), 5)
        self.assertIn("unknown", loops.KINDS)
        self.assertIn("unknown", loops.PRIORITIES)


# --------------------------------------------------------------------------- the projection

class TheProjectionFilter(Base):
    """*"`loops.py render` emits only records that are `open` and carry a non-null `carried_reason`,
    or that were opened since the last rebuild."* The inverted default is the whole mechanism — a
    record whose reason was never written is dropped by arithmetic, not by diligence."""

    def test_a_brand_new_record_renders_without_a_carried_reason(self):
        item = self.add()
        view = loops.project(loops.load(self.state))
        self.assertEqual([r["id"] for r in view["shown"]], [item["id"]])

    def _stamp_rebuild_after_the_items(self):
        """Move `last_rendered` past every record, deterministically.

        A real second render would do this, but the stamps are second-resolution and `_is_new_since`
        answers YES on a tie — deliberately, so an item added in the same second as a rebuild is
        shown rather than lost. Here the fixture steps past it so the *carried_reason* arm is what is
        being measured."""
        store = loops.load(self.state)
        latest = max((r["opened"] for r in store["items"].values()), default=None)
        store["last_rendered"] = (loops._parse_stamp(latest) + timedelta(seconds=1)).isoformat(
            timespec="seconds")
        loops.save(store, self.state)

    def test_after_a_rebuild_a_record_with_no_carried_reason_drops(self):
        self.seed_carry_over()
        item = self.add()
        loops.write_render(self.state)
        self._stamp_rebuild_after_the_items()
        # A second render: the item is no longer new, and nobody named a reason to carry it.
        view = loops.project(loops.load(self.state))
        self.assertEqual(view["shown"], [])
        self.assertEqual(view["withheld"].get("no carried_reason"), 1)
        self.assertIn(item["id"], loops.load(self.state)["items"])  # dropped from the VIEW only

    def test_a_tie_on_the_rebuild_stamp_shows_the_item_rather_than_losing_it(self):
        # A redundantly-shown work item costs one line; a silently-dropped one is the failure the
        # register exists to prevent.
        self.seed_carry_over()
        item = self.add()
        store = loops.load(self.state)
        store["last_rendered"] = store["items"][item["id"]]["opened"]
        loops.save(store, self.state)
        self.assertEqual([r["id"] for r in loops.project(loops.load(self.state))["shown"]],
                         [item["id"]])

    def test_carrying_it_with_a_reason_keeps_it(self):
        self.seed_carry_over()
        item = self.add()
        loops.write_render(self.state)
        loops.carry(self.state, item_id=item["id"], because="still waiting on the Jobs panel")
        self._stamp_rebuild_after_the_items()
        view = loops.project(loops.load(self.state))
        self.assertEqual([r["id"] for r in view["shown"]], [item["id"]])

    def test_the_other_audience_is_withheld_and_counted(self):
        self.add(audience="assistant")
        view = loops.project(loops.load(self.state), audience="owner")
        self.assertEqual(view["shown"], [])
        self.assertEqual(view["withheld"]["audience=assistant"], 1)

    def test_held_is_withheld_and_pointed_at_rather_than_shown(self):
        item = self.add()
        loops.hold(self.state, item_id=item["id"], because="cannot verify it is still live")
        view = loops.project(loops.load(self.state))
        self.assertEqual(view["shown"], [])
        self.assertEqual(view["withheld"]["status=held"], 1)
        self.assertIn("status=held (awaiting a decision)",
                      loops.render_region(loops.load(self.state)))

    def test_in_progress_is_shown_same_as_open(self):
        item = self.add()
        loops.start(self.state, item_id=item["id"])
        view = loops.project(loops.load(self.state))
        self.assertEqual([r["id"] for r in view["shown"]], [item["id"]])

    def test_in_progress_rows_render_first(self):
        # The row actually being worked leads the list.
        self.seed_carry_over()
        first = self.add(text="opened first")
        second = self.add(text="opened second, then started")
        loops.write_render(self.state)
        loops.carry(self.state, item_id=first["id"], because="still relevant")
        loops.carry(self.state, item_id=second["id"], because="still relevant")
        loops.start(self.state, item_id=second["id"])
        view = loops.project(loops.load(self.state))
        self.assertEqual([r["id"] for r in view["shown"]], [second["id"], first["id"]])

    def test_terminal_records_are_retained_but_never_rendered(self):
        for verb, kwargs in ((loops.resolve, {"because": "shipped"}),
                             (loops.drop, {"because": "superseded"})):
            item = self.add()
            verb(self.state, item_id=item["id"], **kwargs)
        store = loops.load(self.state)
        self.assertEqual(len(store["items"]), 2)
        self.assertEqual(loops.project(store)["shown"], [])

    def test_hold_stamps_the_clock_the_holding_bound_is_read_off(self):
        item = self.add()
        loops.hold(self.state, item_id=item["id"], because="unverifiable")
        got = loops.get(loops.load(self.state), item["id"])
        self.assertIsNotNone(got["held_since"])
        self.assertEqual(loops.held_count(loops.load(self.state)), 1)


class TheScopeLine(Base):
    """*"The artifact states the filter that produced it"*, and *"what is missing is visible as a
    NUMBER even though its content is not."*"""

    def test_it_names_the_source_the_schema_and_the_filter(self):
        self.add()
        region = loops.render_region(loops.load(self.state))
        self.assertIn("state/open-loops.json", region)
        self.assertIn(loops.SCHEMA, region)
        self.assertIn("filter: audience=owner, status=open, in_progress, or observation-complete",
                      region)

    def test_it_counts_what_it_withheld(self):
        self.add(audience="assistant")
        self.add(audience="assistant", text="another assistant-facing item")
        self.add()
        region = loops.render_region(loops.load(self.state))
        self.assertIn("showing 1 of 3 live records", region)
        self.assertIn("2 audience=assistant", region)

    def test_an_empty_projection_still_states_its_filter(self):
        region = loops.render_region(loops.load(self.state))
        self.assertIn("filter:", region)
        self.assertIn("No work items match", region)


class TheDormantOverlay(Base):
    """*"Every projection must apply the overlay or under-report."* With no threshold the render
    **says the overlay did not run**, so the under-report is on the page as a sentence instead of
    being invisible."""

    def test_the_threshold_has_no_default_anywhere(self):
        self.assertIn('"--dormant-after-days", type=int, default=None', LOOPS_SOURCE)
        self.assertIs(loops.project(loops.load(self.state))["dormant_after_days"], None)

    def test_without_a_threshold_the_scope_line_says_the_overlay_did_not_run(self):
        self.add()
        region = loops.render_region(loops.load(self.state))
        self.assertIn("dormant overlay: NOT APPLIED", region)
        self.assertIn("threshold is unset and is the owner's", region)

    def test_with_a_threshold_a_stale_record_leaves_the_view_and_is_counted(self):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        item = self.add()
        store = loops.load(self.state)
        store["items"][item["id"]]["last_touched"] = old.isoformat(timespec="seconds")
        loops.save(store, self.state)
        view = loops.project(loops.load(self.state), dormant_after_days=90)
        self.assertEqual(view["shown"], [])
        self.assertEqual(view["withheld"]["status=dormant"], 1)
        region = loops.render_region(loops.load(self.state), dormant_after_days=90)
        self.assertIn("dormant overlay: applied at 90 days", region)

    def test_dormant_is_never_written_to_a_record(self):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        item = self.add()
        store = loops.load(self.state)
        store["items"][item["id"]]["last_touched"] = old.isoformat(timespec="seconds")
        loops.save(store, self.state)
        loops.project(loops.load(self.state), dormant_after_days=1)
        self.assertEqual(loops.load(self.state)["items"][item["id"]]["status"], "open")

    def test_a_touch_leaves_dormant_by_arithmetic(self):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        item = self.add()
        store = loops.load(self.state)
        store["items"][item["id"]]["last_touched"] = old.isoformat(timespec="seconds")
        loops.save(store, self.state)
        self.assertTrue(loops.is_dormant(loops.get(loops.load(self.state), item["id"]), 90))
        loops.update(self.state, item_id=item["id"], next_action="pick it back up")
        self.assertFalse(loops.is_dormant(loops.get(loops.load(self.state), item["id"]), 90))


class TheTripwireIsNotAQuota(Base):
    """*"Tripwire, not a quota: if the rebuilt list runs much past ~15 items, the rebuild probably
    didn't happen."* Dropping a record to satisfy a cap destroys the only evidence it was ever open."""

    def test_past_fifteen_it_reports_and_drops_nothing(self):
        for n in range(loops.PROJECTION_TRIPWIRE + 3):
            self.add(text=f"work item number {n}", item_id=f"loop-20260901-item-{n:02d}")
        view = loops.project(loops.load(self.state))
        self.assertEqual(len(view["shown"]), loops.PROJECTION_TRIPWIRE + 3)
        self.assertTrue(view["tripwire"])
        region = loops.render_region(loops.load(self.state))
        self.assertIn("TRIPWIRE", region)
        self.assertIn("nothing was dropped", region.lower())
        for n in range(loops.PROJECTION_TRIPWIRE + 3):
            self.assertIn(f"loop-20260901-item-{n:02d}", region)

    def test_no_truncation_appears_anywhere_in_the_render_path(self):
        # A [:15] slice would satisfy "you never see more than ~15" and is the edit this forbids.
        self.assertNotIn("[:PROJECTION_TRIPWIRE]", LOOPS_SOURCE)
        self.assertNotIn("[:15]", LOOPS_SOURCE)


# --------------------------------------------------------------------------- the guarded write

class TheGuardedWrite(Base):
    """The projection is appended BELOW the hand-written content, inside a marked generated region.
    **Nothing is deleted. Nothing old moves.** A rollback is deleting one generated section.

    Each refusal is a test and the hand-written half's survival is asserted BYTE FOR BYTE rather
    than by eyeball."""

    def test_render_defaults_to_stdout_and_writes_nothing(self):
        path = self.seed_carry_over()
        before = self.read(path)
        self.add()
        code, out, _ = self.cli("render")
        self.assertEqual(code, 0)
        self.assertIn(loops.BEGIN_MARK, out)
        self.assertEqual(self.read(path), before)

    def test_write_refuses_when_the_target_is_missing(self):
        self.add()
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.write_render(self.state)
        self.assertIn("REFUSING to create it", str(ctx.exception))
        self.assertFalse(os.path.exists(loops.carry_over_path(self.state)))

    def test_write_refuses_an_empty_payload_unless_told_to_mean_it(self):
        path = self.seed_carry_over()
        before = self.read(path)
        with self.assertRaises(loops.LoopsError) as ctx:
            loops.write_render(self.state)
        self.assertIn("REFUSED", str(ctx.exception))
        self.assertIn("producer that failed", str(ctx.exception))
        self.assertEqual(self.read(path), before)
        loops.write_render(self.state, allow_empty=True)
        self.assertIn(loops.BEGIN_MARK, self.read(path).decode("utf-8"))

    def test_write_refuses_a_malformed_region_in_every_shape(self):
        cases = {
            "two openers": HAND_WRITTEN + loops.BEGIN_MARK + "\nx\n" + loops.BEGIN_MARK
            + "\ny\n" + loops.END_MARK + "\n",
            "closer with no opener": HAND_WRITTEN + loops.END_MARK + "\n",
            "closer before opener": HAND_WRITTEN + loops.END_MARK + "\nx\n"
            + loops.BEGIN_MARK + "\n",
            "opener with no closer": HAND_WRITTEN + loops.BEGIN_MARK + "\nx\n",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.setUp()
                path = self.seed_carry_over(text)
                before = self.read(path)
                self.add()
                with self.assertRaises(loops.LoopsError) as ctx:
                    loops.write_render(self.state)
                self.assertIn("REFUSING to write", str(ctx.exception))
                self.assertEqual(self.read(path), before)

    def test_write_refuses_if_the_hand_written_region_would_change_by_one_byte(self):
        """The pre-write assertion. It is forced here by handing `write_render` a hand-written
        region that is not really a prefix of the file — the only way to reach the branch, and
        precisely the invariant the branch exists to hold."""
        path = self.seed_carry_over()
        before = self.read(path)
        self.add()
        original = loops.split_hand_written

        def liar(text):
            head, had = original(text)
            return head + "a byte that is not in the file\n", had

        loops.split_hand_written = liar
        try:
            with self.assertRaises(loops.LoopsError) as ctx:
                loops.write_render(self.state)
        finally:
            loops.split_hand_written = original
        self.assertIn("byte-prefix", str(ctx.exception))
        self.assertIn("Nothing was written", str(ctx.exception))
        self.assertEqual(self.read(path), before)

    def test_a_failed_post_write_assertion_restores_the_original_bytes(self):
        """The fourth refusal. `memory_write.write_text` is replaced so the first write lands
        something wrong; the module must notice, put the original back, and raise."""
        path = self.seed_carry_over()
        before = self.read(path)
        self.add()
        real = memory_write.write_text
        calls = []

        def sabotage(p, text, newline="preserve", allow_empty=False):
            calls.append(p)
            if p == path and len(calls) == 1:
                return real(p, "the hand-written half is gone\n", newline=newline)
            return real(p, text, newline=newline, allow_empty=allow_empty)

        memory_write.write_text = sabotage
        try:
            with self.assertRaises(loops.LoopsError) as ctx:
                loops.write_render(self.state)
        finally:
            memory_write.write_text = real
        self.assertIn("POST-WRITE ASSERTION FAILED", str(ctx.exception))
        self.assertIn("restored", str(ctx.exception))
        self.assertEqual(self.read(path), before)

    def test_the_hand_written_half_survives_verbatim(self):
        path = self.seed_carry_over()
        before = self.read(path)
        self.add()
        loops.write_render(self.state)
        after = self.read(path)
        self.assertTrue(after.startswith(before.rstrip(b"\n").rstrip(b"\r")),
                        "the hand-written region did not survive as a byte prefix")
        self.assertIn("a hand-written line nothing may touch — µ ✅ 🧷",
                      after.decode("utf-8"))

    def test_re_rendering_replaces_the_region_and_never_stacks_them(self):
        path = self.seed_carry_over()
        self.add()
        loops.write_render(self.state)
        first = self.read(path).decode("utf-8")
        self.add(text="a second item", item_id="loop-20260901-second",
                 carried_reason="named at birth, so the rebuild keeps it")
        loops.write_render(self.state)
        second = self.read(path).decode("utf-8")
        self.assertEqual(second.count(loops.BEGIN_MARK), 1)
        self.assertEqual(second.count(loops.END_MARK), 1)
        self.assertIn("loop-20260901-second", second)
        # BYTE-IDENTICAL, not merely "still a prefix". A render that adds one blank line above the
        # marker passes every other assertion in this file and, run nightly, walks the generated
        # region off the bottom of the screen.
        self.assertEqual(first.split(loops.BEGIN_MARK)[0], second.split(loops.BEGIN_MARK)[0])

    def test_a_third_render_adds_no_blank_line_above_the_marker(self):
        path = self.seed_carry_over()
        self.add(carried_reason="still live")
        heads = []
        for _ in range(3):
            loops.write_render(self.state)
            heads.append(self.read(path).decode("utf-8").split(loops.BEGIN_MARK)[0])
        self.assertEqual(heads[0], heads[1])
        self.assertEqual(heads[1], heads[2])

    def test_a_rollback_is_deleting_one_generated_section(self):
        path = self.seed_carry_over()
        before = self.read(path).decode("utf-8")
        self.add()
        loops.write_render(self.state)
        after = self.read(path).decode("utf-8")
        rolled_back = after.split(loops.BEGIN_MARK)[0]
        self.assertEqual(rolled_back.rstrip("\r\n"), before.rstrip("\r\n"))

    def test_crlf_is_preserved_and_the_file_is_not_normalised(self):
        # `state/` files can be CRLF on Windows, and a helper that normalises rewrites a whole file
        # to bury one added line.
        path = self.seed_carry_over(newline="\r\n")
        self.add()
        loops.write_render(self.state)
        raw = self.read(path)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertGreater(raw.count(b"\r\n"), 10)

    def test_lf_is_preserved_too(self):
        path = self.seed_carry_over(newline="\n")
        self.add()
        loops.write_render(self.state)
        self.assertNotIn(b"\r", self.read(path))

    def test_a_successful_write_stamps_last_rendered(self):
        self.seed_carry_over()
        self.add()
        self.assertIsNone(loops.load(self.state)["last_rendered"])
        loops.write_render(self.state)
        self.assertIsNotNone(loops.load(self.state)["last_rendered"])

    def test_the_region_says_it_is_generated_and_names_its_source(self):
        self.seed_carry_over()
        self.add()
        loops.write_render(self.state)
        text = self.read(loops.carry_over_path(self.state)).decode("utf-8")
        self.assertIn("GENERATED, do not hand-edit", text)
        self.assertIn("loops.py render --write", text)
        self.assertIn("never touched by this tool", text)


# --------------------------------------------------------------------------- who else touches it

class WhoReadsAndWritesTheRegister(Base):
    """Every module beyond `loops.py` that imports it or names `open-loops.json` is pinned by name
    and by shape here, so a new reader or writer is a deliberate edit rather than a silent one."""

    EXCEPTIONS = ("state_backup.py", "cadence_chain.py", "owi_unknowns.py", "owi_resurface.py",
                  "carryover_region.py", "jobs.py", "observation_gate.py")

    def _text(self, name: str) -> str:
        return _slurp(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))

    def test_nothing_else_in_the_tree_reads_the_register(self):
        """`cadence_chain.py` (report-only reader), `owi_unknowns.py` (the unknown-owner picker —
        writes `whose_move` via `update`, and two terminal verbs), `owi_resurface.py` (report-only
        resurfacing instrument), `carryover_region.py` (imports the two region markers only),
        `jobs.py` (`start --loop` calls `loops.start` only), `observation_gate.py` (the gate
        scanner), and `state_backup.py` (byte-copies the file). An eighth name appearing without a
        matching pin is what this test catches."""
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py") or name.startswith("test_") or name == "loops.py":
                continue
            if name in self.EXCEPTIONS:
                continue
            text = _slurp(os.path.join(here, name), errors="replace")
            if "import loops" in text or "from loops" in text or "open-loops.json" in text:
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_the_gate_scanner_only_calls_mark_observation_complete_and_raise_item(self):
        """`observation_gate.py` reads every `observation` row and, on a fully met gate, calls
        `loops.mark_observation_complete` then `loops.raise_item` — never any other verb, and never
        `loops.observe` (the transition INTO observation is a direct action, not something the
        scanner that reads its outcome should invoke)."""
        text = self._text("observation_gate.py")
        self.assertIn("loops.mark_observation_complete(", text)
        self.assertIn("loops.raise_item(", text)
        for forbidden in ("loops.add(", "loops.carry(", "loops.hold(", "loops.start(",
                          "loops.resolve(", "loops.drop(", "loops.update(", "loops.observe(",
                          "loops.owner_kind(", "loops.owner_priority(", "loops.owner_abandon(",
                          "loops.save("):
            self.assertNotIn(forbidden, text)

    def test_jobs_only_calls_loopss_start(self):
        """`jobs.py`'s `start --loop <id>` calls `loops.start` ONLY, after the job itself has
        started — one explicit signal, never inferred from `--goal`, and never any other verb (a job
        may mark a work item started, not resolve/drop/hold it)."""
        import jobs
        text = self._text("jobs.py")
        self.assertIn("loops.start(", text)
        for forbidden in ("loops.load(", "loops.save(", "loops.add(", "loops.carry(", "loops.hold(",
                          "loops.resolve(", "loops.drop(", "loops.update(", "loops.raise_item(",
                          "loops.owner_kind(", "loops.owner_priority(", "loops.owner_abandon("):
            self.assertNotIn(forbidden, text)
        self.assertTrue(callable(jobs.start_job))

    def test_carryover_region_never_touches_the_store_at_all(self):
        """`carryover_region.py` imports `loops` for the two region markers, imported rather than
        re-typed so the two modules cannot disagree on them. It must never reach `loops.load`, any
        verb, or name `open-loops.json` — it owns a DIFFERENT region of the carry-over file."""
        import carryover_region
        text = self._text("carryover_region.py")
        self.assertIn("loops.BEGIN_MARK", text)
        self.assertIn("loops.END_MARK", text)
        for forbidden in ("loops.load(", "loops.save(", "loops.add(", "loops.carry(", "loops.hold(",
                          "loops.resolve(", "loops.drop(", "loops.update(", "loops.raise_item(",
                          "loops.owner_kind(", "loops.owner_priority(", "loops.owner_abandon(",
                          "open-loops.json"):
            self.assertNotIn(forbidden, text)
        self.assertIn("WRAP_SEED", carryover_region.REGIONS)

    def test_owi_resurface_is_read_only(self):
        """`owi_resurface.py` measures the resurfacing miss rate without ever touching the register
        it reads: `loops.load`/`loops.dormant_at_epoch` only."""
        import owi_resurface
        text = self._text("owi_resurface.py")
        for forbidden in ("loops.save(", "loops.add(", "loops.carry(", "loops.hold(",
                          "loops.resolve(", "loops.drop(", "loops.update(", "loops.raise_item(",
                          "loops.owner_kind(", "loops.owner_priority(", "loops.owner_abandon("):
            self.assertNotIn(forbidden, text)
        self.assertTrue(callable(owi_resurface.report))
        self.assertTrue(callable(owi_resurface.log_run))

    def test_state_backup_only_copies_the_stores_bytes(self):
        """`state_backup.py` is not a reader: it byte-copies the file into the nightly rotation
        without decoding it — `open-loops.json` is irreplaceable and rewritten wholesale. It must not
        import this module, must not parse the store, and must name it exactly once, in `FILES`."""
        import state_backup
        text = self._text("state_backup.py")
        self.assertIn("open-loops.json", state_backup.FILES)
        self.assertEqual(text.count("open-loops.json"), 1)
        self.assertNotIn("import loops", text)
        self.assertNotIn("from loops", text)

    def test_cadence_chain_is_read_only(self):
        """`cadence_chain.py` calls `loops.load`/`loops.items`/`loops.get` only — a check-chain with
        a side effect makes a dry-run impossible."""
        import cadence_chain
        text = self._text("cadence_chain.py")
        for forbidden in ("loops.save(", "loops.add(", "loops.carry(", "loops.hold(",
                          "loops.resolve(", "loops.drop(", "loops.update(",
                          "loops.owner_kind(", "loops.owner_priority(", "loops.owner_abandon("):
            self.assertNotIn(forbidden, text)
        self.assertTrue(callable(cadence_chain.evaluate))

    def test_owi_unknowns_only_calls_loopss_own_public_verbs_and_reads(self):
        """`owi_unknowns.py` reads the store and records the owner's picker answer through
        `loops.update`'s `whose_move`. It must never reach `loops.save`, `carry`/`hold`/`drop`/
        `add`, or `owner_kind`/`owner_priority`.

        It ALSO reaches exactly two lifecycle verbs, each exactly once, inside `_apply_terminal`
        only: `loops.resolve` for a `Done` tap and the owner-only `loops.owner_abandon` for an
        `Archived` tap — the tap IS the owner acting, and the module relays it rather than deciding
        it. Pinned at one call site each so a second route to `abandoned` cannot appear unnoticed."""
        import owi_unknowns
        text = self._text("owi_unknowns.py")
        self.assertIn("loops.load(", text)
        self.assertIn("loops.update(", text)
        for forbidden in ("owner_kind(", "owner_priority(", "loops.save(",
                          "loops.carry(", "loops.hold(", "loops.drop(", "loops.add("):
            self.assertNotIn(forbidden, text)
        for once in ("loops.owner_abandon(", "loops.resolve("):
            self.assertEqual(text.count(once), 1, once)
        body = text.split("def _apply_terminal(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("loops.owner_abandon(", body)
        self.assertIn("loops.resolve(", body)
        self.assertTrue(callable(owi_unknowns.apply))

    def test_there_is_no_prune_verb_because_no_age_has_been_chosen(self):
        # Bounded by an age-based prune, never by a count — and no age has been chosen.
        self.assertNotIn('add_parser("prune"', LOOPS_SOURCE)
        self.assertNotIn("def prune(", LOOPS_SOURCE)

    def test_it_sends_nothing_and_spawns_nothing(self):
        body = LOOPS_SOURCE.split('"""', 2)[2]
        for banned in ("subprocess", "urllib", "telegram", "socket", "http"):
            self.assertNotIn(banned, body, f"loops.py reaches for {banned}")


class TheTrackedSeed(Base):
    """`state/open-loops.example.json` is the only version of this store that enters git. A seed
    that does not validate teaches the schema wrong, and one that renders wrong teaches the
    projection wrong."""

    SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "state", "open-loops.example.json")

    def load_seed(self) -> dict:
        return json.loads(_slurp(self.SEED))

    def test_it_parses_and_every_record_validates_against_the_ruled_vocabulary(self):
        seed = self.load_seed()
        self.assertEqual(seed["schema"], loops.SCHEMA)
        for item_id, item in seed["items"].items():
            self.assertEqual(item["id"], item_id)
            self.assertIn(item["status"], loops.STORED_STATUSES, item_id)
            self.assertIn(item["audience"], loops.AUDIENCES, item_id)
            self.assertIn(item["whose_move"], loops.WHOSE_MOVE, item_id)
            self.assertIn(item["kind_assistant"], loops.KINDS, item_id)
            self.assertIn(item["priority_assistant"], loops.PRIORITIES, item_id)
            self.assertTrue(item["terminal_state"], f"{item_id} has no membership test")

    def test_it_exhibits_a_retained_terminal_record(self):
        # A seed with no terminal record teaches that `resolve` deletes.
        statuses = {i["status"] for i in self.load_seed()["items"].values()}
        self.assertTrue(statuses & set(loops.TERMINAL_STATUSES))

    def test_it_exhibits_the_pair_disagreeing(self):
        """*"The disagreement is the signal."* A seed where the owner's is always unset teaches that
        the second column is decoration."""
        disagreements = [
            i for i in self.load_seed()["items"].values()
            if i.get("priority_owner") and i["priority_owner"] != i["priority_assistant"]]
        self.assertTrue(disagreements, "no record shows the two priority columns diverging")

    def test_it_exhibits_a_record_whose_audience_and_whose_move_differ(self):
        # A builder will conflate the two axes, and a seed where they always agree is why.
        crossed = [i for i in self.load_seed()["items"].values()
                   if i["audience"] != i["whose_move"]]
        self.assertTrue(crossed, "no record shows audience and whose_move disagreeing")

    def test_it_renders(self):
        seed = self.load_seed()
        rows = seed["items"].values()
        carried = [i["id"] for i in rows if i["audience"] == "owner" and i["status"] == "open"
                   and i.get("carried_reason")]
        terminal = [i["id"] for i in rows if i["status"] in loops.TERMINAL_STATUSES]
        other_audience = sum(1 for i in rows if i["audience"] != "owner")
        held = sum(1 for i in rows if i["audience"] == "owner" and i["status"] == "held")
        self.assertTrue(carried and terminal and other_audience and held,
                        "the seed must exhibit a carried, a terminal, an assistant-facing and a "
                        "held record")
        loops.save(seed, self.state)
        self.seed_carry_over()
        loops.write_render(self.state)
        text = self.read(loops.carry_over_path(self.state)).decode("utf-8")
        for item_id in carried:
            self.assertIn(item_id, text)
        self.assertIn(f"{other_audience} audience=assistant", text)
        self.assertIn(f"{held} status=held", text)
        for item_id in terminal:
            self.assertNotIn(item_id, text)  # terminal: retained, not shown


class TheCadenceCheckChainIntegration(Base):
    """The store's OPTIONAL `checks` field and the report-only reader, `cadence_verdict`/`report`.
    Nothing here changes what `render` surfaces; these tests pin that the wiring exists and stays
    additive."""

    def test_a_fresh_item_has_no_checks_field_set(self):
        item = self.add()
        self.assertIsNone(item["checks"])

    def test_add_can_set_a_custom_chain(self):
        item = self.add(checks=["resolved-excluded"])
        self.assertEqual(item["checks"], ["resolved-excluded"])

    def test_update_can_set_and_clear_the_chain(self):
        item = self.add()
        updated = loops.update(self.state, item_id=item["id"], checks=["resolved-excluded"])
        self.assertEqual(updated["checks"], ["resolved-excluded"])
        # An empty list clears back to None (cadence_chain.DEFAULT_CHAIN), never an always-SKIP chain.
        cleared = loops.update(self.state, item_id=item["id"], checks=[])
        self.assertIsNone(cleared["checks"])

    def test_cadence_verdict_is_report_only_and_reaches_cadence_chain(self):
        item = self.add(whose_move="assistant")
        result = loops.cadence_verdict(item)
        self.assertIn(result["verdict"], ("INCLUDE", "SKIP", "EXCLUDE", "ABORT"))
        # A fresh open item, whose_move=assistant, matches no handler's predicate in the default chain.
        self.assertEqual(result["verdict"], "SKIP")

    def test_a_terminal_item_reports_excluded_by_resolved_excluded(self):
        item = self.add()
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        done = loops.get(loops.load(self.state), item["id"])
        result = loops.cadence_verdict(done)
        self.assertEqual(result["verdict"], "EXCLUDE")
        self.assertEqual(result["decided_by"], "resolved-excluded")

    def test_report_cli_is_read_only_and_lists_every_row(self):
        self.add()
        self.add(text="a second item")
        code, out, _ = self.cli("report", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("verdict", row)
            self.assertIn("decided_by", row)
        # report never writes: the store on disk is unchanged by running it.
        before = _slurp(loops.store_path(self.state))
        self.cli("report")
        after = _slurp(loops.store_path(self.state))
        self.assertEqual(before, after)


class WhoseMoveGainsBoth(Base):
    """A fifth `whose_move` value for a row genuinely gated on the owner and the assistant acting
    together."""

    def test_both_is_a_legal_whose_move_value(self):
        item = self.add(whose_move="both")
        self.assertEqual(item["whose_move"], "both")

    def test_both_is_not_a_legal_audience_value(self):
        # Only whose_move has a fifth value — AUDIENCES stays the two people a list is FOR.
        with self.assertRaises(loops.LoopsError):
            self.add(audience="both")

    def test_five_whose_move_values_no_more(self):
        self.assertEqual(len(loops.WHOSE_MOVE), 5)
        self.assertIn("both", loops.WHOSE_MOVE)

    def test_cli_add_accepts_both(self):
        code, out, _ = self.cli("add", "--text", "joint planning session prep", "--audience",
                                "owner", "--terminal-state", "the session happens or it's cancelled",
                                "--whose-move", "both", "--next-action", "draft the agenda",
                                "--kind", "chore", "--priority", "high")
        self.assertEqual(code, 0)


class WhoseMoveIsAssistantUpdatable(Base):
    """`update()` takes `--whose-move` — `owi_unknowns.py`'s picker resolution needs a way to correct
    a row's `whose_move` after `add()` mints it. NOT an owner-only field."""

    def test_update_can_change_whose_move(self):
        item = self.add(whose_move="unknown")
        updated = loops.update(self.state, item_id=item["id"], whose_move="owner")
        self.assertEqual(updated["whose_move"], "owner")

    def test_update_validates_whose_move(self):
        item = self.add(whose_move="unknown")
        with self.assertRaises(loops.LoopsError):
            loops.update(self.state, item_id=item["id"], whose_move="nobody")

    def test_omitted_whose_move_is_left_alone(self):
        item = self.add(whose_move="unknown")
        updated = loops.update(self.state, item_id=item["id"], next_action="ask the owner")
        self.assertEqual(updated["whose_move"], "unknown")

    def test_cli_update_accepts_whose_move(self):
        item = self.add(whose_move="unknown")
        code, out, _ = self.cli("update", item["id"], "--whose-move", "both")
        self.assertEqual(code, 0)
        got = loops.get(loops.load(self.state), item["id"])
        self.assertEqual(got["whose_move"], "both")


class DualDormancyReaskThresholds(Base):
    """`loops.py` is the ONE SOURCE OF TRUTH for both cadence numbers, `needs_reask` sits beside
    `is_dormant`, and `dormant_at_epoch` keeps pre-epoch dormancy distinguishable from fresh."""

    def _age(self, item_id, days, reference=None):
        """Stamps `last_touched` `days` before `reference` (default: now) and returns `reference`, so
        a caller can hand the SAME instant back to `is_dormant`/`needs_reask` — otherwise the wall
        clock ticking between the stamp and the assertion can push an exact-boundary case across the
        `>` a hair early."""
        # microsecond=0: `isoformat(timespec="seconds")` truncates sub-second precision when WRITING
        # the stamp, so an un-floored `reference` would compare a few microseconds later than what
        # actually got written.
        reference = (reference or datetime.now(timezone.utc)).replace(microsecond=0)
        store = loops.load(self.state)
        old = (reference - timedelta(days=days)).isoformat(timespec="seconds")
        store["items"][item_id]["last_touched"] = old
        loops.save(store, self.state)
        return reference

    def test_the_two_thresholds_are_distinct(self):
        self.assertEqual(loops.DORMANT_AFTER_DAYS, 14)
        self.assertEqual(loops.REASK_AFTER_DAYS, 10)
        self.assertNotEqual(loops.DORMANT_AFTER_DAYS, loops.REASK_AFTER_DAYS)

    def test_cadence_chain_imports_the_same_numbers_rather_than_a_copy(self):
        import cadence_chain
        self.assertEqual(cadence_chain.DORMANT_AFTER_DAYS, loops.DORMANT_AFTER_DAYS)
        self.assertEqual(cadence_chain.REASK_AFTER_DAYS, loops.REASK_AFTER_DAYS)

    def test_is_dormant_bare_call_uses_the_default(self):
        item = self.add()
        self._age(item["id"], 20)
        got = loops.get(loops.load(self.state), item["id"])
        self.assertTrue(loops.is_dormant(got))

    def test_an_explicit_none_still_means_the_overlay_did_not_run(self):
        # The default must not change render/project/the CLI's own no-default contract.
        item = self.add()
        self._age(item["id"], 20)
        got = loops.get(loops.load(self.state), item["id"])
        self.assertFalse(loops.is_dormant(got, None))

    def test_needs_reask_fires_for_owner_and_both_but_not_others(self):
        for whose_move, expect in (("owner", True), ("both", True), ("assistant", False),
                                   ("external", False), ("unknown", False)):
            with self.subTest(whose_move=whose_move):
                self.setUp()
                item = self.add(whose_move=whose_move)
                self._age(item["id"], 11)
                got = loops.get(loops.load(self.state), item["id"])
                self.assertEqual(loops.needs_reask(got), expect)

    def test_needs_reask_respects_the_ten_day_boundary(self):
        item = self.add(whose_move="owner")
        now = self._age(item["id"], 10)
        self.assertFalse(loops.needs_reask(loops.get(loops.load(self.state), item["id"]), now=now))
        now = self._age(item["id"], 11)
        self.assertTrue(loops.needs_reask(loops.get(loops.load(self.state), item["id"]), now=now))

    def test_needs_reask_is_false_for_a_non_open_row(self):
        item = self.add(whose_move="owner")
        self._age(item["id"], 20)
        loops.hold(self.state, item_id=item["id"], because="cannot verify")
        got = loops.get(loops.load(self.state), item["id"])
        self.assertFalse(loops.needs_reask(got))

    def test_dormant_at_epoch_is_false_with_no_snapshot_on_disk(self):
        item = self.add()
        self.assertFalse(loops.dormant_at_epoch(item, state_dir=self.state))

    def test_dormant_at_epoch_distinguishes_pre_epoch_dormancy_from_fresh(self):
        pre = self.add(text="already dormant before the epoch")
        fresh = self.add(text="went dormant only after the epoch")
        snapshot = {
            "schema": "seneschal.owi-dormant-at-epoch/1",
            "epoch_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dormant_after_days": 14,
            "dormant_ids": [pre["id"]],
        }
        path = os.path.join(self.state, loops.DORMANT_SNAPSHOT_FILE)
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
        self.assertTrue(loops.dormant_at_epoch(pre, state_dir=self.state))
        self.assertFalse(loops.dormant_at_epoch(fresh, state_dir=self.state))

    def test_dormant_at_epoch_fails_closed_never_open_on_a_corrupt_snapshot(self):
        item = self.add()
        path = os.path.join(self.state, loops.DORMANT_SNAPSHOT_FILE)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(loops.dormant_at_epoch(item, state_dir=self.state))

    def test_render_and_the_cli_default_are_unchanged(self):
        self.assertIn('"--dormant-after-days", type=int, default=None', LOOPS_SOURCE)
        self.assertIs(loops.project(loops.load(self.state))["dormant_after_days"], None)


class TheUnionQuery(Base):
    """`items(whose_move=...)` is a plain equality filter; `for_person` is the union query, distinct
    on purpose."""

    def test_items_gains_a_plain_whose_move_filter(self):
        self.add(whose_move="owner")
        self.add(whose_move="assistant", text="an assistant-move item")
        rows = loops.items(loops.load(self.state), whose_move="owner")
        self.assertEqual([r["whose_move"] for r in rows], ["owner"])

    def test_a_plain_whose_move_filter_does_not_pull_in_audience_matches(self):
        self.add(audience="owner", whose_move="assistant")
        rows = loops.items(loops.load(self.state), whose_move="owner")
        self.assertEqual(rows, [])

    def test_for_person_unions_audience_and_whose_move(self):
        a = self.add(audience="owner", whose_move="assistant", text="owner sees it, assistant does it")
        b = self.add(audience="assistant", whose_move="owner", text="assistant sees it, owner decides")
        store = loops.load(self.state)
        owner_ids = {r["id"] for r in loops.for_person(store, "owner")}
        assistant_ids = {r["id"] for r in loops.for_person(store, "assistant")}
        self.assertEqual(owner_ids, {a["id"], b["id"]})
        self.assertEqual(assistant_ids, {a["id"], b["id"]})

    def test_unknown_whose_move_renders_on_both_lists(self):
        item = self.add(whose_move="unknown")
        store = loops.load(self.state)
        self.assertIn(item["id"], [r["id"] for r in loops.for_person(store, "owner")])
        self.assertIn(item["id"], [r["id"] for r in loops.for_person(store, "assistant")])

    def test_both_whose_move_renders_on_both_lists_by_choice(self):
        item = self.add(whose_move="both")
        store = loops.load(self.state)
        self.assertIn(item["id"], [r["id"] for r in loops.for_person(store, "owner")])
        self.assertIn(item["id"], [r["id"] for r in loops.for_person(store, "assistant")])

    def test_external_whose_move_adds_no_person_beyond_what_audience_already_gives(self):
        item = self.add(audience="assistant", whose_move="external")
        store = loops.load(self.state)
        self.assertNotIn(item["id"], [r["id"] for r in loops.for_person(store, "owner")])
        self.assertIn(item["id"], [r["id"] for r in loops.for_person(store, "assistant")])

    def test_for_person_refuses_a_non_person(self):
        with self.assertRaises(loops.LoopsError):
            loops.for_person(loops.load(self.state), "external")

    def test_cli_list_whose_move_is_a_plain_filter(self):
        self.add(whose_move="owner")
        self.add(whose_move="both", text="a joint item")
        code, out, _ = self.cli("list", "--whose-move", "both", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["whose_move"] for r in rows], ["both"])


class TheAssistantsDefaultQuery(Base):
    """The assistant's own working-memory read: UNFILTERED BY PERSON, non-terminal, dormant excluded,
    `unknown`/`both` included, ordered by cadence verdict then age."""

    def _touch(self, item_id, days):
        store = loops.load(self.state)
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        store["items"][item_id]["last_touched"] = old
        loops.save(store, self.state)

    def test_terminal_rows_are_excluded(self):
        item = self.add()
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        self.assertEqual(loops.default_query(loops.load(self.state)), [])

    def test_dormant_rows_are_excluded(self):
        item = self.add()
        self._touch(item["id"], 20)
        self.assertEqual(loops.default_query(loops.load(self.state)), [])

    def test_a_live_row_just_inside_the_dormancy_window_is_included(self):
        item = self.add()
        self._touch(item["id"], 13)
        rows = loops.default_query(loops.load(self.state))
        self.assertEqual([r["id"] for r in rows], [item["id"]])

    def test_it_is_unfiltered_by_person_the_owners_own_items_show_up(self):
        item = self.add(audience="owner", whose_move="owner")
        rows = loops.default_query(loops.load(self.state))
        self.assertEqual([r["id"] for r in rows], [item["id"]])

    def test_unknown_and_both_are_included_same_as_everything_else(self):
        a = self.add(whose_move="unknown", text="unknown item")
        b = self.add(whose_move="both", text="joint item")
        rows = {r["id"] for r in loops.default_query(loops.load(self.state))}
        self.assertEqual(rows, {a["id"], b["id"]})

    def test_ordered_by_cadence_verdict_include_before_skip(self):
        skip_item = self.add(whose_move="assistant", text="nothing due")
        include_item = self.add(whose_move="owner", text="overdue for a re-ask")
        self._touch(include_item["id"], 11)
        rows = loops.default_query(loops.load(self.state))
        self.assertEqual([r["id"] for r in rows], [include_item["id"], skip_item["id"]])

    def test_within_a_verdict_group_oldest_first(self):
        newer = self.add(whose_move="owner", text="raised recently")
        older = self.add(whose_move="owner", text="raised a while ago")
        self._touch(newer["id"], 11)
        self._touch(older["id"], 13)
        rows = loops.default_query(loops.load(self.state))
        self.assertEqual([r["id"] for r in rows], [older["id"], newer["id"]])

    def test_in_progress_rows_lead_regardless_of_cadence_verdict_or_age(self):
        # in_progress is the row being worked, so it leads even an overdue re-ask (INCLUDE) that
        # would otherwise sort first.
        overdue = self.add(whose_move="owner", text="overdue for a re-ask")
        self._touch(overdue["id"], 11)
        started = self.add(whose_move="assistant", text="actually being worked")
        loops.start(self.state, item_id=started["id"])
        rows = loops.default_query(loops.load(self.state))
        self.assertEqual([r["id"] for r in rows], [started["id"], overdue["id"]])

    def test_cli_mine_is_read_only(self):
        self.add(whose_move="owner")
        before = _slurp(loops.store_path(self.state))
        code, out, _ = self.cli("mine", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)
        after = _slurp(loops.store_path(self.state))
        self.assertEqual(before, after)


class TheCli(Base):
    def test_a_refusal_exits_2_with_a_real_sentence(self):
        code, out, err = self.cli("carry", "loop-does-not-exist", "--because", "x")
        self.assertEqual(code, 2)
        self.assertIn("refused", err)
        self.assertFalse(json.loads(out)["ok"])

    def test_add_then_list_then_show_round_trips(self):
        code, out, _ = self.cli("add", "--text", GOOD["text"], "--audience", "owner",
                                "--terminal-state", GOOD["terminal_state"],
                                "--whose-move", "assistant", "--next-action", GOOD["next_action"],
                                "--kind", "enhancement", "--priority", "normal")
        self.assertEqual(code, 0)
        item_id = json.loads(out)["id"]
        code, out, _ = self.cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in json.loads(out)], [item_id])
        code, out, _ = self.cli("show", item_id)
        self.assertEqual(json.loads(out)["terminal_state"], GOOD["terminal_state"])

    def test_update_moves_last_touched_and_cannot_set_a_status(self):
        item = self.add()
        before = loops.get(loops.load(self.state), item["id"])["last_touched"]
        code, _, _ = self.cli("update", item["id"], "--next-action", "write the handler")
        self.assertEqual(code, 0)
        got = loops.get(loops.load(self.state), item["id"])
        self.assertEqual(got["next_action"], "write the handler")
        self.assertGreaterEqual(got["last_touched"], before)
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                loops.main(["--state-dir", self.state, "update", item["id"], "--status", "done"])


class RaiseItemStampsLastRaisedAt(Base):
    """A separate field scoring the positive-resurfacing obligation off whether the assistant
    actually raised the item, not off `carried_reason` (a different projection question) or
    `last_touched` (which moves on any edit)."""

    def test_a_fresh_record_has_no_last_raised_at(self):
        item = self.add()
        self.assertIsNone(item["last_raised_at"])

    def test_raise_stamps_it_and_touches_last_touched(self):
        item = self.add()
        raised = loops.raise_item(self.state, item_id=item["id"])
        self.assertIsNotNone(raised["last_raised_at"])
        self.assertEqual(raised["last_touched"], raised["last_raised_at"])

    def test_carrying_does_not_stamp_last_raised_at(self):
        # carried_reason answers a different question — does this still belong in the next render —
        # and must not be read as "the assistant said this to the owner".
        item = self.add()
        loops.carry(self.state, item_id=item["id"], because="still live")
        got = loops.get(loops.load(self.state), item["id"])
        self.assertIsNone(got["last_raised_at"])

    def test_an_unrelated_update_does_not_stamp_last_raised_at(self):
        item = self.add()
        loops.update(self.state, item_id=item["id"], next_action="poke at it")
        got = loops.get(loops.load(self.state), item["id"])
        self.assertIsNone(got["last_raised_at"])

    def test_cli_raise_round_trips(self):
        item = self.add()
        code, out, _ = self.cli("raise", item["id"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(json.loads(out)["last_raised_at"])
        got = loops.get(loops.load(self.state), item["id"])
        self.assertIsNotNone(got["last_raised_at"])


# ------------------------------------------------------ register -> Notion Tasks projection

class TasksPointerLookup(Base):
    """`_tasks_notion_page_id` — the only place "does this row point at a Tasks row" is answered."""

    def test_no_migration_map_at_all_is_none(self):
        item = self.add()
        self.assertIsNone(loops._tasks_notion_page_id(item["id"], self.state))

    def test_a_page_in_the_map_resolves(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "notion-page-abc")
        self.assertEqual(loops._tasks_notion_page_id(item["id"], self.state), "notion-page-abc")

    def test_a_row_never_mapped_is_invisible_by_construction(self):
        # Added by hand with --renders-elsewhere: no map entry names which surface the URL belongs
        # to, and this function never parses the URL to guess.
        item = self.add(renders_elsewhere="https://www.notion.so/some-task-deadbeef")
        self.assertIsNone(loops._tasks_notion_page_id(item["id"], self.state))

    def test_a_corrupt_map_fails_to_none_not_to_a_raise(self):
        path = os.path.join(self.state, "owi-migration-map.json")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        item = self.add()
        self.assertIsNone(loops._tasks_notion_page_id(item["id"], self.state))


class TheProjectionIsGated(Base):
    """The projection is Notion-backend-only and a no-op while `outbox_common` lacks the
    `task_status` op. Both gates must close it without raising and without touching the outbox."""

    def test_a_non_notion_backend_is_a_no_op(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        for backend in ("markdown", "obsidian", None):
            with self.subTest(backend=backend), \
                    mock.patch.object(loops, "store_backend", return_value=backend):
                self.assertIsNone(loops.forward_task_status(item["id"], "done",
                                                            state_dir=self.state))
                loops.resolve(self.state, item_id=item["id"], because="shipped")
                buckets = loops.project_task_status(self.state)
                self.assertEqual(sum(len(v) for v in buckets.values()), 0)
                self.assertFalse(os.path.exists(ob.db_path(self.state)))

    def test_without_the_task_status_op_the_projection_is_a_no_op(self):
        stub = types.SimpleNamespace(**{k: getattr(ob, k) for k in dir(ob)
                                        if not k.startswith("__") and k != "task_status_key"})
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        with mock.patch.object(loops, "outbox_common", stub):
            self.assertIsNotNone(loops._projection_gate())
            self.assertIsNone(loops.forward_task_status(item["id"], "done", state_dir=self.state))
            loops.resolve(self.state, item_id=item["id"], because="shipped")
            self.assertEqual(sum(len(v) for v in loops.project_task_status(self.state).values()), 0)
        self.assertFalse(os.path.exists(ob.db_path(self.state)))

    def test_cli_project_status_says_when_it_is_gated(self):
        with mock.patch.object(loops, "store_backend", return_value="markdown"):
            code, out, _ = self.cli("project-status", "--json")
        self.assertEqual(code, 0)
        self.assertIn("markdown", json.loads(out)["gated"])

    def _resolver(self, config=None, legacy=False):
        """Point the real `store_backend` at temp files: `config` is written verbatim when given."""
        cfg_path = os.path.join(self.state, "config.json")
        legacy_path = os.path.join(self.state, "notion-mcp.json")
        if config is not None:
            with io.open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write(config)
        if legacy:
            with io.open(legacy_path, "w", encoding="utf-8") as fh:
                fh.write("{}")
        mock.patch.stopall()  # drop Base's pinned backend so the real resolver runs
        return (mock.patch.object(loops, "STORE_CONFIG", cfg_path),
                mock.patch.object(loops, "LEGACY_NOTION_MCP", legacy_path))

    def test_store_backend_reads_the_active_key(self):
        a, b = self._resolver(config='{"active": "obsidian"}', legacy=True)
        with a, b:
            self.assertEqual(loops.store_backend(), "obsidian")

    def test_store_backend_falls_back_to_the_legacy_notion_config(self):
        a, b = self._resolver(legacy=True)
        with a, b:
            self.assertEqual(loops.store_backend(), "notion")

    def test_store_backend_never_raises_and_reads_unconfigured_as_none(self):
        a, b = self._resolver(config="{not json")
        with a, b:
            self.assertIsNone(loops.store_backend())


@unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
class ForwardTaskStatus(Base):
    """`forward_task_status` — the enqueue, and everything that makes it a safe no-op."""

    def test_open_has_no_mapping_and_enqueues_nothing(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        self.assertIsNone(loops.forward_task_status(item["id"], "open", state_dir=self.state))
        self.assertFalse(os.path.exists(ob.db_path(self.state)))

    def test_not_a_tasks_pointer_enqueues_nothing(self):
        item = self.add()
        self.assertIsNone(loops.forward_task_status(item["id"], "done", state_dir=self.state))

    def test_done_forwards_done_plus_completed_date(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-done")
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        r = loops.forward_task_status(item["id"], "done", state_dir=self.state, now=now)
        self.assertTrue(r["ok"])
        entry = self.outbox_entry("page-done", "Done")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["op"], "task_status")
        self.assertEqual(entry["target_kind"], "page")
        self.assertEqual(entry["target_id"], "page-done")
        self.assertEqual(entry["payload"], {"status": "Done", "completed_date": "2026-09-20"})

    def test_dropped_and_abandoned_both_forward_archived_with_no_completed_date(self):
        for status in ("dropped", "abandoned"):
            with self.subTest(status=status):
                item = self.add()
                self.seed_tasks_pointer(item["id"], f"page-{status}")
                loops.forward_task_status(item["id"], status, state_dir=self.state)
                entry = self.outbox_entry(f"page-{status}", "Archived")
                self.assertEqual(entry["payload"], {"status": "Archived"})

    def test_held_and_paused_both_forward_paused(self):
        for status in ("held", "paused"):
            with self.subTest(status=status):
                item = self.add()
                self.seed_tasks_pointer(item["id"], f"page-{status}")
                loops.forward_task_status(item["id"], status, state_dir=self.state)
                entry = self.outbox_entry(f"page-{status}", "Paused")
                self.assertEqual(entry["payload"], {"status": "Paused"})

    def test_a_repeat_of_the_same_status_is_a_no_op(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        first = loops.forward_task_status(item["id"], "done", state_dir=self.state)
        second = loops.forward_task_status(item["id"], "done", state_dir=self.state)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])

    def test_a_later_different_status_mints_a_fresh_entry(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        held = loops.forward_task_status(item["id"], "held", state_dir=self.state)
        done = loops.forward_task_status(item["id"], "done", state_dir=self.state)
        self.assertNotEqual(held["id"], done["id"])
        self.assertIsNotNone(self.outbox_entry("page-1", "Paused"))
        self.assertIsNotNone(self.outbox_entry("page-1", "Done"))

    def test_a_broken_outbox_store_costs_only_the_forward(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        # A directory where the sqlite file should be — connect() raises. This must never propagate.
        os.makedirs(ob.db_path(self.state))
        self.assertIsNone(loops.forward_task_status(item["id"], "done", state_dir=self.state))


class VerbsForwardOnStatusChange(Base):
    """`resolve`/`drop`/`hold`/`owner_abandon` — each calls `forward_task_status` on success, and
    each accepts `forward=False` for an importer relaying a status Notion already carries."""

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_resolve_forwards_done(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        self.assertIsNotNone(self.outbox_entry("page-1", "Done"))

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_drop_forwards_archived(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.drop(self.state, item_id=item["id"], because="no longer relevant")
        self.assertIsNotNone(self.outbox_entry("page-1", "Archived"))

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_hold_forwards_paused(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.hold(self.state, item_id=item["id"])
        self.assertIsNotNone(self.outbox_entry("page-1", "Paused"))

    @unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
    def test_owner_abandon_forwards_archived(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.owner_abandon(self.state, item_id=item["id"])
        self.assertIsNotNone(self.outbox_entry("page-1", "Archived"))

    def test_forward_false_enqueues_nothing(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped", forward=False)
        self.assertFalse(os.path.exists(ob.db_path(self.state)))

    def test_the_register_write_itself_still_succeeds_when_forwarding_is_impossible(self):
        # No migration map at all — forward_task_status is a no-op, but the register mutation, which
        # already happened before the forward attempt, must be unaffected.
        item = self.add()
        got = loops.resolve(self.state, item_id=item["id"], because="shipped")
        self.assertEqual(got["status"], "done")


@unittest.skipUnless(HAS_TASK_STATUS, TASK_STATUS_SEAM)
class ProjectTaskStatus(Base):
    """`project_task_status` / CLI `project-status` — the read-only backlog report."""

    def test_open_lands_in_no_mapping(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        buckets = loops.project_task_status(self.state)
        self.assertEqual([r["id"] for r in buckets["no_mapping"]], [item["id"]])
        self.assertEqual(buckets["mismatch"], [])

    def test_a_status_change_never_forwarded_is_a_mismatch(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped", forward=False)
        buckets = loops.project_task_status(self.state)
        self.assertEqual([r["id"] for r in buckets["mismatch"]], [item["id"]])
        self.assertEqual(buckets["mismatch"][0]["desired_status"], "Done")

    def test_a_forwarded_status_lands_in_queued_then_already_forwarded(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        buckets = loops.project_task_status(self.state)
        self.assertEqual([r["id"] for r in buckets["queued"]], [item["id"]])

        conn = ob.connect(self.state)
        try:
            ob.mark_done(conn, self.outbox_entry("page-1", "Done")["id"])
        finally:
            conn.close()
        buckets = loops.project_task_status(self.state)
        self.assertEqual([r["id"] for r in buckets["already_forwarded"]], [item["id"]])

    def test_a_dead_lettered_forward_is_reported(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        conn = ob.connect(self.state)
        try:
            ob.mark_failed(conn, self.outbox_entry("page-1", "Done")["id"], "boom")
        finally:
            conn.close()
        buckets = loops.project_task_status(self.state)
        self.assertEqual([r["id"] for r in buckets["dead_letter"]], [item["id"]])

    def test_a_row_never_mapped_is_out_of_scope_entirely(self):
        self.add(renders_elsewhere="https://www.notion.so/never-mapped-deadbeef")
        buckets = loops.project_task_status(self.state)
        self.assertEqual(sum(len(v) for v in buckets.values()), 0)

    def test_cli_dry_run_reports_and_enqueues_nothing(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped", forward=False)
        code, out, _ = self.cli("project-status", "--json")
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual([r["id"] for r in parsed["buckets"]["mismatch"]], [item["id"]])
        self.assertEqual(parsed["applied"], [])
        # A dry run opens the outbox store to READ it, which is enough for sqlite to create the
        # file — what matters is that no ENTRY was enqueued.
        self.assertIsNone(self.outbox_entry("page-1", "Done"))

    def test_cli_apply_enqueues_every_mismatch_row(self):
        item = self.add()
        self.seed_tasks_pointer(item["id"], "page-1")
        loops.resolve(self.state, item_id=item["id"], because="shipped", forward=False)
        code, out, _ = self.cli("project-status", "--apply", "--json")
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(len(parsed["applied"]), 1)
        self.assertIsNotNone(self.outbox_entry("page-1", "Done"))


if __name__ == "__main__":
    unittest.main()
