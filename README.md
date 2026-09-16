# free-lunch — CATch: taking the free lunch in a shared CPU cache

**CATch** (`coeffd4.1`) is a userspace controller for consolidated servers. It
takes back last-level-cache capacity from tenants that cannot use it, and it
never makes another tenant worse off than it was without any management.

On a shared node, a **streamer** (a tenant that moves a lot of data and
almost never re-reads it) fills the L3 cache with lines nobody reuses. The
tenants that *do* reuse cache lose hit rate. CATch finds the streamers by how
they behave, confines each one to a small exclusive slice of the cache (Intel
CAT), and lets the remaining tenants claim the freed space by ordinary LRU.
**Every step is checked** against each tenant's own unmanaged performance, the
donor included, and is rolled back if anyone falls below it.

## At a glance

**Scene D3.** How much of the recoverable loss each controller took back, and
what it cost the co-tenant:

![D3 headline: recovery vs harm per controller](streaming_coeff/figures/headline_single_d3.png)

**Scene mix2.** Every tenant's IPC change in every run. The shaded band is the
5% do-no-harm margin:

![mix2: per-tenant IPC change per run](streaming_coeff/figures/mix2_tenants.png)

**Classification.** Tenant natures from memory bandwidth × LLC hit rate,
checked against isolated runs:

![claim-1: nature classification](streaming_coeff/figures/claim1_solo_key.png)

<details><summary>More: per-run D3 figure and per-mix classification</summary>

![D3 per-run](streaming_coeff/figures/headline_d3.png)
![classification per mix](streaming_coeff/figures/claim1_per_mix.png)

</details>

## How it works

1. **Classify** each tenant from two hardware counters, memory bandwidth
   (MBM) and LLC hit rate: STREAMER (high bandwidth, low hit), SENSITIVE,
   DUAL, or NEUTRAL.
2. **Anchor** every tenant's unmanaged IPC (instructions per cycle). That is
   the do-no-harm floor.
3. **Fence the streamers, one at a time.** Each streamer's exclusive band
   starts at its measured occupancy and shrinks one way per verified step. A
   failed step widens it back by one way. Streamers stack from bit 0 upward.
4. **Observe** for one scan while the other tenants expand into the freed
   ways.
5. **Cap absorbers, one at a time.** An absorber is a tenant that took freed
   cache without turning it into IPC. Its band starts at occupancy + 1 way and
   is walked down until its associativity knee.
6. Hand over to the inherited levers: bounded MBA throttling and migration to
   the other socket.

There is no per-victim reservation. The shared remainder of the cache *is*
the reservation. Design, measured rationale, and falsified alternatives:
[`streaming_coeff/coeffd/COEFFD4_DESIGN.md`](streaming_coeff/coeffd/COEFFD4_DESIGN.md).

## Repository map

```
streaming_coeff/
  coeffd/
    coeffd4.1.py            CATch, the finalized controller
    coeffd4.py              previous version (used for the D3 headline runs)
    coeffd3.py              base daemon: sampling, RDT, MBA, migration
    COEFFD4_DESIGN.md       design document
    test_coeffd4*.py        offline unit tests (no hardware, no root)
  exp16_natural_contention/
    run_certify.sh          launches a scene and runs one or more controller arms
    bench_lib.sh            workload launchers, binary paths, turbo-off gate
    run_iso.sh              isolated per-tenant baselines
    run_mix2_oracle.sh      mix2 pipeline (iso -> teeth -> SpiderSense sweep -> grade)
    certify.py              grader: victim recovery + per-donor cost, every tenant
    *_style.py              peer controllers re-implemented from their papers
                            (SpiderSense, SATORI, dCat, Cacheman), with tests
    results_iso/            isolated baselines for scenes D3 and mix2n
    results_certify/        the runs shown in the figures (see below)
  figures/                  headline figures and the classification (claim-1) figure
```

## Requirements

- An Intel server with **RDT**: CAT (L3 masks), MBA and MBM/CMT, with
  `resctrl` mounted at `/sys/fs/resctrl`. Our box is 2× Xeon Gold 6430:
  60 MB L3 per socket, 15 CAT ways (4 MB each), MBA in 10% steps. `WAY_MB`
  in `coeffd4.1.py` assumes 4 MB per way.
- Linux `perf` and root (`sudo`). **Turbo must be off**
  (`/sys/devices/system/cpu/intel_pstate/no_turbo = 1`); the harness refuses
  to run otherwise. Scenes are pinned to socket 0; socket 1 is the migration
  destination.
- Python 3 (standard library). The SATORI arm also needs `scikit-optimize`
  and `scipy`.

### Workloads (not included; build from source)

`bench_lib.sh` holds the paths. Adjust them for your machine.

| Tenant | Source | Expected path |
|---|---|---|
| `npb_*` (cg, mg, ep, ...) | NAS Parallel Benchmarks 3.4.2 (OMP) | `exp16_natural_contention/npb_bin/*.x` |
| `canneal` | PARSEC 3.0 | `$PARSEC/kernels/canneal/...` + `parsec_inputs_can/` |
| `llama` | llama.cpp + qwen2.5-0.5b-instruct Q4_K_M | `bench_extra/llama.cpp/...` |
| `sphinx` | TailBench | `$TAILBENCH_DIR/sphinx/` |
| `memcached` | memcached + memtier_benchmark | `bench_extra/...` |
| `pr` | GAP benchmark suite | `exp8_inbetween_victim/pr` |
| `stream_l` | STREAM (looped) | `exp16_natural_contention/bench_bin/stream_long` |

Peer controllers that are not re-implemented here: **PaLLOC**
(`PALLOC_DIR`) and the **SATORI** artifact (`SATORI_PYDEPS`). Both are cloned
from their authors' public repositories.

## Running

Run the unit tests first (no root needed):

```
cd streaming_coeff/coeffd && python3 test_coeffd4.py && python3 test_coeffd4_1.py
```

**Scene D3** (victims canneal, npb_cg_d80, npb_ep_e; streamers npb_mg_d300,
llama), CATch for a 400 s window:

```
cd streaming_coeff/exp16_natural_contention
sudo env COEFFD4=$PWD/../coeffd/coeffd4.1.py POL="npb_mg_d300 llama" POLNC=4 \
     ISO_AS=D3 AS=D3_c41_400 RUNDSOLO=1 ARMS= CTRLS=c4 C4WIN=400 \
     ./run_certify.sh canneal npb_cg_d80 npb_ep_e
python3 certify.py results_certify/D3_c41_400
```

**Scene mix2** (victims sphinx, memcached, pr; streamer stream_l):

```
sudo env COEFFD4=$PWD/../coeffd/coeffd4.1.py POL="stream_l" POLNC=4 \
     ISO_AS=mix2n AS=mix2n_c41r1 RUNDSOLO=1 ARMS= CTRLS=c4 C4WIN=400 \
     ./run_certify.sh sphinx memcached pr
```

Other arms are chosen with `CTRLS=` (for example `spider_conv`, `dcat`,
`palloc`, `cacheman`, `null`). Environment variables go **after** `sudo`,
because `sudo` resets the environment. `COEFFD4` defaults to `coeffd4.py`.

## Results in this repository

| Directory | What it is |
|---|---|
| `D3_order`, `D3_c4_400b` | CATch (`coeffd4`) live on D3, n=2 |
| `D3_c41_400*` | CATch (`coeffd4.1`) live on D3 |
| `D3_spider`, `D3_spconv*`, `D3_rep_spider_conv` | SpiderSense sweep and its converged picks replayed live |
| `D3_rep_sat_conv*` | SATORI's converged masks replayed live |
| `D3_rep_cm_nest`, `D3_rep_band_dp*` | Cacheman overlap-vs-depth test (nested vs band layouts) |
| `D3_dcat*`, `D3_palloc*`, `D3_cacheman*` | peer controllers, live |
| `D3_null*` | no controller (observe only) |
| `mix2n_*` | mix2: teeth run, three SpiderSense sweeps and replays, CATch runs |

Each run directory holds `cfg.json` and, per phase, `decisions.jsonl` (every
scan, apply, commit and rollback) and `perf_<tenant>.json` (IPC). Grade any
run with `certify.py`.

### What the evidence supports

- **The fence is nearly free for the donor; an MBA throttle is not** (n=2).
  Fencing a streamer cost it about 1% of IPC. Throttling the same streamer
  cost it about 93%.
- **Capacity and associativity are the same knob under CAT** (n=2 runs,
  one tenant). Capping a high-reuse tenant at its *own* occupancy still cost
  it 7–9%. That is why every cap in CATch walks down one way at a time under
  verification instead of being computed once.
- **On mix2, CATch stayed inside the 5% do-no-harm margin in every run.**
  Two of SpiderSense's three converged picks pushed a tenant more than 15%
  below unmanaged. Victim recovery is a tie with SpiderSense's harmless picks,
  not a win.
- **The absorber cap has a visible cost.** On mix2, `coeffd4.1` capped pr at
  5 ways, and pr ended 4.1% below unmanaged (inside the margin, n=1). Its
  occupancy was unchanged while its hit rate fell, which is the associativity
  effect described above, now seen on a second tenant. `coeffd4` never
  managed to cap pr and left it at +0.4/+0.7%.

Recovery percentages have run-to-run noise of ±8–12 percentage points. Read
them as orderings, not exact digits. Donors are counted in every
per-tenant table.

### Known limitations

- Tenants whose harm does not show up in hardware counters (for example,
  latency-only services) cannot be protected without an SLO feed.
- Freed cache goes to whoever reuses most aggressively, which is not
  necessarily the victim. CATch reserves nothing for a named victim.
- A streamer that starts very large gets a wide first band, which briefly
  narrows the other tenants' associativity. The verification rolls that step
  back when it hurts, and the retry starts narrower (seen on D3 and on mix2).
