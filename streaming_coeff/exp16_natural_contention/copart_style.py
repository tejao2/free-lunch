#!/usr/bin/env python3
"""copart_style -- a CoPart-style (Park/Park/Baek, EuroSys'19) fairness
optimizer, reimplemented as the FAIRNESS-SCALAR comparator for coeffd.

Faithful to the paper (see copart_style_DESIGN.md):
  - BLACK-BOX: per-tenant IPS, LLC accesses/s, LLC miss ratio (perf only).
    No latency feed, no QoS target, no declared priority.
  - LEVERS: LLC ways (CAT) + memory bandwidth (MBA), COORDINATED --
    IDENTICAL to coeffd, so lever-parity (no "we crippled it" objection).
  - OBJECTIVE: minimize UNFAIRNESS = sigma/mu of per-tenant SLOWDOWNS,
    slowdown_i = IPS_full_i / IPS_current_i (their Eq 1-2). A PURE fairness
    scalar -- not LC-QoS-first, not aggregate IPC.
  - MECHANISM (their S5): two per-tenant FSM classifiers (LLC and MBA), each
    in SUPPLY / DEMAND / MAINTAIN, then a Hospitals-Residents match that
    moves one resource unit from a SUPPLY tenant to the highest-slowdown
    DEMAND tenant. Random-neighbour exploration when converged.

WHY THIS ARM EXISTS: it is the only comparator that can demonstrate
FAIRNESS != DO-NO-HARM. Minimizing sigma/mu EQUALIZES slowdowns, which is
satisfied by making everyone equally slow -- so a CoPart round can show LOW
unfairness (its win metric) while several tenants sit BELOW unmanaged (our
metric). Neither SpiderSense (IPC) nor Alita (policer) optimizes fairness,
so neither can show this.

Documented deviations (-style):
  - IPS_full (full-resource baseline) comes from our SOLO round IPS
    (solo_ipc.json) instead of their in-band profiling phase.
  - The stable-matching HR solver is overkill at our tenant count; we use
    the faithful greedy equivalent it converges to: each cycle move one unit
    of a resource from the lowest-slowdown SUPPLY tenant to the
    highest-slowdown DEMAND tenant.
  - Contiguous nested-prefix CAT masks (hardware contiguity), same mask math
    as every other arm.

Config: {"tenants":[{"name","grp","cores"}...], "solo_ipc": "<path>"?}
Usage: sudo python3 copart_style.py --config cfg.json --out outdir
"""
import argparse, json, os, signal, subprocess, sys, time

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"
CYCLE_SECS = 2.0
PERF_INT_MS = 2000

# CoPart design parameters (their S5.2/5.3; values from the paper)
ALPHA_LLC_ACC = 1.5e6      # LLC accesses/sec: below => can SUPPLY a way
BETA_MISS_RATIO = 0.01     # LLC miss ratio (1%): below => can SUPPLY a way
DELTA_P = 0.05             # 5% perf change => significant (DEMAND/SUPPLY)
GAMMA_MTR = 0.10           # memory-traffic-ratio low  (can SUPPLY MBA)
GAMMA_BIG_MTR = 0.30       # memory-traffic-ratio high (DEMAND MBA)
THETA_RETRY = 3            # random-neighbour retries when converged
# self.retry is reset ONLY on a successful FSM match. In a scene where every
# tenant is active, no tenant qualifies as a SUPPLY producer, next_state()
# returns None every cycle, and after THETA_RETRY explorations the loop latches
# into `idle` FOREVER -- masks frozen at the last random perturbation for the
# rest of a 400 s arm, graded as if it were a converged policy. CoPart is an
# ONLINE manager that keeps adapting; the permanent latch is an artefact of this
# reimplementation, not of the published policy, so refresh the budget
# periodically. Faithfulness fix, not a policy change.
RETRY_REFRESH_CYCLES = 15  # 30 s at CYCLE_SECS=2.0
MBA_LEVELS = [100, 80, 60, 50, 40, 30, 20, 10]
MIN_WAYS = 1

SUPPLY, MAINTAIN, DEMAND = "SUPPLY", "MAINTAIN", "DEMAND"


def log(m): print(time.strftime("[%H:%M:%S]"), m, flush=True)
def write_line(grp, res, body):
    with open(os.path.join(grp, "schemata"), "w") as f: f.write(f"{res}:{body}\n")
def domain_body(grp, res, val):
    with open(os.path.join(grp, "schemata")) as f:
        for line in f:
            line = line.replace(" ", "").strip()
            if line.startswith(res + ":"):
                parts = line[len(res)+1:].split(";")
                return ";".join(f"{p.split('=')[0]}={val}" if p.split('=')[0] == DOMAIN else p
                                for p in parts)
    raise RuntimeError(f"{res} not in {grp}")


class Tenant:
    def __init__(self, c, out):
        self.name = c["name"]; self.grp = c["grp"]; self.cores = c["cores"]
        self.perf_json = os.path.join(out, f"cp_{self.name}.json")
        self.proc = None
        self.ips = 0.0; self.llc_acc_rate = 0.0; self.miss_ratio = 0.0; self.miss_rate = 0.0
        self.ips_full = None          # solo baseline (IPS at full resources)
        self.prev_ips = None
        self.llc_state = MAINTAIN; self.mba_state = MAINTAIN
        self.ways = 0; self.mba_idx = 0   # mba_idx into MBA_LEVELS (0 = 100%)

    def start_perf(self):
        self.proc = subprocess.Popen(
            ["perf", "stat", "-j", "-o", self.perf_json,
             "-e", "instructions,cycles,LLC-loads,LLC-load-misses",
             "-I", str(PERF_INT_MS), "-C", self.cores, "--", "sleep", "100000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_perf(self):
        p = self.proc
        if p is None or p.poll() is not None: return
        p.send_signal(signal.SIGINT)
        for _ in range(10):
            if p.poll() is not None: return
            time.sleep(0.5)
        p.kill(); p.wait()

    def sample(self):
        # NOTE (bugfix): slowdown must be a RATIO of like units. solo_ipc.json
        # holds IPC, so `ips` here is IPC (instructions/cycle), NOT
        # instructions/sec -- IPS = IPC x freq and freq is ~constant, so the
        # IPC ratio == the IPS ratio CoPart's Eq.1 asks for. The first cut
        # divided IPC_solo by instructions-per-second => slowdown ~0 for all
        # tenants, which silently destroyed the sigma/mu objective.
        ev = {"instructions": [], "cycles": [], "LLC-loads": [], "LLC-load-misses": []}
        try: lines = open(self.perf_json).readlines()
        except OSError: return
        for line in lines[-60:]:
            line = line.strip().rstrip(",")
            if not line.startswith("{"): continue
            try: o = json.loads(line)
            except ValueError: continue
            e = o.get("event") or ""
            try: v = float(o.get("counter-value"))
            except (TypeError, ValueError): continue
            for k in ev:
                if k in e and not (k == "LLC-loads" and "miss" in e):
                    ev[k].append(v)
        n = 3
        interval_s = PERF_INT_MS / 1000.0
        if ev["instructions"] and ev["cycles"]:
            self.prev_ips = self.ips
            ins = sum(ev["instructions"][-n:])
            cyc = sum(ev["cycles"][-n:])
            self.ips = (ins / cyc) if cyc > 0 else 0.0          # IPC (same unit as solo_ipc)
        if ev["LLC-loads"]:
            acc = sum(ev["LLC-loads"][-n:]) / max(1, len(ev["LLC-loads"][-n:]))
            self.llc_acc_rate = acc / interval_s                 # LLC accesses/sec
            if ev["LLC-load-misses"]:
                miss = sum(ev["LLC-load-misses"][-n:]) / max(1, len(ev["LLC-load-misses"][-n:]))
                self.miss_rate = miss / interval_s
                self.miss_ratio = (miss / acc) if acc > 0 else 0.0

    def slowdown(self):
        # their Eq 1: IPS_full / IPS_current  (>=1; higher = worse off)
        if not self.ips_full or self.ips <= 0: return 1.0
        return max(1e-6, self.ips_full / self.ips)

    def perf_delta(self):
        # relative IPS change since last cycle (proxy for their delta_P test)
        if not self.prev_ips or self.prev_ips <= 0: return 0.0
        return (self.ips - self.prev_ips) / self.prev_ips


class CoPartStyle:
    def __init__(self, cfg_path, out):
        os.makedirs(out, exist_ok=True)
        cfg = json.load(open(cfg_path))
        self.tenants = [Tenant(c, out) for c in cfg["tenants"]]
        self.by = {t.name: t for t in self.tenants}
        self.ids = [t.name for t in self.tenants]
        self.decisions = open(os.path.join(out, "decisions.jsonl"), "w")
        self.full = int(open(os.path.join(RESCTRL, "info/L3/cbm_mask")).read(), 16)
        self.nbits = self.full.bit_length()
        # IPS_full baselines from the solo round (our deviation; their
        # profiling phase). solo_ipc.json holds solo IPC -> scale to IPS via
        # the same interval, so the RATIO IPS_full/IPS is what matters.
        sp = cfg.get("solo_ipc")
        self.solo = {}
        if sp and os.path.exists(sp):
            try: self.solo = json.load(open(sp))
            except Exception: pass
        # start EQUAL (their initial partition): ways split evenly, MBA 100%
        base, extra = divmod(self.nbits, len(self.tenants))
        for i, t in enumerate(self.tenants):
            t.ways = base + (1 if i < extra else 0)
            t.mba_idx = 0
        self.stream_miss_rate = 1.0   # denominator for memory-traffic-ratio
        self.prev_state = None; self.retry = 0
        self.stop = False; self.t0 = time.monotonic()
        signal.signal(signal.SIGTERM, self._sig); signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_): self.stop = True
    def record(self, kind, **kw):
        kw.update(kind=kind, t=round(time.monotonic() - self.t0, 1))
        self.decisions.write(json.dumps(kw) + "\n"); self.decisions.flush()

    # ── the fairness objective ───────────────────────────────────────────
    def unfairness(self):
        """their Eq 2: sigma/mu of slowdowns (lower = fairer)."""
        s = [t.slowdown() for t in self.tenants]
        mu = sum(s) / len(s)
        if mu <= 0: return 0.0
        var = sum((x - mu) ** 2 for x in s) / len(s)
        return (var ** 0.5) / mu

    # ── FSM classifiers (their S5.2 / S5.3) ─────────────────────────────
    def classify(self):
        # memory-traffic-ratio denominator = the most bandwidth-intensive
        # tenant's miss rate (their STREAM reference; STREAM is in our scene)
        self.stream_miss_rate = max([t.miss_rate for t in self.tenants] + [1.0])
        for t in self.tenants:
            d = t.perf_delta()
            # LLC FSM: can it SUPPLY a way, does it DEMAND one, or MAINTAIN?
            if t.llc_acc_rate < ALPHA_LLC_ACC or t.miss_ratio < BETA_MISS_RATIO:
                t.llc_state = SUPPLY          # low LLC use -> a way is reclaimable
            elif d >= DELTA_P:
                t.llc_state = DEMAND          # improving with resources -> wants more
            elif d <= -DELTA_P:
                t.llc_state = DEMAND          # degrading -> needs more
            else:
                t.llc_state = MAINTAIN
            # MBA FSM: memory-traffic-ratio vs the reference
            mtr = t.miss_rate / self.stream_miss_rate if self.stream_miss_rate > 0 else 0.0
            if mtr < GAMMA_MTR:
                t.mba_state = SUPPLY          # little memory traffic -> can be throttled
            elif mtr >= GAMMA_BIG_MTR:
                t.mba_state = DEMAND          # heavy traffic -> wants bandwidth
            else:
                t.mba_state = MAINTAIN

    # ── HR matching: move a unit SUPPLY -> highest-slowdown DEMAND ───────
    def next_state(self):
        moved = None
        for res in ("LLC", "MBA"):
            if res == "LLC":
                producers = [t for t in self.tenants if t.llc_state == SUPPLY and t.ways > MIN_WAYS]
                consumers = [t for t in self.tenants if t.llc_state == DEMAND]
            else:
                producers = [t for t in self.tenants if t.mba_state == SUPPLY
                             and t.mba_idx + 1 < len(MBA_LEVELS)]
                consumers = [t for t in self.tenants if t.mba_state == DEMAND and t.mba_idx > 0]
            if not producers or not consumers:
                continue
            # HR preference: the WORST-OFF (highest slowdown) consumer is served
            # first; the LEAST-slowed producer gives up the unit.
            c = max(consumers, key=lambda t: t.slowdown())
            p = min(producers, key=lambda t: t.slowdown())
            if c.name == p.name:
                continue
            if res == "LLC":
                p.ways -= 1; c.ways += 1
            else:
                p.mba_idx += 1      # throttle the producer one notch
                c.mba_idx -= 1      # relax the consumer one notch
            moved = (res, p.name, c.name)
            break                    # one unit per cycle (their per-period step)
        return moved

    def state_tuple(self):
        return tuple((t.name, t.ways, t.mba_idx) for t in self.tenants)

    def random_neighbour(self):
        """their Lines 11-14: when converged, perturb to escape a local optimum."""
        import random
        movers = [t for t in self.tenants if t.ways > MIN_WAYS]
        takers = [t for t in self.tenants if t is not None]
        if not movers or len(takers) < 2: return None
        p = random.choice(movers)
        c = random.choice([t for t in takers if t.name != p.name])
        p.ways -= 1; c.ways += 1
        return ("LLC", p.name, c.name)

    # ── actuation ────────────────────────────────────────────────────────
    def apply_all(self):
        used = 0
        for t in self.tenants:
            k = max(MIN_WAYS, t.ways)
            hi = self.nbits - used; lo = max(0, hi - k)
            mask = ((1 << hi) - 1) & ~((1 << lo) - 1) if lo > 0 else (1 << hi) - 1
            write_line(t.grp, "L3", domain_body(t.grp, "L3", f"{mask:x}"))
            try: write_line(t.grp, "MB", domain_body(t.grp, "MB", str(MBA_LEVELS[t.mba_idx])))
            except OSError: pass
            used += k
            if used >= self.nbits: used = 0

    def restore(self):
        try: write_line(RESCTRL, "L3", domain_body(RESCTRL, "L3", f"{self.full:x}"))
        except OSError: pass
        for t in self.tenants:
            if os.path.isdir(t.grp):
                try:
                    write_line(t.grp, "L3", domain_body(t.grp, "L3", f"{self.full:x}"))
                    write_line(t.grp, "MB", domain_body(t.grp, "MB", "100"))
                except OSError: pass

    def run(self):
        for t in self.tenants: t.start_perf()
        # seed IPS_full from the solo baselines (IPC ratio is scale-free)
        for t in self.tenants:
            v = self.solo.get(t.name)
            if v: t.ips_full = float(v)
        self.apply_all()
        log(f"copart_style up: {len(self.tenants)} tenants, {self.nbits} ways, "
            f"objective=minimize unfairness sigma/mu (fairness scalar)")
        while not self.stop:
            time.sleep(CYCLE_SECS)
            for t in self.tenants:
                if time.monotonic() - self.t0 >= 8: t.sample()
            if time.monotonic() - self.t0 < 8: continue
            # seed ips_full from first observations if no solo baseline given
            for t in self.tenants:
                if not t.ips_full and t.ips > 0: t.ips_full = t.ips
            self.classify()
            unf = self.unfairness()
            self.record("scan", unfairness=round(unf, 4),
                        slowdown={t.name: round(t.slowdown(), 3) for t in self.tenants},
                        llc_fsm={t.name: t.llc_state for t in self.tenants},
                        mba_fsm={t.name: t.mba_state for t in self.tenants},
                        ways={t.name: t.ways for t in self.tenants},
                        mba={t.name: MBA_LEVELS[t.mba_idx] for t in self.tenants})
            self._cyc = getattr(self, "_cyc", 0) + 1
            if self._cyc % RETRY_REFRESH_CYCLES == 0 and self.retry >= THETA_RETRY:
                self.retry = 0
                self.record("retry-refresh", cycle=self._cyc)
            moved = self.next_state()
            if moved is None:
                # converged -> random neighbour (bounded retries), else idle
                if self.retry < THETA_RETRY:
                    moved = self.random_neighbour(); self.retry += 1
                    if moved: self.record("explore", res=moved[0], frm=moved[1], to=moved[2])
                else:
                    self.record("idle", unfairness=round(unf, 4)); continue
            else:
                self.retry = 0
                self.record("match", res=moved[0], frm=moved[1], to=moved[2],
                            unfairness=round(unf, 4))
            self.apply_all()
        log("copart_style shutting down: restoring")
        self.restore()
        for t in self.tenants: t.stop_perf()
        self.decisions.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if os.geteuid() != 0: sys.exit("must run as root (resctrl)")
    CoPartStyle(a.config, a.out).run()


if __name__ == "__main__":
    main()
