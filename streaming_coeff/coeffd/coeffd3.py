#!/usr/bin/env python3
"""coeffd3 -- daemon implementing DECISION_LOGIC.md v4 (triage contract).

Two independent per-victim axes (victim-exclusive CAT reservation +
aggressor-side MBA throttle) + frontier migration, multi-victim triage
ordered by (priority desc, CMT asc), and the equilibrium rule: one
graduated lever change per decision cycle, kept only if the beneficiary
improves AND no equal-or-higher-priority tenant is NEWLY hurt; otherwise
rolled back and blocked.

v6: bounded-exchange throttling + layered relative veto. v5.2's throttle
  eligibility rule ("not hurt, or at-capacity, or sanctioned by priority")
  still let a real victim be exploited: bfs's dram_bound sat at
  12.4-19.1pp -- always UNDER GATE_PP=20 -- while coeffd throttled it
  100->50->35->20->10 (the MBA floor) to serve bc, EVERY step committing
  (run-3 coeffd_mig, run-4 coeffd_l0), leaving bfs 42%/-179% worse than
  unmanaged. "Not hurt" made it ELIGIBLE and its harm was invisible to a
  veto that only sees gate-crossings; worse, the throttle then SUPPRESSED
  the signal further -- a starved tenant issues fewer requests and stalls
  less, so its db actually FELL (14.7->3.0 GB/s MBM), permanently
  defeating guard_release's db-rising causality test. v6 adds a second,
  gate-independent signal -- instruction rate (ips), a work-rate throttling
  cannot flatter -- and uses it three ways: (1) ANY alive peer is now a
  candidate throttle target (the v5.2 eligibility filter is gone), but an
  unsanctioned target's cumulative ips cost is bounded (TARGET_COST_FRAC);
  (2) the veto gains an ips-drop clause so a bystander crossing no gate
  still blocks the step (BYSTANDER_DROP_FRAC), and a committed step must
  demonstrably help more than just its own victim (BENEFIT_MIN); (3)
  guard_release gains a rate-based notch (RATE_GUARD_FRAC) that watches
  ips directly, independent of is_hurt, so a throttle that suppresses its
  own gate signal cannot hide from continuous surveillance.

v4: three per-tenant information levels, mixed freely in one scene --
  Level 0 (opaque): hardware signals only; hurt := dram_bound >= gate.
  Level 1 (+priority): declared rank; harm may flow DOWN the ladder only.
  Level 2 (+SLO): optional "lat_file" (live p99 feed, the patched
    TailBench format "ts_ns count p50 p95 p99") + "qos_ms" target. The
    SLO modifies the SAME machinery in exactly two ways:
      (a) hurt definition & stop: p99 > target admits the tenant to the
          hurt list even when counters can't see the harm (queueing-knee
          blindness, exp14); target met = healed/stop/release.
      (b) guard rail: the veto also fires if a step pushes another FED
          tenant over ITS target (the sharpened harm check that plain
          dram_bound missed at the knee).
    A stale/absent feed degrades that tenant gracefully to its level
    below. Signals still choose every action; declarations only decide
    who counts as hurt and whose harm is acceptable.

Stdlib only. Usage:
  sudo python3 coeffd3.py --config cfg.json --out results/daemon
Config:
  {"tenants": [{"name": "...", "pid": N, "grp": "/sys/fs/resctrl/...",
                "priority": 2, "cores": "2-3",
                "lat_file": "...", "qos_ms": 1.98}, ...],   # last two optional
   "migration": {...}}                                       # optional
Stops on SIGTERM/SIGINT; restores all masks/MBA and kills its perf
children on the way out (bounded INT->KILL, the established perf gotcha:
perf stat -p on an exited PID ignores SIGINT and hangs).
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"                 # only socket-0 schemata are managed

GATE_PP = 20.0               # harm gate: dram_bound %, absolute (solo masstree=9.1)
MBA_TRIGGER_PP = 10.0        # bandwidth_stall_pct trigger (recalibrated hint, v3)
DECIDE_SECS = 10.0
SAMPLE_SECS = 2.0
SHARED_MIN_WAYS = 7          # backstop: shared pool never below this
RSV_MAX_WAYS = 5             # backstop: per-victim slice cap
MBA_LEVELS = [100, 50, 35, 20, 10]
RELEASE_CYCLES = 3           # sustained-quiet cycles before shrinking a lever
# "beneficiary improving" means HARM REDUCTION only: dram_bound fell by at
# least this much. Deliberately NOT "its CMT grew" -- exp11 showed a
# cache-hungry victim fills every byte it is given, so occupancy growth is
# unconditional and would defeat plateau detection; a way that fills but
# doesn't reduce the victim's stalls is capacity wasted, and gets rolled
# back. CMT is still logged as a diagnostic.
IMPROVE_DB_PP = 0.5
# Pragmatic deviation from the doc: blocked steps expire on a TTL, not only
# on scene change -- one noisy verify window must not permanently disable a
# lever. Noted in DECISION_LOGIC.md as an implementation compromise.
BLOCK_TTL_SECS = 180.0
PERF_TAIL_SAMPLES = 5        # mean of the last N interval samples per metric
PERF_INTERVAL_SECS = 2.0     # matches the "-I" ms argument to perf stat
# How perf attaches to a tenant. "pid" = `perf stat -p <pid>` (the historical
# default; every result before 2026-08-07 was taken this way). "cores" =
# `perf stat -C <cores>`, required whenever the registered pid is a WRAPPER
# rather than the workload itself: a `while true; do BIN; done` launcher makes
# the shell the pid, and -p only picks up children forked AFTER perf attaches,
# so a long-iteration child (llama, ffmpeg, clickhouse, xsbench) is counted as
# ZERO instructions / no LLC events while fast-relooping ones (npb, water,
# facesim) are counted fine. -C is exact when tenants own disjoint cores, which
# every experiment scene enforces by pinning. See exp16 Phase-A 2026-08-06.
PERF_ATTACH = "pid"

# ── at-capacity classification (v5, from exp15a canneal/polluter) ────────
# A tenant whose dram_bound sits above the gate but NEVER RESPONDS to
# lever changes is running at its natural memory-bound operating point (a
# pure streamer: being stalled is how it consumes the machine), not
# suffering -- the exp12 "gate can't tell suffering from capacity"
# observation, finally operationalized. Detection is BEHAVIORAL: db
# pinned (range <= AT_CAP_NOISE_PP) across a window spanning >=
# AT_CAP_MIN_ACTS actuations, optionally confirmed by low LLC hit rate
# (streamer signature; a HINT, never the gate -- exp8: PR's coeff band
# said streamer while its 0.96 hit rate said reuser). Consequences: the
# tenant leaves the hurt list (no rsv wasted on it, honest healed/
# frontier), leaves fine_before (a saturated stall signal carries no veto
# information), and loses guard-release protection (db rising under
# throttle is mechanics, not suffering). Remaining protections: the MBA
# hardware floor, and release() unwinding throttles once the beneficiary
# heals. Declassified the moment db finds headroom below the gate.
AT_CAP_WINDOW = 6            # decide cycles (~60s) of db history examined
AT_CAP_NOISE_PP = 2.0        # max db range across the window = "pinned"
AT_CAP_MIN_ACTS = 2          # actuations the window must span (probes earn it)
AT_CAP_HITRATE = 0.5         # LLC hit rate <= this confirms; None = skip check
AT_CAP_MIN_MBM_BPS = 3e9     # v7: a real at-capacity streamer MOVES bandwidth.
                             # A pollution-VICTIM has pinned db + low hit too,
                             # but LOW mbm (it is stalled on OTHERS' traffic,
                             # mem-LATENCY-bound, not mem-BANDWIDTH-bound). exp18
                             # masstree (db 38, hit 0.46, mbm 0.3 GB/s) was
                             # misflagged at-capacity -> dropped from hurt_list
                             # -> its WORKING cache fence got released -> it
                             # reverted to degraded while dumb static (which
                             # never releases) recovered it +44%. Requiring
                             # mbm >= this excludes the latency-victim; the
                             # polluters (mbm ~18 GB/s) still qualify.

# ── v4: SLO layer ────────────────────────────────────────────────────────
FEED_STALE_SECS = 6.0        # older feed line = tenant degrades to Level 0/1
IMPROVE_P99_FRAC = 0.03      # fed victim "improving": p99 fell >=3% (or met target)

# ── migration tier (fires ONLY at the frontier) ─────────────────────────
# Quietness is gated on destination-domain BANDWIDTH, not occupancy.
# Occupancy (CMT) is unusable for an idle socket: lines stay attributed to
# their RMID until EVICTED, and an idle cache evicts nothing -- so a socket
# that has run nothing for hours still reports ~full occupancy (observed:
# 58.2MB "occupied" on a completely idle node 1, which blocked every
# migration in the first mig-mode run). Bandwidth is a rate: stale lines
# generate zero traffic, so MBM tells the truth about idleness.
# Threshold means "has headroom", not "empty": the first mig run set this
# to 2GB/s and the first migrant's own traffic (bc, 3.4GB/s of a ~100GB/s
# socket) then blocked every subsequent migration. A destination with one
# light tenant is still an excellent destination. Loosening is safe: the
# verify step still protects against a genuinely full socket -- a migrant
# that doesn't improve there gets migrated back and the step blocked.
MIG_QUIET_BW_BPS = 20e9      # dest is quiet if its aggregate MBM < 20 GB/s
MIG_VERIFY_CYCLES = 2        # grace before judging: pages move + caches warm
MIG_BLOCK_TTL_SECS = 600.0   # a failed migration isn't retried for a while

# ── v6: bounded-exchange throttling + layered relative veto ─────────────
# See the module docstring for the bfs/mechanism story. The short version:
# db (dram_bound) is a STALL signal, and throttling can suppress it in a
# starved tenant -- so v6 adds a second signal, instruction rate (ips),
# that throttling cannot flatter, and bounds/vetoes on it independently.
TARGET_COST_FRAC = 0.10      # max measured work-rate cost a throttle TARGET
                              # may bear (vs its pre-first-throttle ips
                              # baseline) for a step to commit, unless
                              # sanctioned. The offline grading tolerance is
                              # 5% over 600s windows; the online bound must
                              # sit above the ~5-10% noise of 10s tail-mean
                              # windows, hence 0.10.
BYSTANDER_DROP_FRAC = 0.15   # a fine-at-apply-time tenant whose ips fell
                              # >15% vs its pre-step level counts as
                              # newly-hurt (veto) even if its db never
                              # crossed the gate -- closes the sub-gate
                              # blindness (bfs case).
BENEFIT_MIN = 2              # an MBA step must have improved at least
                              # min(BENEFIT_MIN, #hurt-at-apply) hurt
                              # tenants to commit ("benefits many" -- the
                              # utilitarian justification for spending the
                              # target's value).
RATE_GUARD_FRAC = 0.25
COHERENCE_MBM_DROP = 0.10    # v8 multi-counter coherence: a throttle-target's
                             # ips-cost counts as OUR harm only if its mbm ALSO
                             # dropped >=10% vs the pre-throttle baseline (our
                             # MBA cap actually bit). ips-down + mbm-flat = a
                             # phase change, NOT our throttle -> do not veto on
                             # it. Reduces phase-confounded false rollbacks;
                             # cannot miss real throttle harm (the cap always
                             # drops a bandwidth-bound tenant's mbm).       # continuing surveillance: a throttled tenant
                              # sustaining >25% ips deficit vs its
                              # pre-throttle baseline for RELEASE_CYCLES
                              # decide-cycles earns a guard-release notch
                              # (generous because the baseline ages with
                              # phases).


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def is_running(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return False


def read_mon(grp):
    mbm = llc = 0
    base = os.path.join(grp, "mon_data")
    try:
        for d in os.listdir(base):
            if not d.startswith("mon_L3_"):
                continue
            for fname, acc in (("mbm_total_bytes", "m"), ("llc_occupancy", "l")):
                try:
                    with open(os.path.join(base, d, fname)) as f:
                        v = int(f.read())
                except (OSError, ValueError):
                    v = 0
                if acc == "m":
                    mbm += v
                else:
                    llc += v
    except OSError:
        pass
    return mbm, llc


def parse_perf_tail(path):
    """Mean of the last PERF_TAIL_SAMPLES per metric from a perf -I json
    log, plus LLC hit rate from the raw event lines (None when the events
    are absent/unsupported -- callers must treat hit rate as optional),
    plus ips (instructions/sec, tail-mean over PERF_TAIL_SAMPLES, v6's
    work-rate signal -- see module docstring)."""
    db, mb, ml, loads, misses, ins = [], [], [], [], [], []
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return 0.0, 0.0, 0.0, None, 0.0
    for line in lines[-200:]:
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        mv = o.get("metric-value")
        where = (o.get("metric-unit") or "") + " " + (o.get("metricname") or "")
        if mv not in (None, ""):
            if "tma_dram_bound" in where:
                db.append(float(mv))
            elif "tma_mem_bandwidth" in where:
                mb.append(float(mv))
            elif "tma_mem_latency" in where:
                ml.append(float(mv))
        ev = o.get("event") or ""
        if "LLC-load" in ev:
            try:
                val = float(o.get("counter-value"))
            except (TypeError, ValueError):
                continue        # "<not counted>"/"<not supported>"
            (misses if "miss" in ev else loads).append(val)
        elif ev == "instructions":
            try:
                ins.append(float(o.get("counter-value")))
            except (TypeError, ValueError):
                continue
    avg = lambda xs: sum(xs[-PERF_TAIL_SAMPLES:]) / len(xs[-PERF_TAIL_SAMPLES:]) if xs else 0.0
    hit = None
    tl, tm = sum(loads[-PERF_TAIL_SAMPLES:]), sum(misses[-PERF_TAIL_SAMPLES:])
    if tl > 0:
        hit = max(0.0, 1.0 - tm / tl)
    ips = avg(ins) / PERF_INTERVAL_SECS
    return avg(db), avg(mb), avg(ml), hit, ips


def write_schemata_line(grp, resource, body):
    with open(os.path.join(grp, "schemata"), "w") as f:
        f.write(f"{resource}:{body}\n")


def domain_body(grp, resource, new_value):
    """Rebuild one resource line with only DOMAIN's value replaced."""
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
    raise RuntimeError(f"{resource} line not found in {grp}/schemata")


def parse_cores(spec):
    """'34-37' or '12' -> set of ints."""
    out = set()
    for part in str(spec).split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out.update(range(int(lo), int(hi) + 1))
        elif part:
            out.add(int(part))
    return out


class Tenant:
    def __init__(self, cfg, out_dir):
        self.name = cfg["name"]
        self.pid = int(cfg["pid"])
        self.grp = cfg["grp"]
        self.prio = int(cfg["priority"])
        self.cores = parse_cores(cfg["cores"]) if "cores" in cfg else set()
        self.perf_cores = set(self.cores)   # tracks migration; see restart_perf
        # v4 Level 2 (optional): live p99 feed + declared target
        self.lat_file = cfg.get("lat_file")
        self.qos = float(cfg["qos_ms"]) if cfg.get("qos_ms") else None
        self.p99 = None              # None = no/stale feed -> Level 0/1
        self.perf_json = os.path.join(out_dir, f"perf_{self.name}.json")
        self.perf_proc = None
        self.alive = True
        self.db = self.mb = self.ml = 0.0
        self.ips = 0.0            # v6: instructions/sec, work-rate signal
        self.cmt = 0
        self.mbm_bps = 0.0
        self._mbm_prev = None
        self._mbm_prev_t = None
        # at-capacity classification state (v5)
        self.hit_rate = None       # LLC hit rate, None if events unsupported
        self.db_hist = []          # [(db, act_seq)] one entry per decide cycle
        self.at_capacity = False

    def perf_target(self):
        """Attach args: -C <cores> when asked for (and cores are known),
        else the historical -p <pid>. `cores` follows a migration, so this
        reads self.perf_cores rather than the static self.cores."""
        if PERF_ATTACH == "cores" and self.perf_cores:
            return ["-C", ",".join(str(c) for c in sorted(self.perf_cores))]
        return ["-p", str(self.pid)]

    def start_perf(self):
        self.perf_proc = subprocess.Popen(
            ["perf", "stat", "-j", "-o", self.perf_json,
             "-e", "LLC-loads,LLC-load-misses,instructions",
             "-M", "tma_dram_bound,tma_mem_bandwidth,tma_mem_latency",
             "-I", str(int(PERF_INTERVAL_SECS * 1000))]
            + self.perf_target() + ["--", "sleep", "100000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def restart_perf(self, cores):
        """Re-attach after the tenant's cores change (migration / rollback).
        No-op in pid mode -- the pid is unchanged by a move."""
        if PERF_ATTACH != "cores":
            return
        self.perf_cores = set(cores)
        self.stop_perf()
        self.perf_proc = None
        self.start_perf()

    def stop_perf(self):
        p = self.perf_proc
        if p is None or p.poll() is not None:
            return
        p.send_signal(signal.SIGINT)
        for _ in range(10):
            if p.poll() is not None:
                return
            time.sleep(0.5)
        p.kill()
        p.wait()

    def sample(self):
        if not is_running(self.pid):
            self.alive = False
            return
        mbm, llc = read_mon(self.grp)
        now = time.monotonic()
        if self._mbm_prev is not None and now > self._mbm_prev_t:
            self.mbm_bps = max(0.0, (mbm - self._mbm_prev) / (now - self._mbm_prev_t))
        self._mbm_prev, self._mbm_prev_t = mbm, now
        self.cmt = llc
        self.db, self.mb, self.ml, self.hit_rate, self.ips = \
            parse_perf_tail(self.perf_json)
        self.p99 = self._read_feed() if self.lat_file else None

    def _read_feed(self):
        try:
            with open(self.lat_file) as f:
                parts = f.read().split()
            ts_ns, p99 = int(parts[0]), float(parts[4])
            if time.time() - ts_ns / 1e9 > FEED_STALE_SECS:
                return None
            return p99
        except (OSError, ValueError, IndexError):
            return None

    def slo_violating(self):
        return self.qos is not None and self.p99 is not None and self.p99 > self.qos

    def bwstall(self):
        return self.db * self.mb / 100.0


class Daemon:
    def __init__(self, cfg_path, out_dir, observe=False):
        # observe=True: sense + emit scan records but NEVER actuate. Used only by
        # the classification-credibility screen to capture UNMANAGED signals (a
        # managed scan is contaminated -- a throttled streamer's mbm drops below
        # the gate and misclassifies). Decision logic is bypassed, not changed.
        self.observe = observe
        os.makedirs(out_dir, exist_ok=True)
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.tenants = {t["name"]: Tenant(t, out_dir) for t in cfg["tenants"]}
        self.decisions = open(os.path.join(out_dir, "decisions.jsonl"), "w")
        with open(os.path.join(RESCTRL, "info/L3/cbm_mask")) as f:
            self.full = int(f.read(), 16)
        self.nbits = self.full.bit_length()
        self.alloc = {}          # name -> exclusive ways
        self.slice_order = []    # grant order, slices stack from mask top
        self.mba = {n: 100 for n in self.tenants}
        self.pending = None
        self.blocked = {}        # step key -> monotonic expiry timestamp
        self.quiet = {}          # name -> consecutive below-gate cycles
        self.settled = None      # None | "healed" | "frontier"
        self.stop = False
        # migration tier (optional): {"dest_cores": "34-61",
        # "src_node": 0, "dest_node": 1}
        mig = cfg.get("migration")
        self.mig = None
        if mig:
            self.mig = {"pool": sorted(parse_cores(mig["dest_cores"])),
                        "src": int(mig["src_node"]),
                        "dst": int(mig["dest_node"])}
        self.migrated = {}       # name -> set of dest cores carved for it
        self.mba_beneficiary = {}  # throttle target -> victim it serves
        self.mba_pre_db = {}     # throttle target -> its db at FIRST notch
        self.mba_pre_mbm = {}    # v8: throttle target -> its mbm at FIRST notch (coherence)
        self.mba_pre_ips = {}    # v6: throttle target -> its ips at FIRST
                                  # notch (cumulative cost baseline); set
                                  # exactly like mba_pre_db, but ALSO
                                  # cleared wherever mba is restored to 100
        self.rate_guard_count = {}  # v6: name -> consecutive decide-cycles
                                  # of sustained ips deficit (RATE_GUARD_FRAC)
        self.act_seq = 0         # actuation counter, feeds at-capacity windows
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_):
        self.stop = True

    # ── mask plumbing ────────────────────────────────────────────────
    def masks(self):
        """Per-tenant L3 mask + shared pool, NESTED-PREFIX layout.

        Hardware CBMs must be CONTIGUOUS. The naive "own slice + shared
        pool" mask has a hole for every slice-holder except the one
        adjacent to the pool -- the kernel rejects it with EINVAL (this
        crashed exp12 run 1 the moment a second victim was granted a
        slice). Instead: slices stack from the top of the mask in
        priority order, and each holder's mask is the contiguous prefix
        [bit 0 .. top of its own slice]. Every mask is a prefix, so every
        mask is contiguous. Semantics: a holder's slice is protected from
        the crowd AND from lower-priority holders, but open to
        higher-priority holders -- cache-shaped "harm may flow down the
        ladder, never up".
        """
        holders = [n for n in self.slice_order if self.alloc.get(n, 0) > 0]
        holders.sort(key=lambda n: (-self.tenants[n].prio,
                                    self.slice_order.index(n)))
        used = 0
        prefix = {}
        for name in holders:
            prefix[name] = (1 << (self.nbits - used)) - 1
            used += self.alloc[name]
        shared = (1 << (self.nbits - used)) - 1
        return prefix, shared

    def apply_masks(self):
        prefix, shared = self.masks()
        for name, t in self.tenants.items():
            if not t.alive:
                continue
            mask = prefix.get(name, shared)
            write_schemata_line(t.grp, "L3", domain_body(t.grp, "L3", f"{mask:x}"))
        write_schemata_line(RESCTRL, "L3", domain_body(RESCTRL, "L3", f"{shared:x}"))

    def apply_mba(self, name):
        t = self.tenants[name]
        write_schemata_line(t.grp, "MB", domain_body(t.grp, "MB", str(self.mba[name])))

    def restore_all(self):
        try:
            write_schemata_line(RESCTRL, "L3", domain_body(RESCTRL, "L3", f"{self.full:x}"))
        except OSError:
            pass
        for name, t in self.tenants.items():
            if not os.path.isdir(t.grp):
                continue
            try:
                write_schemata_line(t.grp, "L3", domain_body(t.grp, "L3", f"{self.full:x}"))
                write_schemata_line(t.grp, "MB", domain_body(t.grp, "MB", "100"))
            except OSError:
                pass

    # ── migration mechanics ──────────────────────────────────────────
    def _dest_mbm_total(self):
        dom = f"mon_L3_{self.mig['dst']:02d}"
        total = 0
        grps = [t.grp for t in self.tenants.values() if t.alive] + [RESCTRL]
        for g in grps:
            try:
                with open(os.path.join(g, "mon_data", dom, "mbm_total_bytes")) as f:
                    total += int(f.read())
            except OSError:
                pass
        return total

    def dest_bandwidth(self):
        """Aggregate DRAM traffic on the destination domain (bytes/s),
        all groups (managed + default) -- the observable quietness gate.
        Bandwidth, NOT occupancy: see MIG_QUIET_BW_BPS comment."""
        a = self._dest_mbm_total()
        time.sleep(1.0)
        b = self._dest_mbm_total()
        return max(0.0, b - a)

    def set_affinity_all(self, t, cores):
        moved = 0
        try:
            tids = os.listdir(f"/proc/{t.pid}/task")
        except OSError:
            return 0
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cores)
                moved += 1
            except OSError:
                pass
        return moved

    def migrate_pages(self, t, src, dst):
        # membind caveat: tenants launched with numactl --membind keep that
        # policy for NEW allocations; migratepages moves what exists now.
        r = subprocess.run(["migratepages", str(t.pid), str(src), str(dst)],
                           capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or "").strip()

    def apply_migration(self, v, fine_before):
        need = max(len(v.cores), 1)
        if len(self.mig["pool"]) < need:
            return False
        chunk = set(self.mig["pool"][:need])
        self.mig["pool"] = self.mig["pool"][need:]
        n = self.set_affinity_all(v, chunk)
        v.restart_perf(chunk)          # -C follows the tenant across sockets
        ok, err = self.migrate_pages(v, self.mig["src"], self.mig["dst"])
        step = ("mig", v.name, sorted(chunk)[0])
        self.pending = {"step": step, "victim": v.name, "pre_db": v.db,
                        "pre_cmt": v.cmt, "pre_p99": v.p99,
                        "fine_before": fine_before,
                        "chunk": chunk, "wait": MIG_VERIFY_CYCLES}
        self.record("apply", step=step, victim=v.name, tids_moved=n,
                    pages_ok=ok, pages_err=err,
                    dest_cores=sorted(chunk))
        return True

    def rollback_migration(self, p):
        v = self.tenants[p["victim"]]
        self.set_affinity_all(v, v.cores)
        v.restart_perf(v.cores)
        self.migrate_pages(v, self.mig["dst"], self.mig["src"])
        self.mig["pool"] = sorted(set(self.mig["pool"]) | p["chunk"])

    # ── decision cycle ───────────────────────────────────────────────
    def record(self, kind, **kw):
        # every lever movement bumps act_seq: an at-capacity window is
        # only meaningful if levers demonstrably moved within it
        if kind in ("apply", "rollback", "guard-release", "release"):
            self.act_seq += 1
        kw.update(kind=kind, t=round(time.monotonic() - self.t0, 1))
        self.decisions.write(json.dumps(kw) + "\n")
        self.decisions.flush()

    def snapshot(self):
        return {n: {"db": round(t.db, 1), "bws": round(t.bwstall(), 1),
                    "cmt_mb": round(t.cmt / 1048576, 1),
                    "mbm_gbps": round(t.mbm_bps / 1e9, 1),
                    "ips": round(t.ips, 1),
                    "prio": t.prio, "alive": t.alive,
                    "hit": round(t.hit_rate, 3) if t.hit_rate is not None else None,
                    "atcap": t.at_capacity,
                    "p99": round(t.p99, 3) if t.p99 is not None else None,
                    "qos": t.qos}
                for n, t in self.tenants.items()}

    def is_hurt(self, t):
        """v4 unified hurt: hardware gate (always) OR SLO violation (when a
        fresh feed + target exist). One predicate everywhere -- it defines
        the hurt list, 'fine' for the veto, and 'quiet' for release.
        v5: an at-capacity tenant (db pinned at its natural memory-bound
        operating point, see AT_CAP_* above) is NOT hurt -- its gate signal
        is saturated and carries no information. A live SLO violation still
        wins: a fed tenant missing its target deserves victim status no
        matter how its counters are classified."""
        if t.slo_violating():
            return True
        if t.at_capacity:
            return False
        return t.db >= GATE_PP

    def update_at_capacity(self):
        """Behavioral nature-vs-suffering classification, once per decide
        cycle. A tenant earns at-capacity status by NOT responding: db
        pinned above the gate across a window in which levers demonstrably
        moved (the daemon's own failed probes supply the actuations, so
        classification is measured, never declared). Hit rate confirms
        when available; a starved reuse victim's db is lever-RESPONSIVE,
        which is what actually separates it from a streamer."""
        for t in self.tenants.values():
            if not t.alive:
                continue
            # only UNTHROTTLED samples define a tenant's nature: its db
            # response to its own throttle is mechanics, not evidence of
            # lever-responsiveness (exp15a can round: the committed MBA
            # bent pol's curve 25.5->31.1, un-flattening the window and
            # losing the guard/classifier race).
            if self.mba.get(t.name, 100) >= 100:
                t.db_hist.append((t.db, self.act_seq))
            if len(t.db_hist) > AT_CAP_WINDOW:
                t.db_hist = t.db_hist[-AT_CAP_WINDOW:]
            if t.at_capacity:
                if t.db < GATE_PP - AT_CAP_NOISE_PP:
                    t.at_capacity = False
                    t.db_hist = []
                    self.record("at-capacity-clear", target=t.name,
                                db=round(t.db, 1))
                continue
            if len(t.db_hist) < AT_CAP_WINDOW:
                continue
            dbs = [d for d, _ in t.db_hist]
            acts = [a for _, a in t.db_hist]
            if (min(dbs) >= GATE_PP
                    and max(dbs) - min(dbs) <= AT_CAP_NOISE_PP
                    and acts[-1] - acts[0] >= AT_CAP_MIN_ACTS
                    and (t.hit_rate is None or t.hit_rate <= AT_CAP_HITRATE)
                    # v7 discriminator: at-capacity = a bandwidth ENGINE, not a
                    # latency-stalled victim. A real streamer MOVES bandwidth;
                    # a polluted small-hot-set LC has pinned db + low hit too but
                    # LOW mbm (stalled on OTHERS' traffic). mbm is the DATA-
                    # VERIFIED separator (exp18: masstree 0.3 vs polluters 18
                    # GB/s). The mem_bandwidth-vs-latency TMA split (mb/ml) is
                    # logged below but NOT gated on: at 32% node saturation the
                    # polluters may themselves be mem_LATENCY-bound, so mb>=ml
                    # could wrongly EXCLUDE them -- revisit once mb/ml is seen.
                    and t.mbm_bps >= AT_CAP_MIN_MBM_BPS):
                t.at_capacity = True
                self.record("at-capacity", target=t.name,
                            db=round(t.db, 1),
                            db_range=round(max(dbs) - min(dbs), 2),
                            acts_spanned=acts[-1] - acts[0],
                            hit_rate=round(t.hit_rate, 3)
                            if t.hit_rate is not None else None,
                            mbm_gbps=round(t.mbm_bps / 1e9, 1),
                            mb=round(t.mb, 1), ml=round(t.ml, 1))

    def hurt_list(self):
        hurt = [t for t in self.tenants.values() if t.alive and self.is_hurt(t)]
        hurt.sort(key=lambda t: (-t.prio, t.cmt))
        return hurt

    def _improved(self, t, pre_db, pre_p99):
        """Shared 'did this tenant get better' predicate (v4 SLO-aware,
        used for the victim AND, v6, for each hurt_at_apply tenant in the
        benefit-count check): a fed tenant is judged on its OWN target --
        met target, or p99 fell >=IMPROVE_P99_FRAC; feed stale/absent
        falls back to the hardware criterion (dram_bound fell)."""
        if t.qos is not None and t.p99 is not None:
            return (t.p99 <= t.qos) or \
                   (pre_p99 is not None and t.p99 <= pre_p99 * (1 - IMPROVE_P99_FRAC))
        return pre_db - t.db >= IMPROVE_DB_PP

    def _new_hurt(self, p, v):
        """Priority-aware veto. v4: 'newly hurt' uses the unified is_hurt
        predicate, so a step pushing a fed tenant over ITS target is
        vetoed even if hardware counters look calm (the knee case). v6
        clause (d): ALSO count a fine-at-apply tenant whose ips fell
        > BYSTANDER_DROP_FRAC vs its pre-step ips, even if it never
        crossed the db gate -- closes the sub-gate blindness (bfs case,
        see module docstring) -- and additionally treat at-capacity
        tenants as bystanders via the ips clause ONLY (their db is
        meaningless, but a real ips collapse in them is still harm).
        Only equal-or-higher-priority tenants that were fine at apply time
        count (harm may flow down the ladder). Migration steps are
        unchanged (gate-only; fine_before there is a plain name list)."""
        kind = p["step"][0]
        if kind == "mig":
            return [n for n in p["fine_before"]
                    if self.tenants[n].alive and self.is_hurt(self.tenants[n])
                    and self.tenants[n].prio >= v.prio]
        out = []
        for n, pre in p["fine_before"].items():
            t = self.tenants[n]
            if not t.alive or t.prio < v.prio:
                continue
            gate_cross = self.is_hurt(t)
            pre_ips = pre.get("ips", 0.0)
            ips_drop = pre_ips > 0 and t.ips < (1 - BYSTANDER_DROP_FRAC) * pre_ips
            if gate_cross or ips_drop:
                out.append(n)
        for n, pre in p.get("atcap_before", {}).items():
            t = self.tenants[n]
            if not t.alive or t.prio < v.prio:
                continue
            pre_ips = pre.get("ips", 0.0)
            if pre_ips > 0 and t.ips < (1 - BYSTANDER_DROP_FRAC) * pre_ips:
                out.append(n)
        return out

    def verify(self):
        p = self.pending
        kind, target, level = p["step"]
        # migrations get a grace period: pages move + caches warm before
        # the step is judged
        if kind == "mig" and p.get("wait", 0) > 1:
            p["wait"] -= 1
            self.record("verify-wait", step=p["step"], remaining=p["wait"])
            return
        self.pending = None
        v = self.tenants[p["victim"]]
        improved = self._improved(v, p["pre_db"], p.get("pre_p99"))
        new_hurt = self._new_hurt(p, v)
        ok = improved and not new_hurt
        target_cost = None
        benefit_count = None
        # v6: MBA steps carry two more commit conditions -- the target's
        # cumulative cost is bounded (unless sanctioned), and the step must
        # have benefited more than just its own victim ("benefits many").
        # rsv steps keep the ORIGINAL criteria plus veto clause (d) above;
        # no cost/benefit check -- an rsv step doesn't spend anyone else's
        # value the way an MBA throttle does.
        if kind == "mba" and ok:
            sanctioned = p.get("sanctioned", False)
            hurt_at_apply = p.get("hurt_at_apply", [])
            improved_hurt = sum(
                1 for e in hurt_at_apply
                if self.tenants[e["name"]].alive
                and self._improved(self.tenants[e["name"]], e["db"], e.get("p99")))
            need = min(BENEFIT_MIN, len(hurt_at_apply))
            benefit_count = improved_hurt
            # v6.1: waive benefit-count when SANCTIONED, exactly as the cost
            # bound is waived. Priority already authorizes spending the donor
            # (harm flows down); requiring "benefits many" on top vetoed
            # legitimate L1 throttles that helped the priority victim but not
            # a second tenant (run-5: stream->50 rolled back at benefit=1),
            # collapsing L1 recovery. Unsanctioned (equal-prio, e.g. bfs)
            # steps keep BOTH guards, so the run-3/4 floor fix is intact.
            if not sanctioned and improved_hurt < need:
                ok = False
            tgt = self.tenants.get(target)
            baseline = self.mba_pre_ips.get(target)
            if baseline and baseline > 0 and tgt is not None:
                target_cost = max(0.0, 1.0 - tgt.ips / baseline)
            else:
                target_cost = 0.0
            # v8 COHERENCE: an ips-cost is attributed to OUR throttle only if the
            # target's mbm also dropped (our MBA cap bit). ips-down + mbm-flat =
            # a concurrent phase change, not our doing -> not our cost.
            mbm_base = self.mba_pre_mbm.get(target)
            mbm_coherent = (mbm_base is not None and mbm_base > 0 and tgt is not None
                            and tgt.mbm_bps < mbm_base * (1 - COHERENCE_MBM_DROP))
            cost_real = (target_cost > TARGET_COST_FRAC) and mbm_coherent
            if not sanctioned and cost_real:
                ok = False
        if ok:
            self.record("commit", step=p["step"], victim=p["victim"],
                        target_cost=round(target_cost, 3)
                        if target_cost is not None else None,
                        benefit_count=benefit_count)
            if kind == "mba":
                self.mba_beneficiary[target] = p["victim"]
            if kind == "mig":
                # departure = scene change: release the migrant's node-0
                # levers and give every previously-blocked step a fresh
                # trial against the lighter crowd (doc semantics restored)
                self.migrated[v.name] = p["chunk"]
                if v.name in self.alloc:
                    self.alloc.pop(v.name)
                    self.slice_order.remove(v.name)
                    self.apply_masks()
                if self.mba.get(v.name, 100) < 100:
                    self.mba[v.name] = 100
                    self.apply_mba(v.name)
                    self.mba_pre_ips.pop(v.name, None)
                    self.mba_pre_mbm.pop(v.name, None)
                self.blocked.clear()
            return
        # rollback + block (keyed per victim: the same physical step that
        # failed to help one victim is a fresh, legitimate trial for another)
        ttl = BLOCK_TTL_SECS
        if kind == "rsv":
            self.alloc[target] = level - 1
            if self.alloc[target] <= 0:
                self.alloc.pop(target, None)
                if target in self.slice_order:
                    self.slice_order.remove(target)
            self.apply_masks()
        elif kind == "mba":
            self.mba[target] = p["prev_mba"]
            self.apply_mba(target)
            if p["prev_mba"] >= 100:
                self.mba_pre_ips.pop(target, None)
                self.mba_pre_mbm.pop(target, None)
        else:  # mig
            self.rollback_migration(p)
            ttl = MIG_BLOCK_TTL_SECS
        self.blocked[f"{p['victim']}|{p['step']}"] = time.monotonic() + ttl
        self.record("rollback", step=p["step"], victim=p["victim"],
                    improved=improved, new_hurt=new_hurt,
                    target_cost=round(target_cost, 3)
                    if target_cost is not None else None,
                    benefit_count=benefit_count)

    def propose(self):
        now = time.monotonic()
        self.blocked = {k: exp for k, exp in self.blocked.items()
                        if now < exp}
        # at-capacity tenants are neither hurt nor fine: their saturated
        # stall signal cannot carry veto information (throttling one moves
        # its db by mechanics, not by suffering). Their protections are
        # the MBA floor and release() -- documented in AT_CAP_* notes.
        # v6: fine_before now carries pre-step ips alongside db (veto
        # clause d needs both), and at-capacity tenants get their own
        # ips-only bystander baseline (atcap_before) since their db is
        # meaningless but a real ips collapse in them is still harm.
        fine_before = {n: {"db": t.db, "ips": t.ips}
                       for n, t in self.tenants.items()
                       if t.alive and not self.is_hurt(t) and not t.at_capacity}
        atcap_before = {n: {"ips": t.ips}
                        for n, t in self.tenants.items()
                        if t.alive and t.at_capacity}
        for v in self.hurt_list():
            # migrated tenants live on the destination socket -- node-0
            # levers don't apply to them; if one is hurt over there,
            # there is nothing further to do (log-and-hold via frontier)
            if v.name in self.migrated:
                continue
            # axis 1: grow v's exclusive reservation one way
            cur = self.alloc.get(v.name, 0)
            total = sum(self.alloc.values())
            step = ("rsv", v.name, cur + 1)
            if (cur < RSV_MAX_WAYS and self.nbits - (total + 1) >= SHARED_MIN_WAYS
                    and f"{v.name}|{step}" not in self.blocked):
                if v.name not in self.slice_order:
                    self.slice_order.append(v.name)
                self.alloc[v.name] = cur + 1
                self.apply_masks()
                self.pending = {"step": step, "victim": v.name,
                                "pre_db": v.db, "pre_cmt": v.cmt,
                                "pre_p99": v.p99,
                                "fine_before": fine_before,
                                "atcap_before": atcap_before}
                self.record("apply", step=step, victim=v.name)
                return True
            # axis 2: throttle the top-MBM peer one notch (migrated peers
            # excluded: their MBM is destination-socket traffic now).
            # v5 ESCALATION: reaching this line means axis 1 offered no
            # admissible step for this victim THIS cycle (tried-and-
            # blocked, capped, or pool at floor) -- the reserve->throttle
            # ladder makes throttling admissible regardless of the
            # victim's own stall shape. v3's bwstall>=10pp precondition
            # is retired: it tested the victim's MLP (its coding style),
            # not the cause of harm -- exp15a canneal, a one-load-in-
            # flight pointer chaser suffering pure DRAM-queueing latency,
            # can never look "bandwidth-stalled", yet MBA on the
            # aggressor is exactly its remedy. bwstall stays logged as a
            # diagnostic. A wrong escalation costs one verified rollback.
            if True:
                # v6 BOUNDED-EXCHANGE THROTTLING (replaces v5.2's
                # eligibility filter; see module docstring for the bfs
                # violation this fixes). v5.2 required a target to be
                # "not hurt, at-capacity, or sanctioned" before it could be
                # throttled at all -- but "not hurt" is a GATE read, and
                # the gate can miss real, sub-threshold suffering (bfs sat
                # at 12.4-19.1pp, always under GATE_PP=20). v6 drops
                # eligibility entirely: ANY alive, non-migrated peer is a
                # candidate target. What used to be an entry gate is now
                # an EXIT bound, checked in verify() against a signal
                # throttling cannot flatter (ips): sanctioned targets
                # (strictly lower priority than the victim -- harm may
                # flow down, L1 semantics preserved) are exempt from the
                # cost bound; everyone else's cumulative ips cost is
                # capped at TARGET_COST_FRAC. at_capacity is no longer an
                # exemption either -- it is an inference about a tenant's
                # NATURE (streamer vs suffering reuser), not an
                # authorization to spend its value for free.
                peers = [t for t in self.tenants.values()
                         if t.alive and t.name != v.name
                         and t.name not in self.migrated]
                if peers:
                    # v9 NATURE-ORDERED throttle (replaces raw top-MBM). Only
                    # CHEAP or PRIORITY-AUTHORIZED donors are throttle targets,
                    # and we ITERATE among them (a blocked/at-floor donor
                    # advances to the next). Raw top-MBM was wrong twice: a
                    # high-mbm high-hit DUAL can MASK the real streamer (envA:
                    # npb_cg mbm40 masked npb_ft mbm17) AND, once its throttle
                    # blocked, the single-top pick STUCK and never reached it.
                    #   tier 0 = low-hit high-mbm STREAMER = FREE donor
                    #            (throttling it costs it ~nothing: no reuse)
                    #   tier 1 = lower-priority peer = SANCTIONED (harm flows
                    #            down, L1; throttleable regardless of hit, so a
                    #            high-hit lower-prio DUAL stays a valid target)
                    # An equal/higher-prio CACHE-USER (tier 2) is NOT a target:
                    # throttling it is zero-sum (Alita quota trap) and violates
                    # the triage contract -- probing it just wastes a rollback.
                    # No admissible tier-0/1 donor => stand down (frontier/
                    # migration). verify() stays the authority on the donor
                    # probes we DO make (a streamer probe that doesn't relieve
                    # the victim -- streamer wasn't the aggressor -- rolls back).
                    def _donor_tier(t):
                        if (t.hit_rate is not None
                                and t.hit_rate <= AT_CAP_HITRATE
                                and t.mbm_bps >= AT_CAP_MIN_MBM_BPS):
                            return 0                     # streamer = free donor
                        if t.prio < v.prio:
                            return 1                     # sanctioned (harm down)
                        return None                      # tier 2: not a target
                    donors = [(t, _donor_tier(t)) for t in peers]
                    donors = [(t, k) for t, k in donors if k is not None]
                    donors.sort(key=lambda tk: (tk[1], -tk[0].mbm_bps))
                    raw_top = max(peers, key=lambda t: t.mbm_bps)  # old pick, cf
                    top = nxt = step = None
                    for cand, _tk in donors:
                        lvl = self.mba[cand.name]
                        idx = MBA_LEVELS.index(lvl) if lvl in MBA_LEVELS else 0
                        if idx + 1 >= len(MBA_LEVELS):     # at floor: exhausted
                            continue
                        cstep = ("mba", cand.name, MBA_LEVELS[idx + 1])
                        if f"{v.name}|{cstep}" in self.blocked:   # already tried
                            continue
                        top, nxt, step = cand, MBA_LEVELS[idx + 1], cstep
                        break
                    if top is not None:
                        prev = self.mba[top.name]
                        sanctioned = top.prio < v.prio
                        # hurt_at_apply: v6 benefit-count bookkeeping -- who
                        # counted as hurt (and at what db/p99) BEFORE this
                        # notch, so verify() can measure how many got better.
                        hurt_at_apply = [
                            {"name": h.name, "db": h.db,
                             "p99": h.p99 if h.qos is not None else None}
                            for h in self.hurt_list()]
                        if prev >= 100:
                            # baseline for the guard's causality test AND the
                            # v6 cumulative cost bound: the target's harm/work
                            # level BEFORE we ever touched it
                            self.mba_pre_db[top.name] = top.db
                            self.mba_pre_ips[top.name] = top.ips
                            self.mba_pre_mbm[top.name] = top.mbm_bps
                        self.mba[top.name] = nxt
                        self.apply_mba(top.name)
                        self.pending = {"step": step, "victim": v.name,
                                        "pre_db": v.db, "pre_cmt": v.cmt,
                                        "pre_p99": v.p99,
                                        "prev_mba": prev,
                                        "fine_before": fine_before,
                                        "atcap_before": atcap_before,
                                        "sanctioned": sanctioned,
                                        "target_pre_ips": top.ips,
                                        "hurt_at_apply": hurt_at_apply}
                        # cf: measure the nature-ordering EFFECT -- did v9 pick a
                        # different target than raw top-MBM would have, its tier,
                        # and the signals (validates v9 vs the old pick offline).
                        cf = {"victim_hit": round(v.hit_rate, 3)
                                  if v.hit_rate is not None else None,
                              "victim_mbm_gbps": round(v.mbm_bps / 1e9, 3),
                              "target": top.name, "target_tier": _donor_tier(top),
                              "target_hit": round(top.hit_rate, 3)
                                  if top.hit_rate is not None else None,
                              "target_mbm_gbps": round(top.mbm_bps / 1e9, 3),
                              "target_prio": top.prio,
                              "raw_top": raw_top.name,
                              "raw_top_hit": round(raw_top.hit_rate, 3)
                                  if raw_top.hit_rate is not None else None,
                              "differs_from_raw": raw_top.name != top.name}
                        self.record("apply", step=step, victim=v.name,
                                    sanctioned=sanctioned, cf=cf)
                        return True
        return False

    def try_migration(self):
        """Frontier escalation (DECISION_LOGIC §4 case 2): migrate the
        lowest-CMT still-hurt victim to the destination socket, IF one is
        configured, observably quiet, and the candidate isn't blocked.
        Returns True if a migration step was applied."""
        if not self.mig:
            return False
        bw = self.dest_bandwidth()
        if bw >= MIG_QUIET_BW_BPS:
            self.record("mig-skip", reason="dest not quiet",
                        dest_bw_gbps=round(bw / 1e9, 2))
            return False
        now = time.monotonic()
        fine_before = [n for n, t in self.tenants.items()
                       if t.alive and not self.is_hurt(t)
                       and not t.at_capacity]
        for v in sorted(self.hurt_list(), key=lambda t: t.cmt):
            if v.name in self.migrated or not v.cores:
                continue
            key_prefix = f"{v.name}|('mig'"
            if any(k.startswith(key_prefix) and now < exp
                   for k, exp in self.blocked.items()):
                continue
            return self.apply_migration(v, fine_before)
        return False

    def guard_release(self):
        """Continuous guard (v4, from exp14 run 4): a committed throttle
        whose damage builds SLOWLY (near-saturation tenant, queue growth
        over minutes) passes its one verify window and then strangles the
        target unchecked -- observed: sphinx throttled to 10% for
        masstree, committed cleanly, collapsed to 2.4x its target over
        the round. Fix: the hurt scan already runs every cycle, so no
        throttle may PERSIST on a tenant the throttle is HURTING unless
        the harm is priority-sanctioned. v5.1 causality test: "hurting"
        means hurt SINCE the first notch (sphinx: fine at apply ->
        collapsed later), or a full gate's worth of NEW harm on top of a
        pre-existing hurt level. A target that was already gate-hurt
        before we touched it and has not materially worsened is not the
        guard's business -- exp15a can round: the guard unwound the
        working remedy at t=56 because the saturated polluter's
        mechanical +5.6pp rise read as "hurt". Unwind one notch per
        cycle while the condition holds. Returns True if it acted."""
        for name, lvl in self.mba.items():
            if lvl >= 100:
                continue
            t = self.tenants[name]
            # at-capacity targets keep their throttles -- their db rising
            # under MBA is the mechanical response of a saturated
            # streamer, not slow-compounding suffering.
            if not t.alive or t.at_capacity or not self.is_hurt(t):
                continue
            pre = self.mba_pre_db.get(name)
            if pre is not None and pre >= GATE_PP and t.db - pre < GATE_PP:
                # already hurt before the first notch, and the throttle
                # has not added a gate's worth of new harm: keep it
                continue
            benef = self.mba_beneficiary.get(name)
            if benef is not None and self.tenants[benef].alive \
                    and self.tenants[benef].prio > t.prio:
                continue        # harm flows down: sanctioned, keep
            idx = MBA_LEVELS.index(lvl)
            self.mba[name] = MBA_LEVELS[idx - 1]
            self.apply_mba(name)
            if self.mba[name] >= 100:
                self.mba_pre_ips.pop(name, None)
                self.mba_pre_mbm.pop(name, None)
            # block the step that put it there so propose() doesn't
            # immediately re-apply what the guard just unwound
            if benef:
                self.blocked[f"{benef}|('mba', '{name}', {lvl})"] = \
                    time.monotonic() + BLOCK_TTL_SECS
            self.record("guard-release", target=name, reason="db",
                        to_level=self.mba[name], beneficiary=benef,
                        target_p99=t.p99, target_qos=t.qos,
                        target_db=round(t.db, 1))
            return True
        # v6 SIGNAL-SUPPRESSION GUARD (see module docstring: throttling can
        # suppress the very gate signal that would trigger the check
        # above -- a starved tenant issues fewer requests and stalls less,
        # so its db can FALL under its own throttle). This pass ignores
        # is_hurt/at_capacity entirely and watches ips -- a work-rate
        # signal throttling cannot flatter -- directly against the
        # pre-first-throttle baseline, sustained across RELEASE_CYCLES
        # consecutive decide cycles (same counting pattern as release()).
        for name, lvl in self.mba.items():
            if lvl >= 100:
                continue
            t = self.tenants[name]
            baseline = self.mba_pre_ips.get(name)
            if not t.alive or not baseline or baseline <= 0:
                self.rate_guard_count[name] = 0
                continue
            # SANCTIONED THROTTLES ARE NOT AUTO-RELEASED (mirror the db pass
            # above): a throttle authorized because harm flows DOWN to a
            # strictly-lower-priority donor is meant to hold -- the donor's
            # ips deficit is the intended cost, not slow-compounding harm to
            # discover. Without this, L1 flooring the prio-0 streamer would
            # oscillate: floor -> ips collapses (because floored) -> rate
            # guard releases -> re-floor. Breaks P2 (L1 == prior behavior).
            benef = self.mba_beneficiary.get(name)
            if benef is not None and self.tenants[benef].alive \
                    and self.tenants[benef].prio > t.prio:
                self.rate_guard_count[name] = 0
                continue
            if t.ips < (1 - RATE_GUARD_FRAC) * baseline:
                self.rate_guard_count[name] = self.rate_guard_count.get(name, 0) + 1
            else:
                self.rate_guard_count[name] = 0
                continue
            if self.rate_guard_count[name] < RELEASE_CYCLES:
                continue
            idx = MBA_LEVELS.index(lvl)
            self.mba[name] = MBA_LEVELS[idx - 1]
            self.apply_mba(name)
            if self.mba[name] >= 100:
                self.mba_pre_ips.pop(name, None)
                self.mba_pre_mbm.pop(name, None)
            self.rate_guard_count[name] = 0
            benef = self.mba_beneficiary.get(name)
            if benef:
                self.blocked[f"{benef}|('mba', '{name}', {lvl})"] = \
                    time.monotonic() + BLOCK_TTL_SECS
            self.record("guard-release", target=name, reason="rate",
                        to_level=self.mba[name], beneficiary=benef,
                        target_ips=round(t.ips, 1),
                        baseline_ips=round(baseline, 1))
            return True
        return False

    def release(self):
        """At most one release per cycle, only when nothing else changed."""
        for name in list(self.alloc):
            t = self.tenants[name]
            quiet = (not t.alive) or not self.is_hurt(t)
            self.quiet[name] = self.quiet.get(name, 0) + 1 if quiet else 0
            if self.quiet[name] >= RELEASE_CYCLES and self.alloc[name] > 0:
                self.alloc[name] -= 1
                if self.alloc[name] == 0:
                    self.alloc.pop(name)
                    self.slice_order.remove(name)
                self.apply_masks()
                self.quiet[name] = 0
                self.record("release", lever="rsv", target=name)
                return
        for name, lvl in self.mba.items():
            if lvl >= 100:
                continue
            t = self.tenants[name]
            if not t.alive:
                idx = MBA_LEVELS.index(lvl)
                self.mba[name] = MBA_LEVELS[idx - 1]
                if os.path.isdir(t.grp):
                    self.apply_mba(name)
                if self.mba[name] >= 100:  # v6 cleanup, mirrors mba_pre_db
                    self.mba_pre_ips.pop(name, None)
                    self.mba_pre_mbm.pop(name, None)
                self.record("release", lever="mba", target=name)
                return

    def run(self):
        self.t0 = time.monotonic()
        for t in self.tenants.values():
            t.start_perf()
        log(f"coeffd3 v9 up (v8 + nature-ordered throttle: tier-0 streamer / "
            f"tier-1 sanctioned donors, iterate, stand down on tier-2): "
            f"{len(self.tenants)} tenants, {self.nbits} "
            f"ways, gate={GATE_PP}pp, mba_trigger={MBA_TRIGGER_PP}pp")
        last_decide = 0.0
        try:
            while not self.stop:
                for t in self.tenants.values():
                    if t.alive:
                        t.sample()
                now = time.monotonic()
                # warmup: perf -I 2000 needs a few intervals before the
                # tail-means are real; deciding on all-zero dram_bound
                # produced a bogus "settled healed at t=0" in run 1.
                if now - last_decide >= DECIDE_SECS and now - self.t0 >= 15:
                    last_decide = now
                    self.update_at_capacity()
                    self.record("scan", tenants=self.snapshot(),
                                alloc=dict(self.alloc), mba=dict(self.mba))
                    if self.observe:            # sense-only: skip all actuation
                        time.sleep(SAMPLE_SECS)
                        continue
                    try:
                        if self.pending:
                            self.settled = None
                            self.verify()
                        elif self.guard_release():
                            self.settled = None
                        elif self.propose():
                            self.settled = None
                        else:
                            # frontier escalation BEFORE settling: no
                            # RDT lever is admissible for anyone -- try
                            # migrating the lowest-CMT hurt victim to a
                            # quiet destination socket (§4 case 2)
                            if self.hurt_list() and self.try_migration():
                                self.settled = None
                                time.sleep(SAMPLE_SECS)
                                continue
                            self.release()
                            # settled-state detection: nothing pending,
                            # nothing proposable, nothing migratable.
                            # "healed" = hurt list empty; "frontier" =
                            # victims remain but every step is blocked/
                            # capped and migration is exhausted too. The
                            # daemon stays up, scanning cheaply, and
                            # re-engages on scene change.
                            state = "healed" if not self.hurt_list() else "frontier"
                            if state != self.settled:
                                self.settled = state
                                self.record("settled", state=state,
                                            alloc=dict(self.alloc),
                                            mba=dict(self.mba),
                                            hurt=[t.name for t in self.hurt_list()])
                                log(f"SETTLED ({state}): alloc={self.alloc} "
                                    f"mba={ {k: v for k, v in self.mba.items() if v < 100} } "
                                    f"hurt={[t.name for t in self.hurt_list()]}")
                    except Exception:
                        import traceback
                        self.record("error", trace=traceback.format_exc())
                        raise
                time.sleep(SAMPLE_SECS)
        finally:
            log("coeffd3 shutting down: restoring masks/MBA, stopping perf")
            self.restore_all()
            for t in self.tenants.values():
                t.stop_perf()
            self.decisions.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--observe", action="store_true",
                    help="sense + emit scan records but never actuate (classification screen)")
    ap.add_argument("--perf-attach", choices=("pid", "cores"), default=PERF_ATTACH,
                    help="how perf attaches per tenant: 'pid' (default, historical) "
                         "or 'cores' -- use 'cores' whenever the registered pid is a "
                         "wrapper shell around the real workload, which -p undercounts")
    args = ap.parse_args()
    globals()["PERF_ATTACH"] = args.perf_attach
    if os.geteuid() != 0:
        sys.exit("coeffd3 must run as root (resctrl + perf)")
    Daemon(args.config, args.out, observe=args.observe).run()


if __name__ == "__main__":
    main()
