#!/usr/bin/env python3
"""test_coeffd4_1.py -- offline unit tests for the coeffd4.1 geometry fixes.

Each test encodes a defect found by replaying D3-v9 phase_c4 through masks()
(2026-09-16). Same construct-and-inject pattern as test_coeffd4.py.
coeffd4.1.py is not an importable module name, so it is loaded by path.

Run: python3 test_coeffd4_1.py     (no sudo, no hardware)
Also run the coeffd4 regression suite against 4.1:
  python3 -c 'import sys,importlib.util as u; s=u.spec_from_file_location("coeffd4","coeffd4.1.py"); \
m=u.module_from_spec(s); sys.modules["coeffd4"]=m; s.loader.exec_module(m); exec(open("test_coeffd4.py").read())'
"""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
_spec = importlib.util.spec_from_file_location("coeffd4_1", os.path.join(_here, "coeffd4.1.py"))
C4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C4)

MB = 1048576
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class T:
    def __init__(self, mb=0.0, hit=0.9, mbm=0.0, prio=2, db=0.0):
        self.cmt = mb * MB; self.hit_rate = hit; self.mbm_bps = mbm
        self.prio = prio; self.alive = True; self.db = db; self.ips = 1e9
        self.qos = None; self.p99 = None
    def slo_violating(self):
        return False


def daemon(tenants, **kw):
    for _n, _t in tenants.items():
        _t.name = _n
    d = object.__new__(C4.Coeffd4)
    d.nbits = kw.get("nbits", 15); d.tenants = tenants
    d.slice_order = kw.get("slice_order", []); d.alloc = kw.get("alloc", {})
    d.held = kw.get("held", set()); d.fence = kw.get("fence", {})
    d.capped = kw.get("capped", {}); d.band_lo = {}
    d.blocked = kw.get("blocked", {}); d.migrated = kw.get("migrated", {})
    d.sanctioned = set(); d.db_visible = {}
    d.nature = kw.get("nature", {}); d._nat_run = {}
    d.ipc = kw.get("ipc", {}); d.base = kw.get("base", {})
    d._base_acc = {}; d.base_ready = True; d.futile = False
    d._collat = {}; d._cap_collat = {}
    d.cap_done = kw.get("cap_done", {}); d.cap_seed = kw.get("cap_seed", {})
    d.cap_seed_extra = {}; d.fence_seed = kw.get("fence_seed", {})
    d.act_seq = 0; d.pending = None
    d.fence_active = None; d.cap_active = None; d._settle_left = 0
    d.hurt_list = lambda: [x for x in tenants.values() if d.below_floor(x.name)]
    d.records = []
    d.record = lambda kind, **k: d.records.append((kind, k))
    d.apply_masks = lambda: None
    return d


def ways(m):
    return [b for b in range(15) if m >> b & 1]


# D3-v9 at the moment ELIMINATE fired (t=218 s): both donors at 1 way,
# canneal 25.4 MB, cg 22.5 MB, cg converting at ~0 %/MB.
def d3v9(**kw):
    t = {"npb_mg_d300": T(4, 0.02, mbm=47e9), "llama": T(4, 0.09, mbm=12e9),
         "npb_cg_d80": T(22.5, 0.94, mbm=1e9), "canneal": T(25.4, 0.79, mbm=2e9),
         "npb_ep_e": T(0.1, 0.88)}
    args = dict(nature={"npb_mg_d300": "STREAMER", "llama": "STREAMER",
                        "npb_cg_d80": "DUAL", "canneal": "SENSITIVE",
                        "npb_ep_e": "NEUTRAL"},
                held={"npb_mg_d300", "llama"},
                fence={"npb_mg_d300": 1, "llama": 1},
                fence_seed={"npb_mg_d300": 5, "llama": 6},
                slice_order=["npb_mg_d300", "llama"],
                base={"npb_mg_d300": {"ipc": 2.48, "cmt": 4},
                      "llama": {"ipc": 2.35, "cmt": 17.5},
                      "npb_cg_d80": {"ipc": 0.735, "cmt": 13.8},
                      "canneal": {"ipc": 1.025, "cmt": 9.0},
                      "npb_ep_e": {"ipc": 2.55, "cmt": 0.1}},
                ipc={"npb_mg_d300": 2.47, "llama": 2.36, "npb_cg_d80": 0.736,
                     "canneal": 1.120, "npb_ep_e": 2.55})
    args.update(kw)
    return daemon(t, **args)


print("== 4.1-C: a new donor stacks ABOVE a compacted one; nobody moves ==")
d = d3v9(held={"npb_mg_d300"}, fence={"npb_mg_d300": 1},
         fence_seed={"npb_mg_d300": 5}, slice_order=["npb_mg_d300"])
d.tenants["llama"].cmt = 22 * MB               # pre-fence: seed 6, first step 5
d.masks(); lo_before = d.band_lo["npb_mg_d300"]
check("llama is the admissible donor", d.admissible_donors() == ["llama"], str(d.admissible_donors()))
check("try_fence fired", d.try_fence() is True)
check("first llama step is the SEED (6 ways)", d.pending["step"] == ("fence", "llama", 6)
      and d.pending["seed"] and d.pending["prev_ways"] is None, str(d.pending))
per, shared = d.masks()
check("mg stays at bit 0", lo_before == 0 and ways(per["npb_mg_d300"]) == [0],
      str(ways(per["npb_mg_d300"])))
check("llama seeded above mg (bits 1..6)", ways(per["llama"]) == [1, 2, 3, 4, 5, 6],
      str(ways(per["llama"])))
d.pending = None
d.try_fence()
check("next llama step narrows 6->5", d.pending["step"] == ("fence", "llama", 5)
      and d.pending["prev_ways"] == 6)
d.fence["llama"] = 1
per, shared = d.masks()
check("final layout mg@0, llama@1",
      ways(per["npb_mg_d300"]) == [0] and ways(per["llama"]) == [1])

print("== 4.1: one donor at a time, even if another ranks cheaper ==")
d = d3v9(held={"llama"}, fence={"llama": 4}, fence_seed={"llama": 6},
         slice_order=["llama"])
d.fence_active = "llama"
d.tenants["npb_mg_d300"].cmt = 20 * MB          # mg (hit 0.02) ranks first
check("mg ranks first by hit", d.admissible_donors()[0] == "npb_mg_d300")
d.try_fence()
check("but llama's walk continues", d.pending["step"] == ("fence", "llama", 3),
      str(d.pending["step"]))

print("== 4.1-A: the first cap is occupancy+1, not the crowd width ==")
d = d3v9()
check("crowd is 13 ways before the cap", d.shared_ways() == 13, str(d.shared_ways()))
check("seed = ceil(22.5/4)+1 = 7", d.cap_seed_ways("npb_cg_d80") == 7,
      str(d.cap_seed_ways("npb_cg_d80")))
check("try_eliminate fired", d.try_eliminate() is True)
check("pending step is the seed", d.pending["step"] == ("cap", "npb_cg_d80", 7)
      and d.pending["seed"] is True, str(d.pending["step"]))
per, shared = d.masks()
check("cg holds bits 2..8", ways(per["npb_cg_d80"]) == list(range(2, 9)),
      str(ways(per["npb_cg_d80"])))
check("crowd keeps 6 ways, not 2 (bug A)", ways(shared) == list(range(9, 15)),
      str(ways(shared)))
check("canneal is in the crowd", "canneal" not in per)
# commit it, then the walk continues one way at a time
d.pending = None
d.verify_cap({"step": ("cap", "npb_cg_d80", 7), "absorber": "npb_cg_d80",
              "prev_ways": 13, "seed": True, "pre_ipc": dict(d.ipc)})
check("seed commit recorded", d.cap_seed.get("npb_cg_d80") == 7
      and d.cap_done.get("npb_cg_d80") == 7)
d.try_eliminate()
check("next step walks 7->6", d.pending["step"] == ("cap", "npb_cg_d80", 6)
      and not d.pending["seed"], str(d.pending["step"]))

print("== 4.1-A: a seed that would leave the crowd a sliver is skipped ==")
d = d3v9()
d.tenants["npb_cg_d80"].cmt = 46 * MB          # 12 + 1 = 13 ways of 13
check("no cap applied", d.try_eliminate() is False and not d.capped)
check("cap-skip recorded", any(k == "cap-skip" for k, _ in d.records))

print("== 4.1-A: a failed seed widens the NEXT seed instead of blocking ==")
d = d3v9()
d.try_eliminate(); p = d.pending; d.pending = None
d.ipc["npb_cg_d80"] = 0.68                      # crossed its knee at the seed
d.verify_cap(p)
check("cap released (no committed width)", "npb_cg_d80" not in d.capped, str(d.capped))
check("not blocked", "cap|npb_cg_d80" not in d.blocked)
check("next seed is one way wider (8)", d.cap_seed_ways("npb_cg_d80") == 8,
      str(d.cap_seed_ways("npb_cg_d80")))
d.ipc["npb_cg_d80"] = 0.736
d.try_eliminate()
check("retry applies the wider seed", d.pending["step"] == ("cap", "npb_cg_d80", 8),
      str(d.pending["step"]))

print("== 4.1-A: after a commit, a knee rollback still restores the commit ==")
d = d3v9(capped={"npb_cg_d80": 4}, cap_done={"npb_cg_d80": 5},
         cap_seed={"npb_cg_d80": 7})
d.ipc["npb_cg_d80"] = 0.682
d.verify_cap({"step": ("cap", "npb_cg_d80", 4), "absorber": "npb_cg_d80",
              "prev_ways": 5, "seed": False, "pre_ipc": {"npb_cg_d80": 0.728}})
check("restored to 5", d.capped.get("npb_cg_d80") == 5, str(d.capped))
check("assoc_knee is terminal (blocked)", "cap|npb_cg_d80" in d.blocked)

print("== 4.1: no per-victim reservation (crowd IS the reservation) ==")
_src = open(os.path.join(_here, "coeffd4.1.py")).read()
check("__init__ sets RSV_MAX_WAYS = 0", "c3.RSV_MAX_WAYS = 0" in _src)

print("== 4.1-D: guard-release widens one way, then releases at the seed ==")
C4.c3.Daemon.guard_release = lambda self: False     # isolate the 4.1 part
d = d3v9(capped={"npb_cg_d80": 5}, cap_done={"npb_cg_d80": 5},
         cap_seed={"npb_cg_d80": 7})
d.ipc["npb_cg_d80"] = 0.60
d.guard_release()
check("cap widened 5->6, not released", d.capped.get("npb_cg_d80") == 6, str(d.capped))
d.blocked = {}
d.capped["npb_cg_d80"] = 7; d.guard_release()
check("released past the seed", "npb_cg_d80" not in d.capped
      and "npb_cg_d80" not in d.cap_done and "npb_cg_d80" not in d.cap_seed)

d = d3v9()
d.ipc["llama"] = 1.5                            # donor under its floor
d.guard_release()
check("fence widened 1->2, not released", d.fence.get("llama") == 2
      and "llama" in d.held, str(d.fence))
d.fence["llama"] = 6; d.guard_release()
check("fence released past its seed", "llama" not in d.held and "llama" not in d.fence)

print("== 4.1-E: a failed SEED unfences; a failed narrowing widens back one ==")
d = d3v9(held={"llama"}, fence={"llama": 6}, fence_seed={"llama": 6},
         slice_order=["llama"])
d.rollback_fence({"donor": "llama", "prev_ways": None})
check("seed rollback -> donor back in the crowd", "llama" not in d.held and "llama" not in d.fence)
d = d3v9(held={"llama"}, fence={"llama": 5}, fence_seed={"llama": 6},
         slice_order=["llama"])
d.rollback_fence({"donor": "llama", "prev_ways": 6})
check("6->5 rollback -> stays fenced at 6", d.fence.get("llama") == 6 and "llama" in d.held)

check("SETTLE_SCANS is 1", C4.SETTLE_SCANS == 1)
print("== 4.1: observe expansion SETTLE_SCANS before a NEW absorber ==")
d = d3v9()
d.tenants["npb_cg_d80"].cmt = 22.5 * MB
d._settle_left = C4.SETTLE_SCANS                # the last fence just finished
d.try_eliminate = lambda: (_ for _ in ()).throw(AssertionError("too early"))
r1 = d.propose()
check("one settle scan, no cap, no stand-down",
      r1 is True and not d.capped and not d.futile
      and [k for k, _ in d.records].count("settle-wait") == 1, str(d.records))
fired = {"n": 0}
d.try_eliminate = lambda: (fired.__setitem__("n", 1), True)[1]
check("third scan reaches ELIMINATE", d.propose() is True and fired["n"] == 1)

d = d3v9(capped={"npb_cg_d80": 6}, cap_done={"npb_cg_d80": 6}, cap_seed={"npb_cg_d80": 7})
d.cap_active = "npb_cg_d80"; d._settle_left = C4.SETTLE_SCANS
check("active absorber keeps walking without a wait", d.cap_continuing())
d.propose()
check("...next step 6->5", d.pending and d.pending["step"] == ("cap", "npb_cg_d80", 5),
      str(d.pending and d.pending["step"]))

print("== 4.1: absorbers stack in cap order, not by name ==")
d = d3v9(capped={"zeta": 3, "npb_cg_d80": 4})
d.tenants["zeta"] = T(10); d.tenants["zeta"].name = "zeta"
per, shared = d.masks()
check("zeta (capped first) sits right above the donors", ways(per["zeta"]) == [2, 3, 4]
      and ways(per["npb_cg_d80"]) == [5, 6, 7, 8], f"{ways(per['zeta'])} {ways(per['npb_cg_d80'])}")

if FAILED:
    print(f"\n{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("\nall coeffd4.1 unit tests passed")
