#!/usr/bin/env python3
"""spidersense_style -- a SpiderSense-style (Wei et al., TACO 2026) LLC
allocator, reimplemented as an honest black-box comparator.

Implements the paper's PERSISTENT-time-period strategy (their Section 3.5,
ALGORITHM 2 "Globally Optimal Average IPC", p.11).  ALGORITHM 2 as printed:

    1  BestIPC <- -inf
    2  for m <- 1 to M:                # M = "total number of configurations"
    3     CurrIPC <- 0
    4     configure(c_m)               # applied to the LIVE system
    5     sleep(interval)              # interval = 1 s (their Table 2)
    6     for n <- 1 to N:
    7        i_i <- getAvgCyclesFromWorkload(w_i)
    8        c_i <- getAvgInstructionsFromWorkload(w_i)
    9        a_i <- i_i / c_i
    10       CurrIPC <- CurrIPC + a_i
    11    if CurrIPC > BestIPC: BestConfiguration <- c_m; BestIPC <- CurrIPC
    17 return BestConfiguration

It is an EXHAUSTIVE LIVE SWEEP, not an adaptive allocator: their Section 5.2
defines the Random baseline as selecting "from all possible options", so the
configuration set is the full partition space.  Consequently the persistent
path IS an oracle for its own objective, and the interesting axes of
comparison are (a) what that objective does to individual tenants and (b)
what the search costs while it runs -- neither of which their evaluation,
which measures only the converged state, reports.

FAITHFUL TO THE PAPER
  - BLACK-BOX: per-tenant IPC only (perf instructions/cycles on the tenant's
    cores).  No latency feed, no QoS target, no declared priority.
  - OBJECTIVE: max SUM of per-tenant IPC (line 10).  Their prose says
    "average"; the pseudocode sums and never divides by N.  Harmless: N is
    fixed within a scene, so argmax(sum) == argmax(mean).
  - EXHAUSTIVE over every way-partition (all compositions of the CBM width
    into N parts, each >= 1 way), then CONVERGE AND HOLD -- "frequent changes
    to the global optimal configuration are rare during stable periods,
    making it worthwhile to spend extra time" (p.11).
  - DISJOINT contiguous blocks: "SpiderSense prevents COS overlap across
    different cores, ensuring a one-to-one correspondence between workloads
    and COS" (Section 4).  NB this differs from coeffd3's nested-prefix
    layout -- it is their layout, not ours.
  - LLC ways ONLY, no MBA.  Matches their evaluation, which explicitly
    removes the bandwidth lever from every comparator, and their Section 6
    limitation ("bandwidth-aware isolation ... beyond the scope").
  - Default interval 1.0 s = their Table 2 "Sampling Time Interval".

DOCUMENTED DEVIATIONS  (all recorded in decisions.jsonl "search_begin")
  1. TYPO FIX.  Lines 7-8 as printed assign cycles into i_i and instructions
     into c_i, so a_i = cycles/instructions = CPI, and maximizing summed CPI
     selects the WORST configuration.  We implement the evident intent, IPC.
     (Their ALGORITHM 1 has the same class of defect -- inverted comparators
     that tag every workload Intermittent, and a running mean divided by N
     rather than W_size.  We do not implement ALGORITHM 1 here; see
     --search greedy note below and CONTROLLER_COMPARISON.md.)
  2. RANDOMIZED VISIT ORDER (seeded, recorded).  Their "for m <- 1 to M"
     leaves the order unspecified.  Our tenants restart-loop (bench_lib
     `while true`), so over a 17-33 min scan the workload instances drift;
     a lexicographic scan would systematically favour configurations visited
     during a favourable phase.  Randomizing converts that bias into noise.
  3. TOP-K RUNOFF (--runoff, default 5).  After the scan the top-k
     configurations by summed IPC are RE-MEASURED and the argmax taken among
     those.  Costs ~k*interval.  This is GENEROUS to the paper: it protects
     their argmax from the measurement drift of point 2.  Without it the
     winner of a 1001-sample scan is substantially noise.
  4. INSTANCE FINGERPRINT.  Each probe records LLC-loads per kilo-instruction
     per tenant so that a config measured across a tenant restart is
     detectable afterwards rather than silently graded (see
     memory: instance-flip-invalidates-arms).
  5. SINGLE SOCKET.  TypeScheduler's cross-socket persistent/intermittent
     placement is the SpiderSense++ variant; this arm is single-socket
     SpiderSense, their headline number.

  --search greedy keeps the ORIGINAL budgeted hill-climb (shift one way from
  the lowest-IPC tenant to the highest-IPC tenant, keep if summed IPC rose).
  That donor/recipient rule is OURS, not the paper's -- it is a monotone
  rich-get-richer ratchet that drives the scene to a degenerate corner.  It
  is retained ONLY for scenes where M*interval exceeds the affordable arm
  budget, and any result from it must be labelled a budgeted variant, never
  "SpiderSense".

Config: {"tenants": [{"name","grp","cores"}...]}  (no priority, no feed)
Usage: sudo python3 spidersense_style.py --config cfg.json --out outdir
       [--search exhaustive|greedy] [--interval 1.0] [--settle 0.0]
       [--runoff 5] [--budget 0] [--seed 1]
Restores masks on SIGTERM/SIGINT.
"""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"

PERF_INT_MS = 250           # perf -I granularity; must divide the probe window
MIN_WAYS = 1                # every tenant keeps at least 1 way (their "minimal LLC")

# greedy-mode only (the budgeted variant, NOT the paper's algorithm)
CYCLE_SECS = 2.0
IMPROVE_IPC = 0.005
SETTLE_CYCLES = 2


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def write_schemata_line(grp, resource, body):
    with open(os.path.join(grp, "schemata"), "w") as f:
        f.write(f"{resource}:{body}\n")


def domain_body(grp, resource, new_value):
    with open(os.path.join(grp, "schemata")) as f:
        for line in f:
            line = line.replace(" ", "").strip()
            if line.startswith(resource + ":"):
                parts = line[len(resource) + 1:].split(";")
                out = []
                for p in parts:
                    dom = p.split("=")[0]
                    out.append(f"{dom}={new_value}" if dom == DOMAIN else p)
                return ";".join(out)
    raise RuntimeError(f"{resource} not in {grp}/schemata")


def compositions(total, nparts, minv):
    """All ways to split `total` ways among `nparts` tenants, each >= minv.
    len() == C(total-nparts*minv + nparts-1, nparts-1); for 15 ways / 5
    tenants / minv 1 that is C(14,4) = 1001 -- the paper's "all possible
    options" (their Section 5.2 Random baseline)."""
    if nparts == 1:
        if total >= minv:
            yield (total,)
        return
    for first in range(minv, total - minv * (nparts - 1) + 1):
        for rest in compositions(total - first, nparts - 1, minv):
            yield (first,) + rest


def comp_key(comp):
    return "-".join(str(x) for x in comp)


class Tenant:
    def __init__(self, cfg, out_dir):
        self.name = cfg["name"]
        self.grp = cfg["grp"]
        self.cores = cfg["cores"]
        self.perf_json = os.path.join(out_dir, f"ssperf_{self.name}.json")
        self.proc = None
        self.ipc = 0.0
        # LC tenants only: TailBench live feed (one line "ts_ns n p50 p95 p99",
        # ms, rewritten per >=2 s window). DEBUG side-channel -- never read by
        # the objective, the state file or the runoff.
        self.lat_file = cfg.get("lat_file")

    def read_lat(self):
        """Latest live-feed window, or None. age_s = how old the window is at
        probe end: a 1 s probe at low QPS usually sees the PREVIOUS window, so
        a p99 is attributable to a partition only when age_s < the probe."""
        if not self.lat_file:
            return None
        try:
            with open(self.lat_file) as f:
                parts = f.read().split()
            ts = int(parts[0])
            return {"n": int(parts[1]), "p50": float(parts[2]), "p95": float(parts[3]),
                    "p99": float(parts[4]), "age_s": round(time.time() - ts / 1e9, 2)}
        except (OSError, ValueError, IndexError):
            return None

    def start_perf(self):
        # count on the tenant's CORES (black-box: no PID needed, we own the
        # pinning; also dodges the wrapper-pid attach bug).  LLC-loads backs
        # the instance fingerprint, it is NOT an input to the objective.
        self.proc = subprocess.Popen(
            ["perf", "stat", "-j", "-o", self.perf_json,
             "-e", "instructions,cycles,LLC-loads",
             "-I", str(PERF_INT_MS), "-C", self.cores, "--", "sleep", "100000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_perf(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        p.send_signal(signal.SIGINT)
        for _ in range(10):
            if p.poll() is not None:
                return
            time.sleep(0.5)
        p.kill(); p.wait()

    def rotate_perf(self):
        """Close the search-phase counter file and open a fresh one.  The
        certify harness grades an arm from the perf json spanning the WHOLE
        arm; without this, SpiderSense would be graded on the average of 1001
        thrashing search configurations rather than on the configuration its
        objective actually selected.  The search-phase file is kept as
        <name>.search.json for the cost-of-search analysis."""
        self.stop_perf()
        try:
            os.replace(self.perf_json, self.perf_json[:-5] + ".search.json")
        except OSError:
            pass
        self.start_perf()

    def mark(self):
        """Byte offset of the perf json right now -- everything appended after
        this belongs to the window we are about to open."""
        try:
            return os.path.getsize(self.perf_json)
        except OSError:
            return 0

    def _parse_from(self, offset):
        """Aggregate complete perf intervals appended after `offset`.
        Returns (instructions, cycles, llc_loads, n_intervals)."""
        try:
            with open(self.perf_json) as f:
                f.seek(offset)
                lines = f.readlines()
        except OSError:
            return 0.0, 0.0, 0.0, 0
        per_iv = {}
        for line in lines:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue            # a torn trailing line: perf is still writing
            ev = o.get("event") or ""
            try:
                val = float(o.get("counter-value"))
            except (TypeError, ValueError):
                continue
            iv = o.get("interval")
            if iv is None:
                continue
            d = per_iv.setdefault(iv, {})
            if "instructions" in ev:
                d["ins"] = val
            elif "cycles" in ev:
                d["cyc"] = val
            elif "LLC-loads" in ev:
                d["llc"] = val
        # the FIRST interval after the mark straddles the mask change -- drop it
        keys = sorted(per_iv)[1:]
        ins = sum(per_iv[k].get("ins", 0.0) for k in keys)
        cyc = sum(per_iv[k].get("cyc", 0.0) for k in keys)
        llc = sum(per_iv[k].get("llc", 0.0) for k in keys)
        return ins, cyc, llc, len(keys)

    def read_window(self, offset):
        ins, cyc, llc, n = self._parse_from(offset)
        ipc = ins / cyc if cyc > 0 else 0.0
        # instance fingerprint: LLC-loads per kilo-instruction (3% guard)
        fp = llc / (ins / 1000.0) if ins > 0 else 0.0
        self.ipc = ipc
        return {"ipc": round(ipc, 4), "fp": round(fp, 4), "n": n}

    def sample(self):
        """greedy-mode sampler: mean of the trailing intervals."""
        try:
            size = os.path.getsize(self.perf_json)
        except OSError:
            return
        back = min(size, 200000)
        ins, cyc, _llc, _n = self._parse_from(size - back)
        self.ipc = ins / cyc if cyc > 0 else 0.0


class SpiderSenseStyle:
    def __init__(self, cfg_path, out_dir, args):
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir = out_dir
        self.last_masks = {}
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.tenants = [Tenant(t, out_dir) for t in cfg["tenants"]]
        self.names = [t.name for t in self.tenants]
        self.decisions = open(os.path.join(out_dir, "decisions.jsonl"), "w")
        with open(os.path.join(RESCTRL, "info/L3/cbm_mask")) as f:
            self.full = int(f.read(), 16)
        self.nbits = self.full.bit_length()
        self.args = args
        # start EQUAL (their initial partitioning); also the greedy start point
        n = len(self.tenants)
        base, extra = divmod(self.nbits, n)
        self.ways = {t.name: base + (1 if i < extra else 0)
                     for i, t in enumerate(self.tenants)}
        self.pending = None
        self.stop = False
        self.t0 = time.monotonic()
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_):
        self.stop = True

    def record(self, kind, **kw):
        kw.update(kind=kind, t=round(time.monotonic() - self.t0, 1))
        self.decisions.write(json.dumps(kw) + "\n")
        self.decisions.flush()

    def total_ipc(self):
        return sum(t.ipc for t in self.tenants)

    def apply_ways(self, ways):
        """DISJOINT contiguous blocks stacked from the top of the CBM, in
        config order (their one-COS-per-workload, no-overlap layout)."""
        used = 0
        for t in self.tenants:
            k = max(MIN_WAYS, ways[t.name])
            hi = self.nbits - used
            lo = hi - k
            mask = ((1 << hi) - 1) & ~((1 << lo) - 1) if lo > 0 else (1 << hi) - 1
            write_schemata_line(t.grp, "L3", domain_body(t.grp, "L3", f"{mask:x}"))
            self.last_masks[t.name] = f"{mask:x}"
            used += k

    def apply_masks(self):
        self.apply_ways(self.ways)

    def restore_all(self):
        try:
            write_schemata_line(RESCTRL, "L3", domain_body(RESCTRL, "L3", f"{self.full:x}"))
        except OSError:
            pass
        for t in self.tenants:
            if os.path.isdir(t.grp):
                try:
                    write_schemata_line(t.grp, "L3", domain_body(t.grp, "L3", f"{self.full:x}"))
                except OSError:
                    pass

    def normalize_ways(self):
        tot = sum(self.ways.values())
        while tot > self.nbits:
            k = max(self.ways, key=lambda n: self.ways[n])
            if self.ways[k] > MIN_WAYS:
                self.ways[k] -= 1; tot -= 1
            else:
                break
        while tot < self.nbits:
            k = min(self.ways, key=lambda n: self.ways[n])
            self.ways[k] += 1; tot += 1

    # ---------------- ALGORITHM 2 : exhaustive ----------------

    def probe(self, comp, tag="probe", idx=None):
        """configure(c_m) -> sleep(interval) -> sum per-tenant IPC.
        Lines 4-11 of ALGORITHM 2, with the line 7-9 CPI typo fixed."""
        ways = dict(zip(self.names, comp))
        self.apply_ways(ways)
        marks = {t.name: t.mark() for t in self.tenants}
        deadline = time.monotonic() + self.args.settle + self.args.interval
        while time.monotonic() < deadline and not self.stop:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        per = {t.name: t.read_window(marks[t.name]) for t in self.tenants}
        total = sum(p["ipc"] for p in per.values())
        rec = {"comp": list(comp), "ways": ways, "sum_ipc": round(total, 4),
               "ipc": {k: v["ipc"] for k, v in per.items()},
               "fp": {k: v["fp"] for k, v in per.items()},
               "nsamp": min(v["n"] for v in per.values())}
        # p99 goes into the decisions log ONLY (not `rec`, which feeds the
        # state/argmax), so the algorithm is byte-for-byte what it was.
        lat = {t.name: t.read_lat() for t in self.tenants if t.lat_file}
        self.record(tag, idx=idx, **rec, **({"lat": lat} if lat else {}))
        return rec

    # ---- chunked search state ------------------------------------------

    def _load_state(self):
        if not self.args.state or not os.path.exists(self.args.state):
            return {"done": {}, "anchor": {}, "chunks": 0}
        with open(self.args.state) as f:
            st = json.load(f)
        st.setdefault("done", {}); st.setdefault("anchor", {})
        st.setdefault("chunks", 0)
        return st

    def _save_state(self, st):
        if not self.args.state:
            return
        tmp = self.args.state + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, self.args.state)      # atomic: a killed chunk cannot
                                              # leave a half-written checkpoint

    def _chunk_offsets(self, st):
        """Per-chunk additive offset estimated from the anchor configurations.

        Chunking the sweep across scene relaunches means configurations are
        measured in different scene incarnations.  Without a correction a
        configuration could win simply because its chunk ran fast.  The same
        anchors are measured in EVERY chunk, so the mean anchor level per
        chunk estimates that chunk's offset, and the SPREAD of anchor levels
        across chunks is the resolution limit of the whole comparison: if the
        top configurations differ by less than that spread, the argmax is not
        resolvable and must not be reported as one.
        """
        per_chunk = {}
        for _k, obs in st.get("anchor", {}).items():
            for o in obs:
                per_chunk.setdefault(o["chunk"], []).append(o["sum_ipc"])
        if not per_chunk:
            return {}, 0.0, 0.0
        means = {c: sum(v) / len(v) for c, v in per_chunk.items()}
        grand = sum(means.values()) / len(means)
        drift = (max(means.values()) - min(means.values())) if len(means) > 1 else 0.0
        offs = {c: m - grand for c, m in means.items()}
        # RESIDUAL scatter is the real resolution limit: `drift` is what the
        # correction REMOVES, so testing against it would condemn a correction
        # that worked. What limits us is how well each anchor reproduces once
        # its chunk offset is subtracted.
        resid, n = 0.0, 0
        for _k, obs in st.get("anchor", {}).items():
            corr = [o["sum_ipc"] - offs.get(o["chunk"], 0.0) for o in obs]
            if len(corr) > 1:
                mu = sum(corr) / len(corr)
                resid += (sum((x - mu) ** 2 for x in corr) / (len(corr) - 1)) ** 0.5
                n += 1
        return offs, drift, (resid / n if n else 0.0)

    def _fp_gate(self):
        """Probe one neutral configuration and check every tenant's instance
        fingerprint against the iso baseline. Mirrors certify.py's
        INSTANCE_SPREAD=0.03 / INSTANCE_MIN_FP=0.10 (a relative spread on a
        near-zero fingerprint is meaningless -- npb_ep_e reads 0.004)."""
        try:
            with open(self.args.iso) as f:
                iso = json.load(f)
        except (OSError, ValueError) as e:
            return False, f"cannot read iso baseline {self.args.iso}: {e}"
        # MEASURE THE FINGERPRINT UNDER THE SAME CONDITION THE BASELINE USED.
        # The iso baseline, and every phase_noisy reading the 3% rule was
        # validated on, were taken at the FULL mask. Probing under an equal
        # split (3 of 15 ways) is not the same measurement: canneal reads 2.932
        # at full mask in this very run but 2.999 at 3 ways (+2.3%), because
        # CAT-invariance was only ever established for victims holding generous
        # masks. Comparing a 3-way reading to a full-mask baseline compares two
        # different things, and the difference is the same size as the threshold.
        for t in self.tenants:
            write_schemata_line(t.grp, "L3",
                                domain_body(t.grp, "L3", f"{self.full:x}"))
        marks = {t.name: t.mark() for t in self.tenants}
        deadline = time.monotonic() + self.args.fp_window
        while time.monotonic() < deadline and not self.stop:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        per = {t.name: t.read_window(marks[t.name]) for t in self.tenants}
        r = {"fp": {k: v["fp"] for k, v in per.items()},
             "ipc": {k: v["ipc"] for k, v in per.items()},
             "nsamp": min(v["n"] for v in per.values())}
        self.record("fp_gate", **r)
        only = [x for x in (self.args.fp_tenants or "").split(",") if x]
        bad, seen = [], []
        for t in self.names:
            if only and t not in only:
                continue
            want = (iso.get(t) or {}).get("fp")
            got = r["fp"].get(t)
            if want is None or got is None:
                continue
            if want < self.args.fp_min:
                seen.append(f"{t}=n/a")      # too little traffic to fingerprint
                continue
            d = abs(got - want) / want
            seen.append(f"{t}={got:.3f}/{want:.3f}({100*d:+.1f}%)")
            if d > self.args.fp_tol:
                bad.append(f"{t} {got:.3f} vs iso {want:.3f} "
                           f"({100*d:.1f}% > {100*self.args.fp_tol:.0f}%)")
        detail = " ".join(seen)
        return (not bad), (detail if not bad else "; ".join(bad))

    def run_exhaustive(self):
        allc = list(compositions(self.nbits, len(self.tenants), MIN_WAYS))
        rng = random.Random(self.args.seed)
        shuffled = list(allc)
        rng.shuffle(shuffled)
        # anchors are drawn from the shuffled list with the SAME seed in every
        # chunk, so every chunk measures an identical anchor set
        nanch = max(0, min(self.args.anchors, len(shuffled) - 1))
        anchors = shuffled[:nanch]
        body = shuffled[nanch:]

        st = self._load_state()
        chunk = st["chunks"]
        remaining = [c for c in body if comp_key(c) not in st["done"]]
        est = len(remaining) * (self.args.settle + self.args.interval)
        self.record("search_begin", m=len(allc), nbits=self.nbits,
                    tenants=self.names, interval=self.args.interval,
                    settle=self.args.settle, seed=self.args.seed,
                    runoff=self.args.runoff, budget=self.args.budget,
                    anchors=nanch, chunk=chunk,
                    done_before=len(st["done"]), remaining=len(remaining),
                    est_remaining_secs=round(est, 1),
                    deviations=["typo_fix_ipc_not_cpi", "randomized_order",
                                "topk_runoff", "instance_fingerprint",
                                "single_socket", "chunked_with_anchors"])
        log(f"ALGORITHM 2 chunk {chunk}: {len(st['done'])}/{len(body)} done, "
            f"{len(remaining)} remaining (~{est/60:.1f} min), {nanch} anchors")

        # ---- INSTANCE GATE ------------------------------------------------
        # Within ONE scene incarnation the tenant mix is identical and LLC-loads
        # per instruction is invariant to CAT (it is a PRE-LLC quantity: L2
        # misses, which an L3 mask cannot move). certify.py measures that
        # invariance at 0.9-1.0% across all six arms. So configurations probed
        # inside one chunk ARE directly comparable, exactly as expected.
        #
        # The chunk BOUNDARY is the exception: it relaunches the tenants, and
        # canneal is measured-bistable across relaunches (certify.py: D3v3 saw
        # 2.90 -> 3.45, 15.8%, persisting for the process lifetime). A relaunch
        # is a coin flip between those states. So rather than correcting for it
        # afterwards, REJECT any chunk whose tenants did not come back as the
        # instance the iso baseline describes -- same 3% rule certify.py uses.
        if self.args.iso:
            ok, detail = self._fp_gate()
            if not ok and self.args.fp_mode == "block":
                self.record("fp_gate_failed", detail=detail)
                log(f"!! instance gate FAILED: {detail}")
                log("   this chunk measured a different tenant instance than "
                    "the iso baseline -- discarding it and exiting so the "
                    "scene can be relaunched. No state was written.")
                log("   If this repeats with a SMALL deviation (~3-4%), it is "
                    "not the bistability (15.8%): re-measure iso for this "
                    "roster, or re-run with --fp-mode warn and filter after.")
                return 3
            if not ok:
                # warn mode: a rejected chunk costs a full scene warmup, and
                # every probe already carries its own fingerprint, so record the
                # mismatch and let the grader filter instead of discarding ~10
                # minutes of setup.
                self.record("fp_gate_warn", detail=detail)
                log(f"!! instance gate MISMATCH (continuing, --fp-mode warn): "
                    f"{detail}")
                log("   probes in this chunk are tagged; grade_spider.py will "
                    "flag them rather than silently mixing instances.")
            else:
                self.record("fp_gate_ok", detail=detail)
                log(f"instance gate ok: {detail}")

        t_chunk = time.monotonic()
        # anchors FIRST, so a chunk cut short by its budget still contributes
        # the offset estimate that makes its own probes comparable
        for c in anchors:
            if self.stop:
                break
            r = self.probe(c, tag="anchor", idx=chunk)
            st["anchor"].setdefault(comp_key(c), []).append(
                {"chunk": chunk, "sum_ipc": r["sum_ipc"]})
        # The budget governs BODY probing only. Charging the anchors to it
        # means a budget smaller than the anchor pass measures zero new
        # configurations, and the chunk driver then loops forever making no
        # progress. Chunk wall clock = anchors + budget; size the scene window
        # for that sum, not for the budget alone.
        t_start = time.monotonic()

        n_this = 0
        for c in remaining:
            if self.stop:
                self.record("search_aborted", done=n_this); break
            # ALWAYS take at least one body config, so forward progress is
            # guaranteed no matter how the budget is set
            if (n_this and self.args.budget
                    and (time.monotonic() - t_start) > self.args.budget):
                self.record("chunk_budget_reached", done=n_this)
                break
            r = self.probe(c, idx=n_this)
            st["done"][comp_key(c)] = {"sum_ipc": r["sum_ipc"], "ipc": r["ipc"],
                                       "fp": r["fp"], "chunk": chunk,
                                       "nsamp": r["nsamp"]}
            n_this += 1
            if n_this % 100 == 0:
                log(f"  {n_this} this chunk, {len(st['done'])}/{len(body)} total, "
                    f"{(time.monotonic()-t_start)/60:.1f} min")

        st["chunks"] = chunk + 1
        self._save_state(st)
        left = len(body) - len(st["done"])
        self.record("chunk_end", chunk=chunk, measured_this_chunk=n_this,
                    done_total=len(st["done"]), remaining=left,
                    secs=round(time.monotonic() - t_chunk, 1),
                    anchor_secs=round(t_start - t_chunk, 1))
        if left > 0 and n_this == 0 and not self.stop:
            # cannot happen with the guarantee above, but if it ever does the
            # driver must abort rather than relaunch the scene forever
            self.record("no_progress", chunk=chunk)
            log("!! chunk made NO progress -- aborting rather than spinning")
            return 4
        log(f"chunk {chunk} done: {n_this} configs, {left} still to go")

        if left > 0 or self.stop:
            # more chunks needed -- restore and exit so the scene can be
            # relaunched fresh for the next slice
            log("search INCOMPLETE -- relaunch the scene and resume with the "
                "same --state file")
            return 0

        # ---- search complete: normalize, run off, converge ----------------
        offs, drift, resid = self._chunk_offsets(st)
        rows = []
        for k, v in st["done"].items():
            comp = tuple(int(x) for x in k.split("-"))
            rows.append({"comp": comp, "ways": dict(zip(self.names, comp)),
                         "sum_ipc": v["sum_ipc"],
                         "norm_ipc": v["sum_ipc"] - offs.get(v["chunk"], 0.0),
                         "chunk": v["chunk"]})
        rows.sort(key=lambda r: -r["norm_ipc"])
        top = rows[:max(1, self.args.runoff)]
        gap = top[0]["norm_ipc"] - top[-1]["norm_ipc"] if len(top) > 1 else 0.0
        # 2x the residual: a gap smaller than that is not distinguishable
        # from anchor reproducibility, so the "winner" is a tie.
        floor = 2.0 * resid
        self.record("scan_done", measured=len(rows), chunks=st["chunks"],
                    anchor_drift=round(drift, 4), anchor_resid=round(resid, 4),
                    noise_floor=round(floor, 4), topk_gap=round(gap, 4),
                    resolvable=bool(gap > floor),
                    top=[{"comp": list(r["comp"]),
                          "sum_ipc": r["sum_ipc"],
                          "norm_ipc": round(r["norm_ipc"], 4)} for r in top])
        log(f"between-chunk drift {drift:.4f} (removed by anchor correction); "
            f"residual anchor noise {resid:.4f}")
        if gap <= floor:
            log(f"!! top-{len(top)} gap {gap:.4f} <= noise floor {floor:.4f}: "
                f"the argmax is NOT resolvable. Report it as a tie, not a "
                f"winner -- and note that SpiderSense itself decides on 1 s "
                f"samples, so this limit is theirs too.")

        if len(top) > 1 and not self.stop:
            log(f"runoff over top {len(top)} configs (same scene, same chunk)")
            re_m = [self.probe(tuple(r["comp"]), tag="runoff") for r in top]
            best_r = max(re_m, key=lambda r: r["sum_ipc"])
            best = {"ways": best_r["ways"], "sum_ipc": best_r["sum_ipc"]}
        else:
            best = {"ways": top[0]["ways"], "sum_ipc": top[0]["sum_ipc"]}

        self.ways = dict(best["ways"])
        self.apply_ways(self.ways)
        self.record("converged", ways=self.ways, sum_ipc=best["sum_ipc"],
                    chunks=st["chunks"], anchor_drift=round(drift, 4),
                    anchor_resid=round(resid, 4), resolvable=bool(gap > floor))
        # The graded arm must replay the SAME masks, not re-derive them: the
        # layout depends on tenant ORDER, and cfg.json's order comes from bash
        # associative-array iteration, which is not the declaration order.
        # Publishing the literal masks removes that whole class of mismatch.
        conv = {"ways": self.ways, "masks": dict(self.last_masks),
                "sum_ipc": best["sum_ipc"], "order": list(self.names),
                "resolvable": bool(gap > floor),
                "anchor_drift": round(drift, 4),
                "anchor_resid": round(resid, 4),
                "noise_floor": round(floor, 4), "topk_gap": round(gap, 4),
                "measured": len(rows), "chunks": st["chunks"]}
        st["converged"] = conv
        self._save_state(st)
        with open(os.path.join(self.out_dir, "converged.json"), "w") as f:
            json.dump(conv, f, indent=1)
        log(f"masks published: {conv['masks']}")
        log(f"BestConfiguration = {self.ways}  (sum IPC {best['sum_ipc']:.3f})")
        if self.args.rotate_perf:
            for t in self.tenants:
                t.rotate_perf()
            time.sleep(2.0)
            self.record("perf_rotated", note="graded window = held config only")
        self.hold()

    def hold(self):
        """Converge and hold: 'frequent changes to the global optimal
        configuration are rare during stable periods' (p.11).  We keep
        sampling so the held state is observable, but we do NOT re-search."""
        while not self.stop:
            marks = {t.name: t.mark() for t in self.tenants}
            time.sleep(2.0)
            per = {t.name: t.read_window(marks[t.name]) for t in self.tenants}
            self.record("hold", ways=dict(self.ways),
                        ipc={k: v["ipc"] for k, v in per.items()},
                        fp={k: v["fp"] for k, v in per.items()},
                        total_ipc=round(sum(p["ipc"] for p in per.values()), 4))

    # ---------------- budgeted variant : greedy hill-climb ----------------

    def run_greedy(self):
        self.record("search_begin", mode="greedy_budgeted", tenants=self.names,
                    note="NOT the paper's algorithm; donor/recipient rule is ours")
        log("budgeted hill-climb variant (NOT ALGORITHM 2)")
        while not self.stop:
            time.sleep(CYCLE_SECS)
            for t in self.tenants:
                if time.monotonic() - self.t0 >= 8:
                    t.sample()
            if time.monotonic() - self.t0 < 8:
                continue
            ipcs = {t.name: round(t.ipc, 4) for t in self.tenants}
            cur_total = self.total_ipc()
            self.record("scan", ipc=ipcs, total_ipc=round(cur_total, 4),
                        ways=dict(self.ways))
            if self.pending:
                p = self.pending
                if p["settle"] > 1:
                    p["settle"] -= 1
                    continue
                self.pending = None
                if cur_total >= p["pre_total"] + IMPROVE_IPC:
                    self.record("keep", frm=p["frm"], to=p["to"],
                                gain=round(cur_total - p["pre_total"], 4))
                else:
                    self.ways[p["frm"]] += 1
                    self.ways[p["to"]] -= 1
                    self.normalize_ways()
                    self.apply_masks()
                    self.record("revert", frm=p["frm"], to=p["to"])
                continue
            donors = [t for t in self.tenants if self.ways[t.name] > MIN_WAYS]
            if len(self.tenants) < 2 or not donors:
                continue
            frm = min(donors, key=lambda t: t.ipc).name
            to = max((t for t in self.tenants if t.name != frm),
                     key=lambda t: t.ipc).name
            if frm == to:
                continue
            self.ways[frm] -= 1
            self.ways[to] += 1
            self.normalize_ways()
            self.apply_masks()
            self.pending = {"frm": frm, "to": to, "pre_total": cur_total,
                            "settle": SETTLE_CYCLES}
            self.record("shift", frm=frm, to=to, ways=dict(self.ways))

    def run(self):
        for t in self.tenants:
            t.start_perf()
        self.normalize_ways()
        self.apply_masks()
        log(f"spidersense_style up: {len(self.tenants)} tenants, {self.nbits} ways, "
            f"search={self.args.search}, objective=max SUM IPC (tail-blind)")
        time.sleep(2.0)                      # let perf produce a first interval
        rc = 0
        try:
            if self.args.search == "exhaustive":
                rc = self.run_exhaustive() or 0
            else:
                self.run_greedy()
        finally:
            log("spidersense_style shutting down: restoring")
            self.restore_all()
            for t in self.tenants:
                t.stop_perf()
            self.decisions.close()
        return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--search", choices=["exhaustive", "greedy"],
                    default="exhaustive",
                    help="exhaustive = the paper's ALGORITHM 2 (default); "
                         "greedy = our budgeted hill-climb variant")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="per-config measurement window, seconds "
                         "(their Table 2 Sampling Time Interval = 1s)")
    ap.add_argument("--settle", type=float, default=0.0,
                    help="extra settle before measuring each config; 0 = their "
                         "cadence exactly, 1.0 = the generous variant")
    ap.add_argument("--runoff", type=int, default=5,
                    help="re-measure the top-k configs and take the argmax "
                         "among them (0/1 = disable)")
    ap.add_argument("--budget", type=float, default=0.0,
                    help="max seconds of probing per chunk (0 = unlimited). "
                         "Size it under the SHORTEST tenant's run length so no "
                         "tenant restarts mid-chunk.")
    ap.add_argument("--state", default="",
                    help="checkpoint file: configs already measured. Enables "
                         "CHUNKED search -- run with --budget, let the scene "
                         "restart, re-run with the same --state to resume. "
                         "Keeps every config on a non-restarted tenant instance "
                         "without re-sizing any workload.")
    ap.add_argument("--iso", default="",
                    help="results_iso/<AS>/iso.json. Enables the INSTANCE GATE: "
                         "a chunk whose tenants did not come back as the "
                         "instance the iso baseline describes is discarded "
                         "(exit 3) instead of being folded into the sweep.")
    ap.add_argument("--fp-tol", type=float, default=0.06,
                    help="instance fingerprint tolerance. certify.py uses 3%% "
                         "for WITHIN-run arm-to-arm spread; this is an "
                         "ACROSS-LAUNCH comparison against a baseline measured "
                         "on another day, which is a looser quantity. MEASURED "
                         "over 15 full-mask readings spanning D3v2-D3v9 plus "
                         "today: canneal varies 2.904-2.976 (2.5%%, max +2.4%% "
                         "vs iso). 6%% gives 2.5x headroom over that while "
                         "staying 3.1x below the bistability flip (18.7%%).")
    ap.add_argument("--fp-min", type=float, default=0.10,
                    help="skip the gate for tenants whose iso fingerprint is "
                         "below this (npb_ep_e reads 0.004)")
    ap.add_argument("--fp-mode", choices=["block", "warn"], default="block",
                    help="block = discard a chunk whose instance does not match "
                         "iso (correct, but costs a full scene warmup per "
                         "rejection); warn = record the mismatch and continue, "
                         "leaving the filtering to grade_spider.py")
    ap.add_argument("--fp-window", type=float, default=20.0,
                    help="seconds to measure the gate fingerprint over. The 3%% "
                         "threshold is calibrated on 45 s phase windows; a 2 s "
                         "probe fails it on noise (measured: canneal reads 3.1%% "
                         "off at 2 s, 0.9%% off at 45 s).")
    ap.add_argument("--fp-tenants", default="",
                    help="restrict the gate to these tenants (comma-separated). "
                         "MEASURED on D3v9: canneal 0.9%% and npb_cg_d80 0.3%% "
                         "hold against iso, but llama swings 16.7%% and "
                         "npb_mg_d300 3.9%% -- both donors, and certify.py's "
                         "guard is explicitly scoped to un-throttled tenants. "
                         "Pass the VICTIMS; gating on donors rejects healthy "
                         "chunks.")
    ap.add_argument("--anchors", type=int, default=8,
                    help="configs re-measured in EVERY chunk, to estimate the "
                         "per-chunk offset and the between-chunk noise floor")
    ap.add_argument("--no-rotate-perf", dest="rotate_perf",
                    action="store_false", default=True,
                    help="do NOT restart the perf counters at convergence; "
                         "the graded window then spans the search too")
    ap.add_argument("--seed", type=int, default=1,
                    help="seed for the randomized visit order")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("must run as root (resctrl)")
    sys.exit(SpiderSenseStyle(args.config, args.out, args).run())


if __name__ == "__main__":
    main()
