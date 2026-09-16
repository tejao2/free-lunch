#!/usr/bin/env python3
"""test_coeffd4.py -- offline unit tests for coeffd4's NEW logic.

WHY THIS EXISTS. An audit found coeffd4 had ZERO test coverage: test_coeffd.py
imports the oldest daemon generation, and test_v6_logic.py constructs
coeffd3.Daemon directly (and hardcodes nbits=0 to force the rsv branch off), so
neither ever instantiates Coeffd4. ~500 lines -- band masks, FENCE, ELIMINATE,
the Change-1/2/3 baseline and floor, nature classification -- were unverified.
Every test below encodes a defect that was actually found, so a regression
reintroduces a bug we have already paid for once.

Construct-and-inject pattern borrowed from test_v6_logic.py: build the object
without __init__ (no resctrl, no perf, no root), inject state, call the method.
Run: python3 test_coeffd4.py     (no sudo, no hardware, safe on a busy box)
"""
import sys
import coeffd4 as C4

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
        _t.name = _n                     # real Tenants carry their own name
    d = object.__new__(C4.Coeffd4)
    d.nbits = kw.get("nbits", 15); d.tenants = tenants
    d.slice_order = kw.get("slice_order", []); d.alloc = kw.get("alloc", {})
    d.held = kw.get("held", set()); d.fence = kw.get("fence", {})
    d.capped = kw.get("capped", {}); d.band_lo = {}
    d.blocked = kw.get("blocked", {}); d.migrated = kw.get("migrated", {})
    d.sanctioned = kw.get("sanctioned", set()); d.db_visible = {}
    d.nature = kw.get("nature", {}); d._nat_run = {}
    d.ipc = kw.get("ipc", {}); d.base = kw.get("base", {})
    d._base_acc = {}; d.base_ready = kw.get("base_ready", True)
    d.futile = kw.get("futile", False); d._collat = {}
    d.records = []
    d.record = lambda kind, **k: d.records.append((kind, k))
    d.apply_masks = lambda: None
    return d


def contiguous(m):
    return m != 0 and bin(m)[2:].strip("0").count("0") == 0


def layout_ok(d):
    """Every mask contiguous, inside nbits, and mutually disjoint incl. shared."""
    per, shared = d.masks()
    seen = 0
    for n, m in per.items():
        if not contiguous(m) or m >= (1 << d.nbits) or (m & seen):
            return False, f"{n}=0x{m:x} shared=0x{shared:x}"
        seen |= m
    if shared and (not contiguous(shared) or shared & seen or shared >= (1 << d.nbits)):
        return False, f"shared=0x{shared:x} overlaps 0x{seen:x}"
    return True, ""


print("== B1: donor bands must never overflow nbits or collide with shared ==")
# two admissible streamers, each wanting a wide band -- the crash/corruption case
for widths in ([7, 2], [8, 8], [14, 2], [14, 14], [2, 2]):
    d = daemon({"d0": T(30, 0.02), "d1": T(30, 0.02)},
               held={"d0", "d1"}, fence={"d0": widths[0], "d1": widths[1]},
               slice_order=["d0", "d1"])
    ok, why = layout_ok(d)
    check(f"two donors fenced to {widths}", ok, why)

print("== B2: the capped-band clamp must actually skip when there is no room ==")
for donor_w, cap_w in ((13, 9), (12, 4), (5, 9)):
    d = daemon({"d0": T(30, 0.02), "x": T(15, 0.94)},
               held={"d0"}, fence={"d0": donor_w}, capped={"x": cap_w},
               slice_order=["d0"])
    ok, why = layout_ok(d)
    check(f"donor={donor_w}w cap={cap_w}w", ok, why)

print("== B3: a tenant must never hold both a donor band and a cap band ==")
d = daemon({"x": T(24, 0.02)}, held={"x"}, fence={"x": 6}, capped={"x": 4},
           slice_order=["x"])
per, shared = d.masks()
ok, why = layout_ok(d)
check("no double-booked band", ok and len([k for k in per if k == "x"]) == 1, why)
consumed = bin(per.get("x", 0)).count("1") + bin(shared).count("1")
check("no orphaned ways", consumed == d.nbits, f"accounted {consumed}/{d.nbits}")

print("== seed_ways: from MEASURED occupancy (bytes->MB), never nbits ==")
d = daemon({"mg": T(24.3, 0.02), "ft": T(3.2, 0.71), "tiny": T(0.0, 0.9)})
check("24.3 MB -> 7 ways", d.seed_ways("mg") == 7, str(d.seed_ways("mg")))
check("3.2 MB -> 2 ways", d.seed_ways("ft") == 2, str(d.seed_ways("ft")))
check("0 MB -> floor, not nbits", d.seed_ways("tiny") == C4.FENCE_MIN_WAYS + 1,
      str(d.seed_ways("tiny")))

print("== V7: the futility latch must GATE, not just dedup a log line ==")
d = daemon({"a": T(30, 0.02, mbm=5e9)}, nature={"a": "STREAMER"}, futile=True,
           base={"a": {"ipc": 1.0, "cmt": 30}}, ipc={"a": 1.0})
d.hurt_list = lambda: []
check("propose() holds while futile", d.propose() is False)

print("== V1: harm is judged against the unmanaged floor, not the db gate ==")
# the D3-v3 inversion: cg_d80 db 53.2 not a victim; canneal db 16.3 IS the victim
d = daemon({"cg": T(15, 0.94, db=53.2), "canneal": T(8, 0.74, db=16.3)},
           base={"cg": {"ipc": 0.735, "cmt": 15}, "canneal": {"ipc": 1.031, "cmt": 8}},
           ipc={"cg": 0.740, "canneal": 0.900})       # canneal 13% below floor
check("high-db non-victim NOT flagged", d.is_hurt(d.tenants["cg"]) is False)
check("low-db real victim IS flagged", d.is_hurt(d.tenants["canneal"]) is True)

print("== V9/B7: collateral must ignore sanctioned and migrated tenants ==")
d = daemon({"s": T(10, 0.5), "m": T(10, 0.5), "n": T(10, 0.5)},
           base={k: {"ipc": 1.0, "cmt": 10} for k in "smn"},
           ipc={"s": 0.5, "m": 0.5, "n": 0.5},
           sanctioned={"s"}, migrated={"m": set()})
br = d.floor_breaches()
check("sanctioned excluded", "s" not in br, str(br))
check("migrated excluded", "m" not in br, str(br))
check("ordinary breach still reported", "n" in br, str(br))

print("== absorbers(): worst converter only; donors and victims excluded ==")
d = daemon({"mg": T(26, 0.02, mbm=5e9), "cg": T(26.2, 0.94), "canneal": T(22.9, 0.79)},
           nature={"mg": "STREAMER", "cg": "SENSITIVE", "canneal": "SENSITIVE"},
           base={"mg": {"ipc": 2.476, "cmt": 24.3}, "cg": {"ipc": 0.735, "cmt": 14.9},
                 "canneal": {"ipc": 1.026, "cmt": 7.6}},
           ipc={"mg": 2.45, "cg": 0.736, "canneal": 1.115})
names = [n for _, n, _ in d.absorbers()]
check("absorber = cg only", names == ["cg"], str(names))
check("streamer excluded", "mg" not in names)
check("good converter excluded", "canneal" not in names)

print("== Change 1: floor is fail-CLOSED-ish -- unknown != safe ==")
d = daemon({"a": T(10, 0.9)}, base={}, ipc={"a": 0.1})
check("no baseline -> below_floor False (documented fail-open)",
      d.below_floor("a") is False)
check("...and the tenant is absent from floor_breaches", d.floor_breaches() == [])

print("== baseline must be a STEADY STATE, not a snapshot (D3-v4 ft_d75) ==")
import statistics as _st
d = daemon({"steady": T(10, 0.9), "phasic": T(10, 0.9)}, base={}, base_ready=False)
d.no_floor = set(); d._base_cycles = 0
d.tenants["steady"].name = "steady"; d.tenants["phasic"].name = "phasic"
# steady tenant holds ~1.00; phasic swings like ft_d75 did (1.265 anchor vs 0.87 real)
steady = [1.00, 1.01, 0.99, 1.00, 1.00, 1.01] * 3
phasic = [1.27, 0.87, 1.25, 0.88, 1.26, 0.86] * 3
for i in range(len(steady)):
    d.ipc = {"steady": steady[i], "phasic": phasic[i]}
    d.capture_baseline()
check("steady tenant gets a floor", "steady" in d.base)
check("phasic tenant gets NO floor", "phasic" not in d.base and "phasic" in d.no_floor,
      f"base={sorted(d.base)} no_floor={sorted(d.no_floor)}")
check("baseline still completes (no deadlock)", d.base_ready is True)
# the point of the fix: a floorless tenant must not veto every step
d.ipc = {"steady": 1.00, "phasic": 0.50}
check("floorless tenant cannot veto as collateral", "phasic" not in d.floor_breaches(),
      str(d.floor_breaches()))
check("...and a real breach is still caught",
      (setattr(d, "ipc", {"steady": 0.5, "phasic": 0.87}) or "steady") in d.floor_breaches())

print("== ELIMINATE must be reachable after the fence exhausts (D3-v5) ==")
d = daemon({"mg": T(24, 0.02, mbm=5e9), "cg": T(26, 0.94), "canneal": T(23, 0.79)},
           nature={"mg": "STREAMER", "cg": "SENSITIVE", "canneal": "SENSITIVE"},
           held={"mg"}, fence={"mg": 1},          # donor already at FENCE_MIN_WAYS
           slice_order=["mg"],
           base={"mg": {"ipc": 2.48, "cmt": 24}, "cg": {"ipc": 0.735, "cmt": 14.9},
                 "canneal": {"ipc": 1.03, "cmt": 7.6}},
           ipc={"mg": 2.47, "cg": 0.736, "canneal": 1.10})
check("no donor left to fence", d.admissible_donors() == [], str(d.admissible_donors()))
check("nobody is below their floor", d.hurt_list() == [])
check("but an absorber IS present", [n for _, n, _ in d.absorbers()] == ["cg"])
fired = {"n": 0}
d.try_eliminate = lambda: (fired.__setitem__("n", 1), True)[1]
d.try_fence = lambda: False
check("propose() reaches ELIMINATE instead of standing down",
      d.propose() is True and fired["n"] == 1)

print("== cap must RESUME from its last commit, not re-walk (D3-v8 churn) ==")
d = daemon({"mg": T(24, 0.02, mbm=5e9), "cg": T(26, 0.94)},
           nature={"mg": "STREAMER", "cg": "SENSITIVE"},
           held={"mg"}, fence={"mg": 2}, slice_order=["mg"],
           base={"mg": {"ipc": 2.48, "cmt": 24}, "cg": {"ipc": 0.735, "cmt": 14.9}},
           ipc={"mg": 2.47, "cg": 0.736})
d.cap_done = {}; d._cap_collat = {}; d.sanctioned = set()
d.capped = {"cg": 10}
d.verify_cap({"step": ("cap", "cg", 10), "absorber": "cg", "prev_ways": 11,
              "pre_ipc": {"cg": 0.736}})
check("a commit records progress", d.cap_done.get("cg") == 10, str(d.cap_done))
# now a collateral rollback: someone else fell
d.capped["cg"] = 9
d.base["other"] = {"ipc": 1.0, "cmt": 5}; d.ipc["other"] = 0.5
d.tenants["other"] = T(5, 0.9); d.tenants["other"].name = "other"
d.verify_cap({"step": ("cap", "cg", 9), "absorber": "cg", "prev_ways": 10,
              "pre_ipc": {"cg": 0.736}})
check("rollback resumes at last commit, not uncapped",
      d.capped.get("cg") == 10, f"capped={d.capped}")
check("...and does not discard the walk", d.cap_done.get("cg") == 10)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all coeffd4 unit tests passed")
