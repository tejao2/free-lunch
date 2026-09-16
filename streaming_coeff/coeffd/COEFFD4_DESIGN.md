# coeffd4 — free-lunch-scoped QoS controller (design lock)

Supersedes coeffd3's per-victim reserve→throttle loop. Same triage core
(measured harm, smallest reversible step, verify per step, migration at the
frontier), but **scoped to free-lunch only** and rebuilt on a **band cache
model** with an **aggressor-first** flow. Locks Changes 1–4.

## Motto / scope

Take the free lunch whenever possible; **stand down when the only recovery
would cost someone unsanctioned.** We do NOT build a general-case optimizer.

Free-lunch tiers (per cycle, take the highest available):
- **T1 free / cheap-free:** fence a no-reuse streamer (cache) · bounded MBA
  throttle ≤ cost-limit (bandwidth) · migrate to an idle socket.
- **T2 sanctioned (not free):** deep throttle a *lower-priority* streamer (L1).
- **T3:** stand down.

## Nature taxonomy (mbm × hit, stabilized + hysteretic) + db-visibility

| mbm | hit | class | role |
|-----|-----|-------|------|
| high | low  | STREAMER | free donor (fence cache / bounded throttle) |
| low  | high, db≥gate | SENSITIVE-visible | cache-capacity victim (reserve) |
| low  | high, db<gate | SENSITIVE-invisible (LC) | **blind spot** — not black-box protectable |
| high | high | DUAL | victim on cache + aggressor on bandwidth |
| low  | low  | neutral | leave at baseline |

Channel is **not predictable** from a victim's own counters (canneal proves a
pointer-chaser looks cache-ish but is bandwidth-bound). So: **probe and let
verify redirect.** reserve→improves = cache-bound (keep); reserve→no-improve =
bandwidth-bound (roll back → throttle).

## Levers

- **CAT-fence** (streamer): shrink+COMPACT the streamer's band toward the far
  edge, one way/step. Frees a *contiguous* band adjacent to the victims.
  Verify = **streamer ips flat** (free because no-reuse; a drop ⇒ misclassified
  reuser ⇒ rollback). Free for cache-capacity victims; **backfires on bandwidth
  victims** (fenced streamer misses more ⇒ more DRAM traffic).
- **MBA-throttle** (streamer/dual): nature-ranked (lowest-hit/highest-mbm),
  iterate, fall through when floored. Bounded (T1) unless sanctioned (T2).
  Existing cost-bound + benefit-count + coherence rules carry over.
- **CAT-reserve** (victim): grow the victim's band into the freed contiguous
  region, **improvement-gated** (db drops → keep; plateau/no-improve → stop —
  this is BOTH the fairness stop and the channel detector). Fairness across
  victims: serve top-hurt (priority, then cmt) one way, re-rank; optional
  ≤1 way/victim/round for strict equal-prio round-robin.
- **Migration:** frontier escape (duals whose bandwidth can't be throttled for
  free; saturated equal-prio scenes).

## Control flow (aggressor-first)

```
each decide cycle (one verified step, warmup-gated):
  classify natures (stabilized)
  capture/refresh the UNMANAGED baseline (Change 1)
  if pending → verify (commit / rollback+block)
  elif no admissible free/sanctioned donor exists → HOLD (Change 2)
  else, in phase order:
     1. FENCE   each streamer, shrink+compact toward floor
                (T1; verify = donor IPC flat AND >= its own floor -- Change 3)
                seed the band from MEASURED OCCUPANCY, never from nbits: a band
                of nbits-1 does not restrict the donor, it PARTITIONS and leaves
                the victims one way (D3-v2 c4: 11 attempts, all rolled back)
     1b. ELIMINATE the worst-converting absorber, ONE way per verified step,
                stopping at its associativity knee (see ELIMINATE section)
     2. THROTTLE streamers/duals, iterate nature-ranked       (T1 bounded / T2 sanctioned)
     3. RESERVE freed contiguous band to hurt victims         (improvement-gated)
     4. frontier → MIGRATE lowest-cmt hurt victim to idle socket
     5. else SETTLE (healed/frontier) → hold until scene change
```
Phases 1–2 = reclaim the aggressors' free lunch (cache + bandwidth) first;
phase 3 = distribute it to victims. The **dual is not a free donor** — its
cache reuse means squeezing it is costed; only reclaim from it under sanction
(T2), guarded at mix-start + a 3–5% noise margin.

## ELIMINATE — the absorber cap (added 2026-09-03, measured)

Phase 2, between FENCE and THROTTLE. A fence frees a contiguous band and the
crowd claims it by LRU — but the claimant need not be the tenant that can USE
it. Measured in D3-v2: the fence released 21.3 MB; canneal converted it at
**0.69 %IPC/MB** and `npb_cg_d80` at **0.012 %/MB**, ~57x worse, and cg_d80 took
more of it. Roughly half the free lunch went to a tenant that could not use it.

**Absorber identification is MEASURED, not assumed:** conversion = %IPC gained
per MB gained since the Change-1 baseline (so the baseline must store occupancy,
not just IPC). Excluded: donors, anything already below its floor, and the
worst-hurt tenant.

**The cap WALKS DOWN under two-sided verify. It is never computed and applied in
one shot** — that is the falsified `cat_cap` design, and the reason is the
associativity result below.

## Associativity — why the cap walks, and what it says about the band model

**Under CAT, capacity and associativity are the same knob.** A mask of N ways is
N-way associative regardless of how much capacity N ways represent, and no
placement or alignment changes that. Measured, D3-v2, `npb_cg_d80` at
near-constant occupancy:

| phase | ways | occ MB | hit |
|---|---|---|---|
| `noisy` | 15 | 14.9 | **0.940** |
| `cat_cap` | 4 | **15.2** | **0.882** |
| `eq` | 3 | 11.9 | 0.786 |
| `cat` | 13 | 26.2 | 0.940 |

15.2 MB in 4 ways is strictly worse than 14.9 MB across 15. The static `cat_cap`
arm honoured its occupancy cap and still left the tenant **7.2 % below
unmanaged** — so **no occupancy-denominated cap can be safe by construction**;
the guarantee must be verified in IPC.

**REPLICATED n=2, and instance-clean** (2026-09-04). The falsification is the
most solid result in either run — it is not a ratio-of-differences, so it carries
none of the ~52x amplification that destroyed the recovery digits:

| run | noisy IPC | cat_cap IPC | harm | occ noisy→cap | hit noisy→cap |
|---|---|---|---|---|---|
| D3-v2 | 0.735 | 0.682 | **−7.3 %** | 14.9 → 15.2 MB | 0.94 → 0.88 |
| D3-v3 | 0.736 | 0.670 | **−9.0 %** | 14.5 → 15.0 MB | 0.94 → 0.86 |

The cap was HONOURED both times — occupancy went UP — and the hit rate collapsed
anyway. `npb_cg_d80`'s instance fingerprint (`LLC-loads/KI`) holds to **0.9 % and
1.0 %** across all seven arms in both runs, so this is the same program
throughout: the harm is real, not an instance artefact.

Together with the donor-cost asymmetry these are the project's **only two n=2
claims**, and one of them is this negative.

**SCOPE LIMIT — this is n=2 in RUNS but n=1 in TENANTS.** `npb_cg_d80` is a
single tenant, and the rule below generalises from it to all high-hit tenants.
Three other dev-set victims have never been capped. Treat "walk the cap" as the
conservative default it is, not as a characterised property of high-hit
workloads; capping a second tenant is the cheapest way to earn the general claim.

**This resolves band-vs-nested-prefix, and the answer is neither.** The question
is not the layout, it is WHO gets confined:
- Fencing a **STREAMER** 15→2 ways cost **1.1 %** (hit 0.02 — no reuse, so no
  associativity to lose). Disjoint bands are free here.
- Capping a **REUSE** tenant 15→4 ways cost **7.2 %** (hit 0.94). Disjoint bands
  are expensive here, and nested prefixes would only move the cost to whoever
  ends up with the narrow mask.

So: **bands for low-hit tenants, and for anyone with reuse, narrow ONE WAY AT A
TIME and let verify stop at their associativity knee.** cg_d80 reads hit 0.940
at 13 ways and 0.882 at 4 — the wide steps are free and the walk pays only for
the step that crosses the knee, which is then rolled back. That is the triage
contract (smallest reversible step, verify per step) applied to a resource we
had wrongly treated as one-shot computable.

## Cache = band model (replaces nested-prefix)

Every classified aggressor is an **explicit contiguous band**; victims get
contiguous exclusive bands; a shared remainder holds the crowd (dual/neutral).
- **Seed** each streamer band from its occupancy→ways (rough; verify corrects).
- **Fence = shrink + compact** toward the far edge so freed ways coalesce into
  ONE contiguous band next to the victims (compacting a no-reuse streamer is
  free). Layout ordering (streamers adjacent to the growing victim) is part of
  the plan.
- Drop the static SHARED_MIN_WAYS floor and the RSV_MAX_WAYS cap — the
  ips-flat / improvement gates are the real bounds now.

## Change 1 — unmanaged-baseline harm anchor

Harm is judged vs the **mix-start (unmanaged) baseline**, not the pre-step
state. Capture each tenant's steady baseline at scene start; re-capture on
scene change. Do-no-harm floor = "no equal/higher-prio tenant below its
unmanaged baseline (minus 3–5% noise)." Fixes the zero-sum drift (coeffd3's
per-step reference let 8 explorations compound to −16%). NOTE: the baseline is
the CONTENDED state (streamers present) — there is no clean pre-pollution
solo baseline online. So it is a do-no-harm FLOOR, not a hot-set estimator.

## Change 2 — futility-hysteresis + predictive stand-down

- **Predictive early-out:** if no admissible donor exists (no stable streamer;
  no sanctioned lower-prio; no idle migration dest), stand down WITHOUT probing.
  Streamer test = short-stable (2–3 scans) hit≤0.5 & mbm≥3GB/s — NOT the earned
  at_capacity flag (which needs actuations → would deadlock at t=0).
- **Hysteresis:** after settling with hurt remaining, HOLD; re-engage only on
  scene change (tenant arrive/leave or load shift past threshold). Kills
  coeffd3's ~8× re-exploration churn in zero-sum scenes.

## Change 3 — the donor is a DNH-protected tenant (streamer accounting)

**The problem this closes.** Everywhere above, the streamer is called a "free
donor", and the fence's verify is written as `streamer ips flat` — a
*misclassification* check ("did we mistake a reuser for a streamer?"). Nothing
says the streamer is itself covered by the do-no-harm floor. If it is not, the
DNH claim is **vacuous**: any victim can be "recovered harm-free" by taking
from a tenant that is never counted. That is precisely the accounting gap that
forced the exp16 DNH-oracle retraction, where the aggressor was never measured
and a +14% "harm-free" result evaporated once it was.

**The rule.** A streamer is a *lever target*, not a second-class tenant. It is
in the population for the floor, for verify, and for every reported number.

1. **Baseline.** Change 1 captures the mix-start unmanaged baseline for EVERY
   tenant, donors included. Same noise margin (3–5%).
2. **Two-sided fence verify.** After a fence step, check both, and log which
   one fired — they mean different things and must not be collapsed:
   - `nature_wrong`: donor IPC dropped ⇒ it has reuse ⇒ it was misclassified
     ⇒ roll back and re-classify (blocked from fencing).
   - `floor_breach`: donor IPC below its unmanaged baseline − margin ⇒ the
     classification may be right but the step still cost it ⇒ roll back.
   Same numeric test, two distinct verdicts, because (a) is a sensing failure
   we should learn from and (b) is a contract violation we must undo.
3. **Sanctioned breach is the ONLY exception, and it is recorded.** T2 deep
   throttle of a strictly lower-priority streamer may put it under its floor.
   Every such step emits `{"kind":"sanctioned_breach", tenant, prio,
   victim_prio, depth, baseline, observed}`. Nothing else may breach. The paper
   then states a countable fact — "N breaches, all sanctioned, none at equal
   priority" — instead of an assurance.
4. **Reporting.** Donors appear in every per-tenant table and inside every
   aggregate metric (ΣIPC, PaLLOC Eq.7, CoPart σ/µ, Themis NP). An MBA throttle
   costing the streamer 94% MUST show as a loss; that is the metric working.

**Why this strengthens rather than weakens the result.** It is what makes the
fence-vs-throttle asymmetry a finding at all: on the measured scene the fence
cost the donor **−1.9%** and the throttle **−93.9%**, i.e. the fence stays
inside the floor and the throttle blows through it. Without donor accounting
both look equally "free" and the T1/T2 tiering has no evidence behind it.

**Scene consequence.** A demonstrator scene should contain at least one donor
that is NOT free to fence (a streamer with real reuse, e.g. `npb_ft_d`,
hit 0.49) alongside one that is (`npb_mg_d`, hit 0.02), so the rollback path is
exercised rather than assumed. A scene where every donor is free cannot
distinguish "we respect the floor" from "we never got near it".

## Reuse from coeffd3.py (do NOT rewrite)

perf/TMA sampling (`sample`, `start_perf`, `parse_perf_tail`); RDT reads
(`read_mon`); `is_hurt`/GATE_PP + SLO (`slo_violating`) detection; nature
signals (`update_at_capacity` logic, mbm×hit); verify/rollback engine +
`blocked` set; MBA lever (`apply_mba`, `MBA_LEVELS`, donor tiers, cost-bound,
benefit-count, coherence); migration (`apply_migration`/`try_migration`/
`rollback_migration`); guard/release; main loop skeleton; `record`/jsonl.

**New/replaced:** cache mask geometry (nested-prefix → bands + shrink/compact);
FENCE lever; reserve adapted to bands; aggressor-first phase order; Change-1
baseline store; Change-2 early-out + hysteresis; free-lunch tiering.

**Dropped:** proactive db-invisible reservation (unverifiable); static
SHARED_MIN/RSV_MAX; latency-hiding-LC scenes as black-box targets.

## Honest scope boundaries (state these in the paper)

- **db-invisible LCs are not black-box protectable** (harm invisible to db AND
  ips). They free-ride on streamer suppression; guaranteed protection needs L2/SLO.
- **Duals push to migration** — cache protects their reuse, bandwidth aggression
  is only solved by sanctioned throttle or migration.
- **Free-lunch magnitude = the streamer's reclaimable footprint.** Small footprint
  ⇒ little free lunch. We do not manufacture it.
- **Mix A is a THROTTLE scene, not a fence scene** (canneal is bandwidth-bound).
  The fence needs a genuine cache-capacity victim.

## Demonstrator scenes (to scout + build)

Goal: show the free-lunch claim, i.e. **L0 recovers a cache-capacity victim
harm-free via fence where replicated peers can't / harm.**
1. Scout **cache-capacity victims**: low-mbm/high-hit, db DROPS under CAT
   reservation (not MBA). Candidates: tiled/HPCC DGEMM, compute-bound SPEC
   (perlbench/gcc/povray/namd), small-index search, small-class NPB.
   Discriminator test: db response to CAT vs MBA, solo + controlled polluter.
2. **Fence scene:** cache-capacity victim(s) + no-reuse cache-hog(s)
   (cachebench/STREAM). Verify contention (victim degraded >15%) AND that the
   hog holds reclaimable ways.
3. Keep **throttle scenes** (bandwidth victims) and **migration scenes** (duals /
   saturated) to exercise the other tiers.
4. Run the full controller panel (coeffd4 L0/L1/mig + replicated peers) → the
   strength figure = "only coeffd claims the fence free lunch harm-free."

---

## VALIDATED IN THE FIELD — D3-v9 (2026-09-04)

First run where every designed mechanism fired and was measured. Full numbers in
`exp16_natural_contention/CERTIFY_DESIGN.md` ("D3-v9 — result of record").

- **Nature classification, unsupervised and correct**: both streamers found
  independently (llama hit 0.09 / mbm 11.8 GB/s; mg_d300 hit 0.02 / mbm 47 GB/s),
  canneal SENSITIVE, cg_d80 DUAL.
- **FENCE**: 9 verified steps (mg_d300 5->1 ways, llama 6->1). Donor cost
  +0.7% / -1.4% — inside the squeeze-free bound, replicating the fence-vs-throttle
  asymmetry (~1% vs ~93%) on a second donor.
- **ELIMINATE**: cap walked 12->4 ways on cg_d80, one way per verified step.
- **Change 3 two-sided verify EARNED ITS KEEP**: at 4 ways the step was rolled
  back with `why="assoc_knee"` (IPC 0.728 -> 0.682, hit 0.94 -> 0.89). This is the
  FALSIFIED static `cat_cap` policy — which cost the same tenant -7.3%/-9.0%
  because nothing watched — being detected and reversed ONLINE in one step.
  cg_d80 finished at -0.8%. **The verify loop is what makes the band model safe
  despite the associativity result; the model alone is not.**
- **Change 2 stand-down**: terminated cleanly on "no admissible step", settled
  `healed` with `hurt: []`. MBA never applied (all 100) — the free-lunch scope
  held; no tenant was squeezed to pay for another.
- Victim recovery +79.3% of what co-location cost it; occupancy 9.0 -> 29.3 MB
  against an unmanaged-baseline 29.5.

## DEFECT — the nature test has no footprint floor (found D3-v9)

`npb_ep_e` was labelled **SENSITIVE**. It holds 0.0 MB and moves 0.0 GB/s: on a
non-inclusive L3 (Xeon Gold 6430, 2 MiB L2/core) its working set never leaves L2.
Its `hit ~= 0.90` is a RATIO computed over 145k LLC-loads/interval against
canneal's 50M — 1000x less traffic. It satisfies `hit > HIT_LO and mbm < MBM_HI`
on traffic that barely exists.

**Fix**: gate the nature test on an absolute LLC-load rate; below the floor the
tenant is NEUTRAL regardless of its hit ratio. A tenant that does not touch the
LLC cannot be cache-sensitive, and no cache lever can help or hurt it.

Harmless in D3-v9 — coeffd4 correctly never actuated on ep_e, and it served as a
negative control (+0.1%, zero actuations). But the LABEL is wrong in substance
and a reviewer will ask why a tenant with no cache footprint is called sensitive.
