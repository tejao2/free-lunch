#!/usr/bin/env python3
"""cacheman_style -- a Cacheman-style (Hu et al., Alibaba Cloud, PPoPP'26)
LLC-occupancy fairness controller, reimplemented as an honest black-box
comparator for our 5-tenant D3 LLC-contention scene.

Cacheman is a production system (200k+ machines) whose entire control
signal is LLC occupancy (LLC_occu, read via Intel CMT/resctrl) -- no IPC,
no miss-rate, no declared priority, no SLO feed. It computes a per-tenant
fair baseline proportional to VM size, classifies each tenant's occupancy
against that baseline into {poor, adequate, excess, overflow}, and moves
tenants one step at a time along a GLOBAL, NESTED "gradient" ladder of CLOS
(Classes of Service): CLOS[0] gives full LLC access; each higher CLOS index
is a strictly smaller, nested (not disjoint) region, so suppression is
"shrink how far this tenant can reach into the shared cache", not "give it
an exclusive slice" (contrast with our own coeffd's nested-PREFIX
per-priority slice stack -- Cacheman's nesting is ONE ladder shared by every
tenant, not a per-tenant priority partition). See CACHEMAN_BUILD.md for the
full paper-to-code map and CACHEMAN_BUILD_LOG.md for the reading log this
build followed. Page numbers below are the PPoPP '26 printed page numbers
(pdftotext -layout of the PDF), not pdftotext line numbers.

FAITHFUL CORE (paper page/section per mechanism):
  - LLC_base_i = LLC_total * (cores_i / cores_total) * delta, delta=0.9
    (p.333 S4.3: "LLC_base = LLC_total * (VM_core_number/Socket_core_number)
    * delta"). cores_total (the paper's literal "Socket_core_number") is
    NOT the literal formula here -- it is switchable, default =
    OUR READING (--core-denom=managed, PI-decided default): the SUM OF THE
    MANAGED TENANTS' cores (5*4=20 on D3), citing the paper's own framing
    "taking all possible co-located VMs into account" (S3.2) as license to
    normalize over the co-located set rather than an unobservable physical
    constant. The alternative, paper-literal reading (--core-denom=socket)
    uses this node's PHYSICAL core count (32 on this box, read from
    /sys/devices/system/node/node0/cpulist + topology sibling count).
    Effect of the choice, measured on D3 (CACHEMAN_AUDIT.md correctness
    table, row 1): managed-cores denominator -> base=10.8 MiB/tenant;
    socket-cores denominator -> base=6.75 MiB/tenant. On the archived D3
    noisy-phase medians this FLIPS canneal (the scene's aggressor, ~9.0
    MiB occupancy) from `excess` (under socket denom, 9.0 > 1.1*6.75=7.4)
    to `poor` (under managed denom, 9.0 < 0.95*10.8=10.3) -- i.e. it changes
    whether Cacheman treats the scene's aggressor as a protected victim.
    The managed-cores default is kept because (a) it is the PI's decision
    for the primary run, and (b) it is the reading more charitable to
    Cacheman (a scene where the fair-share target is attainable, since
    Sigma(base) = LLC_total*delta exactly, vs. socket-denom where
    Sigma(base) < LLC_total*delta and is only attainable if the 5 D3
    tenants collectively hold little of a mostly-idle-looking socket).
    Report BOTH: run the socket-cores denominator as a sensitivity arm
    (--core-denom socket or env CACHEMAN_CORE_DENOM=socket) before
    publishing a single number.
  - VMClassify (Algorithm 1, p.333-334): occu > (1+alpha)*base -> excess
    (alpha=0.1); occu < (1-beta)*base -> poor (beta=0.05); else adequate.
    excess AND occu > LLC_upper=(1+theta)*base -> overflow (theta OPTIONAL,
    only for tenants with a declared consistency request; theta example
    0.5). Idle/"Other" VMs (VM_load < Threshold) are left unmanaged in
    CLOS[0] (p.333) -- see DEVIATIONS #2 for the idle proxy used.
  - LLCAllocationAdjustment (Algorithm 2, p.334), one 6-second cycle:
      1. every de-suppressible (level>0) Poor VM is de-suppressed one level
         (lines 20-21, unconditional).
      2. if Poor is nonempty AND the LLC is (near-)full -> PhaseForFairness:
         suppress ONE candidate, preferring Overflow over Excess, else log
         a Warning (lines 1-9). "the LLC is (near-)full" is OUR READING of
         "all LLC space occupied" (line 22) -- see DEVIATIONS #5, this was
         CACHEMAN_AUDIT.md's HIGH-severity C1/C2 defect and is fixed here.
      3. elif Overflow is nonempty -> PhaseForConsistency: suppress ALL of
         Overflow one level (lines 10-12).
      4. elif Excess is nonempty -> PhaseForOptimization, policy=
         "complete fair" ("excess cache resources are still distributed
         proportionally to VM size", p.334 col2) -- IMPLEMENTED as a real
         rebalancing step, see DEVIATIONS #3 (previously a permanent
         no-op; CACHEMAN_AUDIT.md ranked-fix #4).
      Only one-or-a-few VMs move per cycle (p.334 col2).
  - Gradient CLOS ladder (S4.2, p.332-333, Fig 1): a GLOBAL, nested sequence
    of 8 masks, adjacent levels differing by a small, even block of ways
    (own choice of step size -- see DEVIATIONS #1); CLOS[0] == the unmanaged
    full mask, so the arm makes NO write at t=0 (p.334: "newly scheduled VMs
    are placed in CLOS[0] by default").
  - Auditing metrics (S4.5, p.335, kept SEPARATE from the live control
    loop): R_dev = 1 - mean(occu over the run so far)/base; W_poor =
    consecutive-Poor-cycle count; B_violation = (R_dev>k) or (W_poor>=m),
    k=0.1, m=5 (paper's example values). Logged every cycle for the
    reviewer/grader, never read by the control logic itself. RE-AUDIT NOTE
    (N4, 2026-09-12 second pass): this remains true of every ALLOCATION
    decision (no mask write anywhere depends on rdev()/poor_streak/
    b_violation() -- Algorithm 1's own POOR classification, which DOES
    gate allocation, is a live per-cycle test, not these audit metrics).
    D9's stall detector (`is_wedged()`, an OUR-ADDITION harness-honesty
    check with no paper basis) reads live state (`t.state`, `t.level`)
    ONLY, never `b_violation()`/`rdev()` -- see `is_wedged()`'s own
    docstring for why the first version's use of the expanding-window
    R_dev was a defect (N1) and had to be removed. R_dev/W_poor/
    B_violation are reporting-only in every sense that matters for
    Algorithm 1/2 fidelity.
  - Monitoring (S4.6, p.335): per-VM LLC_occu = sum of per-thread CMT
    occupancy (resctrl `mon_data/mon_L3_*/llc_occupancy`), i.e. exactly
    `read_cmt()` below -- the same primitive alita_style.py already uses on
    this harness.
  - BLACK-BOX discipline: nothing here reads IPC, miss-rate, or any tenant-
    declared priority for a DECISION. A perf sampler runs alongside purely
    to satisfy run_certify.sh's grading contract (every arm must produce
    `perf_<tenant>.json`); it is never consulted by the control loop --
    see GradingPerf below and the header note at its definition.

DEVIATIONS / PORTABILITY (numbered; see CACHEMAN_BUILD_LOG.md #1-7 for the
full reasoning behind each):
 D1 CLOS ladder: 8 levels, sizes = 15,13,11,9,7,5,3,1 (linear step of 2
    ways/level on this box's 15-way L3); the paper gives no numeric ladder,
    only the qualitative "small block" property, which this satisfies.
    Implemented on the EXISTING 5 `cert_*` resctrl groups (own group's mask
    rewritten to its current level) -- no new resctrl groups are created,
    per the task's hard constraint (CLOS budget is 8 via L2 num_closids on
    this box; 6 are already spent by cert_* + root).
 D2 Idle/"Other" detection: p.333 S4.3 says an Other VM "is carefully
    identified using multiple real-time metrics, including CPU utilization,
    VM exits, and LLC access patterns... exhibit very few LLC_occu". No
    VM-exit signal is available outside KVM, so this arm uses TWO of the
    three named signals: (a) a per-core CPU-idle-tick-fraction proxy over
    the tenant's own cores (`read_core_idle_frac`, IDLE_FRAC_THRESHOLD),
    stands in for "CPU utilization"; (b) an LLC-occupancy-relative-to-total
    floor (`OTHER_OCC_FRAC`) restores the "LLC access patterns... very few
    LLC_occu" criterion the code previously dropped entirely
    (CACHEMAN_AUDIT.md ranked-fix #3: without it, a CPU-bound-but-
    cache-quiet tenant like npb_ep_e, 0.06 MiB occupancy at 100% CPU, was
    classified Target/Poor every cycle and was the SOLE trigger of every
    suppression in the author's dry run). A tenant is OTHER if EITHER
    signal fires (OR, not AND): matching the paper's plural "using
    multiple ... metrics" without inventing a combination formula the
    paper does not give. KNOWN AMBIGUITY (recorded, not silently picked):
    a truly starved victim also shows low occupancy, so the LLC-access
    criterion could in principle misclassify a victim as Other and exempt
    it from management. This is a genuine open question in the paper
    (CACHEMAN_AUDIT.md S7 item 3) with no textual resolution; OTHER_OCC_FRAC
    is set low enough (1% of total L3, see its definition below) that only
    a near-zero occupier like npb_ep_e triggers it on D3's measured
    footprints (canneal/llama/cg/mg all sit at multiple MiB even when
    suppressed to 1 way). Every OTHER classification is logged per-cycle
    (`state` in the "scan" record) so the reviewer can audit which reading
    fired and how often.
 D3 PhaseForOptimization policy = "complete fair" only. The paper's other
    named option ("perf": miss-rate/IPC best-effort) is NOT implemented:
    the paper's own S3.2 argues at length against performance-metric-driven
    fairness, and the task brief separately forbids IPC-driven logic beyond
    the paper's own mandate. "complete fair" IS implemented for real (fixed
    from a permanent no-op, CACHEMAN_AUDIT.md ranked-fix #4): p.334 col2
    "excess cache resources are still distributed proportionally to VM
    size" -- OUR READING, scoped to D3's five EQUAL-size tenants, where
    "proportional to VM size" collapses to "equal suppression level across
    the REBALANCE CANDIDATE POOL". ACTIVATION of this phase stays tied to
    S_excess being nonempty, matching Algorithm 2 line 26-27 literally. The
    CANDIDATE POOL for the actual de-suppression, however, is every
    Excess-OR-Adequate tenant, not S_excess alone (RE-AUDIT FIX, R5/
    ranked-fix-#4-partial, 2026-09-12 second pass): the first version only
    rebalanced within S_excess, so a tenant the fairness phase had earlier
    suppressed and that has SINCE recovered to Adequate (rather than
    staying Excess) was invisible to this phase and stayed stranded below
    level 0 for the rest of the run -- reproduced in the author's own dry
    run (llama ends at level 6/8 ways-3 while classified Adequate, never
    released). p.334 col2's "reallocated AMONG VMs" (not "among excess
    VMs") licenses the broader pool once the phase is active. Each cycle
    this phase fires (S_poor and S_overflow both empty, S_excess nonempty),
    if the pool does NOT already share the same ladder level, the MOST
    suppressed one is de-suppressed one level (paper's "only one or a few
    VMs" per cycle, p.334 col2), walking the distribution back toward
    equal. If already equal, it is correctly a no-op (recorded as such,
    with the reason). KNOWN LIMITATION: the paper gives no formula for
    unequal-size proportionality; a weighted generalization (e.g. drive
    each tenant's level toward equalizing occ/base rather than raw level)
    is not implemented and not exercised on D3 (all tenants are 4-core).
 D4 Suppression-candidate tie-break (no formula given): largest occ/base
    excess ratio first, ties by name. "Historical suppression behaviour" is
    logged per tenant but NOT used as a tiebreak (no formula to be
    faithful to).
 D5 "LLC is (near-)full" gate (Alg 2 line 22, "all LLC space occupied"):
    FIXED from CACHEMAN_AUDIT.md's HIGH-severity C1/C2 defect. The paper
    gives no formula; the OLD code summed only the 5 managed tenants'
    occupancy against LLC_FULL_FRAC(0.90)*LLC_total(60 MiB)=54 MiB -- which
    is EXACTLY Sigma(base) whenever the managed-cores core-denominator is
    used (Sigma(t.n_cores/cores_total)=1 by construction, so
    Sigma(base)=LLC_total*delta=54 MiB too), and which structurally
    EXCLUDES the root/default resctrl group even though it is measurably
    non-empty (59.3 MiB idle, this box). The result: "somebody is below
    baseline" and "the cache is full" were near-mutually-exclusive
    conditions, and a few suppression cycles could permanently switch the
    fairness phase off (author's dry run: 6/67 cycles firing, then 61
    consecutive no-ops with a tenant stuck at 0.09x baseline).
    OUR READING now (CACHEMAN_AUDIT.md S7 item 1, "Reading B", judged more
    charitable to Cacheman): "all LLC space occupied" means node-wide, not
    managed-tenants-only -- `pool_occupied_bytes()` adds the root/default
    group's own occupancy (real runs: `read_cmt(RESCTRL)`; dry-run has no
    resctrl to read, so it adds a documented SIM_OTHER_FRAC placeholder,
    see its definition) on top of the 5 tenants' sum, and the threshold
    constant is DECOUPLED from DELTA (0.90->0.75, an unrelated number, so
    the two conditions cannot be identical by construction regardless of
    denominator choice). This is deliberately biased toward the gate being
    OPEN (root occupancy is subject to the same CMT eviction-lag CLAUDE.md
    warns is unusable for "is this idle" -- an idle-looking root often
    still reads near-full) -- the safe failure direction for a fairness
    gate is "suppress when in doubt", not "silently stop suppressing".
    RE-AUDIT FINDING (N2, 2026-09-12 second pass): on THIS box the bias is
    total -- root alone reads ~59.3 MiB idle of a 60 MiB L3 (every cache
    line is tagged to SOME RMID), so Sigma(tenants)+root is >= 0.75*total
    on essentially every cycle regardless of what the 5 tenants are doing
    (measured: 67/67 dry-run cycles open). The gate has therefore stopped
    being a discriminator for Algorithm 2 line 22 on D3 -- PhaseForFairness
    now fires whenever S_poor is nonempty, full stop. This is disclosed
    (not silently absorbed): `pool_occupied_bytes()` now logs the actual
    byte total and the threshold alongside the boolean in every "scan"
    record (see `step()`), and CACHEMAN_BUILD.md states the non-
    discrimination finding for the results write-up. Still the correct
    fix for C1/C2 (a self-latching gate is strictly worse than a
    non-discriminating one), just not a strong claim on its own.
 D6 R_dev's "longer window (e.g. 10 minutes)" is implemented as an
    expanding running mean over the whole arm (400 s << 10 min on D3) --
    the longest window actually available, not a truncated fixed one.
 D7 LLC_upper / consistency requests: D3's harness cfg never declares one
    (no such field exists in run_certify.sh's write_cfg). The mechanism
    (overflow classification + PhaseForConsistency) is implemented in full
    behind an OPTIONAL per-tenant "theta" cfg key / --consistency CLI flag
    so it is testable; on D3's actual runs it is expected to never fire
    (stated up front, not a silent gap).
 D8 Grading-only perf sampler (GradingPerf): required by run_certify.sh's
    measure_ctrl contract (every arm must emit perf_<tenant>.json for
    certify.py/peer_metrics.py's IPC grading), NEVER read by the control
    loop. Writes directly to perf_<name>.json (pfx="perf"), so no copy step
    is needed in the wiring.
 D9 [OUR ADDITION, not from the paper] Fail-loud on stall: CACHEMAN_AUDIT.md
    C7 -- a run that logs "warning"/"optimization-noop" every cycle exits 0
    and looks healthy, which is exactly the C1 lock-out failure mode. In
    addition to the pre-existing CMT-unreadable fail-loud (3 consecutive
    unreadable cycles -> SystemExit(2)), `run()` now also aborts loudly
    after STALL_CYCLES consecutive cycles where some tenant has a LIVE
    (current-cycle) violation -- `t.state == POOR` this cycle, i.e. a
    tenant BELOW baseline RIGHT NOW, excluding `OTHER` -- with an
    admissible action available (it is de-suppressible, `level > 0`) AND
    the cycle actuated nothing (`step()` moved zero tenants). This is a
    harness-honesty safety net, not a paper mechanism: it converts a
    silent "Cacheman did nothing" result into a loud, unmissable failure
    so it cannot be published by accident.
    RE-AUDIT FIX (N1, HIGH, 2026-09-12 second pass): the FIRST version of
    this detector keyed on `Tenant.b_violation()` (R_dev>k OR
    poor_streak>=m), and R_dev is an EXPANDING window over the whole arm
    (D6) -- so a tenant that was poor for its first 10 cycles (the normal
    opening state of a contended scene) stayed in B_violation for TENS of
    cycles after it fully recovered to `adequate`, because the historical
    mean stayed dragged down. The re-audit executed this exact case: 10
    early cycles at 0.70x base, then perfect `adequate` cycles, and
    `b_violation()` was still True for cycles 11-29 (19 consecutive) with
    `poor_streak == 0`. Ten quiet, healthy, zero-actuation cycles in that
    window (a converged scene: everyone adequate, levels equal, nothing to
    do) would trip SystemExit(2) on a HEALTHY run -- the safety net
    designed to prevent a false "Cacheman did nothing" result would ITSELF
    have produced one. Fixed by keying on `t.state == POOR` (Algorithm 1's
    own live classification for this cycle, not an audit-only historical
    metric) AND `t.level > 0` (an admissible de-suppress action exists --
    a poor tenant already at level 0 has nothing left to be de-suppressed
    INTO, so its continued poverty is not evidence the CONTROLLER is
    stuck, only that its demand exceeds baseline). `OTHER` tenants are
    excluded by construction (`t.state == POOR` is false for them).
    N4/D6 note: `rdev()`/`poor_streak`/`b_violation()` remain PURELY
    reporting/auditing metrics (S4.5, p.335) -- kept SEPARATE from every
    allocation decision, exactly as the paper requires; the stall detector
    (an addition with no paper basis) reads live classification state
    (`t.state`, `t.level`), never `b_violation()`.
 D10 [OUR ADDITION] Stale-occupancy handling: CACHEMAN_AUDIT.md C3 -- a
    cycle where a tenant's CMT is unreadable in every sample now explicitly
    sets `t.occ = None` (previously it silently retained the prior cycle's
    numeric value, so `record("scan", ...)` would print stale data as if
    freshly sampled). `run()` also now SKIPS `step()` entirely for a cycle
    where any tenant is unreadable (previously `step()` ran unconditionally
    and classified/acted on stale data for that tenant) -- consistent with
    the project's triage contract of never acting on unverified data.
 D11 [OUR FIX] `read_cmt()` now sums only `mon_L3_<DOMAIN>` (domain "0",
    matching every schemata write this arm makes) instead of every
    `mon_L3_*` directory under the group. CACHEMAN_AUDIT.md C5: this scene
    and every schemata write are socket-0-only; summing socket 1 too let a
    reused RMID's stale cross-socket residue (CLAUDE.md: CMT occupancy
    persists until eviction) inflate a tenant's occupancy and misclassify
    it. Inherited from alita_style.py's convention; fixed here, not there
    (out of scope for this task).

Config: {"tenants": [{"name","grp","cores","theta"(optional)}...]}
  cores is the harness's "start-end" contiguous range string.
Usage: cacheman_style.py --config cfg.json --out outdir [--duration SECS]
                          [--consistency name:theta[,name:theta...]]
                          [--core-denom {managed,socket}]
       cacheman_style.py --dry-run --out outdir [--duration SECS]
--core-denom (default "managed", env CACHEMAN_CORE_DENOM overrides the
default): switches the LLC_base core-count denominator, see DEVIATIONS
above and CACHEMAN_AUDIT.md ranked-fix #2. Run "socket" as a sensitivity
arm before publishing a "managed" number.
Restores full masks on SIGTERM/SIGINT/normal exit.
"""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time

RESCTRL = "/sys/fs/resctrl"
DOMAIN = "0"

CYCLE_SECS = 6.0            # p.334 col2: "every 6 seconds"
SETTLE_S = 1.0              # ours: discard the tail right after a mask
                             # write before sampling for classification --
                             # the fidelity fix the task brief asked us to
                             # not relearn (a prior arm judged pre-change
                             # data by sampling too soon after a write).
N_SAMPLES = 4                # CMT samples averaged per cycle ("averaged
SAMPLE_GAP_S = 1.0           # across multiple samples if present", p.333)

DELTA = 0.9                  # p.333 S4.3: hypervisor/maintenance tax
ALPHA = 0.10                 # p.333 S4.3: excess band, "e.g., alpha=0.1"
BETA = 0.05                  # p.333 S4.3: poor band, "e.g., beta=0.05"
THETA_DEFAULT = 0.5          # p.334 col2: "a negotiated value (e.g., 0.5)"
K_VIOLATION = 0.10           # p.335 S4.5: "e.g., 0.1" (B_violation's R_dev term)
M_POOR = 5                   # p.335 S4.5: "e.g., m=5" (W_poor threshold)

NLEVELS = 8                  # p.332-333 Fig 1's own worked example; == this
                             # box's L2 num_closids (the real CLOS ceiling)
LLC_FULL_FRAC = 0.75         # D5 FIX: OUR choice, paper gives no number.
                             # Deliberately NOT 0.90 (== DELTA) any more --
                             # CACHEMAN_AUDIT.md C2: with the managed-cores
                             # denominator, Sigma(base) = LLC_total*DELTA
                             # EXACTLY, so a threshold equal to DELTA is
                             # tautologically identical to Sigma(base) no
                             # matter what DELTA is set to. A different,
                             # unrelated constant breaks that identity.
                             # Set below DELTA (not just != DELTA) so that,
                             # combined with the root-inclusive sum in
                             # pool_occupied_bytes(), the gate survives a
                             # single tenant being suppressed all the way
                             # to the ladder floor (see
                             # TestPoolFullGate.test_gate_survives_a_
                             # suppression_trajectory).
OTHER_OCC_FRAC = 0.01        # D2 FIX: OUR choice, paper gives no number.
                             # "exhibit very few LLC_occu" (p.333) floor,
                             # as a fraction of total L3 bytes (~0.6 MiB on
                             # this box) -- see DEVIATIONS #2 for the
                             # victim-vs-idle ambiguity this carries.
IDLE_FRAC_THRESHOLD = 0.98   # D2: OUR idle-VM proxy threshold (own-core idle
                             # tick fraction); expected never to fire on D3
SIM_OTHER_FRAC = 0.20        # D5 FIX, dry-run only: --dry-run has no real
                             # resctrl root group to read, so this constant
                             # stands in for read_cmt(RESCTRL)'s contribution
                             # to pool_occupied_bytes() in the smoke test.
                             # NEVER used outside dry=True.
ACTIVITY_TAIL_CYCLES = 10    # N-A: cycles of the level trace the activity
                             # summary calls "the tail". 10 cycles ~= 60 s,
                             # matching certify.py's CONTROLLER_TAIL*10 = 60 s
                             # graded window, so `inert_tail` speaks about the
                             # SAME seconds the grader scores.
STALL_CYCLES = 10            # D9: consecutive is_wedged() cycles before run()
                             # fails loud (~60-65 s here). NB (re-audit pass 3,
                             # N-A): is_wedged() is a LOCK-OUT detector, NOT an
                             # inertness detector, and is PROVABLY unable to
                             # fire while the pool_full gate is open -- see its
                             # docstring. Inertness is handled by the
                             # activity-summary record, which REPORTS rather
                             # than aborts.

FAKE_L3_BYTES = 61440 * 1024  # dry-run fallback == this box's real L3 size

POOR, ADEQUATE, EXCESS, OVERFLOW, OTHER = \
    "poor", "adequate", "excess", "overflow", "other"


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


# ---------------------------------------------------------------------------
# resctrl helpers (majority convention across the existing arms: alita_style
# / satori_style both use these exact names/bodies).
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


def read_cmt(grp, domain=DOMAIN):
    """Sum llc_occupancy (bytes) for this group's mon_data/mon_L3_<domain>
    ONLY (D11 / CACHEMAN_AUDIT.md C5 fix -- was every mon_L3_*, i.e. BOTH
    sockets, although this scene, every schemata write (DOMAIN="0"), and
    the task brief are all socket-0). resctrl already aggregates a group's
    per-thread RMID occupancy within one domain, satisfying p.335 S4.6's
    "sum of per-thread CMT occupancy" without needing the other socket.
    Summing the other socket risked inflating occupancy with a reused
    RMID's stale cross-socket residue (CLAUDE.md: CMT occupancy persists
    until eviction). Convention (name/shape) inherited from
    alita_style.py; the domain-scoping fix is local to this arm."""
    path = os.path.join(grp, "mon_data", f"mon_L3_{int(domain):02d}",
                        "llc_occupancy")
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None   # None = unreadable, distinct from a real 0


def read_core_idle_frac(cores_spec):
    """D2: our idle-VM proxy. cores_spec = 'a-b' (harness's own format).
    Returns the fraction of ticks spent idle+iowait across those cores
    between two /proc/stat reads 100ms apart, or None if unreadable."""
    def parse_cores(spec):
        a, _, b = spec.partition("-")
        try:
            return list(range(int(a), int(b) + 1)) if b else [int(a)]
        except ValueError:
            return []

    def snap(cores):
        want = {f"cpu{c}" for c in cores}
        out = {}
        try:
            with open("/proc/stat") as f:
                for line in f:
                    parts = line.split()
                    if parts and parts[0] in want:
                        vals = [int(x) for x in parts[1:8]]
                        out[parts[0]] = vals
        except OSError:
            return None
        return out if len(out) == len(want) else None

    cores = parse_cores(cores_spec)
    if not cores:
        return None
    a = snap(cores)
    if a is None:
        return None
    time.sleep(0.1)
    b = snap(cores)
    if b is None:
        return None
    idle_d = busy_d = 0
    for k in a:
        # /proc/stat fields: user nice system idle iowait irq softirq
        da = [b[k][i] - a[k][i] for i in range(7)]
        idle_d += da[3] + da[4]
        busy_d += sum(da)
    return (idle_d / busy_d) if busy_d > 0 else None


# ---------------------------------------------------------------------------
# D8: grading-only perf sampler. Cacheman's control loop NEVER reads this --
# it exists solely so run_certify.sh's measure_ctrl can grade this arm's IPC
# like every other arm's (certify.py / peer_metrics.py PHASES contract).
# ---------------------------------------------------------------------------

class GradingPerf:
    def __init__(self, name, cores, out_dir):
        self.name = name
        self.cores = cores
        self.json_path = os.path.join(out_dir, f"perf_{name}.json")
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["perf", "stat", "-j", "-o", self.json_path,
             # LLC-loads is NOT used by the control loop -- it is there so
             # certify.py's WITHIN-RUN instance guard (LLC-loads/KI, 3%) can be
             # evaluated on this arm. Without it the guard silently cannot
             # check the arm phase at all (`instance_fp` returns nothing), so a
             # tenant that relaunched into a different steady state mid-run
             # would go undetected here while c4/dcat/spider_conv ARE checked.
             # It is a pre-LLC quantity (L2 misses arriving at the LLC), so it
             # does not itself shift when CAT masks change -- which is exactly
             # why it was chosen as the fingerprint.
             "-e", "instructions,cycles,LLC-loads",
             "-I", "1000", "-C", self.cores, "--", "sleep", "100000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        p.send_signal(signal.SIGINT)          # bounded INT->KILL (CLAUDE.md)
        for _ in range(10):
            if p.poll() is not None:
                return
            time.sleep(0.5)
        p.kill()
        p.wait()


# ---------------------------------------------------------------------------
# Ladder: a SINGLE global nested sequence of masks (D1). level 0 = full
# width = the unmanaged mask; each level is a strict subset of the previous.
# ---------------------------------------------------------------------------

def build_ladder(nbits, nlevels=NLEVELS):
    if nlevels > nbits:
        nlevels = nbits   # degrade gracefully rather than emit duplicate levels
    sizes = []
    for k in range(nlevels):
        s = round(nbits - k * (nbits - 1) / (nlevels - 1)) if nlevels > 1 else nbits
        sizes.append(max(1, s))
    # enforce strictly decreasing (can only fail for pathologically small nbits)
    for i in range(1, len(sizes)):
        if sizes[i] >= sizes[i - 1]:
            sizes[i] = max(1, sizes[i - 1] - 1)
    masks = [(1 << s) - 1 for s in sizes]
    return sizes, masks


class Tenant:
    def __init__(self, c):
        self.name = c["name"]
        self.grp = c.get("grp")
        self.cores = c.get("cores")
        self.n_cores = self._count_cores(c.get("cores"))
        self.theta = c.get("theta")          # None = no consistency request
        self.level = 0
        self.base_bytes = 0.0
        self.occ = None                       # this cycle's averaged occu
        self.state = ADEQUATE
        self.poor_streak = 0
        self.times_suppressed = 0
        self.occ_history = []                 # D6: running R_dev window
        # N3 auditability (re-audit 2026-09-12): an OTHER tenant is invisible
        # to S_poor, to R_dev and to is_wedged(). That is intended (see
        # step()'s note) but must be CHECKABLE, not assumed inert -- these
        # two counters turn "no tenant sat in Other for the whole run" from a
        # 134-record grep into one end-of-run record.
        self.other_cycles = 0                 # cycles classified OTHER
        self.other_after_managed = 0          # OTHER cycles that FOLLOWED a
                                              # non-OTHER cycle = the
                                              # starved-into-invisibility shape

    @staticmethod
    def _count_cores(spec):
        if not spec:
            return 1
        a, _, b = str(spec).partition("-")
        try:
            return int(b) - int(a) + 1 if b else 1
        except ValueError:
            return max(1, str(spec).count(",") + 1)

    def rdev(self):
        if not self.occ_history or self.base_bytes <= 0:
            return 0.0
        return 1.0 - (sum(self.occ_history) / len(self.occ_history)) / self.base_bytes

    def b_violation(self):
        return (self.rdev() > K_VIOLATION) or (self.poor_streak >= M_POOR)


class SimTenant(Tenant):
    """--dry-run only: occupancy is a deterministic function of the
    tenant's own allocated ladder level plus a demand cap, so the ladder's
    directional response is exercised without root/resctrl/perf. Numbers
    mean nothing about any real workload -- this is a logic smoke test."""
    # name -> demand_bytes (what it would occupy given unlimited room),
    # seed_level (starting ladder level, 0 unless noted)
    SHAPES = {
        "canneal":     (160 * 1024 * 1024, 0),    # streamer: demand >> full LLC
        "npb_ep_e":    (0.05 * 1024 * 1024, 3),   # near-zero LLC footprint at
                                                   # EVERY level (0.05 MiB <<
                                                   # OTHER_OCC_FRAC*total~0.6MiB)
                                                   # -- SEEDED at level 3 to
                                                   # exercise the OTHER-tenant
                                                   # ->CLOS[0] release path
                                                   # (fix #3/#5), replacing the
                                                   # old (wrong) poor/de-suppress
                                                   # demonstration this tenant
                                                   # used to carry.
        "npb_cg_d80":  (13 * 1024 * 1024, 0),     # mild excess
        "npb_mg_d300": (12 * 1024 * 1024, 2),     # excess, SEEDED suppressed
                                                   # (level 2) unequal to
                                                   # npb_cg_d80's level 0 --
                                                   # exercises PhaseForOptimization
                                                   # rebalancing (fix #4)
        "llama":       (11 * 1024 * 1024, 7),     # SEEDED deeply suppressed
                                                   # (level 7): near baseline
                                                   # demand but a ladder-forced
                                                   # low reachable region makes
                                                   # it POOR -> exercises
                                                   # unconditional de-suppress
    }

    def __init__(self, name):
        super().__init__({"name": name, "cores": "0-3"})
        self.demand, seed = self.SHAPES[name]
        self.level = seed

    def measure(self, ladder_sizes, total_l3_bytes):
        # occupancy = min(demand, bytes reachable at current level)
        frac = ladder_sizes[self.level] / max(ladder_sizes)
        reachable = total_l3_bytes * frac
        val = min(self.demand, reachable)
        val *= 1 + random.gauss(0, 0.01)
        return max(0.0, val)


class CachemanStyle:
    def __init__(self, cfg_path, out, dry=False, consistency=None,
                 core_denom=None):
        os.makedirs(out, exist_ok=True)
        self.dry = dry
        self._fake_t = 0.0
        self.out = out
        if dry:
            self.tenants = [SimTenant(n) for n in sorted(SimTenant.SHAPES)]
            self.nbits = 15
            self.full = (1 << 15) - 1
            self.total_l3_bytes = FAKE_L3_BYTES
        else:
            cfg = json.load(open(cfg_path))
            self.tenants = sorted((Tenant(c) for c in cfg["tenants"]),
                                   key=lambda t: t.name)
            with open(os.path.join(RESCTRL, "info/L3/cbm_mask")) as f:
                self.full = int(f.read(), 16)
            self.nbits = self.full.bit_length()
            self.total_l3_bytes = self._read_l3_bytes()
        if consistency:
            for tok in consistency.split(","):
                if not tok:
                    continue
                nm, _, th = tok.partition(":")
                for t in self.tenants:
                    if t.name == nm:
                        t.theta = float(th) if th else THETA_DEFAULT
        # D7: any tenant not named above keeps theta=None (cfg default) --
        # no consistency request declared, overflow never classifies for it.

        self.ladder_sizes, self.ladder_masks = build_ladder(self.nbits)
        # Fix #1 (ranked-fix #2 in CACHEMAN_AUDIT.md): the core-count
        # denominator ("Socket_core_number", p.333 S4.3) is switchable, NOT
        # hardcoded as "the literal formula" -- see the module docstring's
        # FAITHFUL CORE section for the full reasoning and measured effect.
        self.core_denom_mode = (core_denom or
                                 os.environ.get("CACHEMAN_CORE_DENOM") or
                                 "managed")
        if self.core_denom_mode not in ("managed", "socket"):
            raise SystemExit(f"FATAL: --core-denom must be 'managed' or "
                              f"'socket', got {self.core_denom_mode!r}")
        self.socket_cores = (self._read_socket_core_count()
                              if self.core_denom_mode == "socket" else None)
        self.cores_denom = CachemanStyle.assign_baselines(
            self.tenants, self.total_l3_bytes, self.core_denom_mode,
            self.socket_cores)

        self.decisions = open(os.path.join(out, "decisions.jsonl"), "w")
        self.grading_perf = ([] if dry else
                              [GradingPerf(t.name, t.cores, out) for t in self.tenants])
        self.stop = False
        self.t0 = self.now()
        self.cycle_n = 0
        self.steps_taken = 0      # cycles on which step() actually ran.
                                  # NOT the same as cycle_n, which also
                                  # counts cycles SKIPPED for unreadable CMT
                                  # (`cmt-unreadable`) -- using cycle_n as the
                                  # denominator would make `always_other`
                                  # under-report after any skipped cycle
                                  # (re-audit pass 3, N3 residual gap).
        self.kind_counts = {}     # N-A: decisions.jsonl histogram, for the
                                  # end-of-run activity summary
        self.level_trace = []     # per-cycle [(name, level), ...] snapshot
        self.starved = 0
        self.stall_cycles = 0
        if not dry:
            signal.signal(signal.SIGTERM, self._sig)
            signal.signal(signal.SIGINT, self._sig)

    @staticmethod
    def assign_baselines(tenants, total_l3_bytes, mode="managed",
                          socket_cores=None):
        """LLC_base_i = LLC_total * (cores_i/cores_denom) * DELTA (p.333
        S4.3). Pure function of its arguments (no resctrl/sysfs) so tests
        can call the REAL formula directly -- CACHEMAN_AUDIT.md S6 flagged
        the old tests as reimplementing this arithmetic instead of calling
        it. mode="managed" (default, PI-decided): cores_denom = sum of the
        given tenants' cores. mode="socket": cores_denom = socket_cores
        (caller-supplied physical core count, see _read_socket_core_count).
        Returns the denominator actually used, for logging/tests."""
        if mode == "socket":
            if not socket_cores:
                raise ValueError("socket_cores required for mode='socket'")
            denom = socket_cores
        else:
            denom = sum(t.n_cores for t in tenants) or 1
        for t in tenants:
            t.base_bytes = total_l3_bytes * (t.n_cores / denom) * DELTA
        return denom

    @staticmethod
    def _read_l3_bytes():
        # any online core works; try a small handful in case cpu0 is offline
        for cpu in (2, 0, 1, 3, 4):
            p = f"/sys/devices/system/cpu/cpu{cpu}/cache/index3/size"
            try:
                with open(p) as f:
                    s = f.read().strip()
                if s.endswith("K"):
                    return int(s[:-1]) * 1024
                if s.endswith("M"):
                    return int(s[:-1]) * 1024 * 1024
                return int(s)
            except (OSError, ValueError):
                continue
        raise SystemExit("FATAL: cannot read L3 cache size from sysfs "
                          "(tried cpu0-cpu4 index3/size) -- refusing to guess "
                          "LLC_total, the denominator of every fair baseline")

    @staticmethod
    def _read_socket_core_count(node="0"):
        """Paper-literal "Socket_core_number" (--core-denom=socket): the
        PHYSICAL core count of the socket the scene is pinned to (node 0,
        per CLAUDE.md hardware map). Reads the node's logical CPU list and
        divides by threads-per-core (from the first CPU's topology sibling
        list) rather than guessing -- refuses to guess, like _read_l3_bytes."""
        def expand(spec):
            cpus = []
            for part in spec.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    a, b = part.split("-")
                    cpus.extend(range(int(a), int(b) + 1))
                else:
                    cpus.append(int(part))
            return cpus

        try:
            with open(f"/sys/devices/system/node/node{node}/cpulist") as f:
                logical = expand(f.read().strip())
            if not logical:
                raise ValueError("empty cpulist")
            cpu0 = logical[0]
            sib_path = (f"/sys/devices/system/cpu/cpu{cpu0}/topology/"
                        f"core_cpus_list")
            if not os.path.exists(sib_path):
                sib_path = (f"/sys/devices/system/cpu/cpu{cpu0}/topology/"
                            f"thread_siblings_list")
            with open(sib_path) as f:
                threads_per_core = max(1, len(expand(f.read().strip())))
            return max(1, len(logical) // threads_per_core)
        except (OSError, ValueError) as e:
            raise SystemExit(
                f"FATAL: cannot read socket physical core count from sysfs "
                f"for node{node} ({e}) -- refusing to guess "
                f"Socket_core_number, the paper-literal LLC_base denominator")

    def _sig(self, *_):
        self.stop = True

    def now(self):
        return self._fake_t if self.dry else time.monotonic()

    def sleep(self, s):
        if self.dry:
            self._fake_t += s
        else:
            time.sleep(s)

    #: actuation record kinds -- the arm "did something to the machine" iff
    #: one of these was emitted. `apply` is the mask write itself; the others
    #: are the decisions that caused it. Used by the activity summary (N-A).
    ACTUATION_KINDS = ("fairness-suppress", "consistency-suppress",
                       "de-suppress", "optimization-rebalance",
                       "other-release", "apply")

    def record(self, kind, **kw):
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        kw.update(kind=kind, t=round(self.now() - self.t0, 2))
        self.decisions.write(json.dumps(kw) + "\n")
        self.decisions.flush()

    def mask_for_level(self, level):
        return self.ladder_masks[max(0, min(level, len(self.ladder_masks) - 1))]

    def write_level(self, t):
        if self.dry:
            return
        m = self.mask_for_level(t.level)
        write_schemata_line(t.grp, "L3", domain_body(t.grp, "L3", f"{m:x}"))

    def restore(self):
        if self.dry:
            return
        try:
            write_schemata_line(RESCTRL, "L3", domain_body(RESCTRL, "L3", f"{self.full:x}"))
        except OSError:
            pass
        for t in self.tenants:
            if os.path.isdir(t.grp):
                try:
                    write_schemata_line(t.grp, "L3", domain_body(t.grp, "L3", f"{self.full:x}"))
                except OSError:
                    pass

    # ── one 6-second control cycle ────────────────────────────────────────
    def sample_all(self):
        """N_SAMPLES CMT reads per tenant, averaged (p.333: 'averaged across
        multiple samples if present'). Returns False if any tenant is
        unreadable in EVERY sample this cycle (fail-loud upstream)."""
        sums = {t.name: 0.0 for t in self.tenants}
        counts = {t.name: 0 for t in self.tenants}
        for i in range(N_SAMPLES):
            for t in self.tenants:
                if self.dry:
                    v = t.measure(self.ladder_sizes, self.total_l3_bytes)
                else:
                    v = read_cmt(t.grp)
                if v is not None:
                    sums[t.name] += v
                    counts[t.name] += 1
            if i < N_SAMPLES - 1:
                self.sleep(SAMPLE_GAP_S)
        ok = True
        for t in self.tenants:
            if counts[t.name] == 0:
                ok = False
                t.occ = None   # D10 fix: do NOT retain the prior cycle's
                               # stale value -- callers must see "unreadable"
                continue
            t.occ = sums[t.name] / counts[t.name]
            # NB: occ_history (R_dev's window) is appended in step(), AFTER
            # classification, and ONLY for non-Other cycles -- see the note
            # there ("Once an Other VM becomes active... subject to LLC
            # state monitoring", p.333, implies NOT monitored while Other).
        return ok

    def classify(self, t):
        if t.occ is None:
            return t.state   # unreadable this cycle: hold last state
        low_cpu = False
        if not self.dry:
            idle = read_core_idle_frac(t.cores)
            low_cpu = idle is not None and idle >= IDLE_FRAC_THRESHOLD
        # D2 fix (CACHEMAN_AUDIT.md ranked-fix #3): restore the paper's
        # "LLC access patterns... exhibit very few LLC_occu" criterion
        # (p.333 S4.3), evaluated regardless of dry/real since it needs no
        # sysfs. Either signal firing -> Other (paper: "multiple real-time
        # metrics", no combination formula given).
        low_llc = (self.total_l3_bytes > 0 and
                   t.occ < OTHER_OCC_FRAC * self.total_l3_bytes)
        if low_cpu or low_llc:
            return OTHER
        if t.occ > (1 + ALPHA) * t.base_bytes:
            if t.theta is not None and t.occ > (1 + t.theta) * t.base_bytes:
                return OVERFLOW
            return EXCESS
        if t.occ < (1 - BETA) * t.base_bytes:
            return POOR
        return ADEQUATE

    def pool_occupied_bytes(self):
        """'all LLC space occupied' (Alg 2 line 22) -- OUR READING (D5 fix,
        CACHEMAN_AUDIT.md ranked-fix #1): node-wide occupied cache, not just
        the 5 managed tenants (the old reading, which made this sum
        identical-by-construction to Sigma(base) under the managed-cores
        denominator -- C1/C2). Adds the root/default resctrl group's own
        occupancy, which captures every RMID this arm does not itself own.
        dry-run has no resctrl to read; SIM_OTHER_FRAC stands in so the
        --dry-run smoke test still exercises the "does not latch off"
        property the fix is for (see SIM_OTHER_FRAC's definition).
        Returns (total_bytes, root_bytes_or_None) -- RE-AUDIT FIX (N2,
        2026-09-12 second pass): the root/other term is now returned
        explicitly (not folded silently into the total) so `step()` can log
        both the total AND whether the root term was itself readable,
        rather than degrading silently to the tenants-only sum when
        `read_cmt(RESCTRL)` returns None."""
        tenant_occ = sum(t.occ or 0 for t in self.tenants)
        if self.dry:
            root_occ = SIM_OTHER_FRAC * self.total_l3_bytes
        else:
            root_occ = read_cmt(RESCTRL)   # None = unreadable, logged as such
        total_occ = tenant_occ + (root_occ or 0)
        return total_occ, root_occ

    def step(self):
        pool_occ, pool_root_occ = self.pool_occupied_bytes()
        pool_threshold = LLC_FULL_FRAC * self.total_l3_bytes
        pool_full = pool_occ >= pool_threshold
        for t in self.tenants:
            t.state = self.classify(t)
            t.poor_streak = t.poor_streak + 1 if t.state == POOR else 0
            # D2 addendum: an Other VM is NOT "subject to LLC state
            # monitoring" (p.333) -- exclude Other cycles from the R_dev
            # window, so a deliberately-unmanaged near-zero occupier (e.g.
            # npb_ep_e) does not manufacture a permanent B_violation.
            # RE-AUDIT NOTE (N3, 2026-09-12 second pass): this has a
            # DECIDED, DOCUMENTED consequence -- a tenant starved all the
            # way down to the OTHER_OCC_FRAC floor (0.01 * total_l3, ~0.6
            # MiB on this box) is classified Other by the LLC-access
            # criterion (see classify()), and is therefore ALSO invisible
            # to R_dev/B_violation AND to is_wedged()'s live-Poor check
            # (state==POOR is false for an Other tenant, by construction)
            # while it stays Other. This is INTENDED, not an oversight: the
            # SAME `other-release` mechanism (`level=0` reset above) is the
            # tenant's escape hatch -- once released to CLOS[0] its
            # occupancy can recover, and it is reclassified Target/Poor the
            # next cycle it fails the Other test, at which point both
            # R_dev and is_wedged() see it again. A tenant that stays below
            # the floor FOREVER even at full CLOS[0] access is, by the
            # paper's own definition (p.333, "very few LLC_occu"), a
            # genuinely quiet workload, not a hidden victim -- but this is
            # the SAME open ambiguity flagged in DEVIATIONS #2 (a real
            # victim also reads low occupancy) and is disclosed, not
            # resolved, here: report `state` per-cycle so a reviewer can
            # check no tenant sits in Other for the whole run.
            if t.state == OTHER:
                t.other_cycles += 1
                # ever been managed before this cycle? then it FELL into
                # Other rather than starting there (npb_ep_e starts there).
                if t.occ_history:
                    t.other_after_managed += 1
            if t.occ is not None and t.state != OTHER:
                t.occ_history.append(t.occ)
        self.record("scan",
                    occ_mb={t.name: round((t.occ or 0) / 1048576, 2) for t in self.tenants},
                    base_mb={t.name: round(t.base_bytes / 1048576, 2) for t in self.tenants},
                    ratio={t.name: round((t.occ or 0) / t.base_bytes, 3) if t.base_bytes else None
                           for t in self.tenants},
                    state={t.name: t.state for t in self.tenants},
                    level={t.name: t.level for t in self.tenants},
                    poor_streak={t.name: t.poor_streak for t in self.tenants},
                    rdev={t.name: round(t.rdev(), 4) for t in self.tenants},
                    b_violation={t.name: t.b_violation() for t in self.tenants},
                    pool_full=bool(pool_full),
                    # N2 fix: log the actual byte total/threshold/root term,
                    # not just the boolean -- otherwise "how close to the
                    # line was it" (and, on D3, "was it ever close at all")
                    # is unrecoverable after the fact.
                    pool_occ_mb=round(pool_occ / 1048576, 2),
                    pool_threshold_mb=round(pool_threshold / 1048576, 2),
                    pool_root_occ_mb=(None if pool_root_occ is None
                                      else round(pool_root_occ / 1048576, 2)))
        if not self.dry and pool_root_occ is None:
            self.record("pool-root-unreadable",
                        reason="read_cmt(RESCTRL) returned None this cycle; "
                               "pool_occ_mb above is tenants-only for this "
                               "cycle, not the full node-wide reading")

        moved = []

        # 0) OTHER tenants are unmanaged and MUST be released to CLOS[0]
        #    (p.333: "leaves them in the default CLOS[0], thus ensuring
        #    immediate access to all LLC ways if their load increases").
        #    Fix for CACHEMAN_AUDIT.md ranked-fix #5: the old code left a
        #    tenant that BECAME Other stuck at whatever level it last held.
        for t in self.tenants:
            if t.state == OTHER and t.level != 0:
                old = t.level
                t.level = 0
                moved.append(t)
                self.record("other-release", tenant=t.name, from_level=old, to_level=0)

        # 1) unconditional de-suppression of every de-suppressible Poor VM
        #    (Algorithm 2, lines 20-21).
        for t in self.tenants:
            if t.state == POOR and t.level > 0:
                old = t.level
                t.level -= 1
                moved.append(t)
                self.record("de-suppress", tenant=t.name, from_level=old, to_level=t.level)

        s_poor = [t for t in self.tenants if t.state == POOR]
        s_excess = [t for t in self.tenants if t.state == EXCESS]
        s_overflow = [t for t in self.tenants if t.state == OVERFLOW]

        def suppressible(pool):
            return [t for t in pool if t.level < len(self.ladder_masks) - 1]

        if s_poor and pool_full:
            # PhaseForFairness (Algorithm 2, lines 1-9): Overflow first, else
            # Excess, else Warning. D4: tie-break by largest occ/base ratio.
            cand_pool = suppressible(s_overflow) or suppressible(s_excess)
            if cand_pool:
                cand = min(cand_pool,
                           key=lambda t: (-((t.occ or 0) / t.base_bytes if t.base_bytes else 0),
                                          t.name))
                old = cand.level
                cand.level += 1
                cand.times_suppressed += 1
                moved.append(cand)
                self.record("fairness-suppress", tenant=cand.name, from_level=old,
                            to_level=cand.level, from_pool=("overflow" if cand in s_overflow
                                                             else "excess"),
                            poor=[t.name for t in s_poor])
            else:
                self.record("warning", reason="no suppressible candidate in "
                            "overflow/excess while poor and LLC full",
                            poor=[t.name for t in s_poor])
        elif s_overflow:
            # PhaseForConsistency (lines 10-12): suppress ALL overflow VMs.
            for t in s_overflow:
                if t.level < len(self.ladder_masks) - 1:
                    old = t.level
                    t.level += 1
                    t.times_suppressed += 1
                    moved.append(t)
                    self.record("consistency-suppress", tenant=t.name,
                                from_level=old, to_level=t.level)
                else:
                    self.record("consistency-at-floor", tenant=t.name, level=t.level)
        elif s_excess:
            # PhaseForOptimization (lines 13-16), policy="complete fair":
            # "excess cache resources are still distributed proportionally
            # to VM size" (p.334 col2). ACTIVATION stays tied to S_excess
            # being nonempty (Algorithm 2 line 26-27's own literal gate,
            # unchanged) -- but re-audit ranked-fix R5/#4-partial found the
            # REBALANCE CANDIDATE POOL was wrongly narrowed to S_excess
            # too, so an Adequate-but-still-suppressed tenant (one the
            # fairness phase suppressed earlier, which has since recovered
            # to Adequate rather than staying Excess) was never reached and
            # stayed stranded below level 0 for the rest of the run (the
            # author's own dry run: llama ends at level 6 while Adequate).
            # p.334 col2 says capacity is "reallocated AMONG VMs" (not
            # "among excess VMs") once baseline fairness holds -- OUR
            # READING: the CANDIDATE pool for de-suppression is every
            # managed, currently-classified Excess-or-Adequate tenant
            # (Poor is already handled unconditionally in phase 1 above;
            # Overflow/Other cannot reach this branch). "Proportional to
            # VM size" collapses, for D3's equal-size tenants, to "equal
            # ladder level across that pool" -- if unequal, de-suppress the
            # MOST suppressed one (paper's "only one or a few VMs" per
            # cycle). Genuinely a no-op when already equal.
            rebalance_pool = [t for t in self.tenants if t.state in (EXCESS, ADEQUATE)]
            levels_present = {t.level for t in rebalance_pool}
            if len(levels_present) > 1:
                most_suppressed = max(rebalance_pool, key=lambda t: (t.level, t.name))
                old = most_suppressed.level
                most_suppressed.level -= 1
                moved.append(most_suppressed)
                self.record("optimization-rebalance", tenant=most_suppressed.name,
                            from_level=old, to_level=most_suppressed.level,
                            from_state=most_suppressed.state,
                            excess=[t.name for t in s_excess],
                            rebalance_pool=[t.name for t in rebalance_pool],
                            levels_before={t.name: t.level if t is not most_suppressed
                                           else old for t in rebalance_pool})
            else:
                self.record("optimization-noop", policy="complete_fair",
                            excess=[t.name for t in s_excess],
                            rebalance_pool=[t.name for t in rebalance_pool],
                            reason="already equal distribution")
        else:
            self.record("steady")

        if moved:
            for t in moved:
                self.write_level(t)
            self.record("apply", levels={t.name: t.level for t in self.tenants},
                        masks={t.name: f"{self.mask_for_level(t.level):x}"
                               for t in self.tenants})
        # N-A: post-cycle level snapshot, so the activity summary can state
        # what the allocation actually WAS over the graded tail -- not merely
        # whether a decision was logged.
        self.level_trace.append({t.name: t.level for t in self.tenants})
        self.steps_taken += 1
        return moved

    def is_wedged(self, moved):
        """D9's stall condition (RE-AUDIT FIX N1, 2026-09-12 second pass).
        MUST be a CURRENT-cycle condition, never an expanding-window
        historical one (that was the defect: b_violation()/R_dev stayed
        True for 19 cycles after a tenant fully recovered to Adequate, so
        the first version of this check could abort a healthy, converged
        run). True iff, THIS cycle:
          1. some tenant is Poor RIGHT NOW (`t.state == POOR`, Algorithm
             1's own live classification -- excludes Other by construction,
             since a tenant cannot be both), AND
          2. the controller had an ADMISSIBLE ACTION available on that
             tenant's behalf: either the Poor tenant is itself
             de-suppressible (`level > 0` -- though note this case can
             never coincide with `not moved`, since step()'s unconditional
             phase 1 always takes that de-suppression; kept for clarity/
             defense-in-depth, not load-bearing), OR some Excess/Overflow
             tenant is suppressible (`level` below the ladder floor) --
             i.e. PhaseForFairness/PhaseForConsistency had raw material to
             act on but did not, which is the actual C1/C7 lock-out shape
             (a broken gate or a broken phase, not "nothing to do"), AND
          3. nothing actuated (`moved` is empty).
        A quiet, converged scene (everyone Adequate/Other, nobody Poor)
        never satisfies (1) and never fires -- this is intentional and
        tested (`TestStall.test_no_abort_when_everyone_adequate`).

        SCOPE LIMIT -- READ BEFORE RELYING ON THIS (re-audit pass 3, N-A,
        2026-09-12). This is a LOCK-OUT detector, NOT an inertness
        detector, and it is PROVABLY UNABLE TO FIRE while the pool_full
        gate is open:
          * clause (2)'s second disjunct is term-for-term step()'s
            `suppressible(s_overflow) or suppressible(s_excess)`, i.e. the
            fairness phase's own `cand_pool`. With `pool_full` True and
            `s_poor` nonempty, a nonempty `cand_pool` means the fairness
            phase SUPPRESSED something, so `moved` is nonempty and clause
            (3) fails;
          * clause (2)'s first disjunct (a de-suppressible Poor tenant) is
            always consumed by step()'s unconditional phase 1, which also
            fills `moved`.
        So `pool_full and is_wedged()` is unsatisfiable. Verified
        empirically as well: 20 000 randomized scenes -> 0 fires with the
        gate open, 120 with it closed. Since N2 established the gate is
        open in 134/134 cycles on this box, this detector contributes NO
        protection on D3. It is kept because it still catches the C1/C7
        shape it was written for (a BROKEN/closed gate), which is a real
        regression risk. Protection against publishing an arm that simply
        never acted comes from the `activity-summary` record instead --
        which reports rather than aborts, because "took no action" is a
        legitimate result here, not a harness failure."""
        max_level = len(self.ladder_masks) - 1
        poor_now = [t for t in self.tenants if t.state == POOR]
        if not poor_now:
            return False
        admissible = (any(t.level > 0 for t in poor_now) or
                      any(t.level < max_level for t in self.tenants
                          if t.state in (EXCESS, OVERFLOW)))
        return admissible and not moved

    def run(self, duration=None):
        log(f"cacheman_style up: {len(self.tenants)} tenants "
            f"{[t.name for t in self.tenants]}, {self.nbits} ways "
            f"({self.total_l3_bytes // 1048576} MiB), ladder sizes={self.ladder_sizes}")
        self.record("start", order=[t.name for t in self.tenants],
                    ladder_sizes=self.ladder_sizes,
                    ladder_masks=[f"{m:x}" for m in self.ladder_masks],
                    base_mb={t.name: round(t.base_bytes / 1048576, 2) for t in self.tenants},
                    cores={t.name: t.cores for t in self.tenants},
                    theta={t.name: t.theta for t in self.tenants})
        for p in self.grading_perf:
            p.start()
        try:
            while not self.stop and (duration is None or self.now() - self.t0 < duration):
                self.sleep(SETTLE_S)
                ok = self.sample_all()
                if not ok:
                    self.starved += 1
                    unread = [t.name for t in self.tenants if t.occ is None]
                    self.record("cmt-unreadable", tenants=unread)
                    if self.starved >= 3:
                        log(f"FATAL: CMT occupancy unreadable for {unread} for "
                            f"3 consecutive cycles -- refusing to run inert "
                            f"(this would look like a real result)")
                        raise SystemExit(2)
                    # D10 fix: do NOT call step() on a cycle with stale/
                    # missing data for any tenant -- previously step() ran
                    # unconditionally and classified/acted on last cycle's
                    # numeric occupancy for the unreadable tenant(s).
                else:
                    self.starved = 0
                    moved = self.step()
                    # D9 fix (RE-AUDIT N1, 2026-09-12 second pass): fail
                    # loud if the controller is stuck -- someone is Poor
                    # RIGHT NOW (live classification, NOT the expanding-
                    # window b_violation()/R_dev, which stayed True for 19
                    # cycles after a tenant fully recovered in the
                    # auditor's executed test) AND the controller had an
                    # admissible action available for STALL_CYCLES in a row
                    # yet actuated nothing. See is_wedged()'s docstring for
                    # the exact condition and why each half is needed.
                    if self.is_wedged(moved):
                        self.stall_cycles += 1
                    else:
                        self.stall_cycles = 0
                    if self.stall_cycles >= STALL_CYCLES:
                        log(f"FATAL: {STALL_CYCLES} consecutive cycles with a "
                            f"LIVE baseline violation, an admissible action "
                            f"available, and zero actuation -- controller is "
                            f"stuck (C1/C7 lock-out pattern); refusing to "
                            f"keep publishing an inert run as 'cacheman'")
                        self.record("fatal-stall", stall_cycles=self.stall_cycles,
                                    state={t.name: t.state for t in self.tenants},
                                    level={t.name: t.level for t in self.tenants})
                        raise SystemExit(2)
                self.cycle_n += 1
                # align to the 6 s cadence, accounting for time already spent
                spent = SETTLE_S + (N_SAMPLES - 1) * SAMPLE_GAP_S
                self.sleep(max(0.0, CYCLE_SECS - spent))
        finally:
            # N3 disclosure record (re-audit 2026-09-12): emitted on EVERY
            # exit path, including the fatal ones, so an arm that aborted is
            # still auditable for the Other-invisibility failure mode.
            try:
                self.record("other-residency", cycles=self.cycle_n,
                            steps_taken=self.steps_taken,
                            other_cycles={t.name: t.other_cycles for t in self.tenants},
                            other_after_managed={t.name: t.other_after_managed
                                                 for t in self.tenants},
                            always_other=[t.name for t in self.tenants
                                          if self.steps_taken and
                                          t.other_cycles >= self.steps_taken],
                            fell_into_other=[t.name for t in self.tenants
                                             if t.other_after_managed])
            except Exception:
                pass
            # N-A disclosure record (re-audit pass 3, 2026-09-12). The stall
            # detector is a LOCK-OUT detector and is provably unable to fire
            # while the pool_full gate is open (which, per N2, is every cycle
            # on this box) -- so it provides NO protection against publishing
            # an arm that simply never acted. This summary is that protection,
            # and it REPORTS rather than aborts: "Cacheman took no action on
            # D3" is a legitimate result about an occupancy-fairness
            # controller meeting this scene, and aborting the run would
            # destroy exactly that finding instead of recording it. The
            # grading side must refuse to print a recovery number for an arm
            # whose tail is inert (CONTROLLER_COMPARISON.md B2).
            try:
                tail = self.level_trace[-ACTIVITY_TAIL_CYCLES:]
                acts = {k: v for k, v in self.kind_counts.items()
                        if k in self.ACTUATION_KINDS}
                # inert tail = every managed tenant sat at level 0 (full 15-way
                # mask = the unmanaged allocation) for the whole tail window
                inert_tail = bool(tail) and all(
                    all(lv == 0 for lv in snap.values()) for snap in tail)
                self.record("activity-summary",
                            cycles=self.cycle_n,
                            actuation_counts=acts,
                            total_actuations=sum(acts.values()),
                            acted=bool(acts),
                            tail_cycles=len(tail),
                            tail_levels_distinct=sorted(
                                {lv for snap in tail for lv in snap.values()}),
                            inert_tail=inert_tail,
                            final_levels=(self.level_trace[-1]
                                          if self.level_trace else None),
                            kind_counts=self.kind_counts)
                if inert_tail:
                    log(f"WARNING: cacheman tail is INERT -- every tenant at "
                        f"level 0 (the unmanaged 15-way mask) for the last "
                        f"{len(tail)} cycles. This arm did not act over its "
                        f"graded window; report it as 'took no action', "
                        f"NEVER as a recovery number.")
            except Exception:
                pass
            log("cacheman_style down: restoring full masks")
            self.restore()
            for p in self.grading_perf:
                p.stop()
            self.decisions.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="synthetic tenants + fake clock, no root/resctrl/perf")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--consistency", default=None,
                    help="D7: name:theta[,name:theta...] -- declares a "
                         "consistency request the D3 harness cfg does not "
                         "carry, so the overflow/consistency mechanism can "
                         "be exercised")
    ap.add_argument("--core-denom", choices=("managed", "socket"), default=None,
                    help="LLC_base core-count denominator (default: env "
                         "CACHEMAN_CORE_DENOM or 'managed'). 'socket' is the "
                         "paper-literal Socket_core_number reading -- run "
                         "both before publishing a single number, see the "
                         "module docstring's FAITHFUL CORE section.")
    a = ap.parse_args()
    if a.dry_run:
        random.seed(1)
        CachemanStyle(None, a.out, dry=True, consistency=a.consistency,
                      core_denom=a.core_denom).run(a.duration or 400)
        return
    if not a.config:
        sys.exit("--config required")
    if os.geteuid() != 0:
        sys.exit("must run as root (resctrl)")
    CachemanStyle(a.config, a.out, consistency=a.consistency,
                  core_denom=a.core_denom).run(a.duration)


if __name__ == "__main__":
    main()
