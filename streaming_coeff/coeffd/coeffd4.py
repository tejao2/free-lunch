#!/usr/bin/env python3
"""coeffd4.py -- free-lunch-scoped QoS controller.

Implements COEFFD4_DESIGN.md. Subclasses coeffd3.Daemon and REPLACES four
things; everything else (perf/TMA sampling, RDT reads, MBA lever with its
cost-bound/benefit-count/coherence rules, migration, guard/release, the main
loop, jsonl records) is inherited unchanged, per the design's reuse list.

  1. CACHE = BANDS, not nested prefixes. Donors stack contiguously from bit 0
     upward; reserved victims stack downward from the top; the crowd holds the
     contiguous remainder. Every mask is a single contiguous run, which is the
     hardware constraint, and the freed ways from a fence coalesce into ONE
     region adjacent to the victims instead of scattering.
  2. FENCE lever + aggressor-first phase order (fence -> throttle -> reserve ->
     migrate -> settle).
  3. Change 1 unmanaged-baseline anchor; Change 2 predictive stand-down +
     futility hysteresis; Change 3 donor-is-DNH-protected.
  4. IPC as the harm/cost signal instead of ips (METRIC_VALIDATION V7).

WHY THE FENCE FIRES WITHOUT IDENTIFYING A VICTIM. coeffd3 gated every lever on
is_hurt(), i.e. on the absolute dram_bound gate. Measured on 31 non-LC
workloads that gate does not work: absolute db vs harm is r=+0.30, victims span
db 4.8-48.1 and non-victims 0.0-19.8, and on the one certified fence scene its
precision was ZERO -- it missed canneal (db 16.3, the only real victim) and
flagged npb_cg_d40 (db 55.4, not a victim). GATE_PP=20 was anchored on a single
LC tenant, never measured.

So coeffd4 does not ask the gate for permission to fence. The fence is a PROBE
whose cost is bounded and VERIFIED on the donor side (Change 3): if the donor
is genuinely a no-reuse streamer the step is free, and whichever tenant is
actually starved claims the freed band by ordinary LRU competition -- which is
exactly what happened when canneal recovered 15.8 -> 25.2 MB with nothing
reserved for it. Detection accuracy is therefore not on the critical path for
the do-no-harm invariant; verification is. The db gate survives only as a
PRIORITY ORDER for phase 3 (reserve), where a wrong guess costs one way and is
caught by the improvement gate.
"""
import argparse
import json
import os
import statistics as st
import time

import coeffd3 as c3
from coeffd3 import log

# ── nature classification (mbm x hit), stabilized ────────────────────
MBM_HI_BPS   = 3e9    # a real streamer MOVES bandwidth (coeffd3 AT_CAP_MIN_MBM_BPS)
HIT_LO       = 0.5    # no-reuse boundary (coeffd3 AT_CAP_HITRATE)
NATURE_STABLE = 3     # consecutive agreeing scans before a call is actionable

# ── Change 1: unmanaged baseline ─────────────────────────────────────
BASELINE_CYCLES = 3   # decide cycles of untouched running averaged into the anchor
BASELINE_MAX    = 12  # keep trying this long for an unstable tenant before
                      # declaring it has NO usable floor
BASELINE_COV    = 0.10  # coefficient of variation over the samples; above this
                      # the anchor is a transient, not a steady state
FLOOR_MARGIN    = 0.05  # 5% -- run-to-run IPC noise; a dip inside this is not harm

# ── fence ────────────────────────────────────────────────────────────
FENCE_MIN_WAYS = 1    # a donor is never fenced out of the cache entirely
FENCE_STEP     = 1    # one way per verified step, per the triage contract
RSV_MIN_CROWD  = 2    # ways that always stay in the shared remainder
WAY_MB         = 4.0  # 60 MB L3 / 15 ways on this box; override per machine
COLLAT_MAX     = 2    # identical collateral rollbacks before the step is blocked

# ── ELIMINATE (the absorber cap). Measured D3-v2 2026-09-03:
# fencing a STREAMER 15->2 ways cost it 1.1% (hit 0.02: no reuse, so no
# associativity to lose). Capping a REUSE tenant 15->4 ways cost it 7.2% (hit
# 0.94) EVEN THOUGH the cap preserved its capacity (14.9 -> 15.2 MB). Under CAT
# a mask of N ways IS N-way associativity, so confining a high-hit tenant is
# never free and no occupancy-denominated cap can be safe by construction.
# CONSEQUENCE FOR THE LAYOUT: disjoint bands are safe for low-hit tenants and
# unsafe for high-hit ones -- the problem is not bands vs nested prefixes, it is
# WHO gets confined. So the cap is not computed and applied in one shot; it walks
# down one way at a time from the tenant's current width under the same
# two-sided verify as the fence, and stops at its associativity knee.
CAP_MIN_WAYS   = 2
ABSORB_MIN_MB  = 2.0   # must have actually gained this much to count as absorbing
ABSORB_MAX_CONV = 0.10 # %IPC gained per MB gained; below this it is wasting cache

NATURES = ("STREAMER", "SENSITIVE", "DUAL", "NEUTRAL")


def cmt_mb(t):
    """Occupancy in MB. coeffd3's Tenant.cmt is BYTES -- its snapshot divides by
    1048576 for display, so anything reading t.cmt directly must convert too.
    Measured consequence of getting this wrong (D3-v3 c4): seed_ways saw
    ~1.5e6 'MB', clamped to nbits, and the fence's first step became 15->14 --
    the exact partitioning defect the seed was added to prevent. The same error
    made every absorber conversion read -0.000 %/MB, so a DONOR and the VICTIM
    were both flagged as absorbers.
    """
    return (t.cmt or 0) / 1048576.0


def parse_ipc_tail(path, n=c3.PERF_TAIL_SAMPLES):
    """Tail-mean true IPC = instructions / CPU_CLK_UNHALTED.THREAD.

    coeffd3 records ips (instructions/SECOND), which folds in the core clock:
    with turbo on, canneal's clock rose 8.5% from solo to loaded and HALVED its
    apparent harm (-17.5% in ips vs -8.4% in IPC on the same scene). Turbo is
    forced off by bench_lib now, but any DVFS or HT-residency change would
    reintroduce the same error, so every harm and cost decision here is taken
    on IPC. perf's own 'insn per cycle' metric field reads 0 (a metric-grouping
    artefact) but BOTH raw counters are already in the same json, so no extra
    events and no re-run are needed.

    ESTIMATOR NOTE (V3) -- deliberate, and stated because it was not. This is a
    tail MEAN over the last PERF_TAIL_SAMPLES intervals; the offline grader
    peer_metrics.true_ipc() is a MEDIAN over the whole phase. Same two counters,
    NOT interchangeable. Online wants responsiveness inside one 10 s decide cycle
    (a whole-phase median would lag the step it is meant to judge); offline wants
    a phase summary robust to a single excursion. Consequence when reading
    results: the controller's own commit/rollback verdicts and the grader's c4
    column use different estimators and can disagree at the margin.
    """
    ins, cyc = {}, {}
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in lines[-400:]:
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        v = o.get("counter-value")
        if v in (None, "", "<not counted>", "<not supported>"):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        ev, t = o.get("event"), o.get("interval")
        if ev == "instructions":
            ins[t] = v
        elif ev == "CPU_CLK_UNHALTED.THREAD":
            cyc[t] = v
    vals = [ins[t] / cyc[t] for t in sorted(ins) if t in cyc and cyc[t] > 0]
    vals = vals[-n:]
    return sum(vals) / len(vals) if vals else None


class Coeffd4(c3.Daemon):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ipc = {}            # name -> current tail-mean IPC
        self.base = {}           # Change 1: name -> {"ipc","db","ips"} unmanaged anchor
        self._base_acc = {}      # name -> [ipc samples] while the anchor is forming
        self.base_ready = False
        self.nature = {}         # name -> stabilized nature
        self._nat_run = {}       # name -> [candidate, consecutive count]
        self.fence = {}          # name -> ways the donor is confined to
        self._collat = {}        # donor -> consecutive collateral rollbacks
        self.capped = {}         # absorber -> ways it is confined to
        self.cap_done = {}       # absorber -> narrowest width it has COMMITTED
        self._cap_collat = {}    # absorber -> consecutive collateral rollbacks
        self.sanctioned = set()  # tenants under an authorised T2 breach
        self.db_visible = {}     # name -> is its harm visible in dram_bound?
        self.no_floor = set()    # tenants with no trustworthy unmanaged anchor
        self._harm_set = frozenset()  # last-seen set of below-floor tenants
        self.band_lo = {}        # name -> low bit of its band (donors only)
        self.held = set()        # donors currently fenced
        self.breaches = 0        # Change 3: sanctioned floor breaches, counted
        self.futile = False      # Change 2: hysteresis latch
        self.scene_key = self._scene_key()

        # V2 -- COEFFD4_DESIGN: "Drop the static SHARED_MIN_WAYS floor and the
        # RSV_MAX_WAYS cap -- the ips-flat / improvement gates are the real
        # bounds now." coeffd4 never touched them, so the INHERITED reserve path
        # silently kept coeffd3's ceilings (victim capped at 5 ways, crowd
        # backstopped at 7). That would have read as "the free-lunch controller
        # under-reserves" when it is leftover inheritance. The band model's own
        # room checks in masks() are the real bound.
        c3.RSV_MAX_WAYS = self.nbits
        c3.SHARED_MIN_WAYS = RSV_MIN_CROWD

    # ── sensing ──────────────────────────────────────────────────────
    def _scene_key(self):
        return tuple(sorted(n for n, t in self.tenants.items() if t.alive))

    def refresh_ipc(self):
        for n, t in self.tenants.items():
            if t.alive:
                v = parse_ipc_tail(t.perf_json)
                if v:
                    self.ipc[n] = v

    def classify(self):
        """mbm x hit -> nature, with NATURE_STABLE-scan hysteresis.

        Hysteresis matters because a single scan straddling a workload phase
        boundary flips the call, and a flipped call sends the fence at a tenant
        with real reuse. The candidate must repeat before it is actionable.
        """
        for n, t in self.tenants.items():
            if not t.alive or t.hit_rate is None:
                continue
            hi_bw = t.mbm_bps >= MBM_HI_BPS
            lo_hit = t.hit_rate <= HIT_LO
            cand = ("STREAMER" if (hi_bw and lo_hit) else
                    "DUAL" if (hi_bw and not lo_hit) else
                    "NEUTRAL" if lo_hit else "SENSITIVE")
            run = self._nat_run.get(n)
            if run and run[0] == cand:
                run[1] += 1
            else:
                self._nat_run[n] = run = [cand, 1]
            if run[1] >= NATURE_STABLE:
                self.nature[n] = cand
                # The taxonomy splits low-mbm/high-hit into SENSITIVE-visible
                # (db >= gate) and SENSITIVE-invisible / LC (the documented
                # black-box blind spot). classify() folded both into SENSITIVE
                # and snapshot() logged neither, so the paper's scope-boundary
                # claim had no data trail. db is NOT used to gate anything here
                # -- it is recorded, which is all it has earned.
                self.db_visible[n] = (t.db is not None and t.db >= c3.GATE_PP)

    def capture_baseline(self):
        """Change 1: the mix-start UNMANAGED anchor, for EVERY tenant.

        Donors are in here too (Change 3). If they were not, do-no-harm would be
        vacuous -- any victim can be 'recovered harm-free' by taking from a
        tenant nobody counts, which is the accounting error that forced the
        exp16 DNH-oracle retraction.

        NOTE this anchor is the CONTENDED state (the streamers are already
        running), so it is a do-no-harm FLOOR, not a hot-set estimator. It says
        'no worse than unmanaged', not 'as good as solo'.
        """
        if self.base_ready:
            return                  # anchored; later samples are never used (B6)
        for n, v in self.ipc.items():
            self._base_acc.setdefault(n, []).append(v)
        # THE ANCHOR MUST BE A STEADY STATE, NOT A SNAPSHOT. Measured in D3-v4:
        # npb_ft_d75 is phasic (the harness warns "never reached steady state in
        # 300s" for it), its 3-sample median anchored at IPC 1.265, and it then
        # ran at ~0.87 for the entire scene -- reading 31.8% "below floor"
        # permanently, from before any action was taken. floor_breaches() then
        # flagged it as collateral on EVERY step, rolling back the fence twice
        # and blocking it. The do-no-harm machinery worked perfectly on a
        # measurement that was wrong. So: require the samples to be stable, keep
        # looking if they are not, and if a tenant never settles, say it has NO
        # floor rather than inventing one it will always violate.
        for n, xs in self._base_acc.items():
            if n in self.base or len(xs) < BASELINE_CYCLES:
                continue
            m = st.median(xs)
            cov = (st.pstdev(xs) / m) if m else 9.9
            if cov <= BASELINE_COV:
                t = self.tenants[n]
                self.base[n] = {"ipc": m, "db": t.db, "ips": t.ips,
                                "cmt": cmt_mb(t), "cov": round(cov, 3)}
            elif len(xs) >= BASELINE_MAX:
                self.no_floor.add(n)
                self.record("baseline-unstable", tenant=n, cov=round(cov, 3),
                            samples=len(xs), spread=[round(min(xs), 3), round(max(xs), 3)],
                            effect="no do-no-harm floor; excluded from the "
                                   "collateral veto so it cannot veto every step")
        self._base_cycles = getattr(self, "_base_cycles", 0) + 1
        want = [n for n, t in self.tenants.items() if t.alive]
        # ready when every live tenant is anchored, OR after a grace period --
        # a tenant whose perf events never materialise (unsupported counter,
        # dead wrapper) must not deadlock the controller into never acting. It
        # simply has no floor, and below_floor() reports False for it, which is
        # recorded in the scan rows as floor=None rather than silently passing.
        if not self.base_ready and (
                all(n in self.base or n in self.no_floor for n in want)
                or self._base_cycles >= BASELINE_MAX + 3):
            self.base_ready = True
            missing = [n for n in want if n not in self.base]
            self.no_floor |= set(missing)
            if missing:
                log(f"!! no IPC baseline for {missing} -- they have NO do-no-harm "
                    f"floor and every figure for them reads '--'")
            self.record("baseline", base=dict(self.base), missing=missing,
                        nature=dict(self.nature))
            log(f"unmanaged baseline anchored for {len(self.base)}/{len(want)} tenants")

    def harm_changed(self):
        """Has the SET of tenants below their floor changed since we stood down?

        COEFFD4_DESIGN's re-engage condition is "tenant arrive/leave OR LOAD
        SHIFT past threshold". The V7 latch fix implemented only the first half,
        so in D3-v4 coeffd4 stood down at a scan where nobody was below floor and
        then never looked again for the remaining 16 scans -- even though the
        harm state kept changing underneath it. Arrival/departure is not the only
        way a scene becomes actionable again.
        """
        now = frozenset(self.floor_breaches())
        if now != getattr(self, "_harm_set", frozenset()):
            self._harm_set = now
            return True
        return False

    def scene_changed(self):
        k = self._scene_key()
        if k != self.scene_key:
            log(f"scene change {self.scene_key} -> {k}: re-anchoring")
            self.record("scene-change", was=list(self.scene_key), now=list(k))
            self.scene_key = k
            self.base, self._base_acc, self.base_ready = {}, {}, False
            self.no_floor, self._base_cycles = set(), 0
            self.futile = False          # Change 2: hysteresis releases here
            return True
        return False

    def is_hurt(self, t):
        """MEASURED harm, replacing coeffd3's absolute dram_bound gate.

        V1: coeffd4's own docstring calls the db gate falsified and demotes it to
        a probe-ordering prior, but coeffd3's is_hurt() (db >= GATE_PP=20) still
        HARD-GATED the inherited THROTTLE and RESERVE phases through
        super().propose(). Measured in the D3-v3 c4 arm, it is inverted:

            npb_cg_d80  db 53.2  -> flagged HURT, and is NOT a victim (-4.2%)
            canneal     db 16.3  -> not flagged, and IS the only victim (-12.6%)

        which is why the one inherited step in that run reserved cache for the
        absorber. Harm is now judged the way Change 1 defines it -- below the
        tenant's own unmanaged baseline -- plus the L2 SLO signal when a tenant
        supplies one. Note the honest limit: the baseline is the CONTENDED state,
        so this detects "worse than unmanaged", NOT "worse than solo". The fence
        does not need victim identification (it is aggressor-side and victims
        free-ride); reserve does, and this is the strongest signal we actually
        have online.
        """
        return self.below_floor(t.name) or t.slo_violating()

    # ── Change 3: the floor, applied to everyone ─────────────────────
    def below_floor(self, name):
        """IPC under its own unmanaged anchor by more than the noise margin."""
        b, cur = self.base.get(name), self.ipc.get(name)
        if not b or cur is None or not b["ipc"]:
            return False
        return cur < b["ipc"] * (1.0 - FLOOR_MARGIN)

    def floor_breaches(self, exclude=()):
        """Tenants below their unmanaged floor, excluding those that are ALLOWED
        to be.

        Two exclusions that were missing and both cause spurious rollbacks:
        - A tenant under a live T2 SANCTIONED throttle is deliberately below its
          floor. Counting it as "collateral" made an unrelated FENCE or CAP step
          on a different tenant roll back for damage the operator authorised.
          The sanction was special-cased in the MBA verify path and never
          propagated to the two other call sites that read the same floor state.
        - A MIGRATED tenant lives on the destination socket, where a dip is
          cross-socket overhead, not something a node-0 mask did. coeffd3 skips
          migrated tenants everywhere it reasons about node-0 levers; this did
          not.
        """
        out = []
        for n in self.base:
            if n in exclude or not self.tenants[n].alive:
                continue
            if n in self.sanctioned:            # authorised to be under its floor
                continue
            if n in self.migrated:              # different socket, different cause
                continue
            if self.below_floor(n):
                out.append(n)
        return out

    # ── band cache model (replaces nested-prefix) ────────────────────
    def masks(self):
        """Contiguous bands: donors low, reserved victims high, crowd between.

        Layout, bit 0 upward:
            [0 .. D)            fenced donors, stacked in fence order
            [D .. N-R)          shared remainder (the crowd + free-riding victims)
            [N-R .. N)          reserved victim slices, stacked top-down by prio
        Fencing a donor shrinks its run toward bit 0, so every way it gives up
        lands in ONE contiguous piece immediately adjacent to the remainder --
        which is what lets an unidentified victim claim it by plain LRU.

        Returns (per-tenant mask, shared mask), same contract as coeffd3.masks.
        """
        per = {}
        lo = 0
        # ROOM IS BOUNDED FOR EVERY BAND, DONORS INCLUDED. The donor loop
        # originally had no nbits bound at all: two admissible streamers (this
        # project's own scenes run two) could stack past bit 14, and then either
        # the CBM write is rejected EINVAL -- which propagates out of
        # apply_masks() and kills the daemon mid-scene -- or, at exactly
        # lo == nbits, the `shared` expression below falls to its else branch and
        # hands out the FULL mask, silently overlapping every band already
        # assigned and destroying the isolation the fence exists to create.
        for name in [n for n in self.slice_order if n in self.held]:
            room = self.nbits - lo - RSV_MIN_CROWD
            w = min(max(FENCE_MIN_WAYS, self.fence.get(name, self.nbits)), room)
            if w < FENCE_MIN_WAYS:
                continue            # no room left: leave it in the shared pool
            per[name] = ((1 << w) - 1) << lo
            self.band_lo[name] = lo
            lo += w
        for name, w in sorted(self.capped.items()):
            if name in per:
                continue            # already banded as a fenced donor (B3)
            # clamp: donors already consumed [0, lo), and the crowd must keep
            # RSV_MIN_CROWD. Without this a cap seeded before the donors were
            # fenced can run off the top of the mask and produce a >nbits CBM,
            # which resctrl rejects with EINVAL.
            # CLAMP ORDER MATTERS. This was written as
            #   w = max(CAP_MIN_WAYS, min(w, room)); if w < CAP_MIN_WAYS: continue
            # where the max() floors w at CAP_MIN_WAYS unconditionally, so the
            # guard below could NEVER fire -- verified by enumeration: room=-5,
            # -1, 0 all yielded w=2 and skip=False. The clamp meant to prevent an
            # over-wide CBM instead guaranteed one. Clamp first, THEN decide.
            w = min(w, self.nbits - lo - RSV_MIN_CROWD)
            if w < CAP_MIN_WAYS:
                continue            # no room to cap it: leave it in the crowd
            per[name] = ((1 << w) - 1) << lo
            lo += w
        hi = self.nbits
        holders = [n for n in self.slice_order
                   if n not in self.held and n not in self.capped
                   and self.alloc.get(n, 0) > 0]
        holders.sort(key=lambda n: (-self.tenants[n].prio, self.slice_order.index(n)))
        for name in holders:
            w = self.alloc[name]
            if hi - w < lo + RSV_MIN_CROWD:
                break                      # never squeeze the crowd out
            per[name] = ((1 << w) - 1) << (hi - w)
            hi -= w
        shared = ((1 << (hi - lo)) - 1) << lo if hi > lo else (1 << self.nbits) - 1
        return per, shared

    # ── Change 2: is any donor admissible at all? ────────────────────
    def admissible_donors(self):
        """Stable streamers we could fence, cheapest-first.

        Deliberately NOT gated on the earned at_capacity flag: that needs prior
        actuations to be set, so gating on it would deadlock at t=0 and the
        controller would stand down on a scene it could actually help.
        """
        out = []
        for n, t in self.tenants.items():
            if not t.alive or self.nature.get(n) != "STREAMER":
                continue
            if n in self.migrated or f"fence|{n}" in self.blocked:
                continue
            if n in self.capped:
                # It was capped as an absorber and has since stabilized as a
                # STREAMER. Release the cap before fencing it -- holding both
                # double-books its band, orphans the narrower one entirely (6
                # dead ways in the measured case), and makes verify attribute
                # the fence's pre/post IPC to a mask that was never applied.
                self.capped.pop(n, None)
                self.apply_masks()
                self.record("cap-released", tenant=n, why="reclassified STREAMER")
            if (self.fence.get(n) or self.seed_ways(n)) <= FENCE_MIN_WAYS:
                continue
            out.append(n)
        # cheapest donor first = lowest hit rate = least plausible reuse
        out.sort(key=lambda n: (self.tenants[n].hit_rate if
                                self.tenants[n].hit_rate is not None else 1.0))
        return out

    # ── phase 1: FENCE ───────────────────────────────────────────────
    def seed_ways(self, name):
        """First band width for a donor = its MEASURED occupancy, in ways.

        COEFFD4_DESIGN says "seed each streamer band from its occupancy->ways";
        the first implementation defaulted to `self.nbits` instead, and that is
        catastrophic rather than merely wrong. Before any fence every tenant
        holds the FULL mask and they SHARE all 15 ways. Handing the donor an
        exclusive band of nbits-1 does not restrict it -- it PARTITIONS, giving
        the donor 14 exclusive ways and squeezing every victim into the single
        remaining one. Measured in D3-v2 phase c4: 11 attempts, all at 15->14,
        all correctly rolled back as `collateral`, canneal driven to 1.6 MB
        occupancy and -16.6% against unmanaged.

        Seeding at occupancy makes the first step a no-op FOR THE DONOR (it
        already holds that much) while immediately giving the victims the rest
        as an exclusive region. Verify then corrects the estimate downward.
        """
        mb = cmt_mb(self.tenants[name])
        w = int(mb / WAY_MB) + (1 if (mb % WAY_MB) else 0)
        return max(FENCE_MIN_WAYS + 1, min(self.nbits, w))

    def try_fence(self):
        for n in self.admissible_donors():
            t = self.tenants[n]
            cur = self.fence.get(n) or self.seed_ways(n)
            new = max(FENCE_MIN_WAYS, cur - FENCE_STEP)
            if new == cur:
                continue
            self.fence[n] = new
            if n not in self.slice_order:
                self.slice_order.insert(0, n)
            self.held.add(n)
            self.apply_masks()
            self.act_seq += 1
            self.pending = {
                "step": ("fence", n, new), "victim": None, "donor": n,
                "pre_ipc": dict(self.ipc), "pre_db": t.db, "prev_ways": cur,
                "fine_before": {m: {"db": x.db, "ips": x.ips}
                                for m, x in self.tenants.items() if x.alive},
            }
            self.record("apply", step=("fence", n, new), donor=n,
                        donor_hit=t.hit_rate, donor_mbm=t.mbm_bps,
                        ways=f"{cur}->{new}")
            return True
        return False

    def shared_ways(self):
        """Width of the free-for-all remainder = what an uncapped tenant holds."""
        _, shared = self.masks()
        return bin(shared).count("1")

    def absorbers(self):
        """Tenants converting freed cache POORLY, worst first -- measured.

        An absorber is not "the tenant we suspect"; it is one that demonstrably
        took capacity and did nothing with it. Conversion = %IPC gained per MB
        gained since the unmanaged baseline. In D3-v2 canneal converted at
        0.69 %/MB and npb_cg_d80 at 0.012 %/MB -- 57x worse -- which identified
        the absorber correctly. Excluded: donors, anything already below its
        floor (never squeeze the injured), and the worst-hurt tenant.
        """
        out = []
        for n, t in self.tenants.items():
            if not t.alive or n in self.held or n not in self.base:
                continue
            if self.nature.get(n) == "STREAMER":
                continue        # a streamer is a FENCE target, never a cap target
            if self.below_floor(n):
                continue        # never squeeze a tenant that is already injured
            if self.is_hurt(self.tenants[n]):
                continue        # the intended RESERVE beneficiary is not an
                                # absorber -- the docstring promised this
                                # exclusion and the code did not implement it
            d_mb = cmt_mb(t) - (self.base[n].get("cmt") or 0)
            if d_mb < ABSORB_MIN_MB:
                continue
            b = self.base[n]["ipc"]; cur = self.ipc.get(n)
            if not b or cur is None:
                continue
            conv = ((cur / b - 1.0) * 100.0) / d_mb
            if conv <= ABSORB_MAX_CONV:
                out.append((conv, n, d_mb))
        out.sort()
        return out

    def try_eliminate(self):
        """Narrow the worst absorber by ONE way, verified.

        Deliberately NOT the static cat_cap arm's one-shot occupancy cap: that
        jumped npb_cg_d80 straight to 4 ways and cost it 7.2%, because capacity
        and associativity are the same knob under CAT. Walking down from its
        CURRENT width means the step that would cross its associativity knee is
        the step that gets rolled back, and everything before it is free.
        """
        for conv, n, d_mb in self.absorbers():
            if f"cap|{n}" in self.blocked:
                continue
            cur = self.capped.get(n) or self.cap_done.get(n) or self.shared_ways()
            new = cur - 1
            if new < CAP_MIN_WAYS:
                continue
            self.capped[n] = new
            self.apply_masks()
            self.act_seq += 1
            self.pending = {"step": ("cap", n, new), "victim": None, "absorber": n,
                            "pre_ipc": dict(self.ipc), "pre_db": self.tenants[n].db,
                            "prev_ways": cur, "conv": conv, "gained_mb": d_mb}
            self.record("apply", step=("cap", n, new), absorber=n,
                        conv_pct_per_mb=conv, gained_mb=d_mb,
                        hit=self.tenants[n].hit_rate, ways=f"{cur}->{new}")
            return True
        return False

    def verify_cap(self, p):
        n = p["absorber"]
        pre, cur = p["pre_ipc"].get(n), self.ipc.get(n)
        hurt_self = (pre and cur is not None and cur < pre * (1.0 - FLOOR_MARGIN))
        floor_breach = self.below_floor(n)
        collateral = self.floor_breaches(exclude=(n,))
        if hurt_self or floor_breach or collateral:
            # Restore to the last COMMITTED width, not to "uncapped". Releasing
            # the whole cap made the next try_eliminate reseed from the full
            # crowd width and re-walk every step it had already paid for:
            # measured in D3-v8, the walk ran 13->12->11->10, rolled back at 9,
            # then repeated 13->12->11->10->9 from scratch -- 16 commits to
            # achieve 4 ways of narrowing, which is exactly the re-exploration
            # churn Change 2 exists to prevent.
            # NB: do NOT gate this on shared_ways() -- that is computed from the
            # CURRENT capped state, so comparing the restore target against it is
            # circular and released the cap entirely (caught by the unit test).
            # A committed width was already verified safe; restore to it.
            back = self.cap_done.get(n)
            if back is not None:
                self.capped[n] = back
            else:
                self.capped[n] = p["prev_ways"]
                if p["prev_ways"] >= self.shared_ways():
                    self.capped.pop(n, None)
            self.apply_masks()
            why = ("assoc_knee" if hurt_self else
                   "floor_breach" if floor_breach else "collateral")
            # assoc_knee is TERMINAL for this tenant: one way further is what
            # hurt it, and its knee will not move while the scene does not.
            if hurt_self or floor_breach:
                self.blocked[f"cap|{n}"] = time.monotonic() + c3.BLOCK_TTL_SECS
            else:
                self._cap_collat[n] = self._cap_collat.get(n, 0) + 1
                if self._cap_collat[n] >= COLLAT_MAX:
                    self.blocked[f"cap|{n}"] = time.monotonic() + c3.BLOCK_TTL_SECS
                    self.record("cap-blocked", absorber=n, reason="repeated collateral",
                                count=self._cap_collat[n], collateral=collateral)
            self.record("rollback", step=p["step"], absorber=n, why=why,
                        ipc_pre=pre, ipc_post=cur, hit=self.tenants[n].hit_rate,
                        floor=(self.base.get(n) or {}).get("ipc"),
                        collateral=collateral)
            return
        self.cap_done[n] = min(self.cap_done.get(n, self.nbits), p["step"][2])
        self._cap_collat.pop(n, None)
        self.record("commit", step=p["step"], absorber=n,
                    absorber_cost_pct=((cur / pre - 1.0) * 100.0
                                       if (pre and cur is not None) else None),
                    freed_to_crowd_ways=1)

    def rollback_fence(self, p):
        n = p["donor"]
        self.fence[n] = p["prev_ways"]
        if p["prev_ways"] >= self.nbits:
            self.held.discard(n)
            self.fence.pop(n, None)
        self.apply_masks()

    def verify_fence(self, p):
        """Change 3: TWO-SIDED. Same numeric test, two different verdicts.

        nature_wrong -- the donor's own IPC fell relative to the step. It has
            reuse we did not see, i.e. our classification was wrong. Roll back
            AND block it from being fenced again, because re-probing a reuser
            costs it every time.
        floor_breach -- the donor is under its unmanaged anchor. The nature call
            may be right and the step still cost it more than the contract
            allows. Roll back, but do NOT block: the floor is a state, not a
            property, and it may be admissible again later.

        Collapsing these into one 'it got slower' branch loses the distinction
        between a sensing failure we should learn from and a contract violation
        we must undo.
        """
        n = p["donor"]
        pre = p["pre_ipc"].get(n)
        cur = self.ipc.get(n)
        nature_wrong = (pre and cur is not None and cur < pre * (1.0 - FLOOR_MARGIN))
        floor_breach = self.below_floor(n)
        collateral = self.floor_breaches(exclude=(n,))

        if nature_wrong or floor_breach or collateral:
            self.rollback_fence(p)
            why = ("nature_wrong" if nature_wrong else
                   "floor_breach" if floor_breach else "collateral")
            if nature_wrong:
                self.blocked[f"fence|{n}"] = time.monotonic() + c3.BLOCK_TTL_SECS
                self.nature[n] = "DUAL"       # it has reuse; stop calling it free
                self._nat_run[n] = ["DUAL", NATURE_STABLE]
            if why == "collateral":
                # Repeating a step that harmed the same bystanders is not
                # triage, it is thrashing -- and the retry itself costs them,
                # because the bad mask is live for the whole verify window. In
                # D3-v2 the identical 15->14 step was retried 11 times and the
                # victims spent ~45% of the c4 window under it. Block after
                # COLLAT_MAX so the futility latch (Change 2) can fire.
                self._collat[n] = self._collat.get(n, 0) + 1
                if self._collat[n] >= COLLAT_MAX:
                    self.blocked[f"fence|{n}"] = time.monotonic() + c3.BLOCK_TTL_SECS
                    self.record("fence-blocked", donor=n, reason="repeated collateral",
                                count=self._collat[n], collateral=collateral)
            else:
                self._collat.pop(n, None)
            self.record("rollback", step=p["step"], donor=n, why=why,
                        donor_ipc_pre=pre, donor_ipc_post=cur,
                        donor_floor=(self.base.get(n) or {}).get("ipc"),
                        collateral=collateral)
            return

        # committed. The step is free by measurement, not by assumption: we do
        # NOT require a victim to have improved, because the freed band is
        # claimed by LRU and the tenant that claims it need not be one we
        # identified. What we DO require is that nobody went below their floor.
        self._collat.pop(n, None)
        gain = {m: (self.ipc[m] / p["pre_ipc"][m] - 1.0) * 100.0
                for m in self.ipc
                if m in p["pre_ipc"] and p["pre_ipc"][m] and m != n}
        self.record("commit", step=p["step"], donor=n,
                    donor_cost_pct=((cur / pre - 1.0) * 100.0
                                    if (pre and cur is not None) else None),
                    peer_ipc_delta_pct=gain)

    # ── verify dispatch ──────────────────────────────────────────────
    def verify(self):
        p = self.pending
        if p and p["step"][0] == "fence":
            self.pending = None
            self.verify_fence(p)
            return
        if p and p["step"][0] == "cap":
            self.pending = None
            self.verify_cap(p)
            return
        super().verify()
        if self.pending is not None:
            return          # migration grace: the step has not been judged yet
        # coeffd3's propose() re-derives "streamer = free donor" from INSTANTANEOUS
        # hit/mbm in its own _donor_tier closure, which never consults
        # self.nature. So a donor that verify_fence just proved has reuse (and
        # relabelled DUAL) is invisible to the throttle phase and can be picked
        # as free on the next cycle -- Change 3 reopened at the seam between the
        # new phases and the inherited ones. We cannot hook a closure, so undo
        # the mis-tiered step here, using machinery that already exists.
        if p and p["step"][0] == "mba":
            tgt = p["step"][1]
            vic = self.mba_beneficiary.get(tgt)
            lower = (vic in self.tenants
                     and self.tenants[tgt].prio < self.tenants[vic].prio)
            if (self.nature.get(tgt) not in (None, "STREAMER") and not lower
                    and self.mba.get(tgt, 100) < 100):
                self.record("mistier-undo", tenant=tgt, nature=self.nature.get(tgt),
                            depth=self.mba[tgt],
                            why="stabilized nature is not STREAMER and step is "
                                "not priority-sanctioned")
                self.mba[tgt] = 100
                self.sanctioned.discard(tgt)
                self.apply_mba(tgt)
                self.mba_pre_db.pop(tgt, None); self.mba_pre_mbm.pop(tgt, None)
                self.mba_pre_ips.pop(tgt, None)
                return
        # Change 3 applies to inherited levers too: an MBA throttle that put an
        # EQUAL-priority tenant under its floor is a violation regardless of
        # what the inherited cost-bound thought. A LOWER-priority tenant under
        # its floor is a SANCTIONED breach -- allowed, but counted, so the paper
        # reports a number instead of an assurance.
        if p and p["step"][0] == "mba":
            tgt = p["step"][1]
            if self.below_floor(tgt) and self.mba.get(tgt, 100) < 100:
                vic = self.mba_beneficiary.get(tgt)
                vp = self.tenants[vic].prio if vic in self.tenants else None
                if vp is not None and self.tenants[tgt].prio < vp:
                    self.breaches += 1
                    self.sanctioned.add(tgt)
                    self.record("sanctioned_breach", tenant=tgt, prio=self.tenants[tgt].prio,
                                victim=vic, victim_prio=vp, depth=self.mba[tgt],
                                baseline=(self.base.get(tgt) or {}).get("ipc"),
                                observed=self.ipc.get(tgt), total=self.breaches)
                else:
                    self.record("floor-violation-undo", tenant=tgt, depth=self.mba[tgt],
                                baseline=(self.base.get(tgt) or {}).get("ipc"),
                                observed=self.ipc.get(tgt))
                    self.mba[tgt] = 100
                    self.sanctioned.discard(tgt)
                    self.apply_mba(tgt)
                    self.mba_pre_db.pop(tgt, None)
                    self.mba_pre_mbm.pop(tgt, None)
                    self.mba_pre_ips.pop(tgt, None)

    # ── V8: continuous guard for the two NEW persistent levers ───────
    def guard_release(self):
        """coeffd3's guard, then the same surveillance for FENCE and CAP.

        coeffd3.guard_release() iterates self.mba in both of its passes and never
        looks at self.fence or self.capped. coeffd4 introduced two persistent,
        ONE-SHOT-VERIFIED levers without extending it -- reintroducing for the
        fence and the cap exactly the defect guard_release was built to fix for
        MBA. Not hypothetical: it is the exp14 finding that a throttled sphinx
        passed one verify window and then collapsed over minutes ("one-shot
        verification insufficient for slow-compounding actions"). A fenced donor
        whose working set grows, or a capped absorber drifting past its
        associativity knee, had nothing to catch it.

        Releasing WIDENS by one way -- the reverse of the step that narrowed --
        so recovery is as incremental and reversible as the squeeze was.
        """
        if super().guard_release():
            return True
        for n in sorted(self.held):
            if n in self.sanctioned or not self.tenants[n].alive:
                continue
            if not self.below_floor(n):
                continue
            cur = self.fence.get(n, self.nbits)
            self.record("guard-release", lever="fence", tenant=n,
                        ways=f"{cur}->{cur + 1}",
                        floor=(self.base.get(n) or {}).get("ipc"),
                        observed=self.ipc.get(n))
            if cur + 1 >= self.seed_ways(n):
                self.fence.pop(n, None); self.held.discard(n)
            else:
                self.fence[n] = cur + 1
            self.blocked[f"fence|{n}"] = time.monotonic() + c3.BLOCK_TTL_SECS
            self.apply_masks()
            self.futile = False        # the scene moved; re-arm exploration
            return True
        for n in sorted(self.capped):
            if n in self.sanctioned or not self.tenants[n].alive:
                continue
            if not self.below_floor(n):
                continue
            cur = self.capped[n]
            self.record("guard-release", lever="cap", tenant=n,
                        ways=f"{cur}->{cur + 1}",
                        floor=(self.base.get(n) or {}).get("ipc"),
                        observed=self.ipc.get(n))
            if cur + 1 >= self.shared_ways():
                self.capped.pop(n, None)
            else:
                self.capped[n] = cur + 1
            self.blocked[f"cap|{n}"] = time.monotonic() + c3.BLOCK_TTL_SECS
            self.apply_masks()
            self.futile = False
            return True
        return False

    # ── aggressor-first phase order ──────────────────────────────────
    def propose(self):
        now = time.monotonic()
        self.blocked = {k: e for k, e in self.blocked.items() if now < e}
        if not self.base_ready:
            return False                       # never act before the anchor exists

        # Change 2 predictive stand-down: no admissible donor, nothing to
        # sanction, nowhere to migrate -> HOLD without probing. coeffd3 probed
        # anyway and re-explored ~8x per zero-sum scene, each probe costing the
        # tenant it touched.
        # The early-out must also ask whether the free lunch we ALREADY took is
        # being wasted. D3-v5: coeffd4 committed three fence steps, walked
        # npb_mg_d300 down to FENCE_MIN_WAYS, and then stood down "no admissible
        # donor, no hurt victim" -- so try_eliminate() was never reached, because
        # phase 2 sits AFTER a gate that only knows about donors and hurt
        # victims. But an absorber soaking up freed cache it cannot convert is
        # exactly the case ELIMINATE exists for, and it need not coincide with
        # anyone being below their floor: online, harm is measured against the
        # UNMANAGED baseline, so a victim that is merely un-helped is not "hurt".
        donors = self.admissible_donors()
        if not donors and not self.hurt_list() and not self.absorbers():
            if not self.futile:
                self.futile = True
                self.record("stand-down", why="no admissible donor, no hurt victim",
                            natures=dict(self.nature))
            return False

        # Change 2 hysteresis, ENFORCED. self.futile used to be write-only: it
        # suppressed a duplicate log record and gated nothing, so every lever ran
        # again at full strength on the very next cycle and the only brake was
        # coeffd3's `blocked` TTL -- exactly the mechanism the design says was
        # already insufficient. Once we have settled with harm remaining, HOLD
        # until the scene changes.
        if self.futile:
            if not self.harm_changed():
                return False
            self.futile = False        # the harm state moved: re-engage
            self.record("re-engage", why="floor-breach set changed",
                        breaches=sorted(self._harm_set))

        if self.try_fence():                   # phase 1: reclaim from streamers
            return True
        if self.try_eliminate():               # phase 2: stop the waste
            return True
        if super().propose():                  # phases 3-4 (throttle, reserve)
            return True
        if not self.futile:                    # Change 2 hysteresis latch
            self.futile = True
            self.record("stand-down", why="no admissible step",
                        natures=dict(self.nature), fenced=sorted(self.held))
        return False

    # ── main loop hooks ──────────────────────────────────────────────
    def snapshot(self):
        s = super().snapshot()
        for n, row in s.items():
            row["ipc"] = self.ipc.get(n)
            row["nature"] = self.nature.get(n)
            row["floor"] = (self.base.get(n) or {}).get("ipc")
            row["below_floor"] = self.below_floor(n) if n in self.base else None
            row["no_floor"] = n in self.no_floor
            row["fence_ways"] = self.fence.get(n)
            row["cap_ways"] = self.capped.get(n)
            row["db_visible"] = self.db_visible.get(n)
            row["sanctioned"] = n in self.sanctioned
        return s

    def run(self):
        # the sense/decide loop is coeffd3's; we only need the extra sensing to
        # happen before each decide, which update_at_capacity() is called for.
        orig = self.update_at_capacity

        def hook():
            self.refresh_ipc()
            self.classify()
            if self.scene_changed():
                self._nat_run.clear()
            self.capture_baseline()
            orig()

        self.update_at_capacity = hook
        log(f"coeffd4 up (free-lunch-scoped, band cache model, aggressor-first): "
            f"{len(self.tenants)} tenants, {self.nbits} ways, "
            f"floor margin {FLOOR_MARGIN*100:.0f}%, nature stable after "
            f"{NATURE_STABLE} scans")
        super().run()
        self.record("summary", sanctioned_breaches=self.breaches,
                    fenced=sorted(self.held), fence_ways=dict(self.fence),
                    capped=dict(self.capped), natures=dict(self.nature))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--observe", action="store_true")
    ap.add_argument("--perf-attach", choices=("pid", "cores"), default="cores",
                    help="cores is the default here: coeffd3's pid mode "
                         "undercounts every tenant launched behind a wrapper "
                         "shell, which is all of them in exp16")
    a = ap.parse_args()
    c3.PERF_ATTACH = a.perf_attach
    d = Coeffd4(a.config, a.out, observe=a.observe)
    try:
        d.run()
    finally:
        for t in d.tenants.values():
            t.stop_perf()
        d.restore_all()
        d.decisions.close()


if __name__ == "__main__":
    main()
