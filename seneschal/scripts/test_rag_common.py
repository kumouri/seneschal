#!/usr/bin/env python3
"""Tests for `rag_common.embed_texts` — the sub-batching that keeps an ingest small enough for
Ollama to answer.

**The load-bearing family is `SubBatching`.** `test_never_loses_an_input_and_keeps_the_order`
reproduces the SHAPE of the failure the bounds exist for — a whole document's worth of chunks sent as
ONE request, answered `HTTP 400`, caught as `OllamaError`, exited 3 as "embedder unavailable", and
skipped silently by a Dream step whose skip-policy was written for a *down* embedder. The index
freezes and nothing alarms, because a transport bug wears the costume of a healthy graceful
degradation. `test_no_request_ever_exceeds_the_item_cap` guards the bound that actually fixes it.

**The 400 is not a payload-size limit.** Its body reads `Post "http://127.0.0.1:<port>/tokenize":
dial tcp ... actively refused it`: Ollama opens one internal connection per input item, and a big
batch overruns its own runner's listen backlog. So the driver is ITEM COUNT and the failure is
PROBABILISTIC — hence a 64-item cap AND a 256 kB budget: two bounds, because there are two ways for a
request to be too big (see the comment above `rag_common.EMBED_PAYLOAD_BUDGET_BYTES`).

Two things these tests exist to make impossible, both worse than the bug they replace:

* **A short result.** The caller zips this list against its chunks, so a silently-dropped input
  mis-pairs every vector after the gap — it would corrupt the index rather than skip it. Every
  case here asserts `len(result) == len(texts)` and the order.
* **A bound that discards.** An item larger than the entire budget is still SENT, alone. The bounds
  shrink a batch; they never decide what gets embedded.

No Ollama and no network — `rag_common._post_embed` is the seam, and the fake transport records
every request so the partition itself is asserted, not just the outcome.

Run:  python -m unittest seneschal.scripts.test_rag_common
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rag_common as rc  # noqa: E402

CFG = {"OLLAMA_URL": "http://localhost:11434", "EMBED_MODEL": "nomic-embed-text"}


class FakeOllama:
    """A recording stand-in for `_post_embed`.

    `requests` holds the `input` list of every call, so a test can assert the exact partition — the
    request COUNT alone would pass just as happily on a version that dropped half the batch.
    """

    def __init__(self, dim=4, fail_on=None, respond=None):
        self.requests: list[list[str]] = []
        self.dim = dim
        self.fail_on = fail_on          # 0-based index of a call that raises
        self.respond = respond          # optional: (index, batch) -> response dict

    def __call__(self, url, payload, timeout):
        i = len(self.requests)
        self.requests.append(list(payload["input"]))
        if self.fail_on == i:
            raise OSError("connection reset by peer")
        if self.respond is not None:
            out = self.respond(i, payload["input"])
            if out is not None:
                return out
        return {"embeddings": [[float(len(t))] * self.dim for t in payload["input"]]}


class _Patched(unittest.TestCase):
    """Swaps in the fake transport, and lets a test lower the two bounds, for one test only."""

    def use(self, fake):
        real = rc._post_embed
        rc._post_embed = fake
        self.addCleanup(lambda: setattr(rc, "_post_embed", real))
        return fake

    def bounds(self, budget=None, max_items=None):
        for name, value in (("EMBED_PAYLOAD_BUDGET_BYTES", budget),
                            ("EMBED_MAX_BATCH_ITEMS", max_items)):
            if value is not None:
                real = getattr(rc, name)
                setattr(rc, name, value)
                self.addCleanup(lambda n=name, v=real: setattr(rc, n, v))


class SubBatching(_Patched):
    def test_never_loses_an_input_and_keeps_the_order(self):
        # ~5 kB each against a 16 kB budget: 3 fit per request, so 10 items is 4 requests.
        texts = [f"{i:04d}" + "x" * 5000 for i in range(10)]
        fake = self.use(FakeOllama())
        self.bounds(budget=16 * 1024, max_items=1000)   # bytes are what binds here

        vecs = rc.embed_texts(texts, cfg=CFG)

        self.assertEqual(len(vecs), len(texts), "one vector per input, always")
        self.assertGreater(len(fake.requests), 1, "an over-budget batch must be split")
        # The partition, not just the count: concatenated in order it must BE the input.
        self.assertEqual([t for req in fake.requests for t in req], texts)
        # And each request must actually respect the budget. Exact, not approximate: the per-item
        # cost accounting (`+ 2` for `", "`) sums to precisely `len(json.dumps(list))`, so a
        # tolerance here would hide the off-by-one it is meant to catch (`+ 1` overshoots the
        # budget by ~1 byte per item).
        for req in fake.requests:
            encoded = len(json.dumps(req).encode("utf-8"))
            self.assertLessEqual(encoded, rc.EMBED_PAYLOAD_BUDGET_BYTES)

    def test_a_run_logs_worth_of_chunks_is_split_at_the_shipped_bounds(self):
        # The realistic case at the SHIPPED bounds, not test-shrunk ones: ~850 chunks of ~800 chars
        # is a ~700 kB single body. It must now be several requests, and still one vector each.
        texts = ["chunk %03d " % i + "word " * 158 for i in range(856)]
        fake = self.use(FakeOllama())
        vecs = rc.embed_texts(texts, cfg=CFG)
        self.assertEqual(len(vecs), 856)
        self.assertGreater(len(fake.requests), 1)
        self.assertEqual([t for req in fake.requests for t in req], texts)

    def test_no_request_ever_exceeds_the_item_cap(self):
        # THE bound that actually fixes the failure: Ollama opens one internal connection per input
        # item, so a big batch overruns its runner's listen backlog. A byte budget alone still
        # leaves requests of hundreds of tiny items.
        texts = [f"t{i}" for i in range(500)]
        fake = self.use(FakeOllama())
        rc.embed_texts(texts, cfg=CFG)
        self.assertTrue(all(len(r) <= rc.EMBED_MAX_BATCH_ITEMS for r in fake.requests))
        self.assertEqual([t for req in fake.requests for t in req], texts)

    def test_an_oversized_single_item_is_still_sent_alone(self):
        # A bound may shrink a batch. It may never drop an input: a chunk nothing embeds is a
        # hole in the index, and a raise here would take the other 2 chunks down with it.
        giant = "g" * (rc.EMBED_PAYLOAD_BUDGET_BYTES + 5000)
        texts = ["small a", giant, "small b"]
        fake = self.use(FakeOllama())

        vecs = rc.embed_texts(texts, cfg=CFG)

        self.assertEqual(len(vecs), 3)
        self.assertIn([giant], fake.requests, "the over-budget item rides in a slice of its own")
        self.assertEqual([t for req in fake.requests for t in req], texts)

    def test_an_under_budget_input_is_still_exactly_one_request(self):
        # The unchanged-behaviour guard: sub-batching must not turn every small ingest into a
        # request storm against a loopback server. This is `rag_query`'s single-query path and
        # `rag_projects`'s small batches.
        texts = ["a", "b", "c"]
        fake = self.use(FakeOllama())
        self.assertEqual(len(rc.embed_texts(texts, cfg=CFG)), 3)
        self.assertEqual(fake.requests, [texts])

    def test_empty_input_is_no_requests_at_all(self):
        fake = self.use(FakeOllama())
        self.assertEqual(rc.embed_texts([], cfg=CFG), [])
        self.assertEqual(fake.requests, [])


class SlicePartition(unittest.TestCase):
    """`_embed_slices` on its own — total, ordered, and bounded by BOTH limits."""

    def test_is_total_and_ordered(self):
        texts = [f"t{i}" * (i + 1) for i in range(50)]
        slices = rc._embed_slices(texts, budget=200, max_items=1000)
        self.assertEqual([t for s in slices for t in s], texts)
        self.assertTrue(all(s for s in slices), "no empty slice")

    def test_the_item_cap_binds_even_when_bytes_do_not(self):
        # Tiny texts: the byte budget would happily take all 100 in one request, and that is the
        # request shape that fails. Whichever bound binds first has to win.
        texts = [f"t{i}" for i in range(100)]
        slices = rc._embed_slices(texts, budget=10 ** 9, max_items=7)
        self.assertTrue(all(len(s) <= 7 for s in slices))
        self.assertEqual([t for s in slices for t in s], texts)

    def test_a_lone_oversized_item_gets_its_own_slice(self):
        slices = rc._embed_slices(["a", "z" * 500, "b"], budget=100, max_items=1000)
        self.assertIn(["z" * 500], slices)
        self.assertEqual([t for s in slices for t in s], ["a", "z" * 500, "b"])

    def test_empty_input_is_no_slices(self):
        self.assertEqual(rc._embed_slices([]), [])


class FailuresDoNotBecomePartialResults(_Patched):
    """Everything here would, if it returned instead of raising, write a mis-paired index."""

    def test_a_failure_in_the_second_sub_batch_raises(self):
        texts = ["x" * 5000 for _ in range(10)]
        self.bounds(budget=16 * 1024, max_items=1000)
        fake = self.use(FakeOllama(fail_on=1))

        with self.assertRaises(rc.OllamaError):
            rc.embed_texts(texts, cfg=CFG)
        self.assertEqual(len(fake.requests), 2, "it stops at the failure, it does not soldier on")

    def test_a_short_response_in_a_later_sub_batch_raises(self):
        # Per-sub-batch shape validation: a single up-front check would, on a multi-request path,
        # wave through every response after the first.
        texts = ["x" * 5000 for _ in range(10)]
        self.bounds(budget=16 * 1024, max_items=1000)

        def respond(i, batch):
            if i == 2:
                return {"embeddings": [[1.0, 2.0, 3.0, 4.0]]}   # one vector for a 3-item batch
            return None

        self.use(FakeOllama(respond=respond))
        with self.assertRaises(rc.OllamaError):
            rc.embed_texts(texts, cfg=CFG)

    def test_a_dim_change_between_sub_batches_raises(self):
        # `chunks.dim` is one width for the whole table and `unpack_vec` is handed it, so a ragged
        # batch would be packed and read back as garbage rather than refused.
        texts = ["x" * 5000 for _ in range(10)]
        self.bounds(budget=16 * 1024, max_items=1000)

        def respond(i, batch):
            if i >= 1:
                return {"embeddings": [[1.0] * 7 for _ in batch]}   # 7 wide, not 4
            return None

        self.use(FakeOllama(dim=4, respond=respond))
        with self.assertRaisesRegex(rc.OllamaError, "width changed"):
            rc.embed_texts(texts, cfg=CFG)

    def test_an_empty_vector_raises_rather_than_being_stored(self):
        self.use(FakeOllama(respond=lambda i, batch: {"embeddings": [[] for _ in batch]}))
        with self.assertRaises(rc.OllamaError):
            rc.embed_texts(["a", "b"], cfg=CFG)

    def test_an_unreachable_server_raises(self):
        self.use(FakeOllama(fail_on=0))
        with self.assertRaises(rc.OllamaError):
            rc.embed_texts(["a"], cfg=CFG)

    def test_a_non_dict_response_raises(self):
        self.use(FakeOllama(respond=lambda i, batch: ["not", "a", "dict"]))
        with self.assertRaises(rc.OllamaError):
            rc.embed_texts(["a"], cfg=CFG)


if __name__ == "__main__":
    unittest.main()
