#!/usr/bin/env python3
"""dcat_style -- our rendition of dCat (Xu et al., EuroSys'18) for exp16.

WHY THIS ARM: dCat is coeffd's closest DECISION-LOGIC relative and its
floor-ancestor: baseline-guarantee first, then grow-if-benefit, donor
harvesting, and an explicit Streaming (no-reuse) class pinned to minimum
cache -- the ancestor of our squeeze-free-donor taxonomy. Running it
turns the REVIEW.md §6 argument (entitlement floor != unmanaged floor)
into a measurement.

FAITHFUL CORE (their §3, Fig 4/6/7):
 - Baseline = performance (IPC) measured at the tenant's ENTITLEMENT
   partition ("the cache partition paid by tenants"); re-measured after
   each phase change. Guarantee: a tenant is never held below its
   entitlement ways unless the FSM itself classified it Donor/Streaming
   (their semantics: donors "do not suffer" by definition of the class).
 - Per-tenant FSM: Keeper / Donor / Receiver / Streaming / Unknown /
   Reclaim (phase change -> restore entitlement, highest priority).
 - Phase detection: memory-accesses-per-instruction (their l1_ref/
   ret_ins), 10% change threshold.
 - Growth: Unknown/Receiver gain 1 way per round from the free pool
   (Unknown priority over Receiver, their §3.5); kept only if IPC
   improves >= 5% (ipc_imp_thr), else Keeper; if ways reach 3x
   entitlement with no improvement -> Streaming -> drop to MIN_WAYS.
 - Donors (low LLC utilization) shrink 1 way per round toward MIN_WAYS;
   freed ways go to the pool (unallocated pool ways belong to NOBODY --
   their resource-pool semantics, capacity deliberately idle).
 - LLC ways ONLY. No MBA anywhere (faithful -- dCat has no bandwidth
   lever; that absence is hypothesis H-dcat2).

DEVIATIONS (documented, all either forced or favorable to dCat):
 1. Per-TENANT resctrl groups. NOTE (audit 2026-07-18): this is NOT a
    deviation -- dCat is ITSELF per-workload-COS ("cores running one
    workload only have one COS", their S3.5), so per-tenant groups are
    FAITHFUL. (The per-core scheme is PaLLoC, not dCat.) Kept in the list
    only as an implementation note.
 2. Entitlement = equal split of (15 - FREE_POOL_WAYS) ways; the
    remaining FREE_POOL_WAYS stay UNALLOCATED as dCat's redistribution
    pool (no "paid partition" exists for bare processes; equal share +
    reserved pool matches their eval baselines, which leave headroom, e.g.
    6 VMs x 3 of 20 = pool 2). AUDIT 2026-07-18: the earlier all-15 split
    left pool=0 -> growth was fully donor-gated -> frozen at ~static-equal;
    FREE_POOL_WAYS fixes it. Sharpens H-dcat1 (smaller floor).
 3. L1-dcache-loads approximated by LLC-loads for the phase ratio when
    the PMU slot is unavailable (event list tries L1 first).
 4. Slices are disjoint contiguous (CAT constraint), recomputed in
    tenant order on every change.
 5. Their max-Sum(norm_IPC) performance-table allocator is simplified
    to: grow every Unknown/Receiver one way per round, pool permitting,
    Unknown first then by miss rate -- this is FAIR-MODE-flavored, not their
    max-perf headline default (AUDIT: a favorable-to-us-LIMITED rendition;
    real dCat concentrates the pool on the highest-marginal-IPC tenant and
    would recover MORE). ALSO OMITTED (audit, was hidden here): the perf-
    table phase-RECURRENCE fast-path (Fig 7a-t4/12, jump straight to
    preferred ways) -- our `explored` flag only suppresses re-probe WITHIN a
    phase, so recurring phases re-pay +1/round discovery. Both omissions
    make our dCat recover somewhat LESS than a faithful dCat; documented so
    the comparison is not silently strawmanned. Remaining lower-sev items
    (D1 gradual-vs-snap shrink, loads-only ref/miss, pool-dry hysteresis,
    IPC-gain averaging window) tracked in REVIEW.md audit notes.

PRE-REGISTERED HYPOTHESES (frozen before the first run):
 H-dcat1 entitlement floor != unmanaged floor: bc/pr's equal slice
         (2-3 ways) is far under their unmanaged shared-cache footprint
         (bc CMT ~16MB ~ 4 ways) -> both end below unmanaged while
         dCat's OWN guarantee (vs baseline-at-entitlement) is met.
 H-dcat2 no bandwidth lever: stream keeps 100% MBA; even a perfect
         Streaming classification (1 way) leaves the bandwidth channel
         of harm untouched -> masstree recovery partial at best.
 H-dcat3 IPC-gated growth is tail-blind: masstree's IPC barely moves
         with ways while its p99 does (the same blindness our v5.2
         at-capacity gate showed vs the oracle) -> masstree never earns
         Receiver ways; the LC is under-served by the growth test.

Usage: dcat_style.py --config cfg.json --out outdir
Config: {"tenants":[{"name","grp","cores"}...]}  (same shape as copart)
"""
import argparse, json, os, signal, subprocess, sys, time

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"
CYCLE_SECS = 2.0
PERF_INT_MS = 2000

MIN_WAYS = 1
IPC_IMP_THR = 0.05        # their ipc_imp_thr default 5% (§5.1)
LLC_REF_LOW = 1.5e6       # refs/sec below this = underutilized -> Donor.
                          # PAPER GIVES NO llc_ref_thr VALUE (audit U4);
                          # anchored to CoPart's calibrated LLC-access
                          # SUPPLY threshold (same 1.5e6). Flag in the
                          # hyperparameter audit; per-HW calibration TODO.
MISS_RATE_THR = 0.03      # their llc_miss_rate_thr 3% (§5.1, Fig 8)
PHASE_THR = 0.10          # 10% change in mem-accesses/instr = phase change
STREAM_MULT = 3           # ways >= 3x entitlement w/o gain -> Streaming
BASELINE_SETTLE = 3       # cycles at entitlement before baseline is trusted
KEEPER_BAND = 0.5         # a fitting tenant with miss_rate in
                          # [KEEPER_BAND*MISS_RATE_THR, MISS_RATE_THR] rests as
                          # Keeper; below it is a D2 donor (harvestable). The
                          # band is the stable "just fits" resting zone that
                          # stops D2 shrink from oscillating (paper E2/E3).
FREE_POOL_WAYS = 3        # ways reserved UNALLOCATED at start so dCat has a
                          # pool to redistribute -- the paper's own evals do
                          # this (6 VMs x 3 ways of 20 = 18, pool=2). AUDIT FIX
                          # (2026-07-18): equal-split of ALL 15 ways summed to
                          # 15 -> pool=0 -> growth fully donor-gated -> dCat was
                          # frozen at ~static-equal. Reserving a pool un-freezes
                          # the allocator so the FSM's benefit machinery runs.

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
        self.name = c["name"]; self.grp = c["grp"]; self.cores = c["cores"]
        self.perf_json = os.path.join(out, f"dc_{self.name}.json")
        self.proc = None
        self.ipc = 0.0; self.prev_ipc = 0.0
        self.llc_ref_rate = 0.0; self.miss_rate = 0.0; self.mem_per_ins = 0.0
        self.state = KEEPER
        self.ways = 0; self.entitle = 0
        self.baseline_ipc = None       # IPC at entitlement (their Get Baseline)
        self.baseline_mpi = None       # mem/instr at baseline (phase reference)
        self.settle = 0                # cycles left before baseline is recorded
        self.grew = False              # a growth step awaiting its IPC check
        self.unknown_blocked = 0       # rounds an Unknown couldn't grow (pool dry)
        self.explored = False          # this phase already probed (their perf
                                       # table: no re-discovery within a phase)

    def start_perf(self):
        self.proc = subprocess.Popen(
            ["perf", "stat", "-j", "-o", self.perf_json,
             "-e", "instructions,cycles,LLC-loads,LLC-load-misses,L1-dcache-loads",
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

    def sample(self):
        ev = {"instructions": [], "cycles": [], "LLC-loads": [],
              "LLC-load-misses": [], "L1-dcache-loads": []}
        try: lines = open(self.perf_json).readlines()
        except OSError: return
        for line in lines[-80:]:
            line = line.strip().rstrip(",")
            if not line.startswith("{"): continue
            try: o = json.loads(line)
            except ValueError: continue
            e = o.get("event") or ""
            try: v = float(o.get("counter-value"))
            except (TypeError, ValueError): continue
            if "L1-dcache-loads" in e: ev["L1-dcache-loads"].append(v)
            elif "LLC-load-misses" in e: ev["LLC-load-misses"].append(v)
            elif "LLC-loads" in e: ev["LLC-loads"].append(v)
            elif "instructions" in e: ev["instructions"].append(v)
            elif "cycles" in e: ev["cycles"].append(v)
        n = 3; iv = PERF_INT_MS / 1000.0
        if ev["instructions"] and ev["cycles"]:
            ins = sum(ev["instructions"][-n:]); cyc = sum(ev["cycles"][-n:])
            self.prev_ipc = self.ipc
            self.ipc = ins / cyc if cyc > 0 else 0.0
            refs = ev["L1-dcache-loads"] or ev["LLC-loads"]   # deviation 3
            if refs and ins > 0:
                self.mem_per_ins = sum(refs[-n:]) / ins
        if ev["LLC-loads"]:
            k = ev["LLC-loads"][-n:]
            self.llc_ref_rate = (sum(k) / max(1, len(k))) / iv
            if ev["LLC-load-misses"]:
                m = ev["LLC-load-misses"][-n:]
                acc = sum(k)
                self.miss_rate = (sum(m) / acc) if acc > 0 else 0.0

    def phase_changed(self):
        if self.baseline_mpi is None or self.baseline_mpi <= 0: return False
        return abs(self.mem_per_ins - self.baseline_mpi) / self.baseline_mpi > PHASE_THR


class DcatStyle:
    def __init__(self, cfg_path, out):
        os.makedirs(out, exist_ok=True)
        cfg = json.load(open(cfg_path))
        self.tenants = [Tenant(c, out) for c in cfg["tenants"]]
        self.decisions = open(os.path.join(out, "decisions.jsonl"), "w")
        self.full = int(open(os.path.join(RESCTRL, "info/L3/cbm_mask")).read(), 16)
        self.nbits = self.full.bit_length()
        # entitlement = equal split (deviation 2); order = config order
        # entitlement = equal split of (ways - FREE_POOL_WAYS); the reserved
        # pool stays unallocated at start so dCat has room to redistribute
        # (see FREE_POOL_WAYS note). floor never below MIN_WAYS.
        alloc_ways = max(len(self.tenants) * MIN_WAYS,
                         self.nbits - FREE_POOL_WAYS)
        base, extra = divmod(alloc_ways, len(self.tenants))
        for i, t in enumerate(self.tenants):
            t.entitle = max(MIN_WAYS, base + (1 if i < extra else 0))
            t.ways = t.entitle
            t.settle = BASELINE_SETTLE
        self.stop = False; self.t0 = time.monotonic()
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_): self.stop = True

    def record(self, kind, **kw):
        kw.update(kind=kind, t=round(time.monotonic() - self.t0, 1))
        self.decisions.write(json.dumps(kw) + "\n"); self.decisions.flush()

    def pool(self):
        return self.nbits - sum(t.ways for t in self.tenants)

    def apply_all(self):
        # disjoint contiguous slices in tenant order; pool ways unallocated
        bit = 0
        for t in self.tenants:
            mask = ((1 << t.ways) - 1) << bit
            bit += t.ways
            write_line(t.grp, "L3", domain_body(t.grp, "L3", f"{mask:x}"))

    def restore(self):
        # per-tenant isolation, matching copart/spidersense: one failed write must
        # not abort the loop and skip stop_perf()/decisions.close() for the rest.
        for t in self.tenants:
            try:
                write_line(t.grp, "L3", domain_body(t.grp, "L3", f"{self.full:x}"))
            except Exception:
                pass

    # ── the dCat round: baseline -> phase -> categorize -> allocate ──────
    def step(self):
        acts = []
        # 1) baselines: recorded after settling at entitlement
        for t in self.tenants:
            if t.settle > 0:
                t.settle -= 1
                if t.settle == 0 and t.ipc > 0:
                    t.baseline_ipc = t.ipc
                    t.baseline_mpi = t.mem_per_ins
                    self.record("baseline", tenant=t.name,
                                ipc=round(t.ipc, 4), mpi=round(t.mem_per_ins, 6))
        # 2) Reclaim on phase change (their highest priority): back to
        #    entitlement, re-measure baseline
        for t in self.tenants:
            if t.settle == 0 and t.phase_changed():
                need = t.entitle - t.ways
                if need > 0:            # harvest from above-entitlement holders
                    for d in sorted(self.tenants, key=lambda x: x.ways - x.entitle,
                                    reverse=True):
                        while need > 0 and d.ways > max(d.entitle, MIN_WAYS):
                            d.ways -= 1; need -= 1
                    t.ways = t.entitle - max(0, need)
                elif need < 0:
                    t.ways = t.entitle   # excess returns to the pool
                t.state = KEEPER; t.settle = BASELINE_SETTLE; t.grew = False
                t.explored = False; t.unknown_blocked = 0
                acts.append(("reclaim", t.name))
                self.record("revert", tenant=t.name, note="phase-reclaim",
                            ways=t.ways)
        # 3) judge pending growths + categorize
        for t in self.tenants:
            if t.settle > 0 or t.baseline_ipc is None: continue
            if t.grew:                   # did last round's +1 way pay off?
                t.grew = False
                gain = (t.ipc - t.prev_ipc) / t.prev_ipc if t.prev_ipc > 0 else 0.0
                if gain >= IPC_IMP_THR:
                    t.state = RECEIVER
                elif t.state == RECEIVER:
                    t.ways -= 1          # a Receiver that stopped improving:
                    t.state = KEEPER     # give the way back, settle (Fig 7a t2)
                    t.explored = True    # perf-table memory: phase is mapped
                    acts.append(("give-back", t.name))
                    self.record("revert", tenant=t.name, note="no-ipc-gain",
                                ways=t.ways)
                elif t.ways >= STREAM_MULT * t.entitle:
                    t.state = STREAMING   # grew far, never improved: no reuse
                    t.ways = MIN_WAYS
                    t.explored = True
                    acts.append(("streaming-pin", t.name))
                    self.record("revert", tenant=t.name, note="streaming-pin",
                                ways=t.ways)
                # else: UNKNOWN keeps the way and keeps probing (their Fig 7b:
                # MLOAD climbs 3->9 ways without improvement BEFORE the pin;
                # no per-step give-back on the Unknown path)
                continue
            if t.state == STREAMING: continue
            if t.llc_ref_rate <= LLC_REF_LOW:
                t.state = DONOR                       # D1 (paper E1): idle/low-LLC
            elif t.miss_rate > MISS_RATE_THR:
                # explored phases are not re-probed (their performance table);
                # a fresh probe needs a phase change, which resets the flag
                if t.state not in (RECEIVER,) and not t.explored:
                    t.state = UNKNOWN                 # E4: contended -> wants cache
            elif t.miss_rate < KEEPER_BAND * MISS_RATE_THR:
                # AUDIT FIX (2026-07-18): D2 donor path (paper E2/§3.4) was
                # MISSING -- a high-ref, comfortably-fitting tenant fell to the
                # old `else: KEEPER` and was never harvested, starving the pool.
                # dCat peels such tenants one way/round until the working set
                # stops fitting (miss rises into the Keeper band), returning
                # ways to the pool for real victims.
                t.state = DONOR
            else:
                t.state = KEEPER                      # rests in the just-fits band
        # 4) donors shrink toward MIN_WAYS (freed ways -> pool)
        for t in self.tenants:
            if t.state == DONOR and t.ways > MIN_WAYS:
                t.ways -= 1
                acts.append(("donor-shrink", t.name))
                self.record("apply", step=["shrink", t.name, t.ways],
                            state=t.state)
        # 5) growth from the pool: Unknown first (their §3.5), then
        #    Receiver, by miss rate
        cands = [t for t in self.tenants
                 if t.state in (UNKNOWN, RECEIVER) and t.settle == 0]
        cands.sort(key=lambda t: (t.state != UNKNOWN, -t.miss_rate))
        for t in cands:
            if self.pool() <= 0:
                # AUDIT FIX (2026-07-18, run-5): the pool-dry->Streaming path
                # is REMOVED. With a real free pool (FREE_POOL_WAYS) all
                # Unknowns grow to 4w in lockstep (round-robin allocator,
                # DEV5), the pool hits 0, and EVERY Unknown then pinned to 1w
                # simultaneously -- a mass streaming-pin (audit U7). Streaming
                # is terminal, so 9 ways sat idle while every tenant thrashed
                # at 1w (dcat -364%, pathological, not faithful). The paper's
                # §3.4 pool-dry clause assumes its max-perf allocator has
                # CONCENTRATED cache, not our lockstep spread; without that
                # allocator the clause misfires. Faithful streaming detection
                # remains via the 3x-baseline trigger in the grew-check above
                # (Fig 13). Pool-dry now just means "hold current ways".
                continue
            t.ways += 1; t.grew = True; t.unknown_blocked = 0
            acts.append(("grow", t.name))
            self.record("apply", step=["grow", t.name, t.ways], state=t.state)
        if acts:
            self.apply_all()
        return acts

    def run(self):
        for t in self.tenants: t.start_perf()
        self.apply_all()
        log(f"dcat_style up: {len(self.tenants)} tenants, {self.nbits} ways, "
            f"entitlement={[t.entitle for t in self.tenants]}")
        try:
            time.sleep(6)                       # first perf intervals
            while not self.stop:
                for t in self.tenants: t.sample()
                self.step()
                self.record("scan",
                            state={t.name: t.state for t in self.tenants},
                            ways={t.name: t.ways for t in self.tenants},
                            ipc={t.name: round(t.ipc, 3) for t in self.tenants},
                            miss={t.name: round(t.miss_rate, 3) for t in self.tenants},
                            pool=self.pool())
                time.sleep(CYCLE_SECS)
        finally:
            log("dcat_style down: restoring full masks")
            self.restore()
            for t in self.tenants: t.stop_perf()
            self.decisions.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if os.geteuid() != 0: sys.exit("must run as root")
    DcatStyle(a.config, a.out).run()


if __name__ == "__main__":
    main()
