#!/usr/bin/env python3
"""certify.py -- grade a run_certify.sh scene.

Answers two questions the Phase-A way-sweep cannot answer on our 4MB/way L3:

  1. IS IT A VICTIM?  performance drop from solo to noisy, against the two peer
     conventions -- SpiderSense's LLC-critical bar (>10% under a noisy
     neighbour) and the stricter way-sweep bar (15%) for reference.
  2. WHICH CHANNEL?   of the performance the polluters took away, how much does
     a CAT fence give back, and how much does an MBA throttle give back? A
     cache-capacity victim recovers under CAT; a bandwidth victim recovers under
     MBA; canneal proved a tenant's own counters cannot predict which.

and reports the AGGRESSOR's cost in both recovery phases -- the accounting that
was missing when exp16's "harm-free" DNH-oracle result had to be retracted. A
fence that costs a no-reuse polluter nothing is the free lunch; an MBA throttle
that costs it 80% of its work is a real bill someone pays.

Usage: certify.py results_certify/<victim> [more...]
"""
import sys, os, re, json, glob, statistics as st
from peer_metrics import scene_ipc          # true IPC = instructions / CPU_CLK_UNHALTED.THREAD

# GRADING SIGNAL = IPC, NOT IPS (rule adopted 2026-08-10, see METRIC_VALIDATION V7).
# instructions/sec silently folds in the core clock. With turbo on, canneal's
# clock rose 8.5% from solo to loaded, which HALVED its apparent harm: the same
# scene reads -17.5% in IPS and -8.4% in IPC. Turbo is now forced off by
# bench_lib's require_turbo_off, but IPS would still drift with any DVFS or
# HT-residency change, so the grade is taken on IPC and IPS is kept only as a
# secondary column.

PHASES = ["solo", "noisy", "eq", "cat", "cat_cap", "mba",
          "copart", "dcat", "spider", "spider_conv", "satori", "palloc",
          "cacheman", "null", "sat_conv", "cm_nest", "nest_dp", "band_dp",
          "c3", "c4", "dsolo"]
# arms that may legitimately be absent: cat_cap only runs when a CAP tenant was
# named, and no scene run before 2026-08-10 has one.
OPTIONAL_PHASES = {"cat_cap", "eq", "copart", "dcat", "spider",
                   "spider_conv", "satori", "palloc", "cacheman",
                   "null", "sat_conv", "cm_nest", "nest_dp", "band_dp",
                   "c3", "c4", "dsolo"}
# Phases driven by a live controller rather than a static mask. They are graded
# on the LAST scans only: coeffd4 spends its first cycles anchoring the
# unmanaged baseline (BASELINE_CYCLES) and then converges one verified step at a
# time, so a median over the whole window would average the converged state
# together with the unmanaged state it started from and understate whatever the
# controller achieved -- or hide whatever it broke.
CONTROLLER_PHASES = {"copart", "dcat", "spider", "satori", "palloc",
                     "cacheman", "null", "c3", "c4"}
# CoPart's 5-policy ladder (EQ / ST / CAT-only / MBA-only / theirs), which
# dCat + PARTIES + CLITE also use in some form. The single-lever arms are what
# PROVE coordination matters; EQ is PaLLOC's normalizer. Order = table order.
# NB spider_conv is deliberately NOT in CONTROLLER_PHASES: it is a static mask
# (SpiderSense's converged answer replayed), so the whole window is its steady
# state and a tail-only median would just discard data.
LADDER = ["eq", "cat", "cat_cap", "mba", "copart", "dcat", "spider",
          "spider_conv", "satori", "palloc", "cacheman", "null",
          "sat_conv", "cm_nest", "nest_dp", "band_dp", "c3", "c4"]
CONTROLLER_TAIL = 6          # scans (~60s at DECIDE_SECS=10) judged as converged
VICTIM_GATE = 10.0    # SpiderSense Fig9: >10% degradation under noisy neighbour
STRICT_GATE = 15.0    # PaLLOC/CoPart/dCat way-sweep convention, for reference
RECOVER_GATE = 25.0   # % of lost performance a channel must give back to count
FREE_GATE = 5.0       # aggressor cost at or under this = squeeze-free donor
# A tenant is only comparable ACROSS phases if it was in the same phase of its own
# execution in each. A looping batch job that finishes and re-enters its init
# stage mid-scene is not: cg_d20 read hit 0.94 in solo/noisy/mba and 0.03 in cat
# (matrix generation), producing a nonsense "+7948% recovery". Hit rate is the
# cleanest phase fingerprint we have, so flag any tenant whose hit swings more
# than this across phases and refuse to quote its recovery.
PHASE_SPREAD = 0.25
# ── INSTANCE-CONSISTENCY GUARD (added 2026-09-04, and it supersedes the hit
# guard for this failure mode). LLC-loads per instruction is a PRE-LLC quantity:
# no CAT mask and no MBA setting can move it, so it fingerprints WHICH PROCESS
# INSTANCE is running. A looping tenant that restarts mid-scene can come back
# with materially different memory behaviour, and then an arm is measured on a
# different instance than the solo/noisy baseline it is divided by.
#
# MEASURED (D3v2 vs D3v3, identical scene): npb_cg_d80 holds this to 1.0% across
# all six arms in BOTH runs, and canneal holds 0.9% in D3v2 -- but canneal in
# D3v3 jumps 2.90 -> 3.45 (15.8%) starting mid-`cat` and stays there through
# cat_cap and mba. That single fact accounts for the ~50pp recovery swing: the
# denominator (solo-noisy = 0.148 IPC) is small, so 1% of canneal's IPC is ~8pp
# of recovery, and the instance penalty is ~6-8% of IPC.
#
# WHY THE hit-SPREAD GUARD CANNOT CATCH IT: canneal's hit spread was 0.14, under
# the 0.25 threshold -- and it moved the MISLEADING way. The extra L2 misses all
# HIT in L3, so the new instance looks BETTER cached (hit 0.84 vs 0.81) while
# running 6% slower. hit is a ratio whose denominator moved 19%.
#
# SCOPE: only valid for tenants whose MBA level is constant across the compared
# arms -- an MBA-throttled tenant's ratio moves for real (mg_d300 goes 2.7 -> 9.1
# under `mba`). Victims are never throttled, so it applies exactly where needed.
INSTANCE_SPREAD = 0.03       # 3%; measured arm-insensitivity is 0.9-1.0%
INSTANCE_MIN_FP = 0.10       # LLC-loads/KI below this = no memory traffic to
                             # fingerprint (npb_ep reads 0.003 and a RELATIVE
                             # spread on near-zero is meaningless -- it flagged
                             # the negative control as phase-inconsistent)


# ── ISOLATED BASELINE (run_iso.sh / iso_report.py) ─────────────────────────────
# Our `solo` phase is victims-together-without-donors, NOT isolation. The peers
# all normalize to a tenant running ALONE (CoPart IPS_full/IPS_solo, Themis
# solo-normalized progress, CLITE solo-isolation, PIVOT run-alone; Alita lists
# solo and free-share as SEPARATE rungs). Ours therefore hides victim-on-victim
# contention inside the baseline and UNDERSTATES harm. Report both:
#   deg      = harm attributable TO THE DONORS   (vonly -> noisy)  = our mechanism
#   slowdown = peer-standard                     (iso   -> noisy)  = comparable
ISO_DIR = os.environ.get("ISO_DIR", "results_iso")


def load_iso(scene_dir):
    """{tenant: rec} from results_iso/<AS>/iso.json, or {} if none."""
    name = os.path.basename(scene_dir.rstrip("/"))
    cands = [os.path.join(ISO_DIR, name, "iso.json")]
    # a roster's iso baseline is reusable across that roster's scenes, so allow
    # an explicit pointer and a family fallback (D3v9 -> D3)
    # An iso baseline belongs to a ROSTER, and every scene of that roster should
    # find it: D3v9 -> D3 (run-number suffix) and D3_spider / D3_c4 -> D3 (the
    # ladder's one-controller-per-dir naming). Strip the arm suffix FIRST, then
    # the run number. Getting this wrong is silent: load_iso returns {} and the
    # whole peer-standard slowdown table vanishes with no error.
    # NB: rstrip("0123456789") is WRONG here -- on "D3_spider" the arm suffix is
    # already gone and it would eat the roster's own digit, giving "D". Strip a
    # trailing v<runnumber> specifically.
    fam = re.sub(r"v\d+$", "", name.split("_", 1)[0])
    if fam and fam != name:
        cands.append(os.path.join(ISO_DIR, fam, "iso.json"))
    if os.environ.get("ISO_JSON"):
        cands.insert(0, os.environ["ISO_JSON"])
    for f in cands:
        if os.path.exists(f):
            try:
                return json.load(open(f)), f
            except ValueError:
                pass
    return {}, None


def instance_fp(wdir, tenant):
    """{phase: LLC-loads per kilo-instruction} for one tenant."""
    out = {}
    for p in PHASES:
        f = os.path.join(wdir, f"phase_{p}", f"perf_{tenant}.json")
        if not os.path.exists(f):
            continue
        ld, ins = {}, {}
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
            ev, i = o.get("event"), o.get("interval")
            if ev == "LLC-loads":
                ld[i] = v
            elif ev == "instructions":
                ins[i] = v
        vals = [ld[k] / ins[k] * 1000 for k in ld if k in ins and ins[k] > 0]
        if vals:
            out[p] = st.median(vals)
    return out


def phase_stats(od, tail=None):
    """median per-tenant signals over the scans of one phase.

    tail=N: use only the last N scans (controller phases -- see CONTROLLER_TAIL).
    """
    acc = {}
    p = os.path.join(od, "decisions.jsonl")
    if not os.path.exists(p):
        return {}
    if tail:
        scans = [l for l in open(p) if '"kind": "scan"' in l or '"kind":"scan"' in l]
        lines = scans[-tail:]
    else:
        lines = open(p)
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("kind") != "scan":
            continue
        for name, t in o.get("tenants", {}).items():
            d = acc.setdefault(name, {"ips": [], "mbm": [], "hit": [], "db": [], "cmt": [],
                                      "p99": []})
            # p99 (ms) = LC tenants' live TailBench feed, None for batch tenants
            # and for a stale feed. DEBUG/side metric only -- nothing is graded on it.
            for k, src in (("ips", "ips"), ("mbm", "mbm_gbps"), ("hit", "hit"),
                           ("db", "db"), ("cmt", "cmt_mb"), ("p99", "p99")):
                v = t.get(src)
                if v is not None:
                    d[k].append(v)
    out = {}
    for n, d in acc.items():
        out[n] = {k: (st.median(v) if v else None) for k, v in d.items()}
        # max beside the median: a p99 climbing scan to scan (over the knee) shows
        # up as max >> median, which a median alone hides
        out[n]["p99max"] = max(d["p99"]) if d["p99"] else None
    return out


def pct(a, b):
    """percent change from a to b, None-safe."""
    if not a or b is None:
        return None
    return (b - a) / a * 100.0


def inert_arms(wdir):
    """Arms that ran to completion but DID NOTHING over the graded window.

    CACHEMAN_AUDIT.md S9 / B2. Cacheman's two inertness guards (its stall
    detector and its CMT-unreadable abort) are provably unable to fire while
    its pool_full gate is open, and on this box that gate is open in every
    cycle (finding N2). So the arm can exit 0, look perfectly healthy, and
    still have sat at level 0 -- the unmanaged 15-way mask -- for the whole
    window certify grades. Scoring that stretch would publish UNMANAGED
    SHARING under a controller's name, which is the single most dangerous
    failure mode in this comparison: it cannot show up as a crash, only as a
    plausible-looking number.

    The controller states this about itself in an `activity-summary` record
    whose tail window is pinned to CONTROLLER_TAIL*10 = 60 s, i.e. the SAME
    seconds graded here. Any arm that emits one with inert_tail=true has its
    recovery digits suppressed (-> 'INERT') rather than printed.

    Returns {arm: summary_record}. Arms that emit no such record (every arm
    other than cacheman today) are simply absent -- this suppresses nothing
    it was not asked to.
    """
    out = {}
    for p in PHASES:
        f = os.path.join(wdir, f"phase_{p}", "decisions.jsonl")
        if not os.path.exists(f):
            continue
        for line in open(f):
            if '"activity-summary"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("kind") == "activity-summary" and o.get("inert_tail"):
                out[p] = o
    return out


def grade(wdir):
    ph = {p: phase_stats(os.path.join(wdir, f"phase_{p}"),
                         tail=CONTROLLER_TAIL if p in CONTROLLER_PHASES else None)
          for p in PHASES}
    # overlay true IPC from the raw perf counters; absent => that tenant simply
    # has no ipc key and every ipc-based figure for it reports "--" rather than
    # silently falling back to a different metric.
    # controller phases are graded on their CONVERGED TAIL in IPC as well as in
    # the decisions signals -- see peer_metrics.true_ipc's tail note.
    # CONTROLLER_TAIL scans x 10 s = 60 s, converted per perf file by its own
    # interval (dcat logs 1 s, satori 100 ms, the rest 2 s) -- equal SECONDS
    # for every arm, bit-identical to the old 30-interval count for 2 s files
    _tail_s = {p: CONTROLLER_TAIL * 10 for p in CONTROLLER_PHASES}
    for p, row in scene_ipc(wdir, tail_s=_tail_s).items():
        for n, v in row.items():
            ph.setdefault(p, {}).setdefault(n, {})["ipc"] = v
    # victims = whoever ran in the solo phase; donors = whoever joined for noisy
    vics = sorted(ph.get("solo", {}))
    dsolo = ph.get("dsolo", {})          # donors alone, their own counterfactual
    pols = sorted(n for n in ph.get("noisy", {}) if n not in vics)
    if not vics:
        return None

    def sig(p, n, k="ipc"):
        return (ph.get(p, {}).get(n) or {}).get(k)

    # phase-consistency: same tenant, same execution stage in every phase?
    incons = {}
    bad_arm = {}          # (tenant, arm) -> measured on a different instance
    for n in vics:
        fp = {p: v for p, v in instance_fp(wdir, n).items() if p != "dsolo"}
        base = [fp[p] for p in ("solo", "noisy") if p in fp]
        if not base or st.median(base) < INSTANCE_MIN_FP:
            continue                     # no memory traffic to fingerprint
        b = st.median(base)
        off = {p: abs(v - b) / b for p, v in fp.items() if p not in ("solo", "noisy")}
        for p, dev in off.items():
            if dev > INSTANCE_SPREAD:
                bad_arm[(n, p)] = (dev, round(fp[p], 3), round(b, 3))
        if bad_arm:
            spread = (max(fp.values()) - min(fp.values())) / max(fp.values())
            worst = max(off, key=off.get) if off else "?"
            incons.setdefault(n, (spread, worst,
                                  {p: round(v, 3) for p, v in fp.items()}, "instance"))
    for n in vics + pols:
        hits = {p: sig(p, n, "hit") for p in PHASES if sig(p, n, "hit") is not None}
        if len(hits) >= 2 and (max(hits.values()) - min(hits.values())) > PHASE_SPREAD:
            med = st.median(list(hits.values()))
            odd = max(hits, key=lambda p: abs(hits[p] - med))   # phase furthest from the rest
            incons[n] = (max(hits.values()) - min(hits.values()), odd, hits, "hit")

    inert = inert_arms(wdir)
    per = {}
    for v in vics:
        solo, noisy = sig("solo", v), sig("noisy", v)
        deg = pct(solo, noisy)
        lost = (solo - noisy) if (solo and noisy is not None) else None
        rec = {}
        for p in LADDER:
            x = sig(p, v)
            # B2: an arm that took no action over the graded window is NOT a
            # recovery result -- it is unmanaged sharing wearing a
            # controller's name. Suppress the digit rather than print it.
            if p in inert:
                rec[p] = None
                continue
            rec[p] = (100.0 * (x - noisy) / lost) if (lost and lost > 0 and x is not None) else None
        per[v] = dict(solo=solo, noisy=noisy, deg=deg, rec=rec,
                      incons=incons.get(v))

    # DONOR COST IS PER-DONOR AND IS NEVER AVERAGED. A scene may deliberately mix
    # a squeeze-free donor with one that is not (npb_mg_d hit 0.02 vs npb_ft_d
    # hit 0.49, to exercise the Change-3 rollback path). Averaging those two
    # lets the non-free donor's loss contaminate the one number the fence claim
    # rests on -- "the fence costs the squeeze-free donor ~2% where MBA costs it
    # ~94%" is a statement about a NAMED donor, not about a mean.
    cost = {}
    for p in LADDER:
        if p in inert:
            cost[p] = {n: None for n in pols}   # B2: see inert_arms()
            continue
        cost[p] = {n: pct(sig("noisy", n), sig(p, n)) for n in pols}

    # bandwidth fraction, READ from the run rather than assumed. The fence
    # result is only interpretable while harm is cache-mediated; past ~70% of
    # peak the scene converts into a bandwidth/throttle scene and the fence is
    # the wrong lever (the exp16 Mix A failure).
    bw = {}
    for p in PHASES:
        tot = [sig(p, n, "mbm") for n in vics + pols]
        tot = [x for x in tot if x is not None]
        bw[p] = sum(tot) if tot else None

    # the condition the first failed scene taught us: was the victim pushed BELOW
    # its knee? we cannot know its knee here, but we CAN report the occupancy the
    # fence actually moved, which is what bounds any possible recovery.
    freed = None
    a = sum(x for x in (sig("noisy", n, "cmt") for n in pols) if x)
    b = sum(x for x in (sig("cat", n, "cmt") for n in pols) if x)
    if a and b is not None:
        freed = a - b
    return dict(vics=vics, pols=pols, ph=ph, per=per, cost=cost, freed=freed,
                incons=incons, bw=bw, bad_arm=bad_arm, inert=inert)


def fmt(x, unit="%", nd=1):
    return "--" if x is None else f"{x:+.{nd}f}{unit}"



def ladder_table(rows):
    """Merge a LADDER run across its per-controller directories.

    run_ladder.sh gives each controller its OWN fresh scene, so the arms live in
    DIFFERENT result dirs (D3_spider, D3_copart, ...). The per-dir tables above
    then print one row per (dir, victim) with no dir label and one populated
    column each -- correct numbers, unreadable as a comparison. This merges them:
    one row per victim, each arm taken from whichever run actually ran it.

    Each arm's recovery is measured against ITS OWN run's unmanaged baseline,
    which is the point of the fresh-scene design -- so `degrad` is reported per
    run as a SCENE-EQUIVALENCE check. If the runs degraded the victim by very
    different amounts they were not the same scene, and the arms are not
    comparable no matter how clean each one is on its own.
    """
    if len(rows) < 2:
        return
    # arm -> (dirname, value) for each victim, plus per-run degradation
    vics, degs, arms, dup = [], {}, {}, []
    for d, r in rows:
        dn = os.path.basename(d.rstrip("/"))
        for v in r["vics"]:
            if v not in vics:
                vics.append(v)
            if r["per"][v]["deg"] is not None:
                degs.setdefault(v, {})[dn] = r["per"][v]["deg"]
            # Recovery is a ratio against what was LOST. For a tenant that lost
            # almost nothing the denominator is near zero and the ratio is
            # nonsense (npb_cg_d80 read -466.2% off a -3.9% degradation). The
            # main table already gates on this; the merge must too.
            deg = r["per"][v]["deg"]
            if deg is None or -deg <= VICTIM_GATE:
                continue
            for a in LADDER:
                x = r["per"][v]["rec"].get(a)
                if x is None or (v, a) in r["bad_arm"]:
                    continue
                if a in arms.get(v, {}):
                    # each arm should come from exactly ONE run in a ladder; a
                    # duplicate means the runs overlap and one would be silently
                    # dropped by the dict write.
                    prev = arms[v][a][0]
                    dup.append(f"{v}/{a}: {prev} and {dn}")
                    continue
                arms.setdefault(v, {})[a] = (dn, x)
    if not any(arms.values()):
        return

    print("\n" + "=" * 128)
    print("LADDER COMPARISON -- merged across runs, each arm graded WITHIN its own scene")
    print("=" * 128)
    if dup:
        print("  !! ARM RAN IN MORE THAN ONE RUN -- showing the FIRST, ignoring:")
        for x in dup:
            print(f"       {x}")
        print("     (in a ladder each arm should come from exactly one run)")
    for v in vics:
        got = arms.get(v, {})
        if not got:
            continue
        dv = degs.get(v, {})
        print(f"\n  {v}")
        if dv:
            lo, hi = min(dv.values()), max(dv.values())
            spread = abs(hi - lo)
            flag = ("" if spread <= 3.0 else
                    f"   !! {spread:.1f} pp spread -- scenes NOT equivalent, arms not comparable")
            print(f"    unmanaged degradation per run: "
                  + ", ".join(f"{k} {x:+.1f}%" for k, x in sorted(dv.items())) + flag)
        print(f"    {'arm':<10}{'from run':<14}{'recovery':>10}")
        for a in LADDER:
            if a in got:
                dn, x = got[a]
                print(f"    {a:<10}{dn:<14}{x:>+9.1f}%")

    # donor cost, per donor, per arm -- never averaged (see the note above)
    dcost = {}
    for d, r in rows:
        dn = os.path.basename(d.rstrip("/"))
        for a in LADDER:
            for n, x in r["cost"].get(a, {}).items():
                if x is not None:
                    dcost.setdefault(n, {})[a] = (dn, x)
    if dcost:
        print(f"\n  DONOR COST per arm (vs that run's own unmanaged baseline):")
        print(f"    {'donor':<16}" + "".join(f"{a:>10}" for a in LADDER))
        for n, byarm in sorted(dcost.items()):
            print(f"    {n:<16}"
                  + "".join((f"{byarm[a][1]:>+9.1f}%" if a in byarm else f"{'--':>10}")
                            for a in LADDER))

def main():
    dirs = [d for d in (sys.argv[1:] or sorted(glob.glob("results_certify/*"))) if os.path.isdir(d)]
    if not dirs:
        sys.exit("no results_certify/<scene> dirs -- run run_certify.sh first")
    rows = [(d, r) for d, r in ((d, grade(d)) for d in dirs) if r]
    if not rows:
        sys.exit("no gradable results (empty phases? check phase_*/decisions.jsonl)")

    print("=" * 128)
    print("SCENE CERTIFICATION -- is there a free lunch, can we take it, and who pays?")
    print(f"gates: victim if degradation >{VICTIM_GATE}% (SpiderSense Fig9; {STRICT_GATE}% "
          f"= floor for quoting recovery DIGITS; orderings hold below it)")
    print(f"       channel counts if it returns >{RECOVER_GATE}% of what was lost; "
          f"donor squeeze-free if cost <={FREE_GATE}%")
    print("=" * 128)
    # CoPart's 5-policy ladder (EQ / ST / CAT-only / MBA-only / theirs), which
    # dCat+PARTIES+CLITE also use in some form: the single-lever arms are what
    # PROVE coordination matters, and EQ is PaLLOC's normalizer.
    print(f"{'scene / victim':<22}{'degrad':>9}{'victim?':>9}"
          + "".join(f"{a:>8}" for a in LADDER) + f"   {'VERDICT':<30}")
    print("-" * 128)
    for d, r in rows:
        # "free fence" means free for EVERY donor it touched. If any donor paid
        # more than the gate, the fence was not free -- that is the whole point
        # of putting a non-squeeze-free donor in the scene.
        cvals = [x for x in r["cost"]["cat"].values() if x is not None]
        all_free = bool(cvals) and all(abs(x) <= FREE_GATE for x in cvals)
        # B2 (CACHEMAN_AUDIT.md S9): print INERT, not a blank. A blank reads
        # as "this arm was not run"; INERT means it RAN, completed cleanly,
        # and did nothing -- a result about the controller. Scene-scoped, so
        # it still prints for a scene with no victims at all.
        inert_here = [a for a in LADDER if a in r.get("inert", {})]
        for v in r["vics"]:
            e = r["per"][v]
            deg = e["deg"]
            # one dict, so adding a controller arm does not mean editing four
            # parallel places (which is how `eq` stayed unreported for weeks)
            arms = {a: e["rec"].get(a) for a in LADDER}
            isvic = "--" if deg is None else ("YES" if -deg > VICTIM_GATE else "no")
            # recovery is a ratio against what was lost -- meaningless when almost
            # nothing was lost, and unreadable when the phases are not comparable
            if deg is None or -deg <= VICTIM_GATE:
                arms = {a: None for a in LADDER}
            for a in LADDER:
                if (v, a) in r["bad_arm"]:
                    arms[a] = None

            cat, mba = arms["cat"], arms["mba"]
            cap, c4 = arms["cat_cap"], arms["c4"]
            nbad = sum(1 for (tn, _p) in r["bad_arm"] if tn == v)
            if nbad:
                verdict = f"{nbad} arm(s) on a DIFFERENT INSTANCE -- suppressed"
            elif deg is None or -deg <= VICTIM_GATE:
                verdict = "not a victim in this scene"
            elif -deg <= STRICT_GATE:
                # A MANAGEMENT tool intervenes on 10%+ harm (SpiderSense Fig9);
                # 15% was only ever the floor for quoting recovery DIGITS, since
                # run-to-run recovery noise is +/-8-12pp. Between the two gates
                # the tenant is a real victim and the ORDERING of levers is
                # trustworthy -- the digits are not. Decision 2026-09-03.
                verdict = "VICTIM (10% gate); recovery ordering only, +/-8-12pp"
            else:
                hits = [(n, x) for n, x in (("cache", cat), ("bandwidth", mba))
                        if x is not None and x > RECOVER_GATE]
                if not hits:
                    verdict = "harmed, NEITHER lever recovers"
                elif len(hits) == 2:
                    verdict = "harmed, both channels respond"
                else:
                    verdict = f"{hits[0][0]}-capacity victim"
                    if hits[0][0] == "cache" and all_free:
                        verdict += " (FREE fence)"
            print(f"{v:<22}{fmt(deg):>9}{isvic:>9}"
                  + "".join(("   INERT" if a in r.get("inert", {})
                             else f"{fmt(arms[a]):>8}") for a in LADDER)
                  + f"   {verdict:<30}")
        for a in inert_here:
            su = r["inert"][a]
            print(f"    !! arm '{a}' ran to completion but TOOK NO ACTION over "
                  f"the graded window: every tenant sat at level 0 (the "
                  f"unmanaged mask)")
            print(f"       for the last {su.get('tail_cycles')} cycles "
                  f"({su.get('total_actuations')} actuations earlier in the "
                  f"run). Report as 'took no action', NEVER as a recovery "
                  f"number. See CACHEMAN_AUDIT.md S9/B2.")
    print("-" * 128)

    ladder_table(rows)

    for d, r in rows:
        print(f"\n--- {os.path.basename(d)}   victims: {', '.join(r['vics'])}"
              f"   donors: {', '.join(r['pols']) or 'none'}")
        if r["pols"]:
            print(f"    DONOR ACCOUNTING (per donor -- never averaged):")
            print(f"      'co-loc' = what SHARING already cost it (dsolo -> noisy), the")
            print(f"      donor's own counterfactual; the rest = what OUR LEVER cost it")
            print(f"      on top (vs noisy). A donor can be a victim too.")
            print(f"    {'donor':<16}{'hit':>7}{'co-loc':>9}"
                  + "".join(f"{a:>9}" for a in LADDER))
            for n in r["pols"]:
                h = (r["ph"].get("noisy", {}).get(n) or {}).get("hit")
                ds = (r["ph"].get("dsolo", {}).get(n) or {}).get("ipc")
                ny = (r["ph"].get("noisy", {}).get(n) or {}).get("ipc")
                coloc = pct(ds, ny) if (ds and ny) else None
                print(f"    {n:<16}{(f'{h:.2f}' if h is not None else '--'):>7}"
                      f"{fmt(coloc):>9}"
                      + "".join(f"{fmt(r['cost'][p].get(n)):>9}" for p in LADDER))
            if not r["ph"].get("dsolo"):
                print(f"    !! no dsolo phase -- donor co-location harm UNMEASURED "
                      f"(re-run; RUNDSOLO=1)")
        iso, isof = load_iso(d)
        if iso:
            print(f"\n    PEER-STANDARD SLOWDOWN vs ISOLATED baseline ({isof}):")
            print(f"      the denominator CoPart/Themis/CLITE/PIVOT all use. Our")
            print(f"      'degrad' column above is harm attributable to the DONORS")
            print(f"      only -- it hides victim-on-victim contention in its baseline.")
            print(f"    {'tenant':<16}{'iso IPC':>9}{'noisy':>9}{'slowdown':>10}"
                  f"{'LLC-crit?':>11}{'fp iso':>9}{'fp scene':>10}{'':>3}")
            for n in list(r["vics"]) + list(r["pols"]):
                rec = iso.get(n)
                ny = (r["ph"].get("noisy", {}).get(n) or {}).get("ipc")
                if not rec or ny is None:
                    print(f"    {n:<16}{'--':>9}{'--':>9}{'--':>10}"
                          f"{'--':>11}{'--':>9}{'--':>10}   no iso baseline")
                    continue
                sd = pct(rec["ipc"], ny)
                # SpiderSense: LLC-critical = >10% drop under a noisy neighbour,
                # measured against ISOLATION. This is that test, for the first time.
                crit = "YES" if (sd is not None and sd < -VICTIM_GATE) else "no"
                scene_fp = instance_fp(d, n).get("noisy")
                fi, fs = rec.get("fp"), scene_fp
                warn = ""
                if fi and fs and fi >= INSTANCE_MIN_FP:
                    if abs(fs - fi) / fi > INSTANCE_SPREAD:
                        warn = "   !! INSTANCE MISMATCH -- iso baseline NOT valid here"
                elif not fi or fi < INSTANCE_MIN_FP:
                    warn = "   (no LLC traffic: instance unguarded)"
                print(f"    {n:<16}{rec['ipc']:>9.3f}{ny:>9.3f}{fmt(sd):>10}"
                      f"{crit:>11}{(f'{fi:.3f}' if fi else '--'):>9}"
                      f"{(f'{fs:.3f}' if fs else '--'):>10}{warn}")
        else:
            print(f"\n    !! no isolated baseline -- peer-standard slowdown UNMEASURED.")
            print(f"       Run: sudo AS=<roster> POL=\"...\" ./run_iso.sh <tenants>")

        bwr = " ".join(f"{p}={r['bw'][p]:.0f}" for p in PHASES
                       if r["bw"].get(p) is not None)
        pk = float(os.environ.get("PEAK_BW", "113"))
        nb = r["bw"].get("noisy")
        if nb:
            flag = "  <-- ABOVE 70%: bandwidth-mediated, fence result NOT interpretable" \
                   if nb / pk > 0.70 else ""
            print(f"    scene GB/s per phase: {bwr}   (noisy = {100*nb/pk:.0f}% "
                  f"of {pk:.0f} peak){flag}")
        if r["freed"] is not None:
            print(f"    fence moved {r['freed']:.1f} MB of LLC occupancy off the donors")
        print(f"{'tenant':<16}{'phase':<8}{'IPC':>7}{'ips':>16}{'mbm GB/s':>10}{'hit':>7}"
              f"{'occ MB':>8}{'db':>7}{'p99 ms med/max':>18}")
        for name in r["vics"] + r["pols"]:
            for p in PHASES:
                s = (r["ph"].get(p, {}) or {}).get(name)
                if not s:
                    continue
                def g(k, f="{:.2f}"):
                    return f.format(s[k]) if s.get(k) is not None else "--"
                i_ = f"{s['ips']:,.0f}" if s.get("ips") else "--"
                print(f"{name:<16}{p:<8}{g('ipc','{:.3f}'):>7}{i_:>16}{g('mbm','{:.1f}'):>10}{g('hit'):>7}"
                      f"{g('cmt','{:.1f}'):>8}{g('db','{:.1f}'):>7}"
                      f"{(g('p99','{:.0f}') + '/' + g('p99max','{:.0f}')) if s.get('p99') is not None else '':>18}")
        miss = [p for p in PHASES if not r["ph"].get(p) and p not in OPTIONAL_PHASES]
        if miss:
            print(f"  !! no data for phase(s): {', '.join(miss)}")
    print()
    for d, r in rows:
        for n, (spread, odd, sig, kind) in r.get("incons", {}).items():
            if kind == "instance":
                print(f"!! {n}: LLC-loads/KI varies {spread*100:.1f}% across phases {sig}")
                print(f"   -- a PRE-LLC quantity no mask can move, so this is a DIFFERENT")
                print(f"   PROCESS INSTANCE, flipping at/around '{odd}'. Arms measured on")
                print(f"   the new instance are divided by a baseline from the old one and")
                print(f"   their digits are NOT comparable. Size the tenant so ONE")
                print(f"   invocation outlasts the whole scene.")
            else:
                print(f"!! {n}: hit varies {spread:.2f} across phases {sig} -- it was in a")
                print(f"   DIFFERENT execution stage during '{odd}' (a looping batch job that")
                print(f"   finished and re-entered init). Size it to outlast the scene.")
    print("Reading it. CATrec>>MBArec = cache-capacity victim = the FENCE scene coeffd4 needs.")
    print("MBArec>>CATrec = bandwidth victim -- fence is the wrong lever and can backfire.")
    print("Neither, WITH a large 'fence moved N MB' = the free lunch existed and was taken,")
    print("  but the victims were already ABOVE their knee, so it bought them nothing.")
    print("  That is a SCENE defect (not enough pressure), not a mechanism failure.")
    elimination(rows)


def elimination(rows):
    """cat -> cat_cap: did capping the absorber redirect the windfall?

    `cat` releases the donor's band into a free-for-all. That race is biased --
    cache fill rate scales with MISS rate, so a high-bandwidth tenant installs
    lines fastest and can win a band it cannot convert (measured: npb_cg_d40
    took 10.3 MB to canneal's 9.4 and converted 32x worse). `cat_cap` pins that
    absorber to its own pre-fence occupancy and lets the rest have the surplus.

    The cap is bounded BY the release, so the absorber can never end below what
    unmanaged sharing gave it -- being wrong here costs zero, not negative. What
    we are looking for is therefore one-sided: converters gain, absorber flat.
    """
    todo = [(d, r) for d, r in rows if r["ph"].get("cat_cap")]
    if not todo:
        return
    print()
    print("=" * 128)
    print("ELIMINATION (cat -> cat_cap): capping the absorber at its pre-fence share")
    print("=" * 128)
    for d, r in todo:
        print(f"\n--- {os.path.basename(d)}")
        print(f"{'tenant':<16}{'occ cat':>9}{'occ cap':>9}{'d occ':>8}"
              f"{'rec cat':>9}{'rec cap':>9}{'d rec':>8}")
        sig = lambda p, n, k="ips": (r["ph"].get(p, {}) or {}).get(n, {}).get(k)
        for n in r["vics"] + r["pols"]:
            oc, op = sig("cat", n, "cmt"), sig("cat_cap", n, "cmt")
            e = r["per"].get(n, {})
            rc = (e.get("rec") or {}).get("cat")
            rp = (e.get("rec") or {}).get("cat_cap")
            do = f"{op - oc:+.1f}" if (oc is not None and op is not None) else "--"
            dr = f"{rp - rc:+.1f}" if (rc is not None and rp is not None) else "--"
            print(f"{n:<16}{fmt(oc,'',1):>9}{fmt(op,'',1):>9}{do:>8}"
                  f"{fmt(rc):>9}{fmt(rp):>9}{dr:>8}")
        gains = [(n, (r["per"][n]["rec"]["cat_cap"] - r["per"][n]["rec"]["cat"]))
                 for n in r["vics"]
                 if r["per"][n]["rec"].get("cat") is not None
                 and r["per"][n]["rec"].get("cat_cap") is not None]
        if not gains:
            print("  no comparable recovery pair -- cap effect not gradable")
            continue
        best = max(gains, key=lambda t: t[1])
        worst = min(gains, key=lambda t: t[1])
        if best[1] > STRICT_GATE and worst[1] > -STRICT_GATE:
            print(f"  ELIMINATION PAYS: {best[0]} +{best[1]:.1f}pp recovery, nobody below "
                  f"-{STRICT_GATE}pp -- the windfall was misallocated and capping fixed it.")
        elif best[1] <= STRICT_GATE:
            print(f"  NO EFFECT: best gain {best[1]:+.1f}pp is inside the {STRICT_GATE}pp floor. "
                  f"Either the free-for-all was already right, or the")
            print(f"  cap was too generous (it is a ceil() of measured occupancy, so it errs "
                  f"toward the absorber by design).")
        else:
            print(f"  MIXED: {best[0]} gained {best[1]:+.1f}pp but {worst[0]} lost "
                  f"{worst[1]:+.1f}pp -- capping moved harm rather than waste. Check whether")
            print(f"  the capped tenant was an absorber at all (conversion %/MB across "
                  f"noisy->cat), not merely the biggest holder.")


if __name__ == "__main__":
    main()
