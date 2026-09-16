#!/usr/bin/env python3
"""make_headline_fig.py -- D3 controller comparison, two panels (ledger §20b).

WHY TWO PANELS. D3_null / D3_null2 show that canneal loses 1.8-2.5% IPC over a
400 s window with NO controller, because llama changes phase inside it (hit
0.08 -> 0.17, occupancy 16 -> 24 MB). Live controller arms are graded at
340-400 s; static replays are graded about a minute after `noisy`. The two
kinds of arm sit in different llama phases, so they are not drawn on one
axis:

  A  converged answers, each replayed as a static layout in its own run
  B  live controllers, 400 s window, graded on the last 60 s, with the
     time-matched no-controller arm drawn as the reference

Each row has two small multiples. Left: canneal recovery as % of the best of
the 993 swept partitions (estimator §19: (arm - own-run noisy) / (iso -
own-run noisy), divided by the sweep's ceiling). Right: what the allocation
cost npb_cg_d80, the non-aggressor co-tenant (arm vs own-run noisy IPC).
Dots = individual runs; bar = their mean. Every number is recomputed from
perf files on disk.

Usage: python3 make_headline_fig.py   ->  headline_d3.{pdf,png}
"""
import json
import os
import statistics as st
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "exp16_natural_contention")
RES = os.path.join(EXP, "results_certify")
sys.path.insert(0, EXP)
from peer_metrics import true_ipc   # noqa: E402

VICTIM, COST = "canneal", "npb_cg_d80"
TAIL_S = 60                          # certify.py: controller tail, in seconds
STATE = os.path.join(RES, "spider_state.json")
ISO = json.load(open(os.path.join(EXP, "results_iso", "D3", "iso.json")))

# Okabe-Ito, one hue per controller in both panels (validated: CVD dE >= 11,
# normal-vision dE >= 16.4; orange/sky < 3:1 contrast -> every mean is
# labelled with its value).
INK, INK2, GRID = "#1a1a1a", "#555555", "#dddddd"
COL = {"CATch": "#0072B2", "SATORI": "#009E73", "SpiderSense": "#D55E00",
       "Cacheman": "#CC79A7", "dCat": "#E69F00", "PaLLOC": "#56B4E9",
       "ref": "#8a8a8a"}

plt.rcParams.update({
    "font.size": 10.0, "axes.edgecolor": "#bbbbbb", "axes.labelcolor": INK,
    "text.color": INK, "xtick.color": INK2, "ytick.color": INK2,
    "pdf.fonttype": 42, "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
})


def ipc(run, phase, tenant, live):
    p = os.path.join(RES, run, f"phase_{phase}", f"perf_{tenant}.json")
    return true_ipc(p, tail_s=TAIL_S) if live else true_ipc(p)


def arm(run, phase, live):
    """(canneal recovery %, npb_cg_d80 IPC change %) inside one run."""
    nv, nc = ipc(run, "noisy", VICTIM, False), ipc(run, "noisy", COST, False)
    av, ac = ipc(run, phase, VICTIM, live), ipc(run, phase, COST, live)
    rec = 100.0 * (av - nv) / (ISO[VICTIM]["ipc"] - nv)
    return rec, 100.0 * (ac / nc - 1.0)


def sweep():
    """Ceiling (best canneal recovery over the sweep) and the equal-split row."""
    S = json.load(open(STATE))
    w = os.path.join(RES, "D3_spider", "phase_noisy")
    nv = true_ipc(os.path.join(w, f"perf_{VICTIM}.json"))
    nc = true_ipc(os.path.join(w, f"perf_{COST}.json"))
    lost = ISO[VICTIM]["ipc"] - nv
    recs = {k: 100.0 * (v["ipc"][VICTIM] - nv) / lost for k, v in S["done"].items()}
    eq = S["done"]["3-3-3-3-3"]
    return max(recs.values()), len(recs), (recs["3-3-3-3-3"],
                                           100.0 * (eq["ipc"][COST] / nc - 1.0))


# (label, colour key, runs, phase, note)
REPLAY = [
    ("CATch layout", "CATch", ["D3_rep_band_dp", "D3_rep_band_dp2"], "band_dp", ""),
    ("SATORI", "SATORI", ["D3_rep_sat_conv", "D3_rep_sat_conv2"], "sat_conv",
     "needs solo profiles"),
    ("SpiderSense", "SpiderSense", ["D3_spconv", "D3_spconv400",
                                    "D3_rep_spider_conv"], "spider_conv", ""),
    ("Cacheman", "Cacheman", ["D3_rep_cm_nest"], "cm_nest", "steady-state ladder"),
]
LIVE = [
    ("CATch", "CATch", ["D3_order"], "c4", "n=1"),
    ("dCat (5% / 3%)", "dCat", ["D3_dcat", "D3_dcat_thr1"], "dcat", ""),
    ("Cacheman", "Cacheman", ["D3_cacheman", "D3_cacheman2",
                              "D3_cacheman_socket"], "cacheman", ""),
    ("PaLLOC", "PaLLOC", ["D3_palloc", "D3_palloc2"], "palloc",
     "run 2 unsettled"),
]
NULL = ("no controller", "ref", ["D3_null", "D3_null2"], "null",
        "time-matched reference")


def collect(rows, live, ceiling):
    out = []
    for lab, ck, runs, ph, note in rows:
        pts = [arm(r, ph, live) for r in runs]
        out.append((lab, ck, [100.0 * p[0] / ceiling for p in pts],
                    [p[1] for p in pts], note, len(runs)))
    return out


def draw(ax_r, ax_c, data, title, ref=None):
    n = len(data)
    for i, (lab, ck, rec, cost, note, k) in enumerate(data):
        c = COL[ck]
        for ax, vals, fmt in ((ax_r, rec, "{:.0f}%"), (ax_c, cost, "{:+.1f}%")):
            m = st.mean(vals)
            ax.barh(i, m, height=0.56, color=c, alpha=0.30, zorder=2)
            ax.scatter(vals, [i] * len(vals), s=46, color=c, zorder=4,
                       edgecolor="white", linewidth=1.2)
            lo, hi = min(vals + [0]), max(vals + [0])
            x = hi + 0.03 * (ax.get_xlim()[1] - ax.get_xlim()[0])
            if len(vals) == 1:
                txt = fmt.format(m)
            elif ax is ax_c and len(vals) > 2:     # keep the narrow panel inside
                txt = f"{fmt.format(min(vals))} … {fmt.format(max(vals))}"
            else:
                txt = " / ".join(fmt.format(v) for v in vals)
            ax.text(x, i, txt, va="center", fontsize=8.6, color=INK, zorder=5)
        ax_r.text(-0.02, i, lab, transform=ax_r.get_yaxis_transform(),
                  ha="right", va="center", fontsize=10, fontweight="bold",
                  color=INK)
        sub = f"n={k}" + (f" · {note}" if note and note != "n=1" else "")
        ax_r.text(-0.02, i + 0.32, sub, transform=ax_r.get_yaxis_transform(),
                  ha="right", va="center", fontsize=7.8, color=INK2)
    for ax in (ax_r, ax_c):
        ax.set_ylim(n - 0.4, -0.6)
        ax.set_yticks([])
        ax.axvline(0, color=INK2, lw=0.8, zorder=3)
        ax.grid(axis="x", color=GRID, lw=0.6, zorder=0)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
    ax_r.axvline(100, color=INK2, lw=1.0, ls=":", zorder=3)
    if ref is not None:
        lo, hi = min(ref), max(ref)
        ax_r.axvspan(lo, hi, color=COL["ref"], alpha=0.18, zorder=1, lw=0)
        ax_r.text(hi + 1.5, n - 0.55,
                  f"no controller, same window: {ref[0]:.0f}% / {ref[1]:.0f}%",
                  fontsize=7.8, color=INK2, va="center")
    ax_r.text(-0.34, 1.13, title, transform=ax_r.transAxes,
              fontsize=11.5, fontweight="bold", va="bottom", ha="left")


def main():
    ceiling, npart, (eq_rec, eq_cost) = sweep()
    rep = collect(REPLAY, False, ceiling)
    rep.append(("equal split", "ref", [100.0 * eq_rec / ceiling], [eq_cost],
                "3 ways each, sweep probe", 1))
    live = collect(LIVE + [NULL], True, ceiling)

    fig, axes = plt.subplots(2, 2, figsize=(10.2, 7.6),
                             gridspec_kw={"width_ratios": [1.55, 1.0],
                                          "height_ratios": [len(rep), len(live)],
                                          "hspace": 0.62, "wspace": 0.10})
    for ax in axes[:, 0]:
        ax.set_xlim(-30, 118)
    for ax in axes[:, 1]:
        ax.set_xlim(-58, 30)
    draw(axes[0, 0], axes[0, 1], rep,
         "A  Converged answers, replayed as static layouts (one per run)")
    draw(axes[1, 0], axes[1, 1], live,
         "B  Live controllers, 400 s window, graded on the last 60 s")
    for r in (0, 1):
        axes[r, 1].set_title("cost to npb_cg_d80", loc="left",
                             fontsize=9.5, color=INK2, pad=4)
        axes[r, 0].set_title("canneal recovery (% of best partition)",
                             loc="left", fontsize=9.5, color=INK2, pad=4)
    axes[1, 0].set_xlabel(
        f"% of best of {npart} swept partitions (dotted 100% = {ceiling:+.1f}% recovery)")
    axes[1, 1].set_xlabel("IPC change vs unmanaged")
    fig.text(0.01, 0.005,
             "D3: victims canneal, npb_cg_d80, npb_ep_e; donors npb_mg_d300, llama. "
             "Dots = runs, bar = mean. Panels are not comparable: live arms are "
             "graded after llama's\nphase change, replays before it (ledger §20b). "
             "npb_cg_d80 is bistable at 4–5 ways (±10 pp, §18b): CATch, SpiderSense "
             "and dCat cost values there are\nranges, not points. PaLLOC run 2 was "
             "still reallocating inside the graded tail (§12h).",
             fontsize=7.4, color=INK2, va="bottom")
    fig.subplots_adjust(left=0.20, right=0.985, bottom=0.15, top=0.93)
    for ext in ("pdf", "png"):
        out = os.path.join(HERE, f"headline_d3.{ext}")
        fig.savefig(out, facecolor="white", dpi=200)
        print("wrote", out)
    print(f"ceiling {ceiling:+.1f}% over {npart} partitions")
    for lab, _, rec, cost, _, _ in rep + live:
        print(f"  {lab:18s} ceil% {['%.0f' % v for v in rec]}  cg_d80 {['%+.1f' % v for v in cost]}")


if __name__ == "__main__":
    main()
