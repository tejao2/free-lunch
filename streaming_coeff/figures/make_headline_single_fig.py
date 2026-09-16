#!/usr/bin/env python3
"""make_headline_single_fig.py -- D3 comparison as ONE panel, one row per controller.

Each controller is reduced to the mean of its runs:
  left  = canneal recovery as % of the best of the 993 swept partitions
  right = harm to npb_cg_d80 (the non-aggressor co-tenant), IPC vs unmanaged

Which runs feed each row (listed on the figure, under the name):
  live    = the controller itself ran for 400 s, graded on the last 60 s
  replay  = its converged layout was applied statically in a fresh run
SATORI and SpiderSense have only replays (their searches take hours, so the
live search is not a 400 s arm). Every other controller uses its live runs.
Mixing the two kinds is a known confound (ledger §20b: llama changes phase
inside the 400 s window); the footnote says so.

CATch picks up every live run listed in CATCH_LIVE that exists on disk, so a
repeat run appears automatically once it lands.

Usage: python3 make_headline_single_fig.py  ->  headline_single_d3.{pdf,png}
"""
import os
import statistics as st

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from make_headline_fig import (COL, INK, INK2, GRID, HERE, RES, arm,  # noqa: E402
                               sweep)

CATCH_LIVE = ["D3_order", "D3_c4_400b"]

# (label, colour key, runs, phase, live?, note)
ROWS = [
    ("CATch", "CATch", CATCH_LIVE, "c4", True, ""),
    ("SATORI", "SATORI", ["D3_rep_sat_conv", "D3_rep_sat_conv2"], "sat_conv",
     False, "needs solo profiles"),
    ("SpiderSense", "SpiderSense", ["D3_spconv", "D3_spconv400",
                                    "D3_rep_spider_conv"], "spider_conv", False, ""),
    ("dCat", "dCat", ["D3_dcat", "D3_dcat_thr1"], "dcat", True,
     "thr 5% and 3%"),
    ("PaLLOC", "PaLLOC", ["D3_palloc", "D3_palloc2"], "palloc", True,
     "run 2 unsettled"),
    ("Cacheman", "Cacheman", ["D3_cacheman", "D3_cacheman2",
                              "D3_cacheman_socket"], "cacheman", True, ""),
    ("no controller", "ref", ["D3_null", "D3_null2"], "null", True,
     "reference"),
]


def main():
    ceiling, npart, (eq_rec, eq_cost) = sweep()
    data = []
    for lab, ck, runs, ph, live, note in ROWS:
        runs = [r for r in runs if os.path.isdir(os.path.join(RES, r, f"phase_{ph}"))]
        pts = [arm(r, ph, live) for r in runs]
        data.append((lab, ck, st.mean(100.0 * p[0] / ceiling for p in pts),
                     st.mean(p[1] for p in pts),
                     f"{'live 400 s' if live else 'replay'}, n={len(runs)}", note))
    data.insert(len(data) - 1, ("equal split", "ref", 100.0 * eq_rec / ceiling, eq_cost,
                    "sweep probe, n=1", "3 ways each"))

    n = len(data)
    fig, (ar, ac) = plt.subplots(1, 2, figsize=(9.6, 5.4),
                                 gridspec_kw={"width_ratios": [1.6, 1.0],
                                              "wspace": 0.10})
    ar.set_xlim(-25, 112)
    ac.set_xlim(-50, 14)
    for i, (lab, ck, rec, cost, kind, note) in enumerate(data):
        c = COL[ck]
        for ax, v, fmt in ((ar, rec, "{:.0f}%"), (ac, cost, "{:+.1f}%")):
            ax.barh(i, v, height=0.58, color=c, zorder=2)
            span = ax.get_xlim()[1] - ax.get_xlim()[0]
            x = max(v, 0) + 0.02 * span
            ax.text(x, i, fmt.format(v), va="center", fontsize=10,
                    fontweight="bold", color=INK, zorder=5)
        ar.text(-0.02, i - 0.08, lab, transform=ar.get_yaxis_transform(),
                ha="right", va="center", fontsize=10.5, fontweight="bold")
        sub = kind + (f" · {note}" if note else "")
        ar.text(-0.02, i + 0.30, sub, transform=ar.get_yaxis_transform(),
                ha="right", va="center", fontsize=7.8, color=INK2)
    for ax in (ar, ac):
        ax.set_ylim(n - 0.45, -0.6)
        ax.set_yticks([])
        ax.axvline(0, color=INK2, lw=0.8, zorder=3)
        ax.grid(axis="x", color=GRID, lw=0.6, zorder=0)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
    ar.axvline(100, color=INK2, lw=1.0, ls=":", zorder=3)
    ar.set_title("canneal recovery, mean of runs", loc="left", fontsize=10,
                 color=INK2, pad=4)
    ac.set_title("harm to npb_cg_d80, mean of runs", loc="left", fontsize=10,
                 color=INK2, pad=4)
    ar.text(-0.44, 1.07, "D3: how much of the recoverable loss each controller "
            "took back, and who paid", transform=ar.transAxes,
            fontsize=12, fontweight="bold", va="bottom")
    ar.set_xlabel(f"% of best of {npart} swept partitions "
                  f"(dotted 100% = {ceiling:+.1f}% recovery)")
    ac.set_xlabel("IPC change vs unmanaged")
    fig.text(0.01, 0.005,
             "Victims canneal, npb_cg_d80, npb_ep_e; donors npb_mg_d300, llama. "
             "Live and replay rows are graded in different llama phases "
             "(ledger §20b), so compare them with care.\n"
             "npb_cg_d80 flips between two modes at 4–5 ways (±10 pp, §18b), so "
             "CATch, SpiderSense and dCat harm values are averages over that. "
             "Per-run values: headline_d3.png.",
             fontsize=7.4, color=INK2, va="bottom")
    fig.subplots_adjust(left=0.235, right=0.985, top=0.89, bottom=0.17)
    for ext in ("pdf", "png"):
        out = os.path.join(HERE, f"headline_single_d3.{ext}")
        fig.savefig(out, facecolor="white", dpi=200)
        print("wrote", out)
    for lab, _, rec, cost, kind, _ in data:
        print(f"  {lab:14s} {rec:5.0f}%  harm {cost:+6.1f}%   ({kind})")


if __name__ == "__main__":
    main()
