#!/usr/bin/env python3
"""Unit tests for dcat_style.py decision logic, no sudo, no hardware.

Each test drives DcatStyle.step() (dry mode, synthetic tenants) with a crafted
measurement and checks one paper transition (Xu et al. EuroSys'18 §3.4-3.5,
Fig 6/7). The R1/R2 cases are the regressions found in the 2026-09-12 review
(DCAT_AUDIT.md §8); before the fix they failed exactly as their docstrings say.

Run: python3 test_dcat_style.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcat_style as D          # noqa: E402
import peer_metrics            # noqa: E402


def mk():
    """5 synthetic tenants, 3-way entitlement, all past their baseline."""
    c = D.DcatStyle(None, tempfile.mkdtemp(prefix="dcat_test_"), dry=True)
    for t in c.tenants:
        t.rebase_at = None; t.baseline_ipc = 1.0; t.baseline_mpi = 0.3
        t.state = D.KEEPER; t.ipc = 1.0
    return c


def M(ipc=1.0, miss=0.5, refs=5e7, mpi=0.3):
    return dict(ipc=ipc, miss_rate=miss, llc_ref_rate=refs, mem_per_ins=mpi, win=[0, 0])


def meas(c, **over):
    m = {t.name: M() for t in c.tenants}
    m.update(over)
    return m


def T(c, name):
    return next(t for t in c.tenants if t.name == name)


class TestPaperEdges(unittest.TestCase):
    def test_R1_explored_donor_stops_at_nontrivial_miss(self):
        """§3.4: D2 shrinks 'until the LLC miss rate becomes non-trivial
        (hence labeled as Keeper)'. Pre-fix: stayed Donor, 8 -> 3 ways."""
        c = mk(); x = T(c, "npb_cg_d80"); x.ways = 8; x.explored = True
        for t in c.tenants:
            if t is not x: t.ways = 1; t.state = D.STREAMING
        c.step(meas(c, npb_cg_d80=M(miss=0.01)))
        self.assertEqual((x.state, x.ways), (D.DONOR, 7))
        for _ in range(3):
            c.step(meas(c, npb_cg_d80=M(miss=0.10)))
            self.assertEqual((x.state, x.ways), (D.KEEPER, 7))

    def test_R2_unknown_with_trivial_miss_stops_growing(self):
        """Fig 7a: growth stops once the miss rate is below the threshold.
        Pre-fix: a judged Unknown was never re-categorized, grew to pool-dry."""
        c = mk(); x = T(c, "canneal")
        x.state = D.UNKNOWN; x.ways = 4; x.grew = True; x.ref_ipc = 1.0; x.grow_steps = 1
        for t in c.tenants:
            if t is not x: t.ways = 2
        others = {n: M(miss=0.02) for n in
                  ("llama", "npb_cg_d80", "npb_ep_e", "npb_mg_d300")}
        c.step(meas(c, canneal=M(ipc=1.02, miss=0.005), **others))
        self.assertEqual((x.state, x.ways), (D.KEEPER, 4))
        self.assertFalse(x.grew)

    def test_R2_idle_receiver_donates(self):
        """Fig 6 Receiver -> Donor on 'low cache references'. Pre-fix: an idle
        Receiver (unchanged L1 loads/instr, so no phase Reclaim) kept 7 ways."""
        c = mk(); x = T(c, "npb_cg_d80"); x.state = D.RECEIVER; x.ways = 7
        for t in c.tenants:
            if t is not x: t.ways = 2
        c.step(meas(c, npb_cg_d80=M(refs=1e3)))
        self.assertEqual((x.state, x.donor_kind, x.ways), (D.DONOR, "D1", D.MIN_WAYS))

    def test_reclaim_above_entitlement_returns_excess(self):
        c = mk(); x = T(c, "npb_cg_d80"); x.ways = 8
        for t in c.tenants:       # pinned bystanders, so nobody grows into the pool
            if t is not x: t.ways = 1; t.state = D.STREAMING
        c.step(meas(c, npb_cg_d80=M(mpi=0.5)))          # +67% loads/instr
        self.assertEqual(x.ways, x.entitle)
        self.assertEqual(c.pool(), 15 - x.entitle - 4)
        self.assertIsNotNone(x.rebase_at)

    def test_guarantee_restores_pinned_tenant(self):
        """Fig 6 Donor -> Receiver on 'performance degradation' [F3]."""
        c = mk(); x = T(c, "canneal"); x.state = D.STREAMING; x.ways = 1
        y = T(c, "npb_cg_d80"); y.ways = 5
        c.step(meas(c, canneal=M(ipc=0.8)))
        self.assertEqual((x.state, x.ways, x.floor_hold), (D.RECEIVER, 3, True))
        self.assertEqual(y.ways, 3)                      # harvested from the excess

    def test_pooldry_pins_flat_unknown(self):
        """§3.4: 'all the available cache size is used ... still Unknown' [F2]."""
        c = mk(); x = T(c, "llama")
        x.state = D.UNKNOWN; x.ways = 4; x.grow_steps = 1; x.best_norm = 1.0
        x.explored = False
        for t in c.tenants:
            if t is not x: t.ways = 3 if t.name != "npb_ep_e" else 2
        c.step(meas(c))
        self.assertEqual((x.state, x.ways), (D.STREAMING, D.MIN_WAYS))


class TestPlumbing(unittest.TestCase):
    def test_llc_loads_logged_not_decided(self):
        self.assertIn("LLC-loads", D.PERF_EVENTS)
        self.assertNotIn("LLC-loads", D.EVENTS)

    def test_apply_records(self):
        """in-loop writes must be `apply` (the harness actuation counters)."""
        out = tempfile.mkdtemp(prefix="dcat_test_")
        D.random.seed(1)
        D.DcatStyle(None, out, dry=True).run(120)
        kinds = [json.loads(l)["kind"] for l in open(os.path.join(out, "decisions.jsonl"))]
        self.assertEqual(kinds.count("entitle"), 1)
        self.assertGreater(kinds.count("apply"), 0)
        self.assertNotIn("masks", kinds)


class TestTailSeconds(unittest.TestCase):
    def _perf(self, step, n):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        for i in range(1, n + 1):
            ts = round(i * step, 6)
            ipc = 1.0 if i <= n - 60 / step else 2.0     # last 60 s at IPC 2
            f.write(json.dumps({"interval": ts, "counter-value": str(ipc * 1e9),
                                "event": "instructions"}) + "\n")
            f.write(json.dumps({"interval": ts, "counter-value": "1e9",
                                "event": "cycles"}) + "\n")
        f.close()
        return f.name

    def test_equal_seconds_across_interval_lengths(self):
        for step in (2.018, 1.0, 0.1):
            f = self._perf(step, int(200 / step))
            self.assertAlmostEqual(peer_metrics.true_ipc(f, tail_s=60), 2.0, msg=step)

    def test_count_tail_unchanged(self):
        f = self._perf(2.0, 100)
        self.assertEqual(peer_metrics.true_ipc(f, tail=30), 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
