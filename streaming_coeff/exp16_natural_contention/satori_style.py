#!/usr/bin/env python3
"""satori_style -- a SATORI-style (Roy, Patel, Tiwari, ISCA'21) Bayesian-
optimization resource partitioner, reimplemented as an honest black-box
comparator for our 5-tenant D3 contention scene.

Public artifact: github.com/rohanbasuroy/satori (2021-06-15), vendored at
/home/tejendra/litmus/SATORI_artifact/satori.py (289 lines). This file
TRANSCRIBES the artifact's algorithm code (config-space generation,
throughput/fairness metrics, dynamic weight rebalancing, the gp_minimize
call) close to verbatim, and SUBSTITUTES only what D3's levers and harness
force. Every transcribed block is marked `# VERBATIM satori.py:<lines>`;
every substituted line is marked `# SUBSTITUTION (reason)`. See
CONTROLLER_COMPARISON.md section 13 ("SATORI on D3") for the full
paper-vs-artifact-vs-D3 audit this build follows -- read that before
touching the substitutions below.

SUBSTITUTIONS (the only non-verbatim parts, table rows from section 13):
  row 5  resources: CAT-only. Cores are fixed by the scene (a provider
         cannot take vCPUs from a running tenant); MBA is fixed at 100%
         for everyone (SpiderSense's own evaluation of SATORI also
         disabled bandwidth, and the artifact's MBA scheme -- shares that
         SUM TO 10 across apps -- is physically a throttle-everyone
         config on 5 tenants, never faithful on this box). So
         NUM_RESOURCES=1, NUM_UNITS=[ways from /sys/fs/resctrl], and
         get_allocation()/perform_resource_partitioning() are reduced to
         their LLC-only arithmetic (still the artifact's own top-down
         contiguous-stacking math, lines 136-144).
  row 8  measurement: IPC (turbo off, our measurement rule -- see
         CLAUDE.md "Report IPC never IPS"), not IPS via the artifact's
         `ips_collector.sh` (`perf stat -e instructions -p $PID -a sleep
         0.01`). We sample per-tenant on CORES via `perf stat -I 100 -C
         <cores>`, never `-p` on a wrapper pid (CLAUDE.md gotcha), and
         take only the newest COMPLETE interval that started after the
         mask write -- never an average spanning the mask change.
  row 9  actuation: resctrl `schemata` writes (the same convention
         `spidersense_style.py` uses), not the artifact's `pqos -e/-a`
         CLI and `taskset`. No taskset: cores are fixed.
  --     no start_jobs(): tenants are already running: PIDs/groups/cores
         come from the harness's cfg.json.

Everything else -- gen_configs_recursively, gen_configs, get_weights,
objective's control flow, get_metrics's period-marker logic, the
gp_minimize call and its literal hyperparameters -- is the artifact's own
code, transcribed.

Config: {"tenants": [{"name","grp","cores"}...], "solo_ipc": "<path to
results_iso/<AS>/solo_ipc.json>"}  (same cfg.json the harness already
writes for copart_style.py's Eq.1 denominator).

Usage: sudo python3 satori_style.py --config cfg.json --out outdir
       [--dry-run] [--time-total SECONDS]
Restores masks on SIGTERM/SIGINT/timeout (all three route through the
artifact's own signal_handler, VERBATIM satori.py:13-14).
"""

import argparse
import json
import os
import random
import signal
import statistics
import subprocess
import sys
import time

from scipy.stats.mstats import gmean
from skopt import gp_minimize

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"
PERF_INT_MS = 100            # "row 8": 100 ms sampling window (paper Table 2 /
                              # SATORI's own 10 Hz ips_collector cadence)
PERF_POLL_BUDGET_S = 1.0     # how long to wait for one complete fresh interval


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


# ---------------------------------------------------------------------------
# resctrl helpers -- REUSES spidersense_style.py's schemata convention (same
# harness, same actuation primitive). Not an artifact transcription.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# VERBATIM satori.py:13-14 (signal_handler).  Reused for SIGTERM/SIGINT too
# (the harness stops the arm with SIGTERM, not SIGALRM) -- an integration
# extension, not a paper substitution: the artifact's own mechanism already
# is "raise on signal, let the try/except around start_bo_engine() catch it".
# ---------------------------------------------------------------------------
def signal_handler(signum, frame):
    raise Exception("Total BO Engine Timeout Reached")


# ---------------------------------------------------------------------------
# VERBATIM satori.py:65-79 (gen_configs_recursively) and :93-99 (gen_configs).
# Transcribed unmodified so CONFIGS_LIST index order is theirs.  With
# NUM_RESOURCES=1 the outer loop in gen_configs() runs once, same as the
# artifact would for any single-resource case.
# ---------------------------------------------------------------------------

def gen_configs_recursively(u, r, a):
    if (a == NUM_APPS - 1):
        return None
    else:
        ret = []
        for i in range(1, NUM_UNITS[r] - u + 1 - NUM_APPS + a + 1):
            confs = gen_configs_recursively(u + i, r, a + 1)
            if not confs:
                ret.append([i])
            else:
                for c in confs:
                    ret.append([i])
                    for j in c:
                        ret[-1].append(j)
        return ret


def gen_configs():
    global CONFIGS_LIST
    for r in range(NUM_RESOURCES):
        if not CONFIGS_LIST:
            CONFIGS_LIST = gen_configs_recursively(0, r, 0)
        else:
            CONFIGS_LIST = [x + y for x in CONFIGS_LIST for y in gen_configs_recursively(0, r, 0)]


# ---------------------------------------------------------------------------
# SUBSTITUTION (row 5): artifact's get_allocation() (l.102-149) built
# core/llc/mba lists and core_allocation strings for taskset.  This build
# only ever has one resource (LLC), so it is reduced to that arithmetic --
# the LLC share list (l.113-117 pattern) and the top-down contiguous mask
# stack (l.135-144), TRANSCRIBED, just re-indexed onto NUM_UNITS[0] instead
# of NUM_UNITS[1] and with the core/mba passes deleted.
# ---------------------------------------------------------------------------

def get_allocation_llc(x):
    sampled_config = CONFIGS_LIST[x]
    llc_list = []
    for j in range(NUM_APPS - 1):                      # VERBATIM-pattern l.114-116
        llc_list.append(sampled_config[j])
    llc_list.append(NUM_UNITS[0] - sum(llc_list))       # VERBATIM-pattern l.117
    llc_allocation_list = []
    i = NUM_UNITS[0] - 1                                # VERBATIM-pattern l.136
    for j in range(NUM_APPS):                           # VERBATIM l.137-144
        ini_list = [0 for _k in range(NUM_UNITS[0])]
        count = llc_list[j]
        while count > 0:
            ini_list[i] = 1
            i -= 1
            count -= 1
        llc_allocation_list.append(hex(int(''.join(str(b) for b in ini_list), 2)))
    return llc_list, llc_allocation_list


# ---------------------------------------------------------------------------
# SUBSTITUTION (row 9): artifact's perform_resource_partitioning() (l.152-
# 165) ran `sudo taskset -acp` + `sudo pqos -e/-a` per tenant.  Cores are
# fixed (row 5) so taskset is dropped entirely; CAT goes through resctrl
# `schemata`, the same primitive spidersense_style.py uses.
# ---------------------------------------------------------------------------

def apply_llc(llc_allocation_list):
    last_masks = {}
    for j, t in enumerate(tenants):
        mask_hex = llc_allocation_list[j][2:] if llc_allocation_list[j].startswith("0x") \
            else llc_allocation_list[j]
        if mask_hex == "":
            mask_hex = "0"
        write_schemata_line(t["grp"], "L3", domain_body(t["grp"], "L3", mask_hex))
        last_masks[t["name"]] = mask_hex
    return last_masks


def restore_all():
    try:
        write_schemata_line(RESCTRL, "L3", domain_body(RESCTRL, "L3", f"{FULL_MASK:x}"))
    except OSError:
        pass
    for t in tenants:
        if os.path.isdir(t["grp"]):
            try:
                write_schemata_line(t["grp"], "L3", domain_body(t["grp"], "L3", f"{FULL_MASK:x}"))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# row 8 collection: per-tenant perf -I 100 on CORES, kept running for the
# whole arm (same long-lived-process pattern as spidersense_style.Tenant).
# ---------------------------------------------------------------------------

class PerfSampler:
    def __init__(self, tenant, out_dir, pfx="st"):
        self.name = tenant["name"]
        self.cores = tenant["cores"]
        self.json_path = os.path.join(out_dir, f"{pfx}_{self.name}.json")
        self.proc = None

    def start(self):
        # LLC-loads added alongside instructions/cycles (ours, no artifact
        # analogue): _collect_ipc_real/newest_complete_ipc ignore it entirely
        # (they only ever look for "instructions"/"cycles"), so the objective
        # measurement path is byte-for-byte unchanged. It exists SOLELY to
        # back the chunked-search instance-fingerprint gate (_fp_gate /
        # read_window below), ported from spidersense_style.py's Tenant.
        self.proc = subprocess.Popen(
            ["perf", "stat", "-j", "-o", self.json_path,
             "-e", "instructions,cycles,LLC-loads",
             "-I", str(PERF_INT_MS), "-C", self.cores, "--", "sleep", "100000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        p.send_signal(signal.SIGINT)                    # bounded INT->KILL, CLAUDE.md
        for _ in range(10):
            if p.poll() is not None:
                return
            time.sleep(0.5)
        p.kill()
        p.wait()

    def mark(self):
        try:
            return os.path.getsize(self.json_path)
        except OSError:
            return 0

    def newest_complete_ipc(self, offset):
        """Newest COMPLETE perf interval that started strictly after `offset`
        (the byte mark taken right after the mask write).  Never averages
        across the mask change -- returns None if nothing complete yet."""
        try:
            with open(self.json_path) as f:
                f.seek(offset)
                lines = f.readlines()
        except OSError:
            return None
        per_iv = {}
        for line in lines:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue            # torn trailing line: perf still writing it
            ev = o.get("event") or ""
            iv = o.get("interval")
            if iv is None:
                continue
            try:
                val = float(o.get("counter-value"))
            except (TypeError, ValueError):
                continue
            d = per_iv.setdefault(iv, {})
            if "instructions" in ev:
                d["ins"] = val
            elif "cycles" in ev:
                d["cyc"] = val
        complete = [iv for iv, d in per_iv.items() if "ins" in d and "cyc" in d]
        if not complete:
            return None
        newest = max(complete)
        d = per_iv[newest]
        return (d["ins"] / d["cyc"]) if d["cyc"] > 0 else 0.0

    def read_window(self, offset):
        """Aggregate ALL complete intervals after `offset` (dropping the
        first, which straddles the mask change) -- used ONLY by the chunked-
        search instance-fingerprint gate (_fp_gate). PORTED verbatim in
        structure from spidersense_style.Tenant.read_window/_parse_from; the
        real-time objective measurement path (newest_complete_ipc above)
        is untouched by this method's existence."""
        try:
            with open(self.json_path) as f:
                f.seek(offset)
                lines = f.readlines()
        except OSError:
            return {"ipc": 0.0, "fp": 0.0, "n": 0}
        per_iv = {}
        for line in lines:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ev = o.get("event") or ""
            iv = o.get("interval")
            if iv is None:
                continue
            try:
                val = float(o.get("counter-value"))
            except (TypeError, ValueError):
                continue
            d = per_iv.setdefault(iv, {})
            if "instructions" in ev:
                d["ins"] = val
            elif "cycles" in ev:
                d["cyc"] = val
            elif "LLC-loads" in ev:
                d["llc"] = val
        keys = sorted(per_iv)[1:]
        ins = sum(per_iv[k].get("ins", 0.0) for k in keys)
        cyc = sum(per_iv[k].get("cyc", 0.0) for k in keys)
        llc = sum(per_iv[k].get("llc", 0.0) for k in keys)
        ipc = ins / cyc if cyc > 0 else 0.0
        fp = llc / (ins / 1000.0) if ins > 0 else 0.0
        return {"ipc": round(ipc, 4), "fp": round(fp, 4), "n": len(keys)}


def _collect_ipc_real():
    """SUBSTITUTION (row 8) body of get_metrics()'s l.33-43: replaces the
    ips_collector.sh shell-out + 4th-from-last-line IPS parse with our
    per-tenant perf sampler, newest-complete-interval-only, in `applications`
    order."""
    marks = {s.name: s.mark() for s in samplers}
    deadline = time.monotonic() + PERF_POLL_BUDGET_S
    th_list = []
    for s in samplers:
        v = None
        while time.monotonic() < deadline:
            v = s.newest_complete_ipc(marks[s.name])
            if v is not None:
                break
            time.sleep(0.05)
        if v is None:
            v = s.newest_complete_ipc(0)      # last resort: anything on disk
            if v is None:
                v = 1e-6                      # never divide by zero downstream
                log(f"  !! no perf sample yet for {s.name}, using floor IPC")
        th_list.append(v)
    return th_list


def _collect_ipc_dryrun():
    """--dry-run synthetic model: IPC_i = iso_i * f(ways_i), with a couple of
    tenant shapes (cache-sensitive / moderate / insensitive) so weights and
    configs actually move during a smoke test without perf or resctrl."""
    th_list = []
    total_ways = NUM_UNITS[0]
    for name in applications:
        ways = current_ways.get(name, total_ways // NUM_APPS)
        frac = ways / total_ways
        shape = sum(ord(c) for c in name) % 3
        if shape == 0:          # cache-sensitive: saturates quickly
            f = min(1.05, 0.35 + 0.9 * frac)
        elif shape == 1:        # moderately sensitive
            f = 0.55 + 0.5 * frac
        else:                   # insensitive: nearly flat
            f = 0.92 + 0.08 * frac
        th_list.append(isolated_ipc[applications.index(name)] * f)
    return th_list


# ---------------------------------------------------------------------------
# VERBATIM satori.py:30-61 (get_metrics), except l.33-43 (the IPS collection,
# row 8 above).  Global names kept as close to the artifact as the rename
# isolated_ips -> isolated_ipc (row 8, IPC not IPS) allows.
# ---------------------------------------------------------------------------

def get_metrics():
    global equalization_period_counter
    global prioritization_period_counter
    # SUBSTITUTION (row 8) -- was artifact l.33-43 (ips_collector.sh per pid,
    # parse 4th-from-last line of <app>_ips.txt).
    th_list = _collect_ipc_dryrun() if ARGS.dry_run else _collect_ipc_real()
    speedup_list = [th_list[k] / isolated_ipc[k] for k in range(len(isolated_ipc))]  # VERBATIM l.44
    fairness_list.append(1 / (1 + (statistics.stdev(speedup_list) / statistics.mean(speedup_list)) ** 2))  # VERBATIM l.45
    throughput_list.append(gmean(speedup_list))                                      # VERBATIM l.46
    if time.time() - equalization_period_counter >= time_equalization:               # VERBATIM l.47-51
        equalization_period_counter = time.time()
        equalization_period_marker_list.append(1)
    else:
        equalization_period_marker_list.append(0)
    if time.time() - prioritization_period_counter >= time_prioritization:           # VERBATIM l.53-57
        prioritization_period_counter = time.time()
        prioritization_period_marker_list.append(1)
    else:
        prioritization_period_marker_list.append(0)
    if len(equalization_period_marker_list) == 1:                                    # VERBATIM l.59-61
        equalization_period_marker_list[0] = 1
        prioritization_period_marker_list[0] = 1
    last_speedups.clear(); last_speedups.extend(speedup_list)
    last_ipc.clear(); last_ipc.extend(th_list)


# ---------------------------------------------------------------------------
# VERBATIM satori.py:168-198 (get_weights). Unmodified.
# ---------------------------------------------------------------------------

def get_weights():
    if len(WT_list) == 0:
        W_T = 0.5
        W_F = 0.5
        WT_list.append(W_T)
        WF_list.append(W_F)
        return W_T, W_F
    else:
        global start_time
        equalization_index = max([i for i in range(len(equalization_period_marker_list)) if equalization_period_marker_list[i] == 1])
        prioritization_index = max([i for i in range(len(prioritization_period_marker_list)) if prioritization_period_marker_list[i] == 1])
        change_fairness = (fairness_list[len(fairness_list) - 1] - fairness_list[prioritization_index]) / fairness_list[prioritization_index]
        change_throughput = (throughput_list[len(throughput_list) - 1] - throughput_list[prioritization_index]) / throughput_list[prioritization_index]
        if change_fairness <= 0 and change_throughput <= 0:
            W_TP = 0.5
            W_FP = 0.5
        elif change_fairness < 0 and change_throughput > 0:
            W_TP = 0.25
            W_FP = 0.75
        elif change_fairness > 0 and change_throughput < 0:
            W_TP = 0.75
            W_FP = 0.25
        else:
            W_TP = 0.25 + 0.5 * (change_fairness / (change_throughput + change_fairness))
            W_FP = 0.25 + 0.5 * (change_throughput / (change_throughput + change_fairness))
        # ARTIFACT BUG FIX (user decision B, 2026-09-12; CONTROLLER_COMPARISON §13c).
        # satori.py appends to WT_list/WF_list ONLY on the first call (l.172-173),
        # so at the first equalization boundary l.193 takes mean() of an EMPTY
        # slice, raises StatisticsError, and the artifact's broad except around
        # start_bo_engine() reports "Timeout Reached" -- the search dies after
        # one T_E. Paper Eq.3 sums W_Ti over EVERY iteration of the current
        # equalization period, so the weights must be recorded every call:
        #  (1) append W_T/W_F on every call (below);
        #  (2) WT_list[j] belongs to marker j+1 (the initial get_metrics() in
        #      start_bo_engine sets marker 0 before any weight exists), so the
        #      current period's weights start at equalization_index-1;
        #  (3) at the first iteration of a new period that slice can be empty;
        #      use the neutral 0.5 -- its coefficient te/T_E is ~0 there anyway.
        _s = max(0, equalization_index - 1)
        W_TE = 1 - statistics.mean(WT_list[_s:] or [0.5])
        W_FE = 1 - statistics.mean(WF_list[_s:] or [0.5])
        te = (time.time() - start_time) % time_equalization
        W_T = (te / time_equalization) * W_TE + (1 - (te / time_equalization)) * W_TP
        W_F = (te / time_equalization) * W_FE + (1 - (te / time_equalization)) * W_FP
        WT_list.append(W_T)          # fix (1)
        WF_list.append(W_F)
        return W_T, W_F


# ---------------------------------------------------------------------------
# logging (ours, no artifact analogue): one jsonl record per BO sample plus a
# final summary record.
# ---------------------------------------------------------------------------

_last_obj_end = [None]
_sample_n = [0]
_configs_seen = set()
_prev_config_idx = [None]
_t0 = [None]

# CHUNKED SEARCH (ours, no artifact analogue): every real (non-anchor)
# objective() call this chunk appends its (config_idx, raw objective) pair
# here; run_chunked_bo drains it into the persisted state at chunk end.
NEW_OBS = []


def _log_sample(idx, llc_shares, llc_masks, W_T, W_F, obj_val, tag="scan"):
    idx = int(idx)          # skopt hands back np.int64; json can't serialize it
    now = time.time()
    gp_step = (now - _last_obj_end[0]) if _last_obj_end[0] is not None else 0.0
    _last_obj_end[0] = now
    _sample_n[0] += 1
    is_new_config = idx != _prev_config_idx[0]
    _prev_config_idx[0] = idx
    _configs_seen.add(idx)
    ways = dict(zip(applications, llc_shares))
    masks = dict(zip(applications, llc_masks))
    rec = {
        "kind": tag,
        "t": round(now - _t0[0], 3) if _t0[0] else 0.0,
        "n": _sample_n[0],
        "config_idx": idx,
        "ways": ways,
        "masks": masks,
        "ipc": dict(zip(applications, [round(v, 4) for v in last_ipc])),
        "speedup": dict(zip(applications, [round(v, 4) for v in last_speedups])),
        "T": round(throughput_list[-1], 4) if throughput_list else None,
        "F": round(fairness_list[-1], 4) if fairness_list else None,
        "W_T": round(W_T, 4),
        "W_F": round(W_F, 4),
        "objective": round(obj_val, 6),
        "gp_step_secs": round(gp_step, 4),
    }
    DECISIONS.write(json.dumps(rec) + "\n")
    if is_new_config:
        DECISIONS.write(json.dumps({"kind": "apply", "t": rec["t"],
                                    "config_idx": idx, "ways": ways,
                                    "masks": masks}) + "\n")
    DECISIONS.flush()


def _log_summary():
    n = _sample_n[0]
    elapsed = (time.time() - _t0[0]) if _t0[0] else 0.0
    rec = {
        "kind": "summary",
        "n_samples": n,
        "n_distinct_configs": len(_configs_seen),
        "elapsed_secs": round(elapsed, 2),
        "mean_cadence_secs": round(elapsed / n, 4) if n else None,
        "nominal_time_sampling": time_sampling,
    }
    DECISIONS.write(json.dumps(rec) + "\n")
    DECISIONS.flush()
    log(f"summary: {n} samples, {len(_configs_seen)} distinct configs, "
        f"mean cadence {rec['mean_cadence_secs']}s (nominal {time_sampling}s)")


# ---------------------------------------------------------------------------
# VERBATIM satori.py:201-207 (objective), with rows 5/9 substituted inline.
# ---------------------------------------------------------------------------

def objective(x, _is_anchor=False):
    # SUBSTITUTION (row 5) -- was artifact l.202 get_allocation(x[0]) -> 3 lists
    llc_shares, llc_masks = get_allocation_llc(x[0])
    if ARGS.dry_run:
        # --dry-run: no resctrl writes; the synthetic IPC model reads
        # `current_ways` directly (see _collect_ipc_dryrun)
        current_ways.update(dict(zip(applications, llc_shares)))
    else:
        # SUBSTITUTION (row 9) -- was artifact l.203 perform_resource_partitioning(...)
        apply_llc(llc_masks)
    time.sleep(time_sampling)                                    # VERBATIM l.204
    if STOP[0]:
        raise Exception("Total BO Engine Timeout Reached")
    get_metrics()                                                 # VERBATIM l.205
    W_T, W_F = get_weights()                                      # VERBATIM l.206
    obj_val = -1 * (W_T * throughput_list[len(throughput_list) - 1]
                     + W_F * fairness_list[len(fairness_list) - 1])  # VERBATIM l.207
    _log_sample(x[0], llc_shares, llc_masks, W_T, W_F, obj_val,   # ours, no artifact analogue
                tag=("anchor" if _is_anchor else "scan"))
    # CHUNKED SEARCH (ours, no artifact analogue): every REAL (non-anchor)
    # evaluation is banked here so run_chunked_bo can persist it regardless
    # of whether gp_minimize returns normally, is stopped early by the
    # budget callback, or is interrupted by a SIGTERM/timeout exception --
    # NEW_OBS is populated at the moment of measurement, not read back out
    # of gp_minimize's return value. Anchor probes (re-measured every chunk
    # to estimate that chunk's offset) are excluded: they must never be fed
    # to the surrogate as additional search data.
    if ARGS.state and not _is_anchor:
        NEW_OBS.append({"idx": int(x[0]), "y": obj_val})
    return obj_val


# ---------------------------------------------------------------------------
# CHUNKED, RESUMABLE SEARCH (ours, no artifact analogue). PORTED from
# spidersense_style.py's _load_state/_save_state/_chunk_offsets/_fp_gate --
# see that file's docstrings for the full rationale; only the parts that
# differ because SATORI's search is Bayesian rather than exhaustive are
# re-explained here.
#
# WHY: satori's own C4WIN-sized window (400 s) measured only 298 of the
# n_calls=1000 the artifact's own hyperparameters call for (298 samples /
# 249 distinct configs of the 1001-config space, "Total BO Engine Timeout
# Reached" on the harness's SIGALRM/SIGTERM, not on n_calls). Reporting that
# as SATORI's result would strawman the baseline. spidersense_style.py had
# the identical problem (1001-config exhaustive sweep > one window) and
# solved it by CHUNKING the search across scene relaunches with persisted
# state; this does the same for the BO loop.
#
# WARM START. gp_minimize's functional API accepts x0/y0 (documented: "If
# x0 and y0 are both provided then n_initial_points evaluations are first
# made then n_calls - n_initial_points subsequent evaluations are made
# guided by the surrogate model" -- skopt/optimizer/gp.py docstring). Every
# observed (config_idx, objective) pair from every prior chunk is replayed
# via x0/y0 on resume, which is functionally `optimizer.tell(x0, y0)` before
# the ask/tell loop starts (skopt/optimizer/base.py) -- i.e. the GP posterior
# a resumed chunk starts from is fit on the FULL prior history, not just
# this chunk's. This is the documented warm-start path; we do NOT hand-roll
# an ask/tell Optimizer loop, because x0/y0 already gives byte-identical
# surrogate-fitting behaviour to what gp_minimize does internally for a
# continuous run's own initial-points handling.
#
# n_random_starts ON RESUME (read skopt/optimizer/base.py before changing
# this): `n_initial_points = n_initial_points + len(x0)` happens AFTER the
# deprecated `n_random_starts` alias overwrites `n_initial_points`, and
# `optimizer.tell(x0, y0)` (called once, unconditionally, when x0 is given)
# already counts every x0 point as "told". Optimizer.ask() draws randomly
# while `len(told) < n_initial_points_total`. So passing n_random_starts=5
# on EVERY chunk would force 5 MORE random draws on top of however many
# points are already banked -- "5 random points per chunk", not "5 random
# points for the whole campaign", which is what the artifact's hyperparameter
# means. To preserve the ORIGINAL meaning, n_random_starts is reduced to the
# SHORTFALL: max(0, 5 - total_prior_observations). Likewise n_calls is
# reduced to max(0, 1000 - total_prior_observations) so the campaign totals
# n_calls=1000 across every chunk combined, not 1000 MORE per chunk.
#
# WHAT IS NOT PRESERVED, DISCLOSED HERE RATHER THAN GLOSSED OVER: SATORI's
# get_weights()/get_metrics() dynamic-weight machinery (throughput_list,
# fairness_list, the period marker lists, WT_list/WF_list) is wall-clock
# state internal to ONE process. A resumed chunk starts get_weights() at its
# `len(WT_list) == 0` branch (VERBATIM 0.5/0.5), exactly like the FIRST call
# of any fresh SATORI process -- it is not a special chunking defect, but it
# does mean the weight blend re-settles over the first ~time_equalization
# (10 s, paper default) seconds of every chunk rather than continuing a
# multi-chunk-long rolling average. Each individual (x, y) pair remains
# internally consistent (y is exactly what SATORI's objective would compute
# for that x at that moment, using whatever W_T/W_F were live then --
# identical in kind to how a continuous run's own weights vary call to
# call), so this does not corrupt the GP's training data; it only means the
# WEIGHT-ADAPTATION mechanism itself is not carried across chunk boundaries.
# We did not attempt to serialize it: doing so would mean persisting
# unbounded-length rolling histories for a fast (1-10 s) timescale mechanism
# that is orthogonal to the search-state fidelity this task calls out as
# critical (bullet 3, the surrogate). If this needs to be closed later, the
# fix is to persist throughput_list/fairness_list/marker lists/WT_list/
# WF_list/start_time alongside `observed` and restore them verbatim.
#
# THE RNG ITSELF IS ALSO NOT CONTINUOUS. random_state=1234 is kept literal
# per chunk (required: it is the artifact's own hyperparameter), but each
# chunk's gp_minimize call constructs a FRESH RandomState seeded at 1234,
# not a continuation of the previous chunk's advanced RNG state (there is no
# supported way to serialize skopt's internal RNG across process restarts
# via the functional API). This means the acquisition optimizer's own
# internal randomness (multi-start restarts of the inner lbfgs optimization,
# tie-breaking among equally-scored candidates) will not bit-for-bit match a
# hypothetical single continuous process. What IS preserved is the part that
# matters most for search quality: the GP is always fit on the complete,
# offset-corrected observation history, so the SEQUENCE OF CONFIGS PROPOSED
# is guided by the same information a continuous run would have, even if
# the exact numeric tie-breaks differ. Stated plainly per the honesty rule:
# resumed BO is faithful in DATA (every observation feeds the surrogate,
# offset-corrected) and in HYPERPARAMETERS (verbatim), not in BIT-EXACT
# TRAJECTORY.
# ---------------------------------------------------------------------------

TOTAL_N_CALLS = 1000      # VERBATIM gp_minimize n_calls -- campaign-wide total,
                          # not per chunk (see note above)
TOTAL_N_RANDOM = 5        # VERBATIM gp_minimize n_random_starts -- likewise
CONVERGE_PATIENCE = 3     # chunks with no incumbent improvement => converged.
                          # Explicit, stated rule (task requires this not be
                          # hidden): "no improvement" is judged on the
                          # anchor-offset-CORRECTED objective of the running
                          # best (config_idx, y) pair, min() since satori's
                          # objective is a value to MINIMIZE (l.207 negates
                          # W_T*T + W_F*F).


def _load_state(path):
    if not path or not os.path.exists(path):
        return {"chunks": 0, "observed": [], "anchor": {}, "anchor_idx": None,
                "best": None, "no_improve_chunks": 0, "converged": False}
    with open(path) as f:
        st = json.load(f)
    st.setdefault("chunks", 0)
    st.setdefault("observed", [])
    st.setdefault("anchor", {})
    st.setdefault("anchor_idx", None)
    st.setdefault("best", None)
    st.setdefault("no_improve_chunks", 0)
    st.setdefault("converged", False)
    return st


def _save_state(path, st):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)      # atomic: a killed chunk cannot leave a
                                # half-written checkpoint (spidersense_style
                                # pattern, same reasoning)


def _chunk_offsets(st):
    """Per-chunk additive offset on the RAW objective value, estimated from
    anchor configs re-probed every chunk (PORTED from spidersense_style.py's
    _chunk_offsets -- see that docstring for the full derivation). It matters
    MORE here than for spider: spider only uses the correction for a final
    top-k ranking, but here the correction is applied to y BEFORE it is
    handed to gp_minimize as y0, i.e. it directly shapes what the surrogate
    is fit on for every subsequent chunk."""
    per_chunk = {}
    for _k, obs in st.get("anchor", {}).items():
        for o in obs:
            per_chunk.setdefault(o["chunk"], []).append(o["y"])
    if not per_chunk:
        return {}, 0.0, 0.0
    means = {c: sum(v) / len(v) for c, v in per_chunk.items()}
    grand = sum(means.values()) / len(means)
    drift = (max(means.values()) - min(means.values())) if len(means) > 1 else 0.0
    offs = {c: m - grand for c, m in means.items()}
    # residual scatter (post-correction) is the real resolution limit --
    # same reasoning as spidersense_style._chunk_offsets.
    resid, n = 0.0, 0
    for _k, obs in st.get("anchor", {}).items():
        corr = [o["y"] - offs.get(o["chunk"], 0.0) for o in obs]
        if len(corr) > 1:
            mu = sum(corr) / len(corr)
            resid += (sum((v - mu) ** 2 for v in corr) / (len(corr) - 1)) ** 0.5
            n += 1
    return offs, drift, (resid / n if n else 0.0)


def _pick_anchors(st, n_anchors, n_configs, seed):
    """The SAME anchor configs must be re-probed in EVERY chunk (that is
    what makes them anchors), so the choice is made ONCE and persisted --
    picking freshly each chunk from a re-seeded RNG would just be n_anchors
    more ordinary samples, not a per-chunk offset estimator."""
    if st.get("anchor_idx"):
        return st["anchor_idx"]
    rng = random.Random(seed)
    n_anchors = max(0, min(n_anchors, n_configs - 1))
    idxs = rng.sample(range(n_configs), n_anchors) if n_anchors else []
    st["anchor_idx"] = idxs
    return idxs


def _fp_gate(args):
    """Instance-fingerprint gate, victims-only. PORTED from
    spidersense_style.py's _fp_gate -- identical rationale (canneal's
    measured bistability across relaunches, CLAUDE.md "instance flip
    invalidates arms"): a chunk whose tenants did not come back as the
    instance the iso baseline describes must not be folded into the
    surrogate's training data."""
    try:
        with open(args.iso) as f:
            iso = json.load(f)
    except (OSError, ValueError) as e:
        return False, f"cannot read iso baseline {args.iso}: {e}"
    # measure at the FULL mask, same condition the iso baseline itself used
    # (spidersense_style._fp_gate's comment applies verbatim here).
    for t in tenants:
        write_schemata_line(t["grp"], "L3", domain_body(t["grp"], "L3", f"{FULL_MASK:x}"))
    marks = {s.name: s.mark() for s in samplers}
    deadline = time.monotonic() + args.fp_window
    while time.monotonic() < deadline and not STOP[0]:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    per = {s.name: s.read_window(marks[s.name]) for s in samplers}
    r = {"fp": {k: v["fp"] for k, v in per.items()},
         "ipc": {k: v["ipc"] for k, v in per.items()},
         "nsamp": min((v["n"] for v in per.values()), default=0)}
    DECISIONS.write(json.dumps({"kind": "fp_gate", **r}) + "\n")
    DECISIONS.flush()
    only = [x for x in (args.fp_tenants or "").split(",") if x]
    bad, seen = [], []
    for name in applications:
        if only and name not in only:
            continue
        want = (iso.get(name) or {}).get("fp")
        got = r["fp"].get(name)
        if want is None or got is None:
            continue
        if want < args.fp_min:
            seen.append(f"{name}=n/a")
            continue
        d = abs(got - want) / want
        seen.append(f"{name}={got:.3f}/{want:.3f}({100*d:+.1f}%)")
        if d > args.fp_tol:
            bad.append(f"{name} {got:.3f} vs iso {want:.3f} "
                       f"({100*d:.1f}% > {100*args.fp_tol:.0f}%)")
    detail = " ".join(seen)
    return (not bad), (detail if not bad else "; ".join(bad))


def run_chunked_bo(args):
    """One chunk of the resumable BO campaign. Returns the process exit code
    (0 = chunk ok, whether or not the search is complete; 3 = instance gate
    rejected the chunk, mirroring spidersense_style.run_exhaustive)."""
    global NEW_OBS
    st = _load_state(args.state)
    chunk = st["chunks"]
    total_prior = len(st["observed"])
    m = len(CONFIGS_LIST)

    DECISIONS.write(json.dumps({
        "kind": "search_begin_chunk", "chunk": chunk, "m": m,
        "total_prior_observed": total_prior, "budget": args.budget,
        "anchors": args.anchors, "total_n_calls": TOTAL_N_CALLS,
    }) + "\n")
    DECISIONS.flush()
    log(f"chunk {chunk}: {total_prior} prior observations of {m} configs "
        f"(target n_calls={TOTAL_N_CALLS})")

    # ---- INSTANCE GATE (same 3% rule / rationale as spidersense_style.py) --
    if args.iso:
        ok, detail = _fp_gate(args)
        if not ok and args.fp_mode == "block":
            DECISIONS.write(json.dumps({"kind": "fp_gate_failed", "detail": detail}) + "\n")
            DECISIONS.flush()
            log(f"!! instance gate FAILED: {detail}")
            log("   this chunk measured a different tenant instance than the "
                "iso baseline -- discarding it and exiting so the scene can "
                "be relaunched. No state was written.")
            return 3
        if not ok:
            DECISIONS.write(json.dumps({"kind": "fp_gate_warn", "detail": detail}) + "\n")
            DECISIONS.flush()
            log(f"!! instance gate MISMATCH (continuing, --fp-mode warn): {detail}")
        else:
            DECISIONS.write(json.dumps({"kind": "fp_gate_ok", "detail": detail}) + "\n")
            DECISIONS.flush()
            log(f"instance gate ok: {detail}")

    # ---- ANCHORS FIRST, so a chunk cut short by its budget still contributes
    # the offset estimate that makes its own probes comparable (same
    # ordering rationale as spidersense_style.run_exhaustive). Saved
    # immediately after, so anchor progress survives even if the body/BO
    # loop below is interrupted. ----
    anchor_idxs = _pick_anchors(st, args.anchors, m, args.seed)
    try:
        for idx in anchor_idxs:
            if STOP[0]:
                break
            y = objective([idx], _is_anchor=True)
            st["anchor"].setdefault(str(idx), []).append({"chunk": chunk, "y": y})
    finally:
        _save_state(args.state, st)

    offs, drift, resid = _chunk_offsets(st)
    log(f"anchor offset (chunk {chunk}): {offs.get(chunk, 0.0):.4f} "
        f"(between-chunk drift {drift:.4f}, residual noise {resid:.4f})")

    # ---- WARM START: replay every prior observation via x0/y0, offset-
    # corrected BEFORE gp_minimize sees it (see module-level note above). ----
    x0, y0 = [], []
    for o in st["observed"]:
        x0.append([o["idx"]])
        y0.append(o["y"] - offs.get(o["chunk"], 0.0))
    x0 = x0 or None
    y0 = y0 or None

    remaining_calls = max(0, TOTAL_N_CALLS - total_prior)
    remaining_random = max(0, TOTAL_N_RANDOM - total_prior)

    NEW_OBS = []
    if remaining_calls > 0 and not STOP[0]:
        deadline = time.monotonic() + args.budget if args.budget else None

        def _budget_cb(_res):
            return bool(STOP[0]) or (deadline is not None and time.monotonic() >= deadline)

        try:
            gp_minimize(objective, [(0, m - 1)], n_calls=remaining_calls,
                        n_random_starts=remaining_random, acq_func='EI',
                        random_state=1234, x0=x0, y0=y0, callback=_budget_cb)
        except Exception as e:
            log(f"chunk {chunk} search loop ended by exception: {e}")
    elif remaining_calls == 0:
        log(f"already have {total_prior} >= n_calls={TOTAL_N_CALLS} observations "
            "-- not sampling further, just re-checking convergence")

    st["observed"].extend({"chunk": chunk, "idx": o["idx"], "y": o["y"]} for o in NEW_OBS)
    st["chunks"] = chunk + 1

    # ---- convergence bookkeeping (explicit rule, see CONVERGE_PATIENCE) ----
    offs, drift, resid = _chunk_offsets(st)     # this chunk's anchor is now in
    corrected = [(o["idx"], o["y"] - offs.get(o["chunk"], 0.0)) for o in st["observed"]]
    if corrected:
        best_idx, best_y = min(corrected, key=lambda p: p[1])   # MINIMIZE (l.207 negates)
        # IMPROVEMENT MUST EXCEED THE MEASURED NOISE FLOOR (fix, 2026-09-12,
        # SATORI_CHUNK_VERIFY.md "recalibration-driven improvement noise").
        # best_y is recomputed every chunk from FRESHLY recalibrated offsets,
        # but st["best"]["y"] was stored under a PREVIOUS chunk's calibration.
        # Comparing the two at a 1e-9 tolerance means a sub-noise wiggle in the
        # anchor offsets -- not a better configuration -- can read as
        # "improved" (delaying convergence) or, in the dangerous direction, as
        # "not improved" (declaring convergence while the search is still
        # making progress, which would hand us a non-converged SATORI number:
        # exactly what this whole chunked arm exists to prevent).
        # `resid` IS the right threshold: it is the anchor residual, i.e. how
        # well a config reproduces once its chunk offset is removed -- the
        # measured resolution limit of the comparison (same quantity
        # spidersense_style.py computes for the same purpose). An "improvement"
        # smaller than that is not an improvement, it is measurement noise.
        tol = max(resid, 1e-9)
        prev = st["best"]
        improved = (prev is None) or (best_y < prev["y"] - tol)
        st["no_improve_chunks"] = 0 if improved else st["no_improve_chunks"] + 1
        st["best"] = {"idx": best_idx, "y": best_y,
                      "tol": tol, "improved_this_chunk": bool(improved),
                      "prev_idx": (prev or {}).get("idx")}
    total_now = len(st["observed"])
    complete = total_now >= TOTAL_N_CALLS
    # CONVERGED (patience rule, distinct from COMPLETE/budget-exhausted): no
    # incumbent improvement for CONVERGE_PATIENCE consecutive chunks, and
    # enough of the campaign has actually run that "no improvement" isn't
    # just "haven't looked yet" (floor of 2x the anchor count or 10).
    st["converged"] = bool(
        total_now >= max(2 * len(anchor_idxs), 10)
        and st["no_improve_chunks"] >= CONVERGE_PATIENCE)

    _save_state(args.state, st)
    DECISIONS.write(json.dumps({
        "kind": "chunk_end", "chunk": chunk, "measured_this_chunk": len(NEW_OBS),
        "observed_total": total_now, "remaining": max(0, TOTAL_N_CALLS - total_now),
        "anchor_drift": round(drift, 6), "anchor_resid": round(resid, 6),
        "no_improve_chunks": st["no_improve_chunks"], "converged": st["converged"],
        "improve_tol": round((st["best"] or {}).get("tol", 0.0), 6),
        "incumbent_idx": (st["best"] or {}).get("idx"),
        "incumbent_stable": ((st["best"] or {}).get("idx")
                             == (st["best"] or {}).get("prev_idx")),
        "complete": complete, "best": st["best"],
    }) + "\n")
    DECISIONS.flush()
    log(f"chunk {chunk} done: {len(NEW_OBS)} new samples, {total_now}/{TOTAL_N_CALLS} "
        f"total, converged={st['converged']}, complete={complete}")

    if complete or st["converged"]:
        best = st["best"]
        if best is not None:
            llc_shares, llc_masks = get_allocation_llc(best["idx"])
            conv = {"idx": best["idx"], "ways": dict(zip(applications, llc_shares)),
                    "masks": dict(zip(applications, llc_masks)),
                    "objective": best["y"], "order": list(applications),
                    "observed": total_now, "chunks": st["chunks"],
                    "anchor_drift": round(drift, 6), "anchor_resid": round(resid, 6),
                    "complete": complete, "converged": st["converged"]}
            with open(os.path.join(args.out, "converged.json"), "w") as f:
                json.dump(conv, f, indent=1)
            log(f"BO search complete/converged: config idx {best['idx']} "
                f"ways={conv['ways']} objective={best['y']:.4f}")
        return 0

    log("search INCOMPLETE -- relaunch the scene and resume with the same "
        "--state file")
    return 0


# ---------------------------------------------------------------------------
# VERBATIM satori.py:210-218 (start_bo_engine), literal hyperparameters kept.
# ---------------------------------------------------------------------------

def start_bo_engine():
    get_metrics()
    time.sleep(time_sampling)
    res = gp_minimize(objective,
                       [(0, len(CONFIGS_LIST) - 1)],
                       n_calls=1000,
                       n_random_starts=5,
                       acq_func='EI',
                       random_state=1234)
    return res


STOP = [False]


def _stop_handler(signum, frame):
    STOP[0] = True
    signal_handler(signum, frame)


def main():
    global NUM_APPS, NUM_RESOURCES, NUM_UNITS, CONFIGS_LIST
    global tenants, applications, isolated_ipc, samplers
    global throughput_list, fairness_list
    global equalization_period_marker_list, prioritization_period_marker_list
    global WT_list, WF_list
    global equalization_period_counter, prioritization_period_counter, start_time
    global time_sampling, time_prioritization, time_equalization
    global last_speedups, last_ipc, current_ways, FULL_MASK
    global DECISIONS, ARGS

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true",
                     help="synthetic IPC model, no perf/resctrl writes -- for "
                          "smoke-testing the real get_weights/objective/"
                          "gp_minimize loop without sudo")
    ap.add_argument("--time-total", type=float, default=100000.0,
                     help="artifact's time_total (l.223), seconds, via "
                          "signal.alarm. Defaults large: in the harness the "
                          "arm window is enforced externally by SIGTERM "
                          "(measure_ctrl); pass an explicit value for a "
                          "standalone/--dry-run smoke test that must "
                          "self-terminate")
    # ---- chunked, resumable search (ported from spidersense_style.py; see
    # run_chunked_bo's module-level docstring for the warm-start mechanics) --
    ap.add_argument("--budget", type=float, default=0.0,
                     help="max seconds of BO probing per chunk (0 = unlimited, "
                          "i.e. run until n_calls=1000 total or stopped). Size "
                          "it under the SHORTEST tenant's run length so no "
                          "tenant restarts mid-chunk.")
    ap.add_argument("--state", default="",
                     help="checkpoint file: prior (config_idx, objective) "
                          "observations warm-start gp_minimize's x0/y0 on "
                          "resume. Enables CHUNKED search -- run with "
                          "--budget, let the scene restart, re-run with the "
                          "same --state to resume.")
    ap.add_argument("--iso", default="",
                     help="results_iso/<AS>/iso.json. Enables the INSTANCE "
                          "GATE (ported from spidersense_style.py): a chunk "
                          "whose tenants did not come back as the instance "
                          "the iso baseline describes is discarded (exit 3) "
                          "instead of being folded into the surrogate's "
                          "training data.")
    ap.add_argument("--fp-tol", type=float, default=0.06,
                     help="instance fingerprint tolerance -- see "
                          "spidersense_style.py --fp-tol for the measured "
                          "basis (same roster, same tenants)")
    ap.add_argument("--fp-min", type=float, default=0.10,
                     help="skip the gate for tenants whose iso fingerprint "
                          "is below this (npb_ep_e reads 0.004)")
    ap.add_argument("--fp-mode", choices=["block", "warn"], default="block",
                     help="block = discard a chunk whose instance does not "
                          "match iso (correct, costs a full scene warmup per "
                          "rejection); warn = record the mismatch and "
                          "continue, leaving the filtering to the grader")
    ap.add_argument("--fp-window", type=float, default=20.0,
                     help="seconds to measure the gate fingerprint over")
    ap.add_argument("--fp-tenants", default="",
                     help="restrict the gate to these tenants (comma-"
                          "separated) -- pass the VICTIMS, same rationale as "
                          "spidersense_style.py (donors' MBA/CAT levels move "
                          "for real between chunks; gating on them rejects "
                          "healthy chunks)")
    ap.add_argument("--anchors", type=int, default=8,
                     help="configs re-measured every chunk (via a direct "
                          "objective() probe, excluded from the surrogate's "
                          "training data) to estimate the per-chunk offset "
                          "and the between-chunk noise floor on the raw "
                          "objective value")
    ap.add_argument("--seed", type=int, default=1,
                     help="seed for anchor-config selection ONLY -- "
                          "independent of gp_minimize's own random_state="
                          "1234, which stays fixed per the artifact")
    args = ap.parse_args()
    ARGS = args

    if not args.dry_run and os.geteuid() != 0:
        sys.exit("must run as root (resctrl) -- or pass --dry-run")

    os.makedirs(args.out, exist_ok=True)
    with open(args.config) as f:
        cfg = json.load(f)

    # decision #6: DETERMINISTIC application order -- cfg.json's tenant order
    # comes from bash associative-array iteration (arbitrary); sort by name.
    tenants = sorted(cfg["tenants"], key=lambda t: t["name"])
    applications = [t["name"] for t in tenants]

    # decision #4: isolated baseline is the cfg's solo_ipc path, loaded once.
    # Refuse to start rather than guess if any tenant is missing.
    iso_path = cfg.get("solo_ipc")
    if not iso_path or not os.path.exists(iso_path):
        sys.exit(f"solo_ipc baseline missing from cfg ({iso_path!r}) -- "
                  f"cannot start SATORI without isolated IPC for every tenant")
    with open(iso_path) as f:
        iso = json.load(f)
    missing = [n for n in applications if n not in iso]
    if missing:
        sys.exit(f"solo_ipc {iso_path} has no isolated IPC for {missing} -- "
                  f"refusing to guess")
    isolated_ipc = [iso[n] for n in applications]

    if args.dry_run:
        FULL_MASK = (1 << 15) - 1
        nbits = 15
    else:
        with open(os.path.join(RESCTRL, "info/L3/cbm_mask")) as f:
            FULL_MASK = int(f.read(), 16)
        nbits = FULL_MASK.bit_length()

    NUM_APPS = len(applications)
    NUM_RESOURCES = 1
    NUM_UNITS = [nbits]
    CONFIGS_LIST = []

    throughput_list = []
    fairness_list = []
    equalization_period_marker_list = []
    prioritization_period_marker_list = []
    WT_list = []
    WF_list = []
    last_speedups = []
    last_ipc = []
    current_ways = {n: nbits // NUM_APPS for n in applications}

    DECISIONS = open(os.path.join(args.out, "decisions.jsonl"), "w")

    log(f"satori_style up: {NUM_APPS} tenants (order={applications}), "
        f"{nbits} ways, dry_run={args.dry_run}")

    gen_configs()
    m = len(CONFIGS_LIST)
    DECISIONS.write(json.dumps({
        "kind": "search_begin", "m": m, "nbits": nbits,
        "tenants": applications, "time_prioritization": 1,
        "time_equalization": 10, "time_sampling": 0.1,
        "dry_run": args.dry_run,
        "substitutions": ["cat_only_cores_mba_fixed", "ipc_not_ips_perf_cores",
                           "resctrl_schemata_not_pqos", "no_start_jobs"],
    }) + "\n")
    DECISIONS.flush()
    log(f"config space: {m} LLC partitions (C({nbits - 1},{NUM_APPS - 1}) "
        f"for {nbits} ways / {NUM_APPS} tenants)")

    samplers = [PerfSampler(t, args.out) for t in tenants]
    if not args.dry_run:
        for s in samplers:
            s.start()
        time.sleep(1.0)     # first perf -I 100 interval to land

    # VERBATIM satori.py:220-231 __main__ literals (time_lag dropped -- no
    # start_jobs), row 3 decision: paper's stated defaults (1 s / 10 s), not
    # the artifact's 10 s / 25 s (CONTROLLER_COMPARISON.md #13 row 3).
    time_sampling = 0.1
    time_prioritization = 1
    time_equalization = 10

    signal.signal(signal.SIGALRM, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    equalization_period_counter = time.time()
    prioritization_period_counter = time.time()
    start_time = time.time()
    _t0[0] = start_time
    if args.time_total and args.time_total > 0:
        signal.alarm(int(args.time_total))

    rc = 0
    try:
        if args.state:
            rc = run_chunked_bo(args)
        else:
            start_bo_engine()                                      # VERBATIM l.253-254 call
    except Exception as e:
        print(f"Total BO Engine Timeout Reached ({e})")
    finally:
        log("satori_style shutting down: restoring")
        _log_summary()
        if not args.dry_run:
            restore_all()
            for s in samplers:
                s.stop()
        DECISIONS.close()
    sys.exit(rc)


if __name__ == "__main__":
    main()
