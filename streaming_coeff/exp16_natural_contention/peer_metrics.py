#!/usr/bin/env python3
"""peer_metrics.py -- score a certify scene on the PEERS' OWN objectives.

Written against verbatim formulas checked out of the papers (2026-08-10). The
point is to stop reporting only our own metric, which no reviewer will accept:
a constrained controller must be shown on the objectives the unconstrained ones
optimise, even where it loses.

THREE RULES, each of which we got wrong before:

1. THE AGGRESSOR IS IN THE POPULATION. SpiderSense's Sum-IPC, CoPart's
   unfairness, PaLLOC's Eq.7 and Themis's fairness all range over every
   co-runner. Excluding the donor is exactly the accounting error that forced
   the exp16 DNH-oracle retraction. An MBA throttle costing the streamer 94%
   MUST show up as a loss here -- that is the metric working, not a bug.
2. DIRECTION IS NOT UNIFORM. CoPart unfairness is LOWER-better and unbounded;
   Themis fairness is HIGHER-better and capped at 1. They are near-reciprocal
   in spirit and must never share a column or a word.
3. TRUE IPC, NOT IPS. instructions/sec is not instructions/cycle. We recover
   real IPC from the raw perf counters (instructions / CPU_CLK_UNHALTED.THREAD),
   both already captured per interval, so no re-run and no perf change is needed.

DELIBERATELY NOT IMPLEMENTED -- do not add these without the missing data:
  * PARTIES EMU. Needs a load knob, per-app knee (max load) and live p99 under a
    load sweep. Our batch tenants have none of the three. Any batch-flavoured
    "aggregate IPS as a fraction of solo" has no QoS constraint and no load
    search, so it is a DIFFERENT metric wearing EMU's name -- the single most
    attackable line we could print.
  * SpiderSense ORACLE-normalised performance. Their denominator is a
    brute-force sweep of the whole config space. Ours would be the best of a
    handful of hand-picked points, an optimistic denominator that inflates every
    arm by an unknown amount. Run the sweep or do not use the word ORACLE.
  * PARTIES QoS satisfaction. Their target is the ABSOLUTE p99 at the solo
    load-vs-latency knee. A relative "1.25x solo p99" bar is a different
    criterion; and these scenes have no latency-critical tenants at all.

Usage: peer_metrics.py results_certify/<scene> [more...]
"""
import sys, os, json, glob, statistics as st

# Every arm certify.py can grade must appear here: scene_ipc() iterates this
# list, so an arm missing from it yields no IPC and the ladder prints '--' for
# it with NO error -- the peer arms (copart/dcat/spider/c3) and spider_conv
# were all silently ungradeable until 2026-09-05 for exactly this reason.
# Keep in sync with certify.PHASES.
PHASES = ['solo', 'noisy', 'eq', 'cat', 'cat_cap', 'mba',
          'copart', 'dcat', 'spider', 'spider_conv', 'satori', 'palloc',
          'cacheman', 'null', 'sat_conv', 'cm_nest', 'nest_dp', 'band_dp',
          'c3', 'c4', 'dsolo']
PHASE_SPREAD = 0.25          # matches certify.py's phase-consistency guard
PHASE_A = "results_phase_a"  # isolated full-resource reference (15 ways, MB100)


def true_ipc(perf_json, tail=None, tail_s=None):
    """instructions / CPU_CLK_UNHALTED.THREAD, median over intervals.

    perf's own 'insn per cycle' metric field reads 0 here (a metric-grouping
    artefact), but both raw counters are present per interval, so real IPC is
    recoverable -- including retroactively from runs already on disk.
    """
    ins, cyc = {}, {}
    if not os.path.exists(perf_json):
        return None
    for line in open(perf_json):
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t, v = o.get("interval"), o.get("counter-value")
        if v in (None, "<not counted>", "<not supported>"):
            continue
        if o.get("event") == "instructions":
            ins[t] = float(v)
        # coeffd3/4 request the TMA metric groups, which expand the cycle
        # counter to CPU_CLK_UNHALTED.THREAD; the peer-style arms request plain
        # `cycles`. Same counter, two spellings -- accept both or every peer arm
        # silently grades as "no data".
        elif o.get("event") in ("CPU_CLK_UNHALTED.THREAD", "cycles"):
            cyc[t] = float(v)
    ts = [t for t in sorted(ins) if t in cyc and cyc[t] > 0]
    vals = [ins[t] / cyc[t] for t in ts]
    # tail_s: the tail in SECONDS, converted per file with its own median perf
    # interval. A fixed interval COUNT grades arms unequally: 30 intervals is
    # 60 s for a 2 s arm but 30 s for dcat (1 s) and 3 s for satori (100 ms).
    # For 2 s files round(60 / ~2.0) = 30, i.e. identical to the old count.
    if tail_s and len(ts) > 1:
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        tail = max(1, round(tail_s / gaps[len(gaps) // 2]))
    # tail: last N perf intervals only. Needed for CONTROLLER phases -- coeffd4
    # converges one verified step at a time, so a median over the whole window
    # averages the settled state together with the unmanaged state it started
    # from. Measured (D3-v7): canneal climbs monotonically 1.022 -> 1.123 across
    # 23 scans and never plateaus; the whole-window median grades it +22.2%
    # while the state it actually reached is +64.8%. A 3x understatement of the
    # controller, produced entirely by the estimator.
    if tail:
        vals = vals[-tail:]
    return st.median(vals) if vals else None


def scene_ipc(wdir, tails=None, tail_s=None):
    """{phase: {tenant: IPC}} from the per-tenant perf JSONs.

    tails: {phase: N} -- use only the last N perf intervals for those phases.
    tail_s: {phase: S} -- use only the last S seconds (per-file interval).
    """
    tails = tails or {}
    tail_s = tail_s or {}
    out = {}
    for p in PHASES:
        d = os.path.join(wdir, f"phase_{p}")
        if not os.path.isdir(d):
            continue
        row = {}
        for f in glob.glob(os.path.join(d, "perf_*.json")):
            n = os.path.basename(f)[5:-5]
            v = true_ipc(f, tail=tails.get(p), tail_s=tail_s.get(p))
            if v:
                row[n] = v
        if row:
            out[p] = row
    return out


def scene_hits(wdir):
    """{phase: {tenant: hit}} -- only to apply the phase-consistency guard."""
    out = {}
    for p in PHASES:
        f = os.path.join(wdir, f"phase_{p}", "decisions.jsonl")
        if not os.path.exists(f):
            continue
        acc = {}
        for line in open(f):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") != "scan":
                continue
            for n, t in o.get("tenants", {}).items():
                if t.get("hit") is not None:
                    acc.setdefault(n, []).append(t["hit"])
        if acc:
            out[p] = {n: st.median(v) for n, v in acc.items()}
    return out


def isolated_ref(tenant):
    """CoPart's IPS_full / Themis's standalone: the tenant ALONE at 15 ways, MB100.

    results_phase_a/<w>/cfg_w15 is semantically exactly that. Returns None when
    the workload was never Phase-A'd -- the custom-sized binaries (cg_d20,
    cg_d40, ft_d8) have no entry, which is why CoPart's metric is suppressed for
    scenes containing them rather than silently mixing reference semantics.
    """
    return true_ipc(os.path.join(PHASE_A, tenant, "cfg_w15", "perf_%s.json" % tenant))


def grade(wdir):
    ipc, hits = scene_ipc(wdir), scene_hits(wdir)
    if "noisy" not in ipc:
        return None
    pop = sorted(ipc["noisy"])                       # ALL co-runners, donor included
    vics = sorted(ipc.get("solo", {}))
    donors = [n for n in pop if n not in vics]

    # rule 3 of certify.py, carried over: a tenant caught re-entering its init
    # stage has a meaningless ratio, and dropping it would change the population
    # a dispersion statistic is computed over. Suppress the scene instead.
    incons = []
    for n in pop:
        hs = [hits[p][n] for p in hits if n in hits[p]]
        if len(hs) >= 2 and max(hs) - min(hs) > PHASE_SPREAD:
            incons.append(n)

    base = "eq" if "eq" in ipc else "noisy"          # PaLLOC's baseline if we have it
    refs = {n: isolated_ref(n) for n in pop}
    missing_ref = [n for n, v in refs.items() if not v]

    rows = {}
    for p in ("noisy", "eq", "cat", "cat_cap", "mba"):
        if p not in ipc:
            continue
        cur = ipc[p]
        r = {"sum_ipc": sum(cur.get(n, 0) for n in pop)}
        # PaLLOC Eq.7: (1/N) sum_i IPC_i,arm / IPC_i,baseline  -- higher better
        if base in ipc and not incons:
            terms = [cur[n] / ipc[base][n] for n in pop
                     if n in cur and ipc[base].get(n)]
            r["palloc"] = sum(terms) / len(terms) if terms else None
        # CoPart Eq.1/2: slowdown_i = IPC_full/IPC_arm ; unfairness = sigma/mu -- LOWER better
        if not missing_ref and not incons:
            sl = [refs[n] / cur[n] for n in pop if n in cur and cur[n]]
            r["copart"] = (st.pstdev(sl) / st.mean(sl)) if len(sl) > 1 else None
        # Themis: NP_i = throughput_colo/throughput_standalone ; fairness = min/max -- HIGHER better, <=1
        if not missing_ref and not incons:
            np_ = [cur[n] / refs[n] for n in pop if n in cur]
            r["themis_fair"] = (min(np_) / max(np_)) if np_ and max(np_) else None
            r["themis_minNP"] = min(np_) if np_ else None
        rows[p] = r
    return dict(dir=wdir, pop=pop, vics=vics, donors=donors, rows=rows,
                base=base, incons=incons, missing_ref=missing_ref)


def f(x, nd=3):
    return "--" if x is None else f"{x:.{nd}f}"


def main():
    dirs = [d for d in (sys.argv[1:] or sorted(glob.glob("results_certify/*"))) if os.path.isdir(d)]
    rows = [r for r in (grade(d) for d in dirs) if r]
    if not rows:
        sys.exit("no gradable scenes")
    for r in rows:
        print("=" * 96)
        print(f"{os.path.basename(r['dir'])}   population (donor INCLUDED): {', '.join(r['pop'])}")
        print(f"  donors: {', '.join(r['donors']) or 'none'}   PaLLOC baseline arm: {r['base']}"
              + ("  <-- 'noisy' is UNMANAGED sharing, not PaLLOC's equal partition;"
                 " relabel any number derived from it" if r['base'] == 'noisy' else ""))
        if r["incons"]:
            print(f"  !! phase-inconsistent: {', '.join(r['incons'])} -> ratio metrics SUPPRESSED for the scene")
        if r["missing_ref"]:
            print(f"  !! no isolated Phase-A reference for: {', '.join(r['missing_ref'])}")
            print("     -> CoPart + Themis SUPPRESSED (mixing reference semantics across tenants")
            print("        would corrupt a dispersion statistic). Fix: Phase-A cfg_w15 for those.")
        print("-" * 96)
        print(f"{'arm':<8}{'SumIPC':>10}{'PaLLOC Eq7':>13}{'CoPart sig/mu':>15}"
              f"{'Themis fair':>13}{'Themis minNP':>14}")
        print(f"{'':8}{'higher+':>10}{'higher+':>13}{'LOWER+':>15}{'higher+ (<=1)':>13}{'higher+':>14}")
        for p in ("noisy", "eq", "cat", "cat_cap", "mba"):
            if p not in r["rows"]:
                continue
            x = r["rows"][p]
            print(f"{p:<8}{f(x['sum_ipc']):>10}{f(x.get('palloc')):>13}"
                  f"{f(x.get('copart')):>15}{f(x.get('themis_fair')):>13}"
                  f"{f(x.get('themis_minNP')):>14}")
        print("-" * 96)
        print("NOT REPORTED, and must not be added without the missing data: PARTIES EMU")
        print("(no load knob / knee / p99), SpiderSense ORACLE-normalised (no brute-force")
        print("sweep), PARTIES QoS satisfaction (no LC tenants, and 1.25x-solo != knee p99).")


if __name__ == "__main__":
    main()
