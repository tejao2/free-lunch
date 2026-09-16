#!/usr/bin/env python3
"""grade_spider.py -- read a SpiderSense ALGORITHM 2 arm and answer two
questions their evaluation does not.

WHY THIS SCRIPT EXISTS.  SpiderSense's persistent-time-period strategy is an
exhaustive live sweep (their ALGORITHM 2): it applies EVERY way-partition to
the running system, scores each by the SUM of per-tenant IPC, and keeps the
argmax.  It is therefore an oracle for its own objective -- there is no point
comparing aggregate IPC against it, we would lose by construction.  The two
axes their paper leaves unmeasured are:

  (Q1) WHAT THE OBJECTIVE COSTS INDIVIDUAL TENANTS.  `CurrIPC += a_i` (their
       line 10) has no floor term, so it trades a tenant down whenever the sum
       wins -- against their own stated principle that "ensuring the
       performance is lower-bound for all workloads is more crucial" (p.10).

  (Q2) WHAT THE SEARCH COSTS WHILE IT RUNS.  Their evaluation measures only
       the converged state, so the M live reconfigurations that precede it are
       invisible in every number they report.

The scan itself supplies both axes for free: each probe records per-tenant IPC
under one configuration, so the 1001 probes ARE a sampled map of the whole
partition space.  That makes the Q1 claim a RANKING claim over the space
("maximizing summed IPC does not select for per-tenant safety") rather than a
fragile argmax claim ("their one config happened to be bad") -- and a ranking
claim survives not having found the exact global optimum.

Baseline caveat, stated in the output: unmanaged per-tenant IPC comes from
phase_noisy (a long window), while each probe is a 1 s window at the paper's
own Table 2 cadence.  Per-probe values are correspondingly noisy; that is why
the headline is a distribution over 1001 probes and a top-k check, not a
single per-probe verdict.  The equal-split composition is reported as a
secondary within-scan reference measured at the SAME cadence.

Usage: python3 grade_spider.py results_certify/<run> [--harm 10] [--topk 20]
"""

import argparse
import glob
import json
import os
import statistics as st
import sys

from peer_metrics import true_ipc


def load_state(path, names):
    """Probes from the CHUNKED search checkpoint -- the canonical source, since
    with chunking the probes are spread across one decisions.jsonl per chunk.
    Returns (probes, chunk_offsets, drift, residual)."""
    chk = json.load(open(path))
    per_chunk = {}
    for _k, obs in chk.get("anchor", {}).items():
        for o in obs:
            per_chunk.setdefault(o["chunk"], []).append(o["sum_ipc"])
    offs, drift, resid = {}, 0.0, 0.0
    if per_chunk:
        means = {c: sum(v) / len(v) for c, v in per_chunk.items()}
        grand = sum(means.values()) / len(means)
        offs = {c: m - grand for c, m in means.items()}
        if len(means) > 1:
            drift = max(means.values()) - min(means.values())
        acc, n = 0.0, 0
        for _k, obs in chk.get("anchor", {}).items():
            corr = [o["sum_ipc"] - offs.get(o["chunk"], 0.0) for o in obs]
            if len(corr) > 1:
                mu = sum(corr) / len(corr)
                acc += (sum((x - mu) ** 2 for x in corr) / (len(corr) - 1)) ** 0.5
                n += 1
        resid = acc / n if n else 0.0
    probes = []
    for k, v in chk.get("done", {}).items():
        comp = tuple(int(x) for x in k.split("-"))
        probes.append({"comp": list(comp), "ways": dict(zip(names, comp)),
                       "sum_ipc": v["sum_ipc"] - offs.get(v.get("chunk", 0), 0.0),
                       "raw_ipc": v["sum_ipc"], "chunk": v.get("chunk", 0),
                       "ipc": v.get("ipc", {}), "fp": v.get("fp", {}),
                       "nsamp": v.get("nsamp", 0)})
    return probes, chk, drift, resid


def load_meta(path):
    meta = {}
    if not os.path.exists(path):
        return meta
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        k = o.get("kind")
        if k in ("search_begin", "scan_done", "converged", "chunk_end",
                 "search_aborted", "perf_rotated", "no_progress"):
            meta.setdefault(k, []).append(o)
    return meta


def unmanaged_ipc(wdir, tenants):
    """per-tenant IPC with no partitioning at all -- the tenant's real
    counterfactual, which is NOT in the composition space (every tenant
    holding all 15 ways is not a disjoint partition)."""
    out = {}
    for t in tenants:
        f = os.path.join(wdir, "phase_noisy", f"perf_{t}.json")
        v = true_ipc(f)
        if v:
            out[t] = v
    return out


def lc_latency_report(wdir):
    """LC p99 during the sweep -- DEBUG side metric, graded on nothing.

    Each probe logs the latest TailBench live window (spidersense_style.py
    Tenant.read_lat). At low QPS a window is >=2 s and a probe is ~1 s, so most
    probes re-read the previous window: only 'fresh' windows (age <= FRESH_S)
    can be attributed to the partition in force. Early-vs-late quartiles show a
    queue that grows across the sweep (open-loop TailBench over its knee is
    bistable), which would make every late probe's IPC read a different regime.
    """
    FRESH_S = 2.0
    noisy = {}
    np_ = os.path.join(wdir, "phase_noisy", "decisions.jsonl")
    if os.path.exists(np_):
        for line in open(np_):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") != "scan":
                continue
            for n, t in o.get("tenants", {}).items():
                if t.get("p99") is not None:
                    noisy.setdefault(n, []).append(t["p99"])
    seq = {}            # tenant -> [(t, window dict)] in probe order
    for d in sorted(glob.glob(os.path.join(wdir, "phase_spider*"))):
        p = os.path.join(d, "decisions.jsonl")
        if not os.path.exists(p):
            continue
        for line in open(p):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            for n, w in (o.get("lat") or {}).items():
                if w:
                    seq.setdefault(n, []).append(w)
    if not seq and not noisy:
        return
    print("\nLC LATENCY (p99 ms, live TailBench feed; DEBUG ONLY, nothing graded on it):")
    for n in sorted(set(seq) | set(noisy)):
        nz = noisy.get(n, [])
        nz_s = f"noisy med {st.median(nz):.0f} max {max(nz):.0f}" if nz else "noisy --"
        ws = seq.get(n, [])
        fresh = [w["p99"] for w in ws if w.get("age_s") is not None and w["age_s"] <= FRESH_S]
        if not fresh:
            print(f"  {n:<16} {nz_s}   sweep: no fresh windows ({len(ws)} probes)")
            continue
        q = max(1, len(fresh) // 4)
        early, late = st.median(fresh[:q]), st.median(fresh[-q:])
        climb = "  <-- CLIMBING across the sweep: check the knee" if late > 2 * early else ""
        print(f"  {n:<16} {nz_s}   sweep: {len(fresh)}/{len(ws)} fresh, "
              f"med {st.median(fresh):.0f} max {max(fresh):.0f}, "
              f"first-quartile {early:.0f} -> last {late:.0f}{climb}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wdir")
    ap.add_argument("--harm", type=float, default=10.0,
                    help="percent below unmanaged that counts as harm")
    ap.add_argument("--topk", type=int, default=20,
                    help="how many top-sum-IPC configs to check for harm")
    ap.add_argument("--state", default="",
                    help="chunked-search checkpoint (default: "
                         "<wdir>/../spider_state.json)")
    a = ap.parse_args()

    # meta comes from whichever phase dirs exist (one per chunk)
    meta = {}
    sdir = None
    for d in sorted(glob.glob(os.path.join(a.wdir, "phase_spider*"))):
        for k, v in load_meta(os.path.join(d, "decisions.jsonl")).items():
            meta.setdefault(k, []).extend(v)
        sdir = d
    beg = (meta.get("search_begin") or [{}])[0]
    tenants = beg.get("tenants")
    if not tenants:
        sys.exit("no search_begin record -- cannot determine tenant order")

    state = a.state or os.path.join(os.path.dirname(a.wdir), "spider_state.json")
    if not os.path.exists(state):
        sys.exit(f"no search checkpoint at {state} (pass --state)")
    probes, sst, drift, resid = load_state(state, tenants)
    if not probes:
        sys.exit("checkpoint has no measured configurations")
    base = unmanaged_ipc(a.wdir, tenants)
    missing = [t for t in tenants if t not in base]

    print("=" * 74)
    print("SpiderSense ALGORITHM 2 (exhaustive max-sum-IPC) -- arm report")
    print("=" * 74)
    print(f"scene      : {len(tenants)} tenants, {beg.get('nbits','?')} ways")
    print(f"space      : M = {beg.get('m','?')} configurations "
          f"(measured {len(probes)})")
    print(f"chunks     : {sst.get('chunks','?')} scene incarnations; "
          f"between-chunk drift {drift:.4f} (removed by anchor correction)")
    print(f"resolution : residual anchor noise {resid:.4f}, so the noise floor "
          f"on any\n             sum-IPC difference is {2*resid:.4f}")
    print(f"cadence    : settle {beg.get('settle','?')}s + interval "
          f"{beg.get('interval','?')}s  (their Table 2 interval = 1.0s)")
    dev = beg.get("deviations") or []
    print(f"deviations : {', '.join(dev) if dev else '(none recorded)'}")

    # ---- Q2: cost of search -------------------------------------------------
    done = meta.get("scan_done", [{}])[0]
    secs = done.get("search_secs")
    if secs:
        print(f"\nCOST OF SEARCH (invisible in their evaluation, which measures")
        print(f"only the converged state):")
        print(f"  {len(probes)} live reconfigurations over {secs/60:.1f} min "
              f"of continuous perturbation")
        print(f"  re-arms on every tenant arrival/departure")

    if meta.get("search_truncated"):
        print("  !! SEARCH TRUNCATED by budget -- the argmax is over a PREFIX "
              "of the space; do not call this converged")

    # ---- instance drift -----------------------------------------------------
    print("\nINSTANCE FINGERPRINT (LLC-loads per kilo-instruction; our tenants "
          "restart-loop,\nso a long scan spans several instances -- 3% guard):")
    drift_flagged = 0
    for t in tenants:
        fps = [p["fp"].get(t, 0.0) for p in probes if p.get("fp")]
        fps = [f for f in fps if f > 0]
        if not fps:
            print(f"  {t:<16} no fingerprint data")
            continue
        med = st.median(fps)
        off = sum(1 for f in fps if abs(f - med) / med > 0.03) if med else 0
        drift_flagged += off
        print(f"  {t:<16} median {med:8.3f}   {off:4d}/{len(fps)} probes >3% off "
              f"({100.0*off/len(fps):.1f}%)")
    if drift_flagged:
        print("  -> probes off the median were measured against a different "
              "tenant instance.\n     Randomized visit order keeps this as "
              "noise rather than a positional bias.")

    lc_latency_report(a.wdir)

    if missing:
        print(f"\n!! no unmanaged baseline for {missing} -- cannot grade harm "
              f"for those tenants")
    if not base:
        sys.exit("\nno phase_noisy perf data; cannot compute harm")

    # ---- relative-to-unmanaged per probe ------------------------------------
    graded = [t for t in tenants if t in base]

    def rel(p):
        """per-tenant IPC relative to unmanaged, and the worst of them"""
        r = {t: (p["ipc"].get(t, 0.0) / base[t] - 1.0) * 100.0 for t in graded}
        return r, min(r.values())

    rows = []
    for p in probes:
        r, worst = rel(p)
        rows.append((p, r, worst))

    # ---- Q1a: the config their objective actually selects --------------------
    conv = meta.get("converged", [{}])[0]
    print("\n" + "-" * 74)
    print("Q1: WHAT THE OBJECTIVE SELECTS")
    print("-" * 74)
    if conv.get("ways"):
        print(f"BestConfiguration = {conv['ways']}   (sum IPC "
              f"{conv.get('sum_ipc','?')})")
        # grade the HELD window from the rotated counters, not the 1s probe
        held = {}
        for t in graded:
            v = true_ipc(os.path.join(sdir, f"perf_{t}.json"))
            if v:
                held[t] = (v / base[t] - 1.0) * 100.0
        if held:
            print(f"\nheld window vs unmanaged (graded counters, {len(held)} "
                  f"tenants):")
            for t in graded:
                if t in held:
                    flag = "  <== HARMED" if held[t] < -a.harm else ""
                    print(f"  {t:<16} {held[t]:+7.1f}%{flag}")
            hurt = [t for t in held if held[t] < -a.harm]
            print(f"\n  verdict: {'HARMS ' + ','.join(hurt) if hurt else 'no tenant below the harm threshold'}")
            print(f"  (harm threshold {a.harm:.0f}% below unmanaged)")
    else:
        print("no `converged` record -- arm did not finish its search")

    # ---- Q1b: the RANKING claim (robust to not finding the true argmax) -----
    print("\n" + "-" * 74)
    print(f"Q1b: DOES max-sum-IPC SELECT FOR PER-TENANT SAFETY?  (top-"
          f"{a.topk} check)")
    print("-" * 74)
    rows.sort(key=lambda x: -x[0]["sum_ipc"])
    top = rows[:a.topk]
    harmful = sum(1 for _, _, w in top if w < -a.harm)
    print(f"of the {len(top)} highest summed-IPC configurations, {harmful} "
          f"harm at least one tenant by >{a.harm:.0f}%")
    print(f"\n  {'rank':>4}  {'sum IPC':>8}  {'worst tenant':>14}  {'worst %':>8}")
    for i, (p, r, w) in enumerate(top[:10], 1):
        wt = min(r, key=r.get)
        print(f"  {i:>4}  {p['sum_ipc']:>8.3f}  {wt:>14}  {w:>+7.1f}%")

    # the whole-space picture: is the objective even correlated with safety?
    ss = [p["sum_ipc"] for p, _, _ in rows]
    ws = [w for _, _, w in rows]
    n = len(ss)
    if n > 2:
        ms, mw = st.mean(ss), st.mean(ws)
        num = sum((ss[i] - ms) * (ws[i] - mw) for i in range(n))
        den = (sum((x - ms) ** 2 for x in ss) * sum((x - mw) ** 2 for x in ws)) ** 0.5
        r_p = num / den if den else 0.0
        print(f"\nacross all {n} probed configurations:")
        print(f"  corr(sum IPC, worst-tenant-vs-unmanaged) = {r_p:+.3f}")
        print(f"  configs harming someone >{a.harm:.0f}%: "
              f"{sum(1 for w in ws if w < -a.harm)}/{n} "
              f"({100.0*sum(1 for w in ws if w < -a.harm)/n:.0f}%)")
        best_safe = [x for x in rows if x[2] >= -a.harm]
        if best_safe:
            bs = best_safe[0]
            print(f"  best HARM-FREE config: sum IPC {bs[0]['sum_ipc']:.3f} "
                  f"= {100.0*bs[0]['sum_ipc']/rows[0][0]['sum_ipc']:.1f}% of "
                  f"their argmax, ways {bs[0]['ways']}")
            print("  -> a harm-free configuration EXISTS in their own search "
                  "space; their\n     objective simply does not select it.")
        else:
            print("  no harm-free configuration in the probed space -- the "
                  "scene is zero-sum,\n     which is a finding about the scene, "
                  "not about their objective.")

    # secondary within-scan reference at the SAME cadence
    eq = [x for x in rows if len(set(x[0]["comp"])) <= 2
          and max(x[0]["comp"]) - min(x[0]["comp"]) <= 1]
    if eq:
        p, r, w = eq[0]
        print(f"\nwithin-scan equal-split reference (same 1s cadence): "
              f"ways {p['comp']}, sum IPC {p['sum_ipc']:.3f}, worst {w:+.1f}%")

    print("\n" + "=" * 74)
    print("Baseline caveat: unmanaged IPC is a long phase_noisy window; each "
          "probe is a\n1 s window at the paper's own cadence. Per-probe values "
          "are noisy by\nconstruction -- the claims above are distributional "
          "(top-k, correlation,\nexistence), not per-probe.")
    print("=" * 74)


if __name__ == "__main__":
    main()
