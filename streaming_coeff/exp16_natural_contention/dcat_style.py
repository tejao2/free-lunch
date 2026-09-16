#!/usr/bin/env python3
"""dcat_style -- our rendition of dCat (Xu et al., EuroSys'18) for exp16.

WHY THIS ARM: dCat is coeffd's closest DECISION-LOGIC relative and its
floor-ancestor: baseline-guarantee first, then grow-if-benefit, donor
harvesting, and an explicit Streaming (no-reuse) class pinned to minimum
cache -- the ancestor of our squeeze-free-donor taxonomy. Running it
turns the REVIEW.md §6 argument (entitlement floor != unmanaged floor)
into a measurement.

REWRITTEN 2026-09-11 against DCAT_AUDIT.md (paper-vs-code audit, 29 rows).
The previous build was NOT FAITHFUL: its "+1 way paid off?" test averaged
perf intervals that mostly PRE-DATED the mask write (audit F1), and the
paper's pool-dry -> Streaming clause had been removed (F2), so on D3 no
tenant could ever become Receiver or Streaming. Audit item numbers are
cited inline as [Fn] / [#n].

FAITHFUL CORE (their §3, Fig 4/6/7):
 - Baseline = IPC at the tenant's ENTITLEMENT partition, re-measured per
   phase [#1,#2]. Guarantee ENFORCED [F3]: any tenant below entitlement
   whose IPC falls > ipc_imp_thr under its baseline is restored to its
   entitlement at Reclaim priority (Fig 6 "performance degradation" edge).
 - FSM: Keeper (start) / Donor / Unknown / Receiver / Streaming, plus
   Reclaim on phase change (highest priority; pool first [F4]).
 - Phase detection: L1 loads per instruction (their l1_ref/ret_ins), 10%.
 - Unknown -> Receiver if one +1-way step gains >= 5% (ipc_imp_thr).
   Receiver stops (keeps its ways [F5, Q2-A]) on < 5% gain OR miss < 3%.
 - Unknown -> Streaming if ways >= 3x entitlement, OR the pool is empty
   [F2] -- in both cases only if it is STILL Unknown: tested with at least
   one extra way and its IPC never reached (1+thr) x baseline at any size
   tried (cumulative reading, Q1-B: the one charitable to slow gainers).
 - Donors: D1 (low LLC refs) snaps to one way [F9]; D2 (refs but small
   miss) shrinks one way per round; freed ways go to an unallocated pool.
 - Growth: Unknown before Receiver (§3.5), one way per round each, evenly
   (rotating order, fair mode [F7] -- the paper p.9 says its two allocation
   modes coincide absent re-allocation events, which D3 does not have).
 - LLC ways ONLY (dCat has no bandwidth lever).
 - Events [P2]: LLC refs/misses = longest_lat_cache.reference / .miss
   (the paper's Table 2 events 2EH/4FH, 2EH/41H); phase = L1-dcache-loads
   (their D1H events) per instruction; IPC = instructions / unhalted cycles
   summed over the tenant's cores.

MEASUREMENT PROTOCOL [F1]: decisions run in synchronized ROUNDS. After a
mask write, DISCARD_S is dropped, then IPC is taken ONLY from perf
intervals that STARTED after write + DISCARD_S (perf's own interval
stamps; >= JUDGE_MIN_INTERVALS of them). Every judgment is therefore on
post-change data, and the reference is the IPC measured (equally clean)
at the pre-grow size in the round the way was added; a tenant resized
for any other reason in a round gets no +1 way that round. All grow/judge records log pre/post IPC,
gain, and the interval window used.

DEVIATIONS / PORTABILITY (documented; audit §2b):
 P1 per-TENANT resctrl groups (equivalent to their per-workload COS under
    disjoint core pinning -- not a deviation in substance).
 P3 LLC_REF_LOW=1.5e6 refs/s: the paper gives no value; D3 has ~2 orders
    of magnitude margin either side (EP ~1e5, all others >= 2.5e7).
 P4 round = 1 s discard + 2 x 1 s intervals (paper: 1 s period) --
    multi-threaded 4-core tenants need >1 interval to judge a 5% step.
    With perf-interval alignment + write lag a round is ~3.3-6 s in practice.
 P6 contiguous masks in fixed NAME order, pool at the top bits (overlaps
    shareable_bits 0x6000 / DDIO on this box).
 P7 KEEPER_BAND hysteresis (paper's "small"/"non-trivial" miss undefined).
 P8 performance-table recurrence fast path + max-perf allocator omitted:
    immaterial on D3 (no phase recurrence / re-allocation in 400 s; the
    two modes coincide there per paper p.9).
 P9 attaches to a running scene from the unmanaged state: masks go to
    entitlement at t=0, baseline after BASELINE_SETTLE_S [F10]. The 30 s is
    OUR warm-out choice (stale lines from the unmanaged period); the paper's
    Fig 8 "30s" is its reporting protocol for the threshold sweep, not a
    baseline procedure. It also applies after a mid-run phase Reclaim, where
    it freezes the tenant 30 s while the paper grows it at once (Fig 7a/10);
    dormant absent phase changes.
 P10 root resctrl group keeps the full mask; no way-flush (paper §6 same).
 [F6] Entitlement = equal share of ALL ways (15/5 = 3 on D3), pool 0,
    as in the paper's main evals (5 VMs x 4 of 20 ways). Any remainder
    (nbits % n) goes to the pool. Tenants are ordered by NAME, never by
    the cfg's bash-hash order. --pool-ways N reserves N ways as a
    labelled SENSITIVITY arm only.
 [F8 NOT DONE] masks are re-packed on every change, so a change to an
    earlier tenant relocates later ones. Mitigated, not removed: all
    tenants are judged in the same round on post-write data.
 [F3 scope] the guarantee is checked for every tenant below entitlement,
    Streaming included (a pinned tenant that loses >5% was not a streamer;
    restoring it is the charitable reading of the guarantee). A restored
    tenant is held at >= entitlement for the rest of its phase so the
    D1/D2 donor rules cannot shrink it straight back (anti-oscillation).

REVIEW FIXES 2026-09-12 (DCAT_AUDIT.md §8):
 [R1] an `explored` Donor whose miss turns non-trivial becomes Keeper (it
    used to keep its Donor state and shrink every round: 8->3 ways at 10%
    miss). §3.4: D2 shrinks "until the LLC miss rate becomes non-trivial
    (hence labeled as Keeper)".
 [R2] Unknown/Receiver take Fig 6's exit edges every round, judged or not:
    idle (llc_ref <= thr) -> D1 Donor; miss < thr -> stop growing, Keeper
    (Fig 7a). Previously a growing Unknown was never re-categorized and an
    idle Receiver held its ways forever.
 [R3] LLC-loads is logged (never read by decisions) so certify's
    instance-flip fingerprint covers this arm; in-loop mask writes are
    `apply` records so the harness actuation counters see them.

PRE-REGISTERED HYPOTHESES (frozen before the first exp16 scene-A run;
kept verbatim as history -- they name scene-A tenants):
 H-dcat1 entitlement floor != unmanaged floor.
 H-dcat2 no bandwidth lever: stream keeps 100% MBA.
 H-dcat3 IPC-gated growth is tail-blind.

Usage: dcat_style.py --config cfg.json --out outdir [--pool-ways N]
       dcat_style.py --dry-run --out outdir     (synthetic tenants, no root)
Config: {"tenants":[{"name","grp","cores"}...]}  (same shape as copart)
"""
import argparse, json, os, random, signal, subprocess, sys, time

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"
PERF_INT_MS = 1000          # paper: 1 s period (§4, §5.1)
PERF_INT_S = PERF_INT_MS / 1000.0
DISCARD_S = 1.0             # dropped after every mask write [F1]
JUDGE_MIN_INTERVALS = 2     # complete post-write intervals per judgment [F1]
BASELINE_SETTLE_S = 30.0    # [F10] paper Fig 8: measured 30 s after assignment
BASELINE_MEASURE_S = 10.0   # baseline = mean over the last 10 s of the settle

MIN_WAYS = 1
IPC_IMP_THR = 0.05          # their ipc_imp_thr 5% (§5.1)
LLC_REF_LOW = 1.5e6         # refs/s; paper gives no value [P3]
MISS_RATE_THR = 0.03        # their llc_miss_rate_thr 3% (§5.1)
PHASE_THR = 0.10            # 10% change in L1 loads/instr = phase change
STREAM_MULT = 3             # ways >= 3x entitlement w/o gain -> Streaming
KEEPER_BAND = 0.5           # [P7]

EV_INS, EV_CYC = "instructions", "cycles"
EV_REF, EV_MISS = "longest_lat_cache.reference", "longest_lat_cache.miss"
EV_L1 = "L1-dcache-loads"
EVENTS = [EV_INS, EV_CYC, EV_REF, EV_MISS, EV_L1]
# grader-only [R3]: certify.instance_fp fingerprints every arm with
# LLC-loads/instruction; logged so this arm is guarded like the others
PERF_EVENTS = EVENTS + ["LLC-loads"]

KEEPER, DONOR, RECEIVER, STREAMING, UNKNOWN = \
    "Keeper", "Donor", "Receiver", "Streaming", "Unknown"


def log(m): print(time.strftime("[%H:%M:%S]"), m, flush=True)


def write_line(grp, res, body):
    with open(os.path.join(grp, "schemata"), "w") as f: f.write(f"{res}:{body}\n")


def domain_body(grp, res, val):
    with open(os.path.join(grp, "schemata")) as f:
        for line in f:
            line = line.replace(" ", "").strip()
            if line.startswith(res + ":"):
                parts = line[len(res)+1:].split(";")
                return ";".join(f"{p.split('=')[0]}={val}" if p.split('=')[0] == DOMAIN else p
                                for p in parts)
    raise RuntimeError(f"{res} not in {grp}")


class Tenant:
    def __init__(self, c, out):
        self.name = c["name"]; self.grp = c.get("grp"); self.cores = c.get("cores")
        self.perf_json = os.path.join(out, f"dc_{self.name}.json")
        self.proc = None; self.perf_t0 = None
        self.seen_events = set()        # every event name perf actually emitted
        # latest clean measurement (one round)
        self.ipc = None; self.prev_ipc = None
        self.llc_ref_rate = 0.0; self.miss_rate = 0.0; self.mem_per_ins = 0.0
        self.state = KEEPER
        self.ways = 0; self.entitle = 0
        self.baseline_ipc = None; self.baseline_mpi = None
        self.rebase_at = None          # monotonic time to (re)record the baseline
        self.grew = False              # +1 way awaiting judgment
        self.ref_ipc = None            # IPC at the pre-grow size (judgment reference)
        self.grow_steps = 0            # extra-way steps tried in this phase
        self.best_norm = 1.0           # max IPC/baseline seen in this phase (Q1-B)
        self.explored = False          # phase already mapped (perf-table stand-in)
        self.floor_hold = False        # restored by the guarantee: no shrink < entitle
        self.donor_kind = None         # "D1" | "D2"

    # ── perf ────────────────────────────────────────────────────────────
    def start_perf(self):
        self.perf_t0 = time.monotonic()   # <= perf's own t0, so interval-start
        self.proc = subprocess.Popen(      # estimates are conservative (early)
            ["perf", "stat", "-j", "-o", self.perf_json, "-e", ",".join(PERF_EVENTS),
             "-I", str(PERF_INT_MS), "-C", self.cores, "--", "sleep", "100000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_perf(self):
        p = self.proc
        if p is None or p.poll() is not None: return
        p.send_signal(signal.SIGINT)
        for _ in range(10):
            if p.poll() is not None: return
            time.sleep(0.5)
        p.kill(); p.wait()

    def measure(self, since):
        """Aggregate over perf intervals that STARTED at/after `since`
        (monotonic). None if fewer than JUDGE_MIN_INTERVALS complete ones."""
        try: lines = open(self.perf_json).readlines()[-400:]
        except OSError: return None
        iv = {}
        for line in lines:
            line = line.strip().rstrip(",")
            if not line.startswith("{"): continue
            try: o = json.loads(line)
            except ValueError: continue
            try: v = float(o.get("counter-value")); i = float(o.get("interval"))
            except (TypeError, ValueError): continue
            e = (o.get("event") or "").split(":")[0]
            self.seen_events.add(e)
            if e in EVENTS: iv.setdefault(i, {})[e] = v
        keep = sorted(i for i, d in iv.items()
                      if len(d) == len(EVENTS)
                      and self.perf_t0 + i - PERF_INT_S >= since)
        if len(keep) < JUDGE_MIN_INTERVALS: return None
        s = {e: sum(iv[i][e] for i in keep) for e in EVENTS}
        if s[EV_CYC] <= 0 or s[EV_INS] <= 0: return None
        return dict(ipc=s[EV_INS] / s[EV_CYC],
                    miss_rate=(s[EV_MISS] / s[EV_REF]) if s[EV_REF] > 0 else 0.0,
                    llc_ref_rate=s[EV_REF] / (len(keep) * PERF_INT_S),
                    mem_per_ins=s[EV_L1] / s[EV_INS],
                    n=len(keep), win=[round(self.perf_t0 + keep[0] - PERF_INT_S, 2),
                                      round(self.perf_t0 + keep[-1], 2)])

    def phase_changed(self):
        if not self.baseline_mpi: return False
        return abs(self.mem_per_ins - self.baseline_mpi) / self.baseline_mpi > PHASE_THR


class SimTenant(Tenant):
    """--dry-run only: a SYNTHETIC tenant whose IPC is a function of its
    ways. Exists to exercise the decision logic without root; its numbers
    mean nothing about any real workload."""
    SHAPES = {  # name: (ipc at 3 ways, relative gain per extra way, cap ways, refs/s, miss)
        "canneal":     (1.00, 0.02, 11, 2.5e7, 0.27),
        "npb_cg_d80":  (0.60, 0.08, 13, 6.9e8, 0.06),
        "npb_mg_d300": (2.47, 0.00, 15, 5.3e7, 0.98),
        "llama":       (2.30, 0.00, 15, 4.0e7, 0.92),
        "npb_ep_e":    (2.50, 0.00, 15, 1.0e5, 0.10),
    }

    def __init__(self, name, ctl):
        super().__init__({"name": name}, "/tmp"); self.ctl = ctl

    def start_perf(self): pass
    def stop_perf(self): pass

    def measure(self, since):
        base, g, cap, refs, miss = self.SHAPES[self.name]
        w = self.ways
        if w >= 3:
            ipc = base * (1 + g * (min(w, cap) - 3))
        else:                                    # below entitlement: steeper loss
            ipc = base * max(0.3, 1 - 4 * g * (3 - w))
        ipc *= 1 + random.gauss(0, 0.005)
        m = miss * (3.0 / max(w, 1)) ** 0.5 if g > 0 else miss
        return dict(ipc=ipc, miss_rate=m, llc_ref_rate=refs, mem_per_ins=0.3,
                    n=JUDGE_MIN_INTERVALS, win=[round(since, 2), round(self.ctl.now(), 2)])


class DcatStyle:
    def __init__(self, cfg_path, out, pool_ways=0, dry=False):
        os.makedirs(out, exist_ok=True)
        self.dry = dry; self._fake_t = 0.0
        if dry:
            self.tenants = [SimTenant(n, self) for n in sorted(SimTenant.SHAPES)]
            self.nbits = 15; self.full = (1 << 15) - 1
        else:
            cfg = json.load(open(cfg_path))
            # [F6] deterministic NAME order -- never the cfg's bash-hash order
            self.tenants = sorted((Tenant(c, out) for c in cfg["tenants"]),
                                  key=lambda t: t.name)
            self.full = int(open(os.path.join(RESCTRL, "info/L3/cbm_mask")).read(), 16)
            self.nbits = self.full.bit_length()
        self.decisions = open(os.path.join(out, "decisions.jsonl"), "w")
        n = len(self.tenants)
        share = max(MIN_WAYS, (self.nbits - pool_ways) // n)   # [F6] equal share
        for t in self.tenants:
            t.entitle = t.ways = share
        self.pool_ways_reserved = pool_ways
        self.rr = 0                                             # [F7] rotation
        self.stop = False; self.t0 = self.now()
        if not dry:
            signal.signal(signal.SIGTERM, self._sig)
            signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_): self.stop = True
    def now(self): return self._fake_t if self.dry else time.monotonic()
    def sleep(self, s):
        if self.dry: self._fake_t += s
        else: time.sleep(s)

    def record(self, kind, **kw):
        kw.update(kind=kind, t=round(self.now() - self.t0, 2))
        self.decisions.write(json.dumps(kw) + "\n"); self.decisions.flush()

    def pool(self):
        return self.nbits - sum(t.ways for t in self.tenants)

    def masks(self):
        out, bit = {}, 0
        for t in self.tenants:                 # [P6] fixed name order, pool on top
            out[t.name] = ((1 << t.ways) - 1) << bit; bit += t.ways
        return out

    def apply_all(self, kind="apply"):
        # "apply" = an in-loop actuation (what run_certify/grade_shootout
        # count); the one-off t=0 write is logged as "entitle" [R3]
        m = self.masks()
        if not self.dry:
            for t in self.tenants:
                write_line(t.grp, "L3", domain_body(t.grp, "L3", f"{m[t.name]:x}"))
        self.record(kind, masks={k: f"{v:x}" for k, v in m.items()},
                    ways={t.name: t.ways for t in self.tenants}, pool=self.pool())

    def restore(self):
        if self.dry: return
        for t in self.tenants:
            try: write_line(t.grp, "L3", domain_body(t.grp, "L3", f"{self.full:x}"))
            except Exception: pass

    def restore_to_entitlement(self, t):
        """Reclaim-priority restore: pool first, then above-entitlement
        holders, largest excess first [F4]. Returns the shortfall.
        A tenant ABOVE entitlement (phase Reclaim) goes back to its baseline
        size and its excess returns to the pool."""
        if t.ways >= t.entitle:
            if t.ways > t.entitle:
                t.ways = t.entitle; self._touched.add(t.name)
            return 0
        need = t.entitle - t.ways
        take = min(need, max(0, self.pool())); t.ways += take; need -= take
        for d in sorted((x for x in self.tenants if x is not t),
                        key=lambda x: x.ways - x.entitle, reverse=True):
            while need > 0 and d.ways > max(d.entitle, MIN_WAYS):
                d.ways -= 1; t.ways += 1; need -= 1
                if d.grew: d.grew = False      # its judgment is now contaminated
                self._touched.add(d.name)
        self._touched.add(t.name)
        return need

    def new_phase(self, t):
        t.state = KEEPER; t.grew = False; t.grow_steps = 0; t.best_norm = 1.0
        t.explored = False; t.floor_hold = False; t.donor_kind = None
        t.baseline_ipc = None; t.baseline_mpi = None
        t.rebase_at = self.now() + BASELINE_SETTLE_S

    # ── one dCat round, on clean post-write measurements ─────────────────
    def step(self, meas):
        before = {t.name: t.ways for t in self.tenants}
        now = self.now()
        # tenants resized this round for any reason other than a grow: they
        # get no +1 way until a clean measurement at the new size exists
        self._touched = set()
        live = []
        for t in self.tenants:
            m = meas.get(t.name)
            if m is None: continue
            t.prev_ipc, t.ipc = t.ipc, m["ipc"]
            t.miss_rate, t.llc_ref_rate, t.mem_per_ins = \
                m["miss_rate"], m["llc_ref_rate"], m["mem_per_ins"]
            # (re)baseline after the settle, and only AT entitlement [F4]
            if t.rebase_at is not None:
                if now >= t.rebase_at and t.ways >= t.entitle:
                    bm = meas.get(("base", t.name)) or m
                    t.baseline_ipc = bm["ipc"]; t.baseline_mpi = bm["mem_per_ins"]
                    t.rebase_at = None
                    self.record("baseline", tenant=t.name, ipc=round(t.baseline_ipc, 4),
                                mpi=round(t.baseline_mpi, 6), ways=t.ways, win=bm["win"])
                continue
            live.append(t)

        # 1) Reclaim on phase change -- highest priority
        for t in live:
            if t.phase_changed():
                short = self.restore_to_entitlement(t)
                self.new_phase(t)
                self.record("reclaim", tenant=t.name, ways=t.ways, shortfall=short)
        live = [t for t in live if t.rebase_at is None]

        # 2) guarantee [F3]: below entitlement AND > thr under baseline
        for t in live:
            if t.ways < t.entitle and t.ipc < (1 - IPC_IMP_THR) * t.baseline_ipc:
                old = (t.state, t.ways)
                short = self.restore_to_entitlement(t)
                t.state = RECEIVER; t.floor_hold = True; t.grew = False
                self.record("guarantee-restore", tenant=t.name, from_state=old[0],
                            from_ways=old[1], ways=t.ways, shortfall=short,
                            ipc=round(t.ipc, 4), baseline=round(t.baseline_ipc, 4))

        judged = set()
        # 3) judge last round's +1 way, on clean pre/post data [F1]
        for t in live:
            if not t.grew: continue
            t.grew = False; judged.add(t.name)
            gain = (t.ipc / t.ref_ipc - 1) if t.ref_ipc else 0.0
            t.best_norm = max(t.best_norm, t.ipc / t.baseline_ipc)
            verdict = None
            if gain >= IPC_IMP_THR:
                t.state = RECEIVER; verdict = "receiver"
            elif t.state == RECEIVER:
                t.state = KEEPER; t.explored = True; verdict = "receiver-stop-keep"  # [F5]
            elif t.ways >= STREAM_MULT * t.entitle and t.best_norm < 1 + IPC_IMP_THR:
                t.state = STREAMING; t.ways = MIN_WAYS; t.explored = True
                self._touched.add(t.name); verdict = "streaming-pin-3x"
            else:
                verdict = "unknown-keeps-probing"      # Fig 7b: no give-back
            if t.state == RECEIVER and t.miss_rate < MISS_RATE_THR:
                t.state = KEEPER; t.explored = True; verdict += "+miss-stop"      # [F5]
            self.record("judge", tenant=t.name, ways=t.ways, pre_ipc=round(t.ref_ipc or 0, 4),
                        post_ipc=round(t.ipc, 4), gain=round(gain, 4),
                        best_norm=round(t.best_norm, 4), miss=round(t.miss_rate, 4),
                        verdict=verdict, win=meas[t.name]["win"])

        # 4) categorize. Unknown/Receiver (judged this round or not) take only
        #    Fig 6's exit edges [R2]: idle -> D1 Donor; trivial misses -> stop
        #    growing as Keeper (Fig 7a), whence the rules below may make it a
        #    D2 Donor next round.
        for t in live:
            if t.state == STREAMING: continue
            if t.state in (UNKNOWN, RECEIVER):
                old = t.state
                if t.llc_ref_rate <= LLC_REF_LOW:
                    t.state = DONOR; t.donor_kind = "D1"
                elif t.miss_rate < MISS_RATE_THR:
                    t.state = KEEPER; t.explored = True
                else:
                    continue
                self.record("exit", tenant=t.name, from_state=old, state=t.state,
                            ways=t.ways, refs=round(t.llc_ref_rate),
                            miss=round(t.miss_rate, 4))
                continue
            if t.name in judged: continue          # stopped this very round
            if t.llc_ref_rate <= LLC_REF_LOW:
                t.state = DONOR; t.donor_kind = "D1"
            elif t.miss_rate > MISS_RATE_THR:
                # an explored (already mapped) tenant does not re-open the
                # probe; non-trivial misses end a donation as Keeper [R1]
                t.state = KEEPER if t.explored else UNKNOWN
            elif t.miss_rate < KEEPER_BAND * MISS_RATE_THR:
                t.state = DONOR; t.donor_kind = "D2"
            else:
                t.state = KEEPER

        # 5) donors release ways: D1 snaps to the minimum [F9], D2 one way/round
        for t in live:
            if t.state != DONOR: continue
            floor = t.entitle if t.floor_hold else MIN_WAYS
            target = floor if t.donor_kind == "D1" else max(floor, t.ways - 1)
            if t.ways > target:
                self.record("donor-shrink", tenant=t.name, donor=t.donor_kind,
                            from_ways=t.ways, ways=target)
                t.ways = target; self._touched.add(t.name)

        # 6) growth: Unknown first, then Receiver; rotating order [F7]
        def rotated(xs):
            xs = sorted(xs, key=lambda t: t.name)
            k = self.rr % len(xs) if xs else 0
            return xs[k:] + xs[:k]
        unk = [t for t in live if t.state == UNKNOWN and t.name not in self._touched]
        rec = [t for t in live if t.state == RECEIVER and t.name not in self._touched]
        if self.pool() <= 0:                       # [F2] pool-dry -> Streaming (Q1-B)
            for t in unk:
                if t.grow_steps >= 1 and t.best_norm < 1 + IPC_IMP_THR:
                    self.record("streaming-pin-pooldry", tenant=t.name, from_ways=t.ways,
                                best_norm=round(t.best_norm, 4), steps=t.grow_steps)
                    t.state = STREAMING; t.ways = MIN_WAYS; t.explored = True
                    self._touched.add(t.name)
            unk = [t for t in unk if t.state == UNKNOWN]
        for t in rotated(unk) + rotated(rec):
            if self.pool() <= 0: break
            t.ref_ipc = t.ipc; t.ways += 1; t.grew = True; t.grow_steps += 1
            self.record("grow", tenant=t.name, state=t.state, ways=t.ways,
                        ipc_before=round(t.ipc, 4))
        self.rr += 1

        self.record("scan", state={t.name: t.state for t in self.tenants},
                    ways={t.name: t.ways for t in self.tenants},
                    ipc={t.name: (round(t.ipc, 4) if t.ipc else None) for t in self.tenants},
                    miss={t.name: round(t.miss_rate, 4) for t in self.tenants},
                    pool=self.pool())
        return before != {t.name: t.ways for t in self.tenants}

    def gather(self, since):
        meas = {}
        for t in self.tenants:
            meas[t.name] = t.measure(since)
            if t.rebase_at is not None and self.now() >= t.rebase_at:
                meas[("base", t.name)] = t.measure(t.rebase_at - BASELINE_MEASURE_S)
        return meas

    def run(self, duration=None):
        for t in self.tenants: t.start_perf()
        self.apply_all("entitle")                 # unmanaged -> entitlement [P9]
        for t in self.tenants: self.new_phase(t)
        log(f"dcat_style up: {len(self.tenants)} tenants {[t.name for t in self.tenants]}, "
            f"{self.nbits} ways, entitlement={[t.entitle for t in self.tenants]}, "
            f"pool={self.pool()} (reserved {self.pool_ways_reserved})")
        self.record("start", order=[t.name for t in self.tenants],
                    entitle={t.name: t.entitle for t in self.tenants}, pool=self.pool(),
                    # the two paper-swept thresholds, recorded per run so a
                    # result can never be read without its configuration
                    ipc_imp_thr=IPC_IMP_THR, miss_rate_thr=MISS_RATE_THR,
                    stream_mult=STREAM_MULT)
        since = self.now() + DISCARD_S
        starved = 0
        try:
            while not self.stop and (duration is None or self.now() - self.t0 < duration):
                self.sleep(max(0.0, since - self.now()) + JUDGE_MIN_INTERVALS * PERF_INT_S + 0.3)
                meas = self.gather(since)
                for _ in range(6):               # perf lag: wait for the intervals
                    if all(meas[t.name] is not None for t in self.tenants): break
                    self.sleep(0.5); meas = self.gather(since)
                missing = [t for t in self.tenants if meas[t.name] is None]
                if missing:
                    # FAIL LOUD: an unmeasured tenant would make dCat look inert
                    # (e.g. perf naming an event differently) -- never publish that
                    starved += 1
                    for t in missing:
                        self.record("perf-incomplete", tenant=t.name,
                                    seen=sorted(t.seen_events), want=EVENTS)
                    if starved >= 3:
                        log(f"FATAL: no complete perf intervals for "
                            f"{[t.name for t in missing]} in 3 rounds; seen events "
                            f"{sorted(missing[0].seen_events)}")
                        raise SystemExit(2)
                else:
                    starved = 0
                changed = self.step(meas)
                if changed:
                    self.apply_all(); since = self.now() + DISCARD_S
                else:
                    since = self.now()
        finally:
            log("dcat_style down: restoring full masks")
            self.restore()
            for t in self.tenants: t.stop_perf()
            self.decisions.close()


def main():
    # declared up-front: these are rebound from the CLI below, and Python
    # requires the declaration to precede any use in this scope (the argparse
    # defaults read them).
    global IPC_IMP_THR, MISS_RATE_THR
    ap = argparse.ArgumentParser()
    ap.add_argument("--config"); ap.add_argument("--out", required=True)
    ap.add_argument("--pool-ways", type=int, default=0,
                    help="reserve N ways unallocated at start (SENSITIVITY arm only)")
    # PAPER-SWEPT SENSITIVITY (CONTROLLER_COMPARISON.md 9g). dCat's Fig. 9
    # sweeps ipc_imp_thr over 3/5/10/20/30/40% and Fig. 8 sweeps
    # llc_miss_rate_thr over 1/3/5/10/20/30%; the paper picks 5% and 3% "in our
    # following experiments" and states outright that "Cloud providers can set
    # this threshold to their desired values". Both are therefore DEPLOYMENT
    # CHOICES, not constants -- and on D3 the default 5% is load-bearing:
    # canneal's marginal gain is 2.79-2.82% per way, so dCat stops after ONE
    # way and leaves 3 ways unused. Exposing them lets us report dCat at its
    # best PUBLISHED configuration instead of only its default, which is the
    # difference between a sensitivity result and a strawman.
    ap.add_argument("--ipc-imp-thr", type=float, default=IPC_IMP_THR,
                    help="dCat ipc_imp_thr (paper default 0.05; Fig.9 sweeps "
                         "0.03-0.40). Below this marginal IPC gain a Receiver "
                         "stops growing.")
    ap.add_argument("--miss-rate-thr", type=float, default=MISS_RATE_THR,
                    help="dCat llc_miss_rate_thr (paper default 0.03; Fig.8 "
                         "sweeps 0.01-0.30).")
    ap.add_argument("--dry-run", action="store_true",
                    help="synthetic tenants + fake clock; exercises the logic, no root")
    ap.add_argument("--duration", type=float, default=None)
    a = ap.parse_args()
    # Rebind the module-level thresholds from the CLI. They are read in several
    # places (judge, guarantee, streaming-pin), so rebinding the globals keeps
    # ONE source of truth rather than threading two floats through the FSM --
    # and the values land in the `start` record below, so every run states the
    # configuration it actually used.
    IPC_IMP_THR = a.ipc_imp_thr
    MISS_RATE_THR = a.miss_rate_thr
    if a.dry_run:
        random.seed(1)
        DcatStyle(None, a.out, a.pool_ways, dry=True).run(a.duration or 400)
        return
    if not a.config: sys.exit("--config required")
    if os.geteuid() != 0: sys.exit("must run as root")
    DcatStyle(a.config, a.out, a.pool_ways).run(a.duration)


if __name__ == "__main__":
    main()
