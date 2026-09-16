#!/usr/bin/env python3
"""iso_report.py -- summarize run_iso.sh output and emit the reusable iso.json.

The isolated baseline is the PEER-STANDARD denominator (CoPart IPS_full/IPS_solo,
Themis solo-normalized progress, CLITE solo-isolation, PIVOT run-alone). Unlike
run_certify.sh's victims-only phase it depends only on the tenant, so it is
measured once per roster and reused across scenes.

It is keyed by INSTANCE FINGERPRINT (LLC-loads per kilo-instruction), because a
looping tenant can restart into a different steady state -- canneal has two
(2.90 vs 3.45) -- and an iso baseline from the wrong instance is a silent
divide-by-the-wrong-number. certify.py refuses a mismatch.
"""
import json, os, sys, statistics as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from peer_metrics import true_ipc


def counters(f):
    """{event: [per-interval values]} from a perf JSON-lines file."""
    ev = {}
    for line in open(f):
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
        ev.setdefault(o.get("event"), {})[o.get("interval")] = v
    return ev


def summarize(wdir, tenant):
    f = os.path.join(wdir, f"iso_{tenant}", f"perf_{tenant}.json")
    if not os.path.exists(f):
        return None
    ev = counters(f)
    ld, ins = ev.get("LLC-loads", {}), ev.get("instructions", {})
    fp = [ld[k] / ins[k] * 1000 for k in ld if k in ins and ins[k] > 0]
    ipc = true_ipc(f)
    if ipc is None:
        return None
    rec = {"ipc": ipc, "fp": st.median(fp) if fp else None,
           "intervals": len(ins)}
    # occupancy / bandwidth come from the daemon's own scan records
    d = os.path.join(wdir, f"iso_{tenant}", "decisions.jsonl")
    if os.path.exists(d):
        occ, mbm, hit = [], [], []
        for line in open(d):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") != "scan":
                continue
            t = (o.get("tenants") or {}).get(tenant)
            if not t:
                continue
            # the daemon's scan record already reports MB and GB/s -- these are
            # cmt_mb / mbm_gbps, NOT the raw cmt (bytes) / mbm (bytes/s) that
            # coeffd3's internal Tenant object carries.
            if t.get("cmt_mb") is not None:
                occ.append(t["cmt_mb"])
            if t.get("mbm_gbps") is not None:
                mbm.append(t["mbm_gbps"])
            if t.get("hit") is not None:
                hit.append(t["hit"])
        for k, v in (("occ_mb", occ), ("mbm_gbs", mbm), ("hit", hit)):
            rec[k] = st.median(v) if v else None
        rec["scans"] = len(occ)
    return rec


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: iso_report.py <results_iso/DIR>")
    wdir = sys.argv[1].rstrip("/")
    tenants = sorted(d[4:] for d in os.listdir(wdir) if d.startswith("iso_"))
    if not tenants:
        sys.exit(f"no iso_* directories under {wdir}")

    out, bad = {}, []
    for t in tenants:
        r = summarize(wdir, t)
        if r is None:
            bad.append(t)
            continue
        out[t] = r

    print(f"=== ISOLATED BASELINES -- {wdir} ===")
    print("  the peer-standard denominator: one tenant alone on the box.")
    print(f"  {'tenant':<16}{'IPC':>8}{'occ MB':>9}{'GB/s':>8}{'hit':>7}"
          f"{'fingerprint':>13}{'scans':>7}")
    for t, r in sorted(out.items()):
        fp = f"{r['fp']:.3f}" if r["fp"] is not None else "n/a"
        print(f"  {t:<16}{r['ipc']:>8.3f}{(r.get('occ_mb') or 0):>9.1f}"
              f"{(r.get('mbm_gbs') or 0):>8.1f}{(r.get('hit') or 0):>7.2f}"
              f"{fp:>13}{r.get('scans', 0):>7}")
    for t in bad:
        print(f"  {t:<16}  !! no usable perf data -- baseline MISSING")

    # A tenant whose fingerprint is ~0 has no LLC traffic at all (npb_ep_e on a
    # non-inclusive L3 reads 0.003 vs canneal's 2.94). The fingerprint cannot
    # identify its instance, so cross-run reuse for it is unguarded -- say so
    # rather than let certify.py silently trust it.
    weak = [t for t, r in out.items() if (r["fp"] or 0) < 0.10]
    if weak:
        print(f"\n  note: {', '.join(weak)} have <0.10 LLC-loads/KI -- no memory")
        print("        traffic to fingerprint. Their iso baseline is still valid,")
        print("        but the instance guard cannot verify it across runs.")

    f = os.path.join(wdir, "iso.json")
    json.dump(out, open(f, "w"), indent=2, sort_keys=True)
    print(f"\n  wrote {f}  ({len(out)} tenants)")
    # copart_style.py wants a FLAT {tenant: solo_ipc} map -- its slowdown is
    # Eq.1 IPS_full/IPS_current, and the isolated run IS its "full" reference.
    # CoPart's own profiling phase is what we are replacing with a real
    # measurement, so this file is the arm's fairness objective made honest.
    g = os.path.join(wdir, "solo_ipc.json")
    json.dump({t: r["ipc"] for t, r in out.items()}, open(g, "w"),
              indent=2, sort_keys=True)
    print(f"  wrote {g}  (CoPart Eq.1 denominator)")


if __name__ == "__main__":
    main()
