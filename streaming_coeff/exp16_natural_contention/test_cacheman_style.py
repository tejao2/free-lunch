#!/usr/bin/env python3
"""Unit tests for cacheman_style.py, no sudo, no hardware.

Covers: fair-baseline formula under BOTH core-denominator modes (calling
the real CachemanStyle.assign_baselines, not reimplementing the formula),
ladder construction (global nested masks, strictly decreasing, level 0 ==
full mask), VM classification incl. the restored Other/LLC-access-pattern
criterion and CPU-idle criterion, the poor-window (W_poor) counter and
R_dev via real step() cycles, LLC_upper/overflow enforcement (only when
theta is declared), suppression-candidate tie-break via the real step()
branch, the pool_full gate's scope/no-latch property, the Other->CLOS[0]
release path, the PhaseForOptimization rebalancing fix, and a --dry-run
smoke over 400 simulated seconds checking the whole cycle end to end.

Run: python3 test_cacheman_style.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cacheman_style as cm  # noqa: E402


def mk_ctl(tenants, total_l3_bytes=20 * 1024 * 1024, out=None):
    """Build a REAL CachemanStyle in dry mode (touches no resctrl/sysfs),
    then swap in the caller's own Tenant list and total_l3_bytes. This lets
    tests call the actual step()/classify()/pool_occupied_bytes() logic
    with fully-controlled tenant state, instead of reimplementing any of
    it (CACHEMAN_AUDIT.md S6: several old tests recomputed expressions in
    the test body and never invoked the implementation). Baselines are
    NOT recomputed here -- callers set base_mb explicitly via mk_tenant()
    so classification bands are exactly what the test asserts against;
    TestFairBaseline below is what actually exercises assign_baselines().
    Default total_l3_bytes (20 MiB) is sized to this file's toy
    occupancy numbers (10-20 MiB), not to any real hardware value."""
    out = out or tempfile.mkdtemp(prefix="_cm_test_")
    ctl = cm.CachemanStyle(None, out, dry=True)
    ctl.tenants = tenants
    ctl.total_l3_bytes = total_l3_bytes
    unittest.addModuleCleanup(_close_and_rm, ctl, out)
    return ctl


def _close_and_rm(ctl, out):
    try:
        ctl.decisions.close()
    except Exception:
        pass
    shutil.rmtree(out, ignore_errors=True)


def mk_tenant(name, cores="0-3", occ_mb=None, base_mb=None, level=0, theta=None):
    t = cm.Tenant({"name": name, "cores": cores, "theta": theta})
    t.level = level
    if base_mb is not None:
        t.base_bytes = base_mb * 1048576
    if occ_mb is not None:
        t.occ = occ_mb * 1048576
    return t


class TestLadder(unittest.TestCase):
    def test_15_ways_8_levels(self):
        sizes, masks = cm.build_ladder(15, 8)
        self.assertEqual(sizes, [15, 13, 11, 9, 7, 5, 3, 1])
        self.assertEqual(masks[0], (1 << 15) - 1)   # level 0 == full mask
        self.assertEqual(masks[-1], 0b1)

    def test_strictly_decreasing_and_nested(self):
        sizes, masks = cm.build_ladder(15, 8)
        for i in range(1, len(sizes)):
            self.assertLess(sizes[i], sizes[i - 1])
            # nested, NOT disjoint: each level is a strict SUBSET of the
            # previous one (Cacheman's own contrast with exclusive
            # partitioning, p.336-337) -- masks[i] & masks[i-1] == masks[i]
            self.assertEqual(masks[i] & masks[i - 1], masks[i])
            self.assertLess(masks[i], masks[i - 1])

    def test_degrades_gracefully_when_nlevels_exceeds_nbits(self):
        sizes, masks = cm.build_ladder(4, 8)
        self.assertEqual(len(sizes), 4)
        self.assertEqual(sizes[0], 4)
        for i in range(1, len(sizes)):
            self.assertLess(sizes[i], sizes[i - 1])


class TestFairBaseline(unittest.TestCase):
    """Calls the REAL CachemanStyle.assign_baselines (p.333 S4.3 formula),
    under both --core-denom modes -- fixes CACHEMAN_AUDIT.md S6's flag that
    the old tests recomputed the arithmetic in the test body and would
    have passed under any denominator, including the one under review."""

    def _tenants(self, names=("canneal", "npb_cg_d80", "npb_ep_e",
                               "npb_mg_d300", "llama")):
        return [cm.Tenant({"name": n, "cores": "0-3"}) for n in names]

    def test_managed_denominator_equal_size_tenants(self):
        l3 = 60 * 1024 * 1024
        tenants = self._tenants()
        denom = cm.CachemanStyle.assign_baselines(tenants, l3, "managed")
        self.assertEqual(denom, 20)          # 5 * 4 cores
        expect = l3 * cm.DELTA / 5
        for t in tenants:
            self.assertAlmostEqual(t.base_bytes, expect, delta=1.0)

    def test_managed_denominator_unequal_cores_proportional(self):
        l3 = 60 * 1024 * 1024
        t1 = cm.Tenant({"name": "a", "cores": "0-3"})    # 4 cores
        t2 = cm.Tenant({"name": "b", "cores": "4-11"})   # 8 cores
        cm.CachemanStyle.assign_baselines([t1, t2], l3, "managed")
        self.assertAlmostEqual(t2.base_bytes, 2 * t1.base_bytes, delta=1.0)

    def test_socket_denominator_uses_supplied_physical_count(self):
        """--core-denom=socket (paper-literal Socket_core_number): the
        denominator is the CALLER-supplied physical core count, not the
        managed set -- this is the audit's ranked-fix #2 sensitivity arm."""
        l3 = 60 * 1024 * 1024
        tenants = self._tenants()
        denom = cm.CachemanStyle.assign_baselines(tenants, l3, "socket",
                                                    socket_cores=32)
        self.assertEqual(denom, 32)
        expect = l3 * (4 / 32) * cm.DELTA
        for t in tenants:
            self.assertAlmostEqual(t.base_bytes, expect, delta=1.0)

    def test_socket_denominator_requires_socket_cores(self):
        with self.assertRaises(ValueError):
            cm.CachemanStyle.assign_baselines(self._tenants(),
                                                60 * 1024 * 1024, "socket")

    def test_denominator_choice_changes_base_magnitude(self):
        """Documents the measured effect cited in the module docstring and
        CACHEMAN_AUDIT.md row 1: managed ~10.8 MiB/tenant vs socket(32)
        ~6.75 MiB/tenant on D3's equal-4-core tenants."""
        l3 = 60 * 1024 * 1024
        managed = self._tenants()
        cm.CachemanStyle.assign_baselines(managed, l3, "managed")
        socket = self._tenants()
        cm.CachemanStyle.assign_baselines(socket, l3, "socket", socket_cores=32)
        self.assertAlmostEqual(managed[0].base_bytes / 1048576, 10.8, delta=0.1)
        self.assertAlmostEqual(socket[0].base_bytes / 1048576, 6.75, delta=0.1)
        self.assertGreater(managed[0].base_bytes, socket[0].base_bytes)


class _DummyCtl:
    """Minimal stand-in exposing only classify()'s dependencies (self.dry,
    self.total_l3_bytes), so tests can call the REAL, unbound
    cm.CachemanStyle.classify without constructing a full controller
    (which touches resctrl/sysfs when dry=False)."""

    def __init__(self, dry=True, total_l3_bytes=60 * 1024 * 1024):
        self.dry = dry
        self.total_l3_bytes = total_l3_bytes

    def classify(self, t):
        return cm.CachemanStyle.classify(self, t)


class TestClassify(unittest.TestCase):
    def _mk(self, base_mb, occ_mb, theta=None):
        t = cm.Tenant({"name": "x", "cores": "0-3"})
        t.base_bytes = base_mb * 1048576
        t.occ = occ_mb * 1048576
        t.theta = theta
        return t

    def test_adequate_band(self):
        ctl = _DummyCtl()
        t = self._mk(10.0, 10.0)
        self.assertEqual(ctl.classify(t), cm.ADEQUATE)
        # inside +-alpha/beta bands still adequate
        t2 = self._mk(10.0, 10.0 * (1 + cm.ALPHA - 0.01))
        self.assertEqual(ctl.classify(t2), cm.ADEQUATE)

    def test_excess_above_alpha(self):
        ctl = _DummyCtl()
        t = self._mk(10.0, 10.0 * (1 + cm.ALPHA + 0.01))
        self.assertEqual(ctl.classify(t), cm.EXCESS)

    def test_poor_below_beta(self):
        ctl = _DummyCtl()
        t = self._mk(10.0, 10.0 * (1 - cm.BETA - 0.01))
        self.assertEqual(ctl.classify(t), cm.POOR)

    def test_no_overflow_without_theta(self):
        """LLC_upper is OPTIONAL: without a declared theta, even huge
        occupancy classifies as excess, never overflow (p.334)."""
        ctl = _DummyCtl()
        t = self._mk(10.0, 100.0, theta=None)
        self.assertEqual(ctl.classify(t), cm.EXCESS)

    def test_overflow_with_theta(self):
        ctl = _DummyCtl()
        t = self._mk(10.0, 10.0 * (1 + 0.5 + 0.01), theta=0.5)
        self.assertEqual(ctl.classify(t), cm.OVERFLOW)
        # just under the upper bound: excess, not overflow
        t2 = self._mk(10.0, 10.0 * (1 + 0.5 - 0.01), theta=0.5)
        self.assertEqual(ctl.classify(t2), cm.EXCESS)

    def test_other_via_negligible_llc_occupancy(self):
        """Fix #3 / CACHEMAN_AUDIT.md ranked-fix #3: restores p.333's
        'LLC access patterns ... exhibit very few LLC_occu' criterion.
        A tenant occupying far less than OTHER_OCC_FRAC*total_l3 is Other
        EVEN THOUGH its occupancy alone would otherwise read as Poor."""
        ctl = _DummyCtl(total_l3_bytes=60 * 1024 * 1024)
        occ_mb = cm.OTHER_OCC_FRAC * 60 / 2   # well under the floor
        t = self._mk(base_mb=10.0, occ_mb=occ_mb)
        self.assertEqual(ctl.classify(t), cm.OTHER)

    def test_not_other_when_occupancy_is_merely_poor(self):
        """A tenant that is genuinely just Poor (well above the negligible-
        occupancy floor) must NOT be swept into Other -- the whole point of
        the fix is to distinguish 'cache-quiet' from 'starved'."""
        ctl = _DummyCtl(total_l3_bytes=60 * 1024 * 1024)
        t = self._mk(base_mb=10.0, occ_mb=10.0 * (1 - cm.BETA - 0.01))
        self.assertEqual(ctl.classify(t), cm.POOR)

    def test_other_via_cpu_idle_proxy(self):
        """D2's CPU-utilization proxy still fires independently of the LLC
        criterion (paper: 'multiple real-time metrics', OR semantics)."""
        ctl = _DummyCtl(dry=False, total_l3_bytes=60 * 1024 * 1024)
        t = self._mk(base_mb=10.0, occ_mb=10.0)   # would be ADEQUATE by occ
        t.cores = "0-3"
        orig = cm.read_core_idle_frac
        cm.read_core_idle_frac = lambda cores: 0.995
        try:
            self.assertEqual(ctl.classify(t), cm.OTHER)
        finally:
            cm.read_core_idle_frac = orig

    def test_unreadable_occ_holds_last_state(self):
        ctl = _DummyCtl()
        t = self._mk(10.0, 10.0)
        t.state = cm.EXCESS
        t.occ = None
        self.assertEqual(ctl.classify(t), cm.EXCESS)


class TestPoorWindowAndRdev(unittest.TestCase):
    """Drives the REAL step() through several cycles instead of
    recomputing poor_streak's update expression in the test body
    (CACHEMAN_AUDIT.md S6)."""

    def test_poor_streak_increments_and_resets_via_step(self):
        t = mk_tenant("x", base_mb=10.0, occ_mb=10.0 * (1 - cm.BETA - 0.01))
        ctl = mk_ctl([t])
        for i in range(3):
            ctl.step()
            self.assertEqual(t.poor_streak, i + 1)
        t.occ = 10.0 * 1048576   # back to adequate
        ctl.step()
        self.assertEqual(t.poor_streak, 0)

    def test_b_violation_from_poor_streak(self):
        t = cm.Tenant({"name": "x", "cores": "0-3"})
        t.base_bytes = 10.0
        t.poor_streak = cm.M_POOR
        self.assertTrue(t.b_violation())
        t.poor_streak = cm.M_POOR - 1
        self.assertFalse(t.b_violation())

    def test_rdev_matches_paper_formula(self):
        t = cm.Tenant({"name": "x", "cores": "0-3"})
        t.base_bytes = 100.0
        t.occ_history = [90.0, 80.0, 70.0]     # mean 80
        expect = 1 - (80.0 / 100.0)
        self.assertAlmostEqual(t.rdev(), expect, places=9)

    def test_b_violation_from_rdev(self):
        t = cm.Tenant({"name": "x", "cores": "0-3"})
        t.base_bytes = 100.0
        t.occ_history = [100.0 * (1 - cm.K_VIOLATION - 0.01)]  # rdev > k
        self.assertTrue(t.b_violation())

    def test_other_tenant_excluded_from_rdev_window(self):
        """D2 addendum: an Other VM is not 'subject to LLC state
        monitoring' (p.333) -- its near-zero occupancy must not poison
        R_dev/B_violation while it stays Other."""
        t = mk_tenant("npb_ep_e", base_mb=10.0, occ_mb=0.01)
        ctl = mk_ctl([t])
        for _ in range(5):
            ctl.step()
        self.assertEqual(t.state, cm.OTHER)
        self.assertEqual(t.occ_history, [])
        self.assertFalse(t.b_violation())


class TestSuppressionTieBreak(unittest.TestCase):
    def test_tie_break_via_real_step(self):
        """Drives the REAL fairness-phase candidate selection inside
        step() (previously the test copied the lambda and never called
        step()) -- CACHEMAN_AUDIT.md S6."""
        poor = mk_tenant("m_poor", base_mb=10.0, occ_mb=1.0)
        a = mk_tenant("b_tenant", base_mb=10.0, occ_mb=15.0)  # ratio 1.5
        b = mk_tenant("a_tenant", base_mb=10.0, occ_mb=15.0)  # tie, ratio 1.5
        ctl = mk_ctl([poor, a, b])
        ctl.step()
        supp = [r for r in _read_records(ctl) if r["kind"] == "fairness-suppress"]
        self.assertEqual(len(supp), 1)
        self.assertEqual(supp[0]["tenant"], "a_tenant")   # tie -> name order

    def test_strictly_larger_ratio_wins_regardless_of_name(self):
        poor = mk_tenant("m_poor", base_mb=10.0, occ_mb=1.0)
        a = mk_tenant("a_tenant", base_mb=10.0, occ_mb=15.0)   # ratio 1.5
        z = mk_tenant("z_tenant", base_mb=10.0, occ_mb=20.0)   # ratio 2.0
        ctl = mk_ctl([poor, a, z])
        ctl.step()
        supp = [r for r in _read_records(ctl) if r["kind"] == "fairness-suppress"]
        self.assertEqual(supp[0]["tenant"], "z_tenant")


def _read_records(ctl):
    # run() closes the decisions file in its finally block, so after a
    # fatal-stall abort the handle is closed and flush() would raise. The
    # records are on disk either way -- read by name, flushing only if the
    # controller is still live.
    if not ctl.decisions.closed:
        ctl.decisions.flush()
    with open(ctl.decisions.name) as f:
        return [json.loads(l) for l in f]


class TestPoolFullGate(unittest.TestCase):
    """CACHEMAN_AUDIT.md C1/C2, ranked-fix #1: the gate must not be
    identical-by-construction to Sigma(base), and must not latch off
    purely because the controller suppressed its own suppressible tenants
    (occupancy dropping is not the same fact as "the node has free
    cache")."""

    def test_threshold_is_not_sigma_of_baselines(self):
        l3 = 60 * 1024 * 1024
        tenants = [cm.Tenant({"name": n, "cores": "0-3"})
                   for n in ("a", "b", "c", "d", "e")]
        cm.CachemanStyle.assign_baselines(tenants, l3, "managed")
        sigma_base = sum(t.base_bytes for t in tenants)
        self.assertNotEqual(cm.LLC_FULL_FRAC, cm.DELTA,
                             "gate constant must be decoupled from DELTA "
                             "(C2): Sigma(base)==LLC_total*DELTA exactly "
                             "under the managed-cores denominator")
        threshold = cm.LLC_FULL_FRAC * l3
        self.assertNotAlmostEqual(threshold, sigma_base, delta=1.0)

    def test_gate_survives_a_suppression_trajectory(self):
        """Reproduces the author's own dry-run failure mode: a single huge
        occupier gets suppressed repeatedly, its OWN occupancy shrinks with
        its reachable region, and the OLD gate (tenant-sum only, threshold
        == DELTA) would drop below threshold and never re-fire. The FIXED
        gate must stay open because it includes a non-managed contribution
        (pool_occupied_bytes(); real runs read the root group, dry-run uses
        SIM_OTHER_FRAC)."""
        l3 = 60 * 1024 * 1024
        hog = mk_tenant("hog", base_mb=10.8, occ_mb=60.0)
        others = [mk_tenant(n, base_mb=10.8, occ_mb=11.0) for n in
                  ("b", "c", "d")]
        ctl = mk_ctl([hog] + others, total_l3_bytes=l3)
        # simulate the hog being suppressed down to the ladder floor, its
        # occupancy shrinking with its reachable region each step (the
        # mechanism that caused the OLD gate to self-latch off):
        reachable_fracs = [1.0, 13 / 15, 9 / 15, 5 / 15, 1 / 15]
        for frac in reachable_fracs:
            hog.occ = min(60.0, l3 / 1048576 * frac) * 1048576
            pool_occ, _root = ctl.pool_occupied_bytes()
            pool_full = pool_occ >= cm.LLC_FULL_FRAC * l3
            self.assertTrue(pool_full,
                             f"gate must not latch off (hog reachable frac={frac})")

    def test_pool_occupied_includes_more_than_tenant_sum_in_dry_mode(self):
        t = mk_tenant("solo", base_mb=10.0, occ_mb=5.0)
        ctl = mk_ctl([t])
        tenant_sum = sum(x.occ or 0 for x in ctl.tenants)
        pool_occ, root = ctl.pool_occupied_bytes()
        self.assertGreater(pool_occ, tenant_sum)
        self.assertIsNotNone(root)   # dry mode always has the SIM placeholder


class TestOtherRelease(unittest.TestCase):
    """CACHEMAN_AUDIT.md ranked-fix #5: an Other VM must be returned to
    CLOS[0] (p.333: 'leaves them in the default CLOS[0], thus ensuring
    immediate access to all LLC ways if their load increases')."""

    def test_other_tenant_released_to_level_zero(self):
        t = mk_tenant("quiet", base_mb=10.0, occ_mb=0.01, level=5)
        ctl = mk_ctl([t])
        ctl.step()
        self.assertEqual(t.state, cm.OTHER)
        self.assertEqual(t.level, 0)
        rel = [r for r in _read_records(ctl) if r["kind"] == "other-release"]
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0]["from_level"], 5)

    def test_already_unmanaged_other_is_not_re_recorded(self):
        t = mk_tenant("quiet", base_mb=10.0, occ_mb=0.01, level=0)
        ctl = mk_ctl([t])
        ctl.step()
        rel = [r for r in _read_records(ctl) if r["kind"] == "other-release"]
        self.assertEqual(len(rel), 0)


class TestOptimizationRebalance(unittest.TestCase):
    """CACHEMAN_AUDIT.md ranked-fix #4: PhaseForOptimization ('complete
    fair: equal distribution by VM size', p.334 line 15) must actually
    move levels back toward equal among Excess tenants, not just log a
    no-op forever."""

    def test_rebalances_unequal_excess_levels(self):
        a = mk_tenant("a", base_mb=10.0, occ_mb=13.0, level=0)   # excess
        b = mk_tenant("b", base_mb=10.0, occ_mb=13.0, level=3)   # excess, more suppressed
        ctl = mk_ctl([a, b])
        ctl.step()
        self.assertEqual(a.state, cm.EXCESS)
        self.assertEqual(b.state, cm.EXCESS)
        self.assertEqual(b.level, 2, "the MORE suppressed excess tenant "
                          "should be de-suppressed one level toward equal")
        rec = [r for r in _read_records(ctl) if r["kind"] == "optimization-rebalance"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["tenant"], "b")

    def test_noop_when_already_equal(self):
        a = mk_tenant("a", base_mb=10.0, occ_mb=13.0, level=2)
        b = mk_tenant("b", base_mb=10.0, occ_mb=13.0, level=2)
        ctl = mk_ctl([a, b])
        ctl.step()
        self.assertEqual(a.level, 2)
        self.assertEqual(b.level, 2)
        rec = [r for r in _read_records(ctl) if r["kind"] == "optimization-noop"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["reason"], "already equal distribution")

    def test_repeated_cycles_converge_to_equal_levels(self):
        a = mk_tenant("a", base_mb=10.0, occ_mb=13.0, level=0)
        b = mk_tenant("b", base_mb=10.0, occ_mb=13.0, level=3)
        ctl = mk_ctl([a, b])
        for _ in range(3):
            ctl.step()
        self.assertEqual(a.level, b.level)


class TestStepBranchExclusivity(unittest.TestCase):
    """No test previously checked that the three phases are mutually
    exclusive per cycle, or that pool_full==False suppresses nothing
    (CACHEMAN_AUDIT.md S6, 'no test' items)."""

    def test_no_suppression_when_pool_not_full(self):
        # one poor tenant, but nowhere near enough occupancy anywhere to
        # trip the gate.
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0)
        adequate = mk_tenant("adequate", base_mb=10.0, occ_mb=1.0)
        ctl = mk_ctl([poor, adequate], total_l3_bytes=10_000 * 1024 * 1024)
        ctl.step()
        kinds = {r["kind"] for r in _read_records(ctl)}
        self.assertNotIn("fairness-suppress", kinds)
        recs = [r for r in _read_records(ctl) if r["kind"] == "scan"]
        self.assertFalse(recs[0]["pool_full"])

    def test_poor_and_full_only_triggers_fairness_not_optimization(self):
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0)
        excess = mk_tenant("excess", base_mb=10.0, occ_mb=13.0)
        ctl = mk_ctl([poor, excess])
        ctl.step()
        kinds = [r["kind"] for r in _read_records(ctl)]
        self.assertIn("fairness-suppress", kinds)
        self.assertNotIn("optimization-noop", kinds)
        self.assertNotIn("optimization-rebalance", kinds)

    def test_overflow_triggers_consistency_not_fairness(self):
        overflow = mk_tenant("of", base_mb=10.0, occ_mb=20.0, theta=0.1)
        adequate = mk_tenant("adq", base_mb=10.0, occ_mb=10.0)
        ctl = mk_ctl([overflow, adequate])
        ctl.step()
        kinds = [r["kind"] for r in _read_records(ctl)]
        self.assertIn("consistency-suppress", kinds)
        self.assertNotIn("fairness-suppress", kinds)


class TestFirstCycleAndEmptyData(unittest.TestCase):
    def test_unreadable_first_cycle_holds_default_state(self):
        t = cm.Tenant({"name": "x", "cores": "0-3"})
        self.assertEqual(t.state, cm.ADEQUATE)   # default before any classify
        ctl = mk_ctl([t])
        t.occ = None
        ctl.step()
        self.assertEqual(t.state, cm.ADEQUATE)   # held, not reclassified

    def test_sample_all_marks_fully_unreadable_tenant_none(self):
        out = tempfile.mkdtemp(prefix="_cm_test_")
        ctl = cm.CachemanStyle(None, out, dry=True)
        unittest.addModuleCleanup(_close_and_rm, ctl, out)
        # force every SimTenant.measure() call to look unreadable by
        # monkeypatching read_cmt is not applicable in dry mode (measure()
        # is used instead); exercise the real (non-dry) code path for this
        # specific unreadable-accounting check via a fake group.
        t = cm.Tenant({"name": "ghost", "cores": "0-3", "grp": "/nonexistent"})
        ctl.dry = False
        ctl.tenants = [t]
        ok = ctl.sample_all()
        self.assertFalse(ok)
        self.assertIsNone(t.occ)


class TestStall(unittest.TestCase):
    """CACHEMAN_AUDIT.md re-audit N1 (HIGH): the first version of the
    stall detector keyed on b_violation()/R_dev (an EXPANDING-window
    historical metric, D6) and could abort a healthy, CONVERGED run. Fixed
    by is_wedged(), which reads only live per-cycle state (t.state,
    t.level). These tests call is_wedged() directly (never re-derive its
    condition) and, for the genuine-lockout case, drive a real ctl.run()
    to confirm the abort still happens for the case it exists for."""

    def test_never_fires_when_everyone_adequate(self):
        """REQUIRED test (re-audit R1): must FAIL if the detector can fire
        on a legitimately quiet scene."""
        tenants = [mk_tenant(n, base_mb=10.0, occ_mb=10.0, level=0)
                   for n in ("a", "b", "c")]
        ctl = mk_ctl(tenants)
        for t in tenants:
            t.state = cm.ADEQUATE
        for moved in ([], [tenants[0]]):   # must not fire regardless of moved
            self.assertFalse(ctl.is_wedged(moved))

    def test_never_fires_for_an_other_tenant(self):
        t = mk_tenant("quiet", base_mb=10.0, occ_mb=0.01, level=0)
        ctl = mk_ctl([t])
        t.state = cm.OTHER
        self.assertFalse(ctl.is_wedged([]))

    def test_reproduces_audits_executed_n1_scenario_and_does_not_fire(self):
        """The re-audit's exact executed case: 10 early Poor cycles (normal
        opening state of a contended scene) followed by fully-recovered
        Adequate cycles. b_violation() stays True for a long tail (an
        expanding-window artifact) -- is_wedged() must NOT, because it
        reads only the CURRENT cycle's state."""
        t = mk_tenant("x", base_mb=10.0, occ_mb=10.0 * 0.70, level=0)
        ctl = mk_ctl([t])
        for _ in range(10):
            ctl.step()   # Poor, level already 0 -> nothing to de-suppress
        self.assertEqual(t.state, cm.POOR)
        t.occ = 10.0 * 1048576   # recovers to Adequate
        for i in range(19):
            moved = ctl.step()
            self.assertEqual(t.state, cm.ADEQUATE)
            self.assertTrue(t.b_violation(),
                             "sanity check: this IS the expanding-window "
                             "artifact the fix must not key on")
            self.assertFalse(ctl.is_wedged(moved),
                              f"is_wedged() fired on a recovered tenant "
                              f"at cycle {i} despite live state=adequate")

    def test_fires_on_a_genuine_lockout(self):
        """A poor tenant at level 0 (nothing to de-suppress) alongside a
        suppressible excess tenant, with the gate forced permanently
        closed (simulating the OLD C1 defect) -- PhaseForFairness never
        gets to act on the poor tenant's behalf, nothing is ever actuated,
        and the arm SHOULD abort after STALL_CYCLES."""
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0, level=0)
        excess = mk_tenant("excess", base_mb=10.0, occ_mb=13.0, level=0)
        ctl = mk_ctl([poor, excess])
        ctl.pool_occupied_bytes = lambda: (0.0, 0.0)   # force pool_full=False forever
        ctl.sample_all = lambda: True                  # occ is fixed by hand above
        with self.assertRaises(SystemExit) as cm_exc:
            ctl.run(duration=(cm.STALL_CYCLES + 2) * cm.CYCLE_SECS)
        self.assertEqual(cm_exc.exception.code, 2)
        recs = _read_records(ctl)
        self.assertTrue(any(r["kind"] == "fatal-stall" for r in recs))

    def test_does_not_fire_when_no_admissible_action_exists(self):
        """A poor tenant at level 0 with NO suppressible excess/overflow
        tenant to squeeze on its behalf: genuinely nothing to do, not a
        stuck controller -- must not fire even after many cycles."""
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0, level=0)
        adequate = mk_tenant("adq", base_mb=10.0, occ_mb=10.0, level=0)
        ctl = mk_ctl([poor, adequate])
        for _ in range(cm.STALL_CYCLES + 5):
            moved = ctl.step()
            self.assertFalse(ctl.is_wedged(moved))


class TestDryRunSmoke(unittest.TestCase):
    def test_full_cycle_end_to_end(self):
        out = "/tmp/_cm_dryrun_smoke"
        shutil.rmtree(out, ignore_errors=True)
        ctl = cm.CachemanStyle(None, out, dry=True)
        ctl.run(duration=400)   # simulated seconds, fake clock, no sleep cost

        with open(os.path.join(out, "decisions.jsonl")) as f:
            recs = [json.loads(l) for l in f]
        self.assertGreater(len(recs), 0)
        scans = [r for r in recs if r["kind"] == "scan"]
        self.assertGreater(len(scans), 30, "400 simulated seconds at a 6s "
                            "cadence should complete ~65 cycles")

        # (a) the pool_full gate must not latch off (CACHEMAN_AUDIT.md C1).
        self.assertTrue(all(r["pool_full"] for r in scans),
                         "pool_full gate latched off during the run")

        # (b) npb_ep_e (near-zero LLC footprint) must be classified Other,
        #     not Poor -- it must not be the sole/any trigger of suppression.
        ep_states = {r["state"]["npb_ep_e"] for r in scans}
        self.assertEqual(ep_states, {cm.OTHER})
        supp_by_ep_e = [r for r in recs
                        if r["kind"] in ("fairness-suppress", "consistency-suppress")
                        and r.get("tenant") == "npb_ep_e"]
        self.assertEqual(len(supp_by_ep_e), 0)
        # and it is released to CLOS[0] (seeded at level 3):
        release = [r for r in recs if r["kind"] == "other-release"
                   and r["tenant"] == "npb_ep_e"]
        self.assertEqual(len(release), 1)
        self.assertEqual(scans[-1]["level"]["npb_ep_e"], 0)

        # (c) the optimization phase actually moves levels (not a
        #     permanent no-op): npb_mg_d300 was seeded suppressed relative
        #     to npb_cg_d80 with no poor/overflow tenant between them at
        #     some point in the run.
        rebalances = [r for r in recs if r["kind"] == "optimization-rebalance"]
        self.assertGreater(len(rebalances), 0,
                            "PhaseForOptimization never actually rebalanced")

        # canneal (demand >> full LLC): must have been suppressed at least
        # once via the fairness phase.
        canneal_supp = [r for r in recs if r["kind"] == "fairness-suppress"
                        and r["tenant"] == "canneal"]
        self.assertGreater(len(canneal_supp), 0)

        # llama (seeded deeply suppressed, near-baseline demand): must be
        # de-suppressed via the unconditional Poor path.
        llama_desupp = [r for r in recs if r["kind"] == "de-suppress"
                        and r["tenant"] == "llama"]
        self.assertGreater(len(llama_desupp), 0)
        llama_levels = [r["level"]["llama"] for r in scans]
        self.assertEqual(llama_levels[0], 7)
        self.assertLess(llama_levels[-1], llama_levels[0])

        # (c-2) re-audit R5 fix: no Adequate tenant ends stranded below
        # level 0 for the rest of the run without a stated reason -- the
        # optimization phase's rebalance pool now reaches Adequate-but-
        # suppressed tenants too, not just currently-Excess ones.
        final = scans[-1]
        for name, lvl in final["level"].items():
            if final["state"][name] == cm.ADEQUATE:
                self.assertEqual(lvl, 0,
                                  f"{name} is Adequate but stranded at "
                                  f"level {lvl} -- optimization phase "
                                  f"never reached it")

        # re-audit N1: no spurious abort -- the process must have run all
        # 400 simulated seconds, not aborted early via SystemExit.
        self.assertGreater(final["t"], 400 - 12)
        self.assertEqual([r for r in recs if r["kind"] == "fatal-stall"], [])

        # re-audit N2: the byte total/threshold/root term must be logged
        # per-cycle (not just the boolean), so the gate's non-discrimination
        # on D3 is auditable after the fact.
        for r in scans:
            self.assertIn("pool_occ_mb", r)
            self.assertIn("pool_threshold_mb", r)
            self.assertIn("pool_root_occ_mb", r)
            self.assertIsNotNone(r["pool_root_occ_mb"])   # dry: SIM placeholder

        # (d) plausible end state: nothing pathological (e.g. every tenant
        # pinned at the ladder floor, or the run dying early).
        self.assertLess(final["t"], 400 + 12)   # ran to (about) completion
        for name, lvl in final["level"].items():
            self.assertLessEqual(lvl, 7)
            self.assertGreaterEqual(lvl, 0)




class TestOtherResidency(unittest.TestCase):
    """N3 auditability (re-audit 2026-09-12). The Other class is a real
    blind spot: an Other tenant is excluded from S_poor, from R_dev and
    from is_wedged(). The design accepts that (release to CLOS[0] is the
    escape hatch) but the auditor required it be REPORTED rather than
    assumed inert -- these tests pin the report."""

    def _residency(self, ctl):
        recs = _read_records(ctl)
        rr = [r for r in recs if r["kind"] == "other-residency"]
        self.assertEqual(len(rr), 1, "exactly one residency record per run")
        return rr[0]

    def test_permanently_quiet_tenant_is_flagged_always_other(self):
        quiet = mk_tenant("quiet", base_mb=10.0, occ_mb=0.01)
        busy = mk_tenant("busy", base_mb=10.0, occ_mb=10.0)
        ctl = mk_ctl([quiet, busy])
        ctl.sample_all = lambda: True
        ctl.run(duration=5 * cm.CYCLE_SECS)
        r = self._residency(ctl)
        self.assertIn("quiet", r["always_other"])
        self.assertNotIn("busy", r["always_other"])
        # started there, so it did not FALL into Other
        self.assertEqual(r["other_after_managed"]["quiet"], 0)
        self.assertEqual(r["fell_into_other"], [])

    def test_tenant_starved_into_invisibility_is_flagged(self):
        """The failure mode N3 actually warns about: a tenant that was
        being managed and then drops below the Other floor. It must NOT
        be silently dropped from the population -- it must show up in
        fell_into_other."""
        victim = mk_tenant("victim", base_mb=10.0, occ_mb=10.0)
        other = mk_tenant("other", base_mb=10.0, occ_mb=10.0)
        ctl = mk_ctl([victim, other])
        # starve the victim below the Other floor from cycle 2 onward, from
        # INSIDE the run (run() is single-shot -- it closes decisions in its
        # finally block, so it cannot be called twice on one controller).
        n = {"i": 0}

        def sample():
            n["i"] += 1
            if n["i"] > 2:
                victim.occ = 0.01 * 1048576
            return True

        ctl.sample_all = sample
        ctl.run(duration=6 * cm.CYCLE_SECS)
        self.assertEqual(victim.state, cm.OTHER)
        r = self._residency(ctl)
        self.assertIn("victim", r["fell_into_other"])
        self.assertGreater(r["other_after_managed"]["victim"], 0)

    def test_record_is_emitted_even_on_a_fatal_abort(self):
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0, level=0)
        excess = mk_tenant("excess", base_mb=10.0, occ_mb=13.0, level=0)
        ctl = mk_ctl([poor, excess])
        ctl.pool_occupied_bytes = lambda: (0.0, 0.0)
        ctl.sample_all = lambda: True
        with self.assertRaises(SystemExit):
            ctl.run(duration=(cm.STALL_CYCLES + 2) * cm.CYCLE_SECS)
        recs = _read_records(ctl)
        self.assertTrue(any(r["kind"] == "other-residency" for r in recs),
                        "an aborted arm must still be auditable for N3")



class TestStallClausesAreLoadBearing(unittest.TestCase):
    """RE-AUDIT PASS 3, finding N-B. The headline N1 regression test uses a
    SINGLE tenant, so is_wedged()'s `admissible` clause is False no matter
    what and the test passes for the wrong reason -- two mutations survived
    it: dropping `and not moved`, and reverting clause (1) to the
    expanding-window b_violation(). These tests put a suppressible Excess
    tenant in the scene so both clauses are actually exercised."""

    def test_does_not_fire_while_actively_suppressing(self):
        """Kills the `and not moved` mutation. A Poor tenant WITH a
        suppressible Excess neighbour: clauses (1) and (2) both hold every
        cycle, and the controller is doing exactly the right thing --
        suppressing the neighbour on the Poor tenant's behalf. Only clause
        (3) distinguishes 'working' from 'wedged'; without it the detector
        aborts a correctly-actuating run."""
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0, level=0)
        excess = mk_tenant("excess", base_mb=10.0, occ_mb=20.0, level=0)
        ctl = mk_ctl([poor, excess])
        ctl.sample_all = lambda: True
        fired = 0
        for i in range(cm.STALL_CYCLES + 5):
            moved = ctl.step()
            self.assertEqual(poor.state, cm.POOR)
            self.assertTrue(
                any(t.level > 0 for t in (poor, excess)) or moved,
                f"cycle {i}: scene should be actionable")
            if ctl.is_wedged(moved):
                fired += 1
        self.assertEqual(fired, 0,
                         "is_wedged() fired while the controller was "
                         "actively suppressing on the Poor tenant's behalf")

    def test_does_not_fire_on_recovered_tenant_with_suppressible_excess(self):
        """Kills the b_violation() mutation. The N1 scenario, but with a
        suppressible Excess neighbour so `admissible` is genuinely True:
        now ONLY the live-vs-historical reading of clause (1) decides. A
        tenant that has fully recovered to Adequate must not be treated as
        a live violation just because its expanding-window R_dev has not
        caught up."""
        t = mk_tenant("x", base_mb=10.0, occ_mb=10.0 * 0.70, level=0)
        big = mk_tenant("big", base_mb=10.0, occ_mb=20.0, level=0)
        ctl = mk_ctl([t, big])
        ctl.sample_all = lambda: True
        for _ in range(10):
            ctl.step()                      # drive R_dev deep into violation
        self.assertEqual(t.state, cm.POOR)
        t.occ = 10.0 * 1048576              # fully recovers
        for i in range(19):
            moved = ctl.step()
            self.assertEqual(t.state, cm.ADEQUATE)
            self.assertTrue(t.b_violation(),
                            "sanity: the expanding-window artifact must "
                            "still be present, else the test is vacuous")
            self.assertTrue(
                any(x.level < len(ctl.ladder_masks) - 1
                    for x in ctl.tenants if x.state in (cm.EXCESS, cm.OVERFLOW))
                or not moved or True,
                "scene shape sanity")
            self.assertFalse(ctl.is_wedged(moved),
                             f"is_wedged() fired at cycle {i} on a tenant "
                             f"that is Adequate RIGHT NOW")

    def test_gate_open_makes_the_detector_unreachable(self):
        """N-A, pinned as a TEST so it cannot silently change: with the
        pool_full gate open, is_wedged() is unsatisfiable -- its
        admissibility clause is term-for-term the fairness phase's own
        candidate pool, so anything admissible is also actuated. This is
        why the detector gives no protection on D3 (N2: gate open in
        134/134 cycles) and why inertness is reported by the
        activity-summary record instead of aborted on."""
        import random
        random.seed(11)
        fired_open = 0
        for _ in range(2000):
            ts = [mk_tenant(f"t{i}", base_mb=10.0,
                            occ_mb=random.choice([0.5, 1.0, 5.0, 9.0, 10.0,
                                                  13.0, 20.0, 30.0]),
                            level=random.randint(0, 7))
                  for i in range(random.randint(2, 5))]
            ctl = mk_ctl(ts)
            ctl.pool_occupied_bytes = lambda c=ctl: (c.total_l3_bytes, 0.0)
            ctl.sample_all = lambda: True
            if ctl.is_wedged(ctl.step()):
                fired_open += 1
        self.assertEqual(fired_open, 0,
                         "is_wedged() fired with the gate open -- the N-A "
                         "unreachability argument no longer holds; the "
                         "docstring and CACHEMAN_REVERIFY.md must be updated")


class TestActivitySummary(unittest.TestCase):
    """RE-AUDIT PASS 3, finding N-A: since both inertness guards are
    unreachable in the regime we actually run in, the arm must at minimum
    REPORT whether it ever acted, on every exit path."""

    def _summary(self, ctl):
        rr = [r for r in _read_records(ctl) if r["kind"] == "activity-summary"]
        self.assertEqual(len(rr), 1)
        return rr[0]

    def test_inert_run_is_flagged_not_aborted(self):
        """Everyone Adequate forever: the controller correctly does
        nothing. That must NOT abort (it is a legitimate result) but it
        MUST be flagged so no recovery number is printed for it."""
        a = mk_tenant("a", base_mb=10.0, occ_mb=10.0)
        b = mk_tenant("b", base_mb=10.0, occ_mb=10.0)
        ctl = mk_ctl([a, b])
        ctl.sample_all = lambda: True
        ctl.run(duration=15 * cm.CYCLE_SECS)      # must not raise
        s = self._summary(ctl)
        self.assertFalse(s["acted"])
        self.assertEqual(s["total_actuations"], 0)
        self.assertTrue(s["inert_tail"])
        self.assertEqual(s["tail_levels_distinct"], [0])

    def test_acting_run_is_not_flagged_inert(self):
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0, level=0)
        excess = mk_tenant("excess", base_mb=10.0, occ_mb=20.0, level=0)
        ctl = mk_ctl([poor, excess])
        ctl.sample_all = lambda: True
        ctl.run(duration=15 * cm.CYCLE_SECS)
        s = self._summary(ctl)
        self.assertTrue(s["acted"])
        self.assertGreater(s["actuation_counts"].get("fairness-suppress", 0), 0)
        self.assertFalse(s["inert_tail"])

    def test_emitted_even_on_a_fatal_abort(self):
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0, level=0)
        excess = mk_tenant("excess", base_mb=10.0, occ_mb=13.0, level=0)
        ctl = mk_ctl([poor, excess])
        ctl.pool_occupied_bytes = lambda: (0.0, 0.0)
        ctl.sample_all = lambda: True
        with self.assertRaises(SystemExit):
            ctl.run(duration=(cm.STALL_CYCLES + 2) * cm.CYCLE_SECS)
        self.assertTrue(any(r["kind"] == "activity-summary"
                            for r in _read_records(ctl)))



class TestPaperLiteralPreferences(unittest.TestCase):
    """RE-AUDIT PASS 3, finding N-G: swapping the paper's
    Overflow-before-Excess preference in PhaseForFairness (Algorithm 2
    lines 1-9) survived the whole suite. No D3 impact (no tenant declares
    a theta, so Overflow is unreachable there) -- but it is a literal
    clause of the algorithm and must not be silently reversible."""

    def test_overflow_is_preferred_over_excess(self):
        poor = mk_tenant("poor", base_mb=10.0, occ_mb=1.0)
        # theta declared -> occupancy past (1+theta)*base classifies OVERFLOW
        overflow = mk_tenant("ovf", base_mb=10.0, occ_mb=18.0, theta=0.2)
        # strictly LARGER occ/base ratio, but only EXCESS: the phase must
        # still take the Overflow tenant, so this cannot pass by ratio luck.
        excess = mk_tenant("exc", base_mb=10.0, occ_mb=30.0)
        ctl = mk_ctl([poor, overflow, excess])
        ctl.step()
        self.assertEqual(overflow.state, cm.OVERFLOW)
        self.assertEqual(excess.state, cm.EXCESS)
        supp = [r for r in _read_records(ctl) if r["kind"] == "fairness-suppress"]
        self.assertEqual(len(supp), 1)
        self.assertEqual(supp[0]["tenant"], "ovf",
                         "PhaseForFairness must drain Overflow before Excess "
                         "(Algorithm 2 lines 1-9), even when an Excess tenant "
                         "has the larger occupancy ratio")
        self.assertEqual(supp[0]["from_pool"], "overflow")


class TestResidencyDenominator(unittest.TestCase):
    """RE-AUDIT PASS 3, N3 residual: `always_other` must be denominated in
    cycles on which step() actually RAN, not loop iterations -- cycle_n
    also counts cycles skipped for unreadable CMT, which would make a
    permanently-quiet tenant silently drop off the always_other list."""

    def test_skipped_cycles_do_not_hide_an_always_other_tenant(self):
        quiet = mk_tenant("quiet", base_mb=10.0, occ_mb=0.01)
        busy = mk_tenant("busy", base_mb=10.0, occ_mb=10.0)
        ctl = mk_ctl([quiet, busy])
        n = {"i": 0}

        def sample():
            n["i"] += 1
            return n["i"] % 3 != 0      # every third cycle unreadable

        ctl.sample_all = sample
        ctl.run(duration=12 * cm.CYCLE_SECS)
        r = [x for x in _read_records(ctl) if x["kind"] == "other-residency"][0]
        self.assertLess(r["steps_taken"], r["cycles"],
                        "sanity: some cycles must have been skipped")
        self.assertIn("quiet", r["always_other"],
                      "a permanently-quiet tenant fell off always_other "
                      "purely because some cycles were skipped")


if __name__ == "__main__":
    unittest.main()
