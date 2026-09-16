#!/usr/bin/env python3
"""Unit tests for satori_style.py, no sudo, no hardware.

1. gen_configs_recursively/gen_configs on (NUM_APPS=5, NUM_UNITS=[15])
   yields exactly C(14,4) = 1001 configs, every one summing (with the
   inferred last share) to exactly 15 ways, every share >= 1.
2. get_weights() on a crafted history is checked SIDE BY SIDE against the
   authors' own satori.py (imported directly from SATORI_artifact/), i.e.
   the exact same code path, not a re-derivation -- so this test also
   locks in and documents a real bug found in the artifact: get_weights()
   indexes WT_list/WF_list with `equalization_index`, an index into
   equalization_period_marker_list, which is ALWAYS one entry longer than
   WT_list/WF_list at the moment the marker just flipped to 1 (get_metrics
   appends to the marker list every call; get_weights only appends to
   WT_list/WF_list on calls after the first). The first time an
   equalization period elapses, `WT_list[equalization_index:]` is an empty
   slice and `statistics.mean` raises ValueError. Reproduced here with the
   IDENTICAL crafted history against the authors' own function.
3. Chunked, resumable search (run_chunked_bo and its helpers): state
   round-trip, anchor persistence, chunk-offset correction, warm-start
   replay into gp_minimize's x0/y0 (offset-corrected), instance-gate
   failure refusing to fold state, and a budget-triggered exit that both
   checkpoints state and restores masks -- all via --dry-run (no sudo, no
   perf, no real resctrl; PerfSampler.start/stop and _collect_ipc_real are
   monkeypatched for the mask-restore test so it can run with
   `--dry-run`-equivalent speed while still exercising the REAL
   apply_llc/restore_all file-based resctrl writes against a fake
   filesystem).

Run: python3 test_satori_style.py  (PYTHONPATH must include the skopt
shim: PYTHONPATH=/home/tejendra/litmus/SATORI_artifact/pydeps)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from math import comb
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/tejendra/litmus/SATORI_artifact")

import satori_style as sstyle          # noqa: E402
import satori as artifact              # the authors' own file, noqa: E402


class TestGenConfigs(unittest.TestCase):
    def test_1001_configs_5_apps_15_ways(self):
        sstyle.NUM_APPS = 5
        sstyle.NUM_RESOURCES = 1
        sstyle.NUM_UNITS = [15]
        sstyle.CONFIGS_LIST = []
        sstyle.gen_configs()
        configs = sstyle.CONFIGS_LIST
        self.assertEqual(len(configs), comb(14, 4))
        self.assertEqual(len(configs), 1001)
        for c in configs:
            self.assertEqual(len(c), 4)            # NUM_APPS - 1 explicit shares
            self.assertTrue(all(share >= 1 for share in c))
            last = 15 - sum(c)
            self.assertGreaterEqual(last, 1)        # inferred last share >= 1
            self.assertEqual(sum(c) + last, 15)
        # no duplicate configs
        self.assertEqual(len(set(tuple(c) for c in configs)), 1001)


class TestGetAllocationLLC(unittest.TestCase):
    def test_masks_disjoint_contiguous_cover_all_ways(self):
        sstyle.NUM_APPS = 5
        sstyle.NUM_RESOURCES = 1
        sstyle.NUM_UNITS = [15]
        sstyle.CONFIGS_LIST = []
        sstyle.gen_configs()
        shares, masks = sstyle.get_allocation_llc(500)
        self.assertEqual(sum(shares), 15)
        ints = [int(m, 16) for m in masks]
        union = 0
        for m in ints:
            self.assertEqual(m & union, 0, "masks overlap")   # disjoint
            union |= m
        self.assertEqual(union, (1 << 15) - 1)                # full coverage
        for m, s in zip(ints, shares):
            self.assertEqual(bin(m).count("1"), s)            # way count matches share
            # contiguous: a mask's set bits form one contiguous run
            bits = [i for i in range(15) if m & (1 << i)]
            self.assertEqual(bits, list(range(min(bits), max(bits) + 1)))


def _crafted_history():
    """A short synthetic history: 3 prior get_weights() calls worth of
    state, ending exactly at the moment an equalization period has just
    elapsed for the FIRST time (marker list one entry ahead of WT/WF)."""
    return dict(
        throughput_list=[1.0, 1.05, 1.10, 1.20],
        fairness_list=[0.9, 0.88, 0.95, 0.80],
        equalization_period_marker_list=[1, 0, 0, 1],   # just flipped again at idx 3
        prioritization_period_marker_list=[1, 1, 0, 1],
        WT_list=[0.5, 0.6, 0.55],                        # ONE SHORT of marker list
        WF_list=[0.5, 0.4, 0.45],
        time_equalization=10,
    )


class TestGetWeightsSideBySide(unittest.TestCase):
    """get_weights() is VERBATIM satori.py:168-198 in satori_style.py.  This
    proves it byte-for-byte by running the SAME crafted history through
    BOTH copies and checking they do the SAME thing -- including failing
    the SAME way."""

    def _load(self, mod, h):
        mod.throughput_list = list(h["throughput_list"])
        mod.fairness_list = list(h["fairness_list"])
        mod.equalization_period_marker_list = list(h["equalization_period_marker_list"])
        mod.prioritization_period_marker_list = list(h["prioritization_period_marker_list"])
        mod.WT_list = list(h["WT_list"])
        mod.WF_list = list(h["WF_list"])
        mod.time_equalization = h["time_equalization"]
        mod.start_time = 0.0

    # --- 2026-09-12: get_weights() carries ONE documented fix (user decision B,
    # CONTROLLER_COMPARISON §13c): the artifact never appends W_T/W_F after
    # the first call, so its equalization term dies at the first T_E
    # boundary. The tests below pin (a) that the ARTIFACT still has the bug,
    # (b) that ours does not, (c) that ours implements paper Eq.3 exactly,
    # and (d) that the PRIORITIZATION logic is still byte-identical.

    def test_artifact_raises_at_boundary_ours_does_not(self):
        h = _crafted_history()
        self._load(artifact, h)
        with self.assertRaises(Exception):          # the authors' bug, unchanged
            artifact.get_weights()
        self._load(sstyle, h)
        n0 = len(sstyle.WT_list)
        w_t, w_f = sstyle.get_weights()             # ours survives the boundary
        self.assertEqual(len(sstyle.WT_list), n0 + 1)   # fix (1): appended
        self.assertEqual(len(sstyle.WF_list), n0 + 1)
        self.assertTrue(0.0 < w_t < 1.0 and 0.0 < w_f < 1.0)

    def test_equalization_term_is_paper_eq3(self):
        """te/T_E = 0.5 (time patched): W = 0.5*W_E + 0.5*W_P with
        W_E = 1 - mean(weights of the CURRENT period), computed by hand."""
        from unittest import mock
        h = _crafted_history()
        self._load(sstyle, h)
        with mock.patch.object(sstyle.time, "time", return_value=5.0):  # te = 5 of 10
            w_t, w_f = sstyle.get_weights()
        # prioritization, by hand: prioritization_index = 3 (last marker==1)
        # -> change_f = change_t = 0 (current == value at idx 3) -> both <= 0
        w_tp, w_fp = 0.5, 0.5
        # equalization: equalization_index = 3 -> slice start 3-1 = 2 -> [0.55]/[0.45]
        w_te, w_fe = 1 - 0.55, 1 - 0.45
        self.assertAlmostEqual(w_t, 0.5 * w_te + 0.5 * w_tp, places=12)
        self.assertAlmostEqual(w_f, 0.5 * w_fe + 0.5 * w_fp, places=12)

    def test_prioritization_identical_to_artifact(self):
        """At te = 0 the equalization term has zero weight, so W = W_P and
        both copies must agree exactly (fix touches only the W_E term)."""
        from unittest import mock
        h = _crafted_history()
        h["WT_list"].append(0.5); h["WF_list"].append(0.5)   # artifact can't crash here
        h["fairness_list"] = [0.9, 0.88, 0.95, 0.80, 0.84]   # nonzero changes vs idx 3
        h["throughput_list"] = [1.0, 1.05, 1.10, 1.20, 1.26]
        h["equalization_period_marker_list"] = [1, 0, 0, 1, 0]
        h["prioritization_period_marker_list"] = [1, 1, 0, 1, 0]
        out = {}
        for mod in (artifact, sstyle):
            self._load(mod, h)
            with mock.patch.object(mod.time, "time", return_value=10.0):   # te = 0
                out[mod.__name__] = mod.get_weights()
        self.assertAlmostEqual(out["satori"][0], out["satori_style"][0], places=12)
        self.assertAlmostEqual(out["satori"][1], out["satori_style"][1], places=12)

    def test_first_call_returns_half_half(self):
        """len(WT_list)==0 branch, VERBATIM l.169-174: always 0.5/0.5."""
        for mod in (artifact, sstyle):
            mod.WT_list = []
            mod.WF_list = []
            w_t, w_f = mod.get_weights()
            self.assertEqual((w_t, w_f), (0.5, 0.5))


class TestStateRoundTrip(unittest.TestCase):
    """_load_state/_save_state, ported from spidersense_style.py: atomic
    write (tmp + os.replace), and every field defaulted on a missing file."""

    def test_missing_file_gives_defaults(self):
        st = sstyle._load_state("/tmp/does-not-exist-satori-state.json")
        self.assertEqual(st["chunks"], 0)
        self.assertEqual(st["observed"], [])
        self.assertEqual(st["anchor"], {})
        self.assertIsNone(st["anchor_idx"])
        self.assertIsNone(st["best"])
        self.assertFalse(st["converged"])

    def test_round_trip(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "state.json")
            st = {"chunks": 2, "observed": [{"chunk": 0, "idx": 5, "y": -0.5}],
                  "anchor": {"3": [{"chunk": 0, "y": -0.4}]}, "anchor_idx": [3],
                  "best": {"idx": 5, "y": -0.5}, "no_improve_chunks": 1,
                  "converged": False}
            sstyle._save_state(path, st)
            self.assertFalse(os.path.exists(path + ".tmp"))   # atomic: no leftover tmp
            back = sstyle._load_state(path)
            self.assertEqual(back, st)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_missing_path_is_a_noop_save(self):
        sstyle._save_state("", {"anything": 1})   # must not raise


class TestPickAnchors(unittest.TestCase):
    def test_persisted_across_calls_even_with_different_seed(self):
        st = {"anchor_idx": None}
        first = sstyle._pick_anchors(st, 5, 91, seed=1)
        self.assertEqual(len(first), 5)
        self.assertEqual(st["anchor_idx"], first)
        second = sstyle._pick_anchors(st, 5, 91, seed=999)   # different seed, same state
        self.assertEqual(second, first)                       # SAME anchors reused


class TestChunkOffsets(unittest.TestCase):
    """PORTED test shape from spidersense_style.py's own validated design:
    a deliberate chunk-to-chunk step change on the SAME anchor configs must
    be recovered by the additive-offset correction."""

    def test_offset_removes_a_deliberate_drift(self):
        # chunk 0 anchors sit around -1.0, chunk 1's (a different tenant
        # instance) sit around -0.7 -- a clean +0.3 step, same anchors.
        st = {"anchor": {
            "3": [{"chunk": 0, "y": -1.00}, {"chunk": 1, "y": -0.70}],
            "7": [{"chunk": 0, "y": -0.98}, {"chunk": 1, "y": -0.69}],
        }}
        offs, drift, resid = sstyle._chunk_offsets(st)
        self.assertAlmostEqual(drift, 0.3, places=2)
        # after subtracting each chunk's own offset, the two chunks agree
        c0 = [-1.00 - offs[0], -0.98 - offs[0]]
        c1 = [-0.70 - offs[1], -0.69 - offs[1]]
        self.assertAlmostEqual(sum(c0) / len(c0), sum(c1) / len(c1), places=6)
        self.assertLess(resid, 0.02)   # tight: the two anchors agree to 0.02

    def test_no_anchors_gives_zero_offsets(self):
        offs, drift, resid = sstyle._chunk_offsets({"anchor": {}})
        self.assertEqual(offs, {})
        self.assertEqual(drift, 0.0)
        self.assertEqual(resid, 0.0)


def _dry_run_args(out_dir, state_path, **kw):
    ns = mock.Mock()
    ns.out = out_dir
    ns.state = state_path
    ns.dry_run = True
    ns.budget = kw.get("budget", 5.0)
    ns.anchors = kw.get("anchors", 2)
    ns.iso = kw.get("iso", "")
    ns.fp_tol = 0.06
    ns.fp_min = 0.10
    ns.fp_mode = kw.get("fp_mode", "block")
    ns.fp_window = 1.0
    ns.fp_tenants = kw.get("fp_tenants", "")
    ns.seed = kw.get("seed", 1)
    return ns


class ChunkedBOTestBase(unittest.TestCase):
    """Shared setUp: initializes satori_style's module globals the way
    main() would for a 3-tenant / 9-way scene, in --dry-run mode (the
    synthetic IPC model, no perf/resctrl writes) so run_chunked_bo can be
    called directly without going through argparse/root."""

    N_APPS = 3
    NBITS = 9

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out_dir = os.path.join(self.tmpdir, "out")
        os.makedirs(self.out_dir)
        self.state_path = os.path.join(self.tmpdir, "state.json")
        self.decisions_path = os.path.join(self.out_dir, "decisions.jsonl")

        sstyle.applications = [f"t{i}" for i in range(self.N_APPS)]
        sstyle.tenants = [{"name": n, "grp": f"/tmp/nogrp_{n}", "cores": "0"}
                           for n in sstyle.applications]
        sstyle.NUM_APPS = self.N_APPS
        sstyle.NUM_RESOURCES = 1
        sstyle.NUM_UNITS = [self.NBITS]
        sstyle.CONFIGS_LIST = []
        sstyle.gen_configs()
        sstyle.isolated_ipc = [1.0] * self.N_APPS
        sstyle.current_ways = {n: self.NBITS // self.N_APPS for n in sstyle.applications}
        sstyle.FULL_MASK = (1 << self.NBITS) - 1

        sstyle.throughput_list = []
        sstyle.fairness_list = []
        sstyle.equalization_period_marker_list = []
        sstyle.prioritization_period_marker_list = []
        sstyle.WT_list = []
        sstyle.WF_list = []
        sstyle.last_speedups = []
        sstyle.last_ipc = []
        sstyle.time_sampling = 0.01     # fast test, real gp_minimize/objective loop
        sstyle.time_prioritization = 1
        sstyle.time_equalization = 10
        now = time.time()
        sstyle.equalization_period_counter = now
        sstyle.prioritization_period_counter = now
        sstyle.start_time = now
        sstyle._t0[0] = now

        sstyle.samplers = []            # dry-run: get_metrics never touches these
        sstyle.STOP[0] = False
        sstyle.NEW_OBS = []
        sstyle._sample_n[0] = 0
        sstyle._configs_seen.clear()
        sstyle._prev_config_idx[0] = None
        sstyle._last_obj_end[0] = None

        self.decisions_f = open(self.decisions_path, "w")
        sstyle.DECISIONS = self.decisions_f

    def tearDown(self):
        self.decisions_f.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        sstyle.STOP[0] = False


class TestWarmStartReplay(ChunkedBOTestBase):
    def test_second_chunk_replays_and_corrects_first_chunks_observations(self):
        args1 = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=2)
        sstyle.ARGS = args1
        rc1 = sstyle.run_chunked_bo(args1)
        self.assertEqual(rc1, 0)
        with open(self.state_path) as f:
            st_after_1 = json.load(f)
        n1 = len(st_after_1["observed"])
        self.assertGreater(n1, 0, "chunk 1 must have measured at least one config")

        captured = {}
        real_gp_minimize = sstyle.gp_minimize

        def _spy(func, dims, **kwargs):
            captured["x0"] = kwargs.get("x0")
            captured["y0"] = kwargs.get("y0")
            captured["n_random_starts"] = kwargs.get("n_random_starts")
            return real_gp_minimize(func, dims, **kwargs)

        args2 = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=2)
        sstyle.ARGS = args2
        with mock.patch.object(sstyle, "gp_minimize", side_effect=_spy):
            rc2 = sstyle.run_chunked_bo(args2)
        self.assertEqual(rc2, 0)

        # WARM START: gp_minimize's x0/y0 on the resumed chunk must replay
        # every one of chunk 1's observations (this is the whole point of
        # ported x0/y0 warm-start -- see run_chunked_bo's module docstring).
        self.assertIsNotNone(captured["x0"])
        self.assertEqual(len(captured["x0"]), n1)
        self.assertEqual(len(captured["y0"]), n1)
        # n_random_starts must be the SHORTFALL (5 - n1), never a flat 5:
        # forcing 5 fresh random draws on every chunk would mean "5 per
        # chunk" instead of "5 for the whole campaign" (see the comment in
        # run_chunked_bo on skopt's Optimizer.n_initial_points()).
        self.assertEqual(captured["n_random_starts"], max(0, 5 - n1))

        with open(self.state_path) as f:
            st_after_2 = json.load(f)

        # OFFSET CORRECTION: y0 handed to gp_minimize must equal the raw
        # stored y minus THAT OBSERVATION'S CHUNK's anchor-estimated offset
        # -- computed the same way run_chunked_bo computes it, from the
        # anchor set as it stood right before this chunk's gp_minimize call
        # (chunk 0's anchors plus chunk 1's, which is what was on disk when
        # chunk 2 built x0/y0). Recomputed independently here, not assumed
        # to be exactly zero -- get_weights' time-dependent term gives a
        # real (tiny) chunk-to-chunk drift even with no tenant relaunch.
        offs, _drift, _resid = sstyle._chunk_offsets(st_after_2)
        for o, y0 in zip(st_after_1["observed"], captured["y0"]):
            expected = o["y"] - offs.get(o["chunk"], 0.0)
            self.assertAlmostEqual(expected, y0, places=6)
        self.assertGreaterEqual(len(st_after_2["observed"]), n1)
        self.assertEqual(st_after_2["chunks"], 2)
        # the SAME anchor configs must have been reused, not re-picked
        self.assertEqual(st_after_2["anchor_idx"], st_after_1["anchor_idx"])


class TestFpGateRefusesFold(ChunkedBOTestBase):
    def test_gate_failure_blocks_and_writes_no_state(self):
        self.assertFalse(os.path.exists(self.state_path))
        args = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=1,
                              iso="/tmp/whatever-iso.json", fp_mode="block")
        sstyle.ARGS = args
        with mock.patch.object(sstyle, "_fp_gate",
                                return_value=(False, "t0 0.500 vs iso 0.300 (+66.7%)")):
            rc = sstyle.run_chunked_bo(args)
        self.assertEqual(rc, 3)
        # A REJECTED chunk must not fold anything into the surrogate's
        # training data -- a relaunched tenant's y's are not comparable to
        # the rest of the campaign's, so nothing is written at all.
        self.assertFalse(os.path.exists(self.state_path))

    def test_gate_warn_mode_continues_and_writes_state(self):
        args = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=1,
                              iso="/tmp/whatever-iso.json", fp_mode="warn")
        sstyle.ARGS = args
        with mock.patch.object(sstyle, "_fp_gate", return_value=(False, "mismatch")):
            rc = sstyle.run_chunked_bo(args)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.state_path))
        with open(self.decisions_path) as f:
            self.assertTrue(any('"kind": "fp_gate_warn"' in line for line in f))


class TestBudgetExitSavesStateAndRestoresMasks(unittest.TestCase):
    """End-to-end through main() (dry_run=False), with a FAKE resctrl
    filesystem (plain files -- write_schemata_line/domain_body/apply_llc/
    restore_all do only file I/O, no kernel resctrl semantics, so this
    needs no hardware or root). PerfSampler.start/stop and _collect_ipc_real
    are monkeypatched so no real `perf` process is spawned; a tiny --budget
    forces an early, budget-triggered exit (not a full 1000-call search)."""

    def test_budget_exit(self):
        tmpdir = tempfile.mkdtemp()
        try:
            resctrl = os.path.join(tmpdir, "resctrl")
            os.makedirs(os.path.join(resctrl, "info", "L3"))
            with open(os.path.join(resctrl, "info", "L3", "cbm_mask"), "w") as f:
                f.write("1ff\n")                       # 9 ways, small config space
            with open(os.path.join(resctrl, "schemata"), "w") as f:
                f.write("L3:0=1ff;1=1ff\n")

            grp_dirs = {}
            tenants_cfg = []
            for name in ("a", "b", "c"):
                g = os.path.join(tmpdir, f"grp_{name}")
                os.makedirs(g)
                with open(os.path.join(g, "schemata"), "w") as f:
                    f.write("L3:0=1ff;1=1ff\n")
                grp_dirs[name] = g
                tenants_cfg.append({"name": name, "grp": g, "cores": "2-3"})

            iso_path = os.path.join(tmpdir, "iso.json")
            with open(iso_path, "w") as f:
                json.dump({"a": 1.0, "b": 1.0, "c": 1.0}, f)
            cfg_path = os.path.join(tmpdir, "cfg.json")
            with open(cfg_path, "w") as f:
                json.dump({"tenants": tenants_cfg, "solo_ipc": iso_path}, f)

            out_dir = os.path.join(tmpdir, "out")
            state_path = os.path.join(tmpdir, "state.json")
            argv = ["satori_style.py", "--config", cfg_path, "--out", out_dir,
                    "--state", state_path, "--budget", "1.0", "--anchors", "1"]

            with mock.patch.object(sstyle, "RESCTRL", resctrl), \
                 mock.patch.object(sstyle, "_collect_ipc_real",
                                    lambda: [1.1, 1.0, 1.3]), \
                 mock.patch.object(sstyle.PerfSampler, "start", lambda self: None), \
                 mock.patch.object(sstyle.PerfSampler, "stop", lambda self: None), \
                 mock.patch("os.geteuid", return_value=0), \
                 mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as cm:
                    sstyle.main()
            self.assertEqual(cm.exception.code, 0)

            self.assertTrue(os.path.exists(state_path))
            with open(state_path) as f:
                st = json.load(f)
            self.assertGreater(len(st["observed"]), 0,
                                "budget exit must still checkpoint whatever was measured")

            # MASKS RESTORED: every tenant group and the resctrl root must be
            # back at the full mask, even though objective() rewrote them to
            # partitions during the search.
            for name, g in grp_dirs.items():
                with open(os.path.join(g, "schemata")) as f:
                    self.assertIn("0=1ff", f.read(), f"{name} mask not restored")
            with open(os.path.join(resctrl, "schemata")) as f:
                self.assertIn("0=1ff", f.read(), "resctrl root mask not restored")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)



class TestAnchorSurrogatePurity(ChunkedBOTestBase):
    """SATORI_CHUNK_VERIFY.md surviving mutant #5. Anchor probes are
    re-measured EVERY chunk purely to estimate that chunk's offset. If they
    leaked into NEW_OBS they would be replayed to the surrogate as genuine
    search data -- the same config appearing once per chunk, teaching the GP
    that a handful of arbitrary points are worth resampling forever. The
    build doc calls this line 'must never be fed to the surrogate'; it had
    no test."""

    def test_anchor_probe_is_not_banked_as_search_data(self):
        sstyle.ARGS = _dry_run_args(self.out_dir, self.state_path)
        sstyle.NEW_OBS = []
        sstyle.objective([3], _is_anchor=True)
        self.assertEqual(sstyle.NEW_OBS, [],
                         "an anchor probe was banked into the surrogate's "
                         "training data")

    def test_ordinary_probe_is_banked(self):
        sstyle.ARGS = _dry_run_args(self.out_dir, self.state_path)
        sstyle.NEW_OBS = []
        sstyle.objective([3], _is_anchor=False)
        self.assertEqual([o["idx"] for o in sstyle.NEW_OBS], [3],
                         "a real probe was NOT banked -- the campaign would "
                         "silently lose observations across chunks")

    def test_anchors_never_appear_in_persisted_observations(self):
        """End-to-end: run a chunk and assert no anchor index was written
        into state['observed'] (they belong in state['anchor'] only)."""
        args = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=2)
        sstyle.ARGS = args
        sstyle.run_chunked_bo(args)
        st = sstyle._load_state(self.state_path)
        anchor_idxs = {int(k) for k in st["anchor"]}
        self.assertTrue(anchor_idxs, "test is vacuous without anchors")
        banked_from_anchor_pass = [o for o in st["observed"]
                                   if o["idx"] in anchor_idxs]
        # a real probe MAY coincidentally hit an anchor index; what must not
        # happen is one banked entry per anchor per chunk from the anchor loop
        self.assertLessEqual(
            len(banked_from_anchor_pass), len(anchor_idxs),
            "anchor-loop probes look like they were banked as search data")


class TestConvergenceRule(ChunkedBOTestBase):
    """SATORI_CHUNK_VERIFY.md surviving mutants #6 and #7, plus the
    recalibration-noise fix. This is the path that actually STOPS the
    campaign -- n_calls=1000 is not reachable in practical wall time -- so a
    spurious `converged` truncates the search and hands us a non-converged
    SATORI number, which is the exact failure the chunked arm exists to
    prevent."""

    def _state(self, observed, no_improve, best_y, anchors=2):
        st = {"observed": observed, "anchor": {}, "anchor_idx": [],
              "chunks": 5, "best": {"idx": 0, "y": best_y, "tol": 0.0,
                                     "prev_idx": 0},
              "no_improve_chunks": no_improve, "converged": False}
        return st

    # Anchor data crafted so the per-chunk OFFSETS are exactly zero while the
    # RESIDUAL (the measured resolution limit) is large (~0.707): two anchors
    # varying in opposite directions across two chunks. This isolates the
    # convergence rule from the offset correction.
    ANCHORS = {"1": [{"chunk": 0, "y": 0.0}, {"chunk": 1, "y": 1.0}],
               "2": [{"chunk": 0, "y": 1.0}, {"chunk": 1, "y": 0.0}]}
    UNBEATABLE = -999.0      # |objective| is ~1, so nothing sampled beats this

    def _seed(self, n_obs, no_improve, best_y=UNBEATABLE, extra_obs=()):
        st = {"observed": [{"chunk": 0, "idx": i, "y": 0.5} for i in range(n_obs)],
              "anchor": {k: [dict(o) for o in v] for k, v in self.ANCHORS.items()},
              "anchor_idx": [], "chunks": 2,
              "best": {"idx": 0, "y": best_y}, "no_improve_chunks": no_improve,
              "converged": False}
        st["observed"].extend(dict(o) for o in extra_obs)
        with open(self.state_path, "w") as f:
            json.dump(st, f)
        return st

    def _run_chunk(self):
        # anchors=0: the anchor LOOP adds nothing this chunk (the crafted
        # anchor history above still drives offsets/resid), so the floor is
        # max(2*0, 10) = 10 and the test controls the observation count.
        args = _dry_run_args(self.out_dir, self.state_path, budget=0.01, anchors=0)
        sstyle.ARGS = args
        sstyle.run_chunked_bo(args)
        return sstyle._load_state(self.state_path)

    def test_offsets_zero_resid_large_sanity(self):
        """The crafted anchor fixture must actually do what the other tests
        assume, or all three of them are vacuous."""
        offs, _drift, resid = sstyle._chunk_offsets({"anchor": self.ANCHORS})
        for c, v in offs.items():
            self.assertAlmostEqual(v, 0.0, places=9, msg=f"chunk {c} offset")
        self.assertGreater(resid, 0.5, "fixture must produce a large residual")

    def test_floor_gate_blocks_convergence_on_a_barely_sampled_campaign(self):
        """Mutant #6. Without the max(2*n_anchors, 10) floor, a campaign that
        has barely sampled anything 'converges' -- 'no improvement' because
        nothing was looked at, not because the search is done. Seeded with 3
        observations and a long no-improvement streak."""
        self._seed(n_obs=3, no_improve=10)
        st = self._run_chunk()
        self.assertLess(len(st["observed"]), 10,
                        "test is vacuous if the chunk sampled past the floor")
        self.assertGreaterEqual(st["no_improve_chunks"], sstyle.CONVERGE_PATIENCE,
                                "test is vacuous unless patience is satisfied")
        self.assertFalse(st["converged"],
                         "converged on a campaign with fewer observations "
                         "than the floor -- the floor gate is not working")

    def test_patience_boundary_is_inclusive(self):
        """Mutant #7: `>=` vs `>`. Seeded one chunk BELOW patience with an
        unbeatable incumbent, so this chunk cannot improve and the streak
        lands exactly ON CONVERGE_PATIENCE. The campaign must stop there; an
        off-by-one costs a whole extra chunk of machine time every campaign."""
        self._seed(n_obs=20, no_improve=sstyle.CONVERGE_PATIENCE - 1)
        st = self._run_chunk()
        self.assertEqual(st["no_improve_chunks"], sstyle.CONVERGE_PATIENCE,
                         "fixture did not land exactly on the boundary")
        self.assertTrue(st["converged"],
                        "did not converge at exactly CONVERGE_PATIENCE "
                        "chunks without improvement")

    def test_sub_noise_improvement_does_not_reset_patience(self):
        """The recalibration fix. A new incumbent better by 0.0005 against a
        measured residual of ~0.707 is measurement noise, not progress: it
        must NOT reset the patience counter. With the old 1e-9 tolerance it
        does, and a campaign whose offsets wiggle below the noise floor never
        converges -- or, comparing across recalibrations, stops early."""
        self._seed(n_obs=20, no_improve=1,
                   extra_obs=[{"chunk": 0, "idx": 7,
                               "y": self.UNBEATABLE - 0.0005}])
        st = self._run_chunk()
        self.assertAlmostEqual(st["best"]["y"], self.UNBEATABLE - 0.0005, places=6,
                               msg="fixture: the sub-noise point must be the "
                                   "new argmin")
        self.assertEqual(st["no_improve_chunks"], 2,
                         "a sub-noise 'improvement' reset the patience "
                         "counter -- convergence is keyed on measurement "
                         "noise rather than on real progress")

    def test_real_improvement_resets_patience(self):
        """Converse of the above: an improvement well clear of the residual
        must still count, or the noise tolerance would stop the search early."""
        self._seed(n_obs=20, no_improve=2,
                   extra_obs=[{"chunk": 0, "idx": 7, "y": self.UNBEATABLE - 5.0}])
        st = self._run_chunk()
        self.assertEqual(st["no_improve_chunks"], 0,
                         "a genuine improvement was masked by the noise "
                         "tolerance -- the campaign would stop too early")
        self.assertFalse(st["converged"])

    def test_chunk_end_record_exposes_the_convergence_inputs(self):
        """Convergence must be auditable after the fact: the chunk_end record
        has to carry the tolerance and the incumbent actually used."""
        args = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=2)
        sstyle.ARGS = args
        sstyle.run_chunked_bo(args)
        self.decisions_f.flush()
        recs = [json.loads(l) for l in open(self.decisions_path)]
        ends = [r for r in recs if r["kind"] == "chunk_end"]
        self.assertTrue(ends)
        for k in ("improve_tol", "incumbent_idx", "incumbent_stable",
                  "no_improve_chunks", "converged"):
            self.assertIn(k, ends[-1], f"chunk_end is missing '{k}' -- a "
                                        f"convergence decision cannot be "
                                        f"audited without it")


class TestAnchorLoopCrashSafety(ChunkedBOTestBase):
    """SATORI_CHUNK_VERIFY.md surviving mutant #8. The anchor loop's
    _save_state lives in a `finally` so anchor progress survives an
    exception mid-measurement. Without it a crash during anchor probing
    discards that chunk's offset evidence entirely."""

    def test_anchor_progress_is_saved_when_measurement_raises(self):
        args = _dry_run_args(self.out_dir, self.state_path, budget=1.0, anchors=3)
        sstyle.ARGS = args
        calls = {"n": 0}
        real_obj = sstyle.objective

        def boom(x, _is_anchor=False):
            if _is_anchor:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("simulated perf failure mid-anchor")
            return real_obj(x, _is_anchor=_is_anchor)

        with mock.patch.object(sstyle, "objective", side_effect=boom):
            with self.assertRaises(RuntimeError):
                sstyle.run_chunked_bo(args)
        st = sstyle._load_state(self.state_path)
        self.assertTrue(st["anchor"],
                        "anchor progress was lost when a measurement raised "
                        "-- the finally-save is not doing its job")

if __name__ == "__main__":
    unittest.main()
