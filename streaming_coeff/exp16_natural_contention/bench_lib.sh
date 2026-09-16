#!/usr/bin/env bash
# bench_lib.sh -- the shared tenant roster + launcher for exp16 scripts.
#
# Extracted verbatim from run_phase_a.sh (2026-08-07) so that Phase-A and the
# certification harness cannot drift apart on binary paths, inputs or launch
# flags. The launch bodies are UNCHANGED; the only difference is that they now
# read locals of launch_tenant() instead of script globals, so more than one
# tenant can be started per run.
#
# Contract for callers:
#   source bench_lib.sh
#   launch_tenant <name> <cores> <resctrl-grp> <outdir> <ncores>
#      -> sets LAUNCH_PID; appends any load generator to LOAD_PIDS[]
#   tenant_ready <name> <outdir> <pid>   -> 0 when the tenant is serving
#   assign_tenant <pid> <grp>            -> put every tid in the RDT group
#
# ONE deliberate behaviour change vs the original launch(): the launchers that
# hardcoded `OMP_NUM_THREADS=4` / a literal `4` thread argument (GAP, PARSEC
# blackscholes/streamcluster/canneal) now use $NCORES. Identical at the default
# NCORES=4; it only differs when a caller sets NC=, where the old code pinned N
# cores but still spawned 4 threads (oversubscription).
#
# NOTE the "wrapper pid" hazard: most launchers are `while true; do BIN; done`,
# so LAUNCH_PID is a SHELL, not the workload. RDT is fine (the shell echoes
# itself into the group and children inherit), but perf must be attached with
# `--perf-attach cores`, never `-p` -- see coeffd3.py PERF_ATTACH.

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP7_DIR="${BASE_DIR}/exp7_tailbench_contention"; EXP8_DIR="${BASE_DIR}/exp8_inbetween_victim"
TAILBENCH_DIR="$(cat "${EXP7_DIR}/tailbench_dir.txt" 2>/dev/null || echo /tmp/tailbench)"
DR=/home/tejendra/litmus/tailbench_data/tailbench.inputs
PARSEC=/home/tejendra/litmus/parsec/pkgs
COEFFD3="${BASE_DIR}/coeffd/coeffd3.py"; COEFFD4="${COEFFD4:-${BASE_DIR}/coeffd/coeffd4.py}"   # override: COEFFD4=<path> (e.g. coeffd4.1.py)
CANNEAL_IN="${SCRIPT_DIR}/parsec_inputs_can"

declare -A BIN=(
  [silo]="${TAILBENCH_DIR}/silo/out-perf.masstree/benchmarks/dbtest_integrated"
  [masstree]="${TAILBENCH_DIR}/masstree/mttest_integrated" [xapian]="${TAILBENCH_DIR}/xapian/xapian_integrated"
  [sphinx]="${TAILBENCH_DIR}/sphinx/decoder_integrated" [imgdnn]="${TAILBENCH_DIR}/img-dnn/img-dnn_integrated"
  [sphinx_q2]="${TAILBENCH_DIR}/sphinx/decoder_integrated" [sphinx_q3]="${TAILBENCH_DIR}/sphinx/decoder_integrated"
  [cc]="${EXP8_DIR}/cc" [bc]="${EXP8_DIR}/bc" [pr]="${EXP8_DIR}/pr" [bfs]="${BASE_DIR}/vendor/gapbs/bfs"
  [sssp]="${EXP8_DIR}/sssp" [tc]="${EXP8_DIR}/tc" [cc_sv]="${EXP8_DIR}/cc_sv"
  [streamcluster]="${PARSEC}/kernels/streamcluster/inst/amd64-linux.gcc/bin/streamcluster"
  [canneal]="${PARSEC}/kernels/canneal/inst/amd64-linux.gcc/bin/canneal"
  [blackscholes]="${PARSEC}/apps/blackscholes/inst/amd64-linux.gcc/bin/blackscholes"
  [pol0]="/home/tejendra/litmus/llcbench/cachebench/cachebench" [pol1]="/home/tejendra/litmus/llcbench/cachebench/cachebench"
  [stream]="${BASE_DIR}/vendor/STREAM/stream"
  # stream_l (mix2, 2026-09-15): SAME stream.c, SAME 80M-element arrays, only
  # NTIMES 10->30000 (gcc -O2 -fopenmp -mcmodel=medium). `stream` finishes in
  # 1.84 s and so re-enters malloc+first-touch every ~2 s of a scene; one
  # stream_l invocation is ~3700 s alone (0.12 s/iter, 4 thr, cores 2-5), longer
  # contended. Rebuild matched the vendor binary: Copy/Scale/Add within 1-6%,
  # wall 1.84 vs 1.84 s. Working set unchanged => nature carries over.
  # ISO A/B (results_iso/mix2_iso_stream{,_l}, cores 2-5, 5 scans each):
  #   stream   IPC 0.549  ips 4.17-4.37e9 (+-5%, alternating)  49.5 GB/s  fp 22.6
  #   stream_l IPC 1.525  ips 12.82e9     (+-0.05%)            65.6 GB/s  fp 7.3
  # The looped `stream` spends ~2/3 of its cycles in per-invocation setup, so
  # any number measured on it describes mostly init, not the streaming kernels
  # (fp differs 3x -- a different program by the 3% instance-guard standard).
  # Use stream_l for any scene; `stream` is kept only so old runs reproduce.
  [stream_l]="${SCRIPT_DIR}/bench_bin/stream_long"
  [npb_ft]="${SCRIPT_DIR}/npb_bin/ft.C.x" [npb_is]="${SCRIPT_DIR}/npb_bin/is.C.x" [npb_cg]="${SCRIPT_DIR}/npb_bin/cg.C.x"
  [npb_mg]="${SCRIPT_DIR}/npb_bin/mg.C.x" [npb_lu]="${SCRIPT_DIR}/npb_bin/lu.C.x"
  [npb_sp]="${SCRIPT_DIR}/npb_bin/sp.C.x" [npb_bt]="${SCRIPT_DIR}/npb_bin/bt.C.x"
  [npb_ep]="${SCRIPT_DIR}/npb_bin/ep.C.x"
  # class D (built 2026-08-07, -mcmodel=medium for IS). One iteration outlasts any
  # window -- ep 305s, mg 314s, is 187s, ft 962s, cg >20min -- so the keep-alive loop never
  # restarts inside a measurement. Class C stays for the short-run comparisons.
  [npb_ep_d]="${SCRIPT_DIR}/npb_bin/ep.D.x" [npb_mg_d]="${SCRIPT_DIR}/npb_bin/mg.D.x"
  [npb_is_d]="${SCRIPT_DIR}/npb_bin/is.D.x" [npb_cg_d]="${SCRIPT_DIR}/npb_bin/cg.D.x"
  # npb_is_d600 (mix2 2nd streamer, 2026-09-15): is.c MAX_ITERATIONS 10->600,
  # class D, same make.def (-O3 -fopenmp -mcmodel=medium); keys/buckets untouched
  # so working set and nature carry over. MEASURED stock is.D.x, 4 thr, cores
  # 16-19, turbo off: wall 175.07 s, timer-0 (iterations only) 63.44 s =>
  # 6.34 s/iter and 111.6 s OUTSIDE the loop (create_seq+alloc+warm rank(1)
  # before, full_verify after). Looped stock is.D is therefore ~64% setup -- the
  # same defect as looped `stream`. 600 iters = ~3800 s of steady ranking.
  [npb_is_d600]="${SCRIPT_DIR}/npb_bin/is.D600.x"
  [npb_ft_d]="${SCRIPT_DIR}/npb_bin/ft.D.x"
  # RUNTIME-SIZED class D (2026-08-07). CG class D is na=1500000, nonzer=21,
  # niter=100 -- na/nonzer set the WORKING SET, niter only repeats the solve. So
  # niter 100->20 gives a byte-identical working set and identical per-iteration
  # cache behaviour at 1/5 the runtime (~1300s -> ~260s), landing in the 2-5 min
  # band a full-run-per-config sweep needs. Recipe: edit CG/npbparams.h niter,
  # `make cg CLASS=D` (setparams only rewrites npbparams.h when the CLASS
  # changes, so the edit survives), copy the binary out, then restore niter=100
  # so the tree stays standard. CAVEAT: NPB's verification checks zeta against
  # the reference for niter=100, so this variant prints VERIFICATION FAILED --
  # expected, and it is why we never quote NPB-official Mop/s for it.
  [npb_cg_d20]="${SCRIPT_DIR}/npb_bin/cg.D20.x"
  # niter=40. CORRECTED 2026-09-03: this comment used to say "~990 s", derived by
  # doubling cg_d20's 495 s. That ignores the MEASURED 87.9 s T_init (cg_d20 wall
  # 495.37 s vs T_bench 407.47 s), so the true figure is 88 + 40x20.37 = ~903 s --
  # MARGINAL against a ~900 s six-phase scene. Use npb_cg_d80 (~1718 s) instead.
  # At niter=20 (495 s)
  # cg re-entered its matrix-generation phase mid-scene and corrupted the `cat`
  # measurement (ips 42e9, hit 0.03 = init, not solver). Same matrix, so nature
  # and the 4-way knee carry over from the cg_d20 sweep unchanged.
  [npb_cg_d40]="${SCRIPT_DIR}/npb_bin/cg.D40.x"
  # FT class D is nx=2048 ny=1024 nz=1024, niter=25 -- the grid sets the working
  # set, niter only repeats time steps. 25->8 gives ~962s -> ~310s, byte-identical
  # footprint. Same VERIFICATION FAILED caveat as cg_d20.
  [npb_ft_d8]="${SCRIPT_DIR}/npb_bin/ft.D8.x"
  # MG class D with nit_default raised (grid lm/lt UNCHANGED, so the working set,
  # nature and knee carry over from npb_mg_d exactly -- only the number of
  # V-cycles differs; VERIFICATION is reported UNSUCCESSFUL because the reference
  # checksum is for niter=50, same caveat as cg_d20/ft_d8).
  # WHY: bench_lib launches tenants in a `while true` loop, so a donor that
  # finishes mid-scene re-enters init and corrupts its OWN cost accounting -- the
  # -1.9% fence cost is the denominator of the 30x cost-efficiency ratio. Same
  # defect that killed cg_d20 on the victim side.
  # MEASURED at 4 threads on idle cores: mg.D150 = 600.75 s (so mg.D ~= 200 s,
  # NOT the 314 s in the older sizing note). A 6-phase certify scene plus the c4
  # controller arm keeps donors alive ~690 s, so D150 is NOT enough -- D300
  # (~1200 s solo, longer under contention) is the one to use.
  [npb_mg_d150]="${SCRIPT_DIR}/npb_bin/mg.D150.x"
  [npb_mg_d300]="${SCRIPT_DIR}/npb_bin/mg.D300.x"
  [npb_mg_d450]="${SCRIPT_DIR}/npb_bin/mg.D450.x"
  # ── DURATION-ROBUST VARIANTS (2026-09-03). The sizing table in
  # CERTIFY_DESIGN.md is NOT trustworthy: it lists npb_mg class D at 314 s, and
  # mg.D150 measured 600.75 s at 4 threads = 4.005 s/iter => plain mg.D ~200 s,
  # i.e. the row over-reports by 1.57x. All five class-D rows were taken in one
  # pass under the same stated conditions, so nothing marks mg's row as the
  # uniquely wrong one -- npb_ft 962 s and npb_cg >1200 s are equally unverified.
  # RESOLUTION: do not size a tenant off that table. Inflate niter by a known
  # MULTIPLE of a variant we already use, which is robust to the unknown base:
  # ft.D75 = 3x ft.D (niter 25->75), cg.D80 = 2x cg.D40 (niter 40->80). Grid
  # dimensions are untouched in both, so working set, nature and knee are
  # identical to the originals and only the number of time steps differs.
  [npb_ft_d75]="${SCRIPT_DIR}/npb_bin/ft.D75.x"
  [npb_cg_d80]="${SCRIPT_DIR}/npb_bin/cg.D80.x"
  [npb_cg_d120]="${SCRIPT_DIR}/npb_bin/cg.D120.x"
  # EP class E (m=40). MEASURED 2026-09-04: ep.C.x is 41.1 s per invocation, and a
  # certify phase window is ~78 s -- so the NEGATIVE CONTROL restarted 2-4 times
  # INSIDE every phase. Its occupancy churned within a single phase (D3-v4 `eq`:
  # 3.2/1.2/2.9/1.8/0.9 MB; `cat_cap`: 2.4/2.0/1.4/0.6/0.1) because each spike is
  # a fresh process touching its arrays, not steady demand. That is the source of
  # ep's baseline CoV 0.177 and of the recurring "+12.9% degradation" artefact --
  # a tenant reading FASTER under contention. EP scales 2^m, so C(32)->D(36) is
  # 16x and D->E(40) another 16x: class E is ~10500 s and never restarts inside a
  # ~1380 s scene. EP holds ~0 MB and moves ~0 GB/s in every class, so the
  # negative-control property is unchanged -- only its own numbers get quiet.
  [npb_ep_e]="${SCRIPT_DIR}/npb_bin/ep.E.x"
  [gups]="${SCRIPT_DIR}/bench_bin/gups" [lbm]="${SCRIPT_DIR}/bench_bin/lbm"
  [xsbench]="/home/tejendra/litmus/bench_extra/XSBench/openmp-threading/XSBench"
  [facesim]="/home/tejendra/litmus/parsec/pkgs/apps/facesim/inst/amd64-linux.gcc/bin/facesim"
  # facesim_l (mix2, 2026-09-15): native input, -lastframe 800 instead of 100.
  # Measured affine: 3 frames 19.13 s, 8 frames 41.10 s => 4.39 s/frame + 5.9 s
  # setup (4 thr, cores 2-5, turbo off) => ~3520 s per invocation. Input carries
  # 2014 control frames, so frame 800 is real motion data, not a wrap.
  [facesim_l]="/home/tejendra/litmus/parsec/pkgs/apps/facesim/inst/amd64-linux.gcc/bin/facesim"
  [water_nsquared]="/home/tejendra/litmus/bench_extra/parsec_run/water_nsquared/water_nsquared"
  # water_spatial REMOVED 2026-08-07: it will not scale to a usable duration.
  # NSTEP 3->10 went 1.1s -> >300s and NMOL 8000->27000 went 1.1s -> >400s without
  # completing 3 steps -- a non-linear blowup (the spatial box grid degenerates at
  # this cutoff/density), so there is no setting that is both long and sane.
  # water_nsquared scales cleanly and linearly (3.3s/step) and is kept.
  [ffmpeg]="/usr/bin/ffmpeg"
  [llama]="/home/tejendra/litmus/bench_extra/llama.cpp/build/bin/llama-cli"
  [clickhouse]="/home/tejendra/litmus/bench_extra/clickhouse/clickhouse"
  [redis]="/home/tejendra/litmus/bench_extra/redis/src/redis-server"
  [memcached]="/home/tejendra/litmus/bench_extra/memcached/memcached" )
LLAMA_MODEL="/home/tejendra/litmus/bench_extra/llama.cpp/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
REDIS_BENCH="/home/tejendra/litmus/bench_extra/redis/src/redis-benchmark"
MEMTIER="/home/tejendra/litmus/bench_extra/memtier_benchmark/memtier_benchmark"
LOADCORES="${LOADCORES:-24-27}"   # load generators pinned OFF the characterized server's cores
declare -A SCALE=([cc]=20 [bc]=20 [pr]=22 [bfs]=22 [sssp]=22 [tc]=20 [cc_sv]=20)
# sphinx: was 20 until 2026-09-15 -- 10x past its knee. exp14 probe (2 threads,
# /tmp/exp14_probe3.log) found ~2.0 qps max sustainable (p99 ~1.1 s, requests
# are ~1 s each) and calibrated 1.5. At 20 the queue grows forever: mix2's iso
# read one latency window at p99 ~668 s after ~11 min, and run_certify's 300 s
# readiness timeout killed every sphinx scene. So the Phase-A sphinx row
# (ips_lift 1.49) and any sphinx number taken at 20 describe an OVERLOADED,
# CPU-saturated instance, not an LC service. sphinx_q2 / sphinx_q3 are the same
# binary at 2.0 / 3.0 qps for the mix2 load ladder (3.0 was never probed).
declare -A QPS=([masstree]=1000 [xapian]=500 [sphinx]=1.5 [sphinx_q2]=2.0 [sphinx_q3]=3.0 [imgdnn]=500 [silo]=1500)
NODE="${NODE:-0}"
LOAD_PIDS=()
LAUNCH_PID=""

# Known INITIALIZATION time per workload, seconds (MEASURED, see CERTIFY_DESIGN
# "THE SIZING LAW"). warm_up() gates on mbm-CoV stability, which CANNOT tell a
# steady init phase from a steady steady-state: D3-v8 declared npb_cg_d80
# "steady after 22s (mbm CoV=0.0156)" while it was still inside makea, and the
# solo phase then measured it at 0.9 MB occupancy instead of 29 MB -- destroying
# the counterfactual every victim is graded against. So a tenant with a known
# init must be held past it regardless of what its bandwidth looks like.
init_secs(){ case "$1" in
  npb_cg_d20|npb_cg_d40|npb_cg_d80|npb_cg_d120|npb_cg_d) echo 88 ;;   # CG class D makea
  npb_is_d600)                               echo 112 ;;  # IS class D create_seq+alloc+warm rank (upper bound, incl. verify)
  npb_mg_d|npb_mg_d150|npb_mg_d300|npb_mg_d450) echo 15 ;;   # MG setup+zran3
  llama)                                     echo 90 ;;   # model load + prompt eval
  canneal)                                   echo 5  ;;   # netlist load
  *)                                         echo 0  ;;
esac; }

default_ncores(){ case "$1" in masstree|xapian|sphinx|sphinx_q2|sphinx_q3|imgdnn|silo) echo 2;; pol0|pol1) echo 1;; *) echo 4;; esac; }

# launch_tenant <name> <cores> <grp> <outdir> <ncores>
launch_tenant(){
  local W="$1" CORES="$2" GRP="$3" OUT="$4" NCORES="$5" WPID="" LOADPID=""
  local d="$OUT/run"; mkdir -p "$d"
  local qps="${QPS[$W]:-200}"
  local warm=$(( ${qps%.*}*10 )); (( warm < 2000 )) && warm=2000
  # sphinx requests take ~1 s, so 2000 warmup requests = ~22 min at 1.5 qps;
  # use exp14's small fixed count. (QPS may be fractional -- bash $(( )) cannot
  # multiply 1.5, hence the %.* truncation above for the integer-QPS services.)
  case "$W" in sphinx|sphinx_q2|sphinx_q3) warm=4 ;; esac
  # TailBench servers ran under `chrt -r 99` until 2026-09-15. MEASURED
  # (diag_rt_perf_stall.sh round 2, /tmp/diag_rt_perf2): perf -I 250 counting the
  # cores of an RR-99 sphinx wrote 68/180 intervals (gaps to 2.74 s), and a
  # second perf on IDLE cores stalled in lockstep (79/180 both); non-RT sphinx on
  # the same cores 168/180 on time. That stall zeroed 229/356 probes of the mix2r
  # SpiderSense sweep. So LC servers are now plain CFS tenants, like every batch
  # tenant (and like a cloud VM). LC_RT=1 restores RR-99 for old reproductions only
  # -- never combine it with 250 ms perf sampling.
  local rtp=""; (( ${LC_RT:-0} )) && rtp="chrt -r 99"
  case "$W" in
   masstree) ( cd "$d" && exec env TBENCH_QPS=${QPS[$W]} TBENCH_MAXREQS=100000000 TBENCH_WARMUPREQS=$warm \
        TBENCH_MINSLEEPNS=10000 TBENCH_LIVE_LATS=$d/live.txt numactl --membind=$NODE taskset -c "$CORES" $rtp \
        "${BIN[$W]}" -j1 mycsba masstree > run.log 2>&1 ) & WPID=$! ;;
   silo) ( cd "$d" && exec env TBENCH_QPS=${QPS[$W]} TBENCH_MAXREQS=100000000 TBENCH_WARMUPREQS=$warm \
        TBENCH_MINSLEEPNS=10000 TBENCH_LIVE_LATS=$d/live.txt numactl --membind=$NODE taskset -c "$CORES" $rtp \
        "${BIN[$W]}" --bench tpcc --num-threads 1 --scale-factor 1 --retry-aborted-transactions \
        --ops-per-worker 10000000 > run.log 2>&1 ) & WPID=$! ;;
   xapian) ( cd "$d" && exec env TBENCH_QPS=${QPS[$W]} TBENCH_MAXREQS=100000000 TBENCH_WARMUPREQS=$warm \
        TBENCH_MINSLEEPNS=10000 TBENCH_LIVE_LATS=$d/live.txt LD_LIBRARY_PATH="${TAILBENCH_DIR}/xapian/xapian-core-1.2.13/install/lib" \
        TBENCH_TERMS_FILE="$DR/xapian/terms.in" numactl --membind=$NODE taskset -c "$CORES" $rtp \
        "${BIN[$W]}" -n 1 -d "$DR/xapian/wiki" -r 1000000000 > run.log 2>&1 ) & WPID=$! ;;
   imgdnn) ( cd "$d" && exec env TBENCH_QPS=${QPS[$W]} TBENCH_MAXREQS=100000000 TBENCH_WARMUPREQS=$warm \
        TBENCH_MINSLEEPNS=10000 TBENCH_LIVE_LATS=$d/live.txt TBENCH_MNIST_DIR="$DR/img-dnn/mnist" numactl --membind=$NODE \
        taskset -c "$CORES" $rtp "${BIN[$W]}" -r 1 -f "$DR/img-dnn/models/model.xml" -n 100000000 > run.log 2>&1 ) & WPID=$! ;;
   sphinx|sphinx_q2|sphinx_q3) ( cd "$d" && exec env TBENCH_QPS=${QPS[$W]} TBENCH_MAXREQS=100000000 TBENCH_WARMUPREQS=$warm \
        TBENCH_MINSLEEPNS=10000 TBENCH_LIVE_LATS=$d/live.txt LD_LIBRARY_PATH="${TAILBENCH_DIR}/sphinx/sphinx-install/lib" \
        TBENCH_AN4_CORPUS="$DR/sphinx" TBENCH_AUDIO_SAMPLES="${TAILBENCH_DIR}/sphinx/audio_samples" \
        numactl --membind=$NODE taskset -c "$CORES" $rtp "${BIN[$W]}" -t 2 > run.log 2>&1 ) & WPID=$! ;;
   cc|bc|pr|bfs|sssp|tc|cc_sv) ( exec env OMP_NUM_THREADS=$NCORES numactl --membind=$NODE taskset -c "$CORES" \
        "${BIN[$W]}" -g "${SCALE[$W]}" -n 100000 > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   blackscholes) ( cd "$d" && exec env OMP_NUM_THREADS=$NCORES numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do ${BIN[$W]} $NCORES '${PARSEC}/apps/blackscholes/run/${BS_INPUT:-in_10M.txt}' out.txt >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   streamcluster) ( cd "$d" && exec env OMP_NUM_THREADS=$NCORES numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do ${BIN[$W]} 10 20 128 16384 16384 1000 none out.txt $NCORES >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   canneal) ( cd "$d" && ln -sfn "$CANNEAL_IN"/* . 2>/dev/null; exec env OMP_NUM_THREADS=$NCORES numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do ${BIN[$W]} $NCORES 15000 2000 400000.nets ${CANNEAL_STEPS:-40000} >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   stream|stream_l) ( exec env OMP_NUM_THREADS=$NCORES numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" >/dev/null 2>&1; done" ) & WPID=$! ;;
   npb_ft|npb_is|npb_cg|npb_mg|npb_lu|npb_sp|npb_bt|npb_ep|npb_ep_d|npb_mg_d|npb_is_d|npb_cg_d|npb_ft_d|npb_cg_d20|npb_cg_d40|npb_ft_d8|npb_mg_d150|npb_mg_d300|npb_mg_d450|npb_ft_d75|npb_cg_d80|npb_cg_d120|npb_ep_e|npb_is_d600) ( cd "$d" && exec env OMP_NUM_THREADS=$NCORES numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   gups|lbm) ( exec env OMP_NUM_THREADS=$NCORES GUPS_LOGSZ=${GUPS_LOGSZ:-28} numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; exec \"${BIN[$W]}\"" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   pol0|pol1) ( exec stdbuf -oL numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; exec ${BIN[$W]} -b -l26 -m27 -x1 -e500 -d1" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   xsbench) ( exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" -t $NCORES -s small -l 50000000 >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   facesim) ( cd /home/tejendra/litmus/parsec/pkgs/apps/facesim/run && exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" -timing -threads $NCORES -lastframe ${FACESIM_FRAMES:-100} >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   facesim_l) ( cd /home/tejendra/litmus/parsec/pkgs/apps/facesim/run && exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" -timing -threads $NCORES -lastframe 800 >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   water_nsquared) ( cd "$(dirname "${BIN[$W]}")" && exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" $NCORES < input_$NCORES >/dev/null 2>&1 || \"${BIN[$W]}\" $NCORES < input_4 >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   ffmpeg) ( cd "$d" && exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" -y -f lavfi -i testsrc2=size=1920x1080:rate=30:duration=60 -c:v libx264 -preset slow -threads $NCORES -f null - >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   llama) ( exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" -m '$LLAMA_MODEL' -p 'The cloud is' -n 100000 -t $NCORES -no-cnv --ignore-eos --simple-io < /dev/null >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   clickhouse) ( exec numactl --membind=$NODE taskset -c "$CORES" \
        bash -c "echo \$BASHPID > '$GRP/tasks' 2>/dev/null; while true; do \"${BIN[$W]}\" local --query \"SELECT number % 20000000 AS k, count() FROM numbers(2000000000) GROUP BY k FORMAT Null\" --max_threads $NCORES >/dev/null 2>&1; done" > "$OUT/${W}.log" 2>&1 ) & WPID=$! ;;
   redis) ( exec numactl --membind=$NODE taskset -c "$CORES" \
        "${BIN[$W]}" --port 6399 --save '' --appendonly no --protected-mode no > "$OUT/${W}.log" 2>&1 ) & WPID=$!
        sleep 3
        taskset -c "$LOADCORES" "$MEMTIER" -s 127.0.0.1 -p 6399 --key-pattern=S:S --key-minimum=1 --key-maximum=500000 --data-size=4096 --ratio=1:0 --requests=500000 -t 1 -c 1 >/dev/null 2>&1
        ( taskset -c "$LOADCORES" "$MEMTIER" -s 127.0.0.1 -p 6399 --key-pattern=R:R --key-minimum=1 --key-maximum=500000 --data-size=4096 --ratio=1:20 --test-time=100000 -t 4 -c 20 >/dev/null 2>&1 ) & LOADPID=$! ;;
   memcached) ( exec numactl --membind=$NODE taskset -c "$CORES" \
        "${BIN[$W]}" -u root -p 11311 -m 8192 -t $NCORES > "$OUT/${W}.log" 2>&1 ) & WPID=$!
        sleep 3
        taskset -c "$LOADCORES" "$MEMTIER" -s 127.0.0.1 -p 11311 -P memcache_binary --key-pattern=S:S --key-minimum=1 --key-maximum=500000 --data-size=4096 --ratio=1:0 --requests=500000 -t 1 -c 1 >/dev/null 2>&1
        ( taskset -c "$LOADCORES" "$MEMTIER" -s 127.0.0.1 -p 11311 -P memcache_binary --key-pattern=R:R --key-minimum=1 --key-maximum=500000 --data-size=4096 --ratio=1:20 --test-time=100000 -t 4 -c 20 >/dev/null 2>&1 ) & LOADPID=$! ;;
   *) echo "ERROR: unknown workload $W" >&2; return 1 ;;
  esac
  LAUNCH_PID="$WPID"
  [[ -n "$LOADPID" ]] && LOAD_PIDS+=("$LOADPID")
  return 0
}

tenant_ready(){ local W="$1" OUT="$2" pid="$3"
  case "$W" in
    masstree|xapian|sphinx|sphinx_q2|sphinx_q3|imgdnn|silo) [[ -s "$OUT/run/live.txt" ]] ;;
    cc|bc|pr|bfs|sssp|tc|cc_sv) grep -q "Trial Time" "$OUT/${W}.log" 2>/dev/null ;;
    *) [[ -d "/proc/$pid" ]] ;;
  esac; }

assign_tenant(){ local pid="$1" grp="$2" t tid
  for t in /proc/"$pid"/task/*; do [[ -d "$t" ]] || continue; tid=$(basename "$t")
    echo "$tid" > "$grp/tasks" 2>/dev/null || true; done; }

has_lat_feed(){ case "$1" in masstree|xapian|sphinx|sphinx_q2|sphinx_q3|imgdnn|silo) return 0;; *) return 1;; esac; }

# Turbo must be OFF for any scene that will be compared against a `solo` arm.
# MEASURED 2026-08-10 on results_certify/npb_cg_d40+canneal+npb_ep: effective
# clock was 3.32 GHz/core in `solo` (4 cores busy) vs 3.03 in noisy/cat/mba
# (16 cores busy) -- an 8.5% swing that lands squarely on the solo-referenced
# harm figure, and inflated canneal's degradation from a true -8.4% (IPC) to a
# reported -17.5% (IPS). The managed arms agreed to within 0.25%, so the
# confound is specific to the cross-load-level comparison we cannot avoid.
# Peer practice in our comparator set is unanimous: SpiderSense, CoPart and
# PaLLOC disable turbo, dCat pins the governor to `performance`. Only PARTIES
# ("we enable hyperthreading and Turbo boosting") and CLITE leave it alone, and
# both were dropped as comparators on 2026-07-14.
# NOTE: no_turbo does NOT survive a reboot, which is exactly why this is a hard
# gate and not a comment. Set with: echo 1 | sudo tee <path>
require_turbo_off(){
  local nt=/sys/devices/system/cpu/intel_pstate/no_turbo
  if [[ ! -r "$nt" ]]; then
    echo "WARN: $nt unreadable -- cannot verify turbo state" >&2; return 0; fi
  if [[ "$(cat "$nt")" != "1" ]]; then
    echo "FATAL: turbo is ON. Any solo-referenced number from this run is invalid." >&2
    echo "       fix: echo 1 | sudo tee $nt" >&2
    exit 1
  fi; }

# ── shared by run_certify.sh and run_iso.sh. Callers must define GRPOF,
# WARMUP_MIN/MAX/COV and a log() function.
grp_mbm(){ cat "${GRPOF[$1]}"/mon_data/mon_L3_*/mbm_total_bytes 2>/dev/null | awk '{s+=$1} END{printf "%.0f",s+0}'; }

# wait until a tenant's bandwidth rate stops drifting = past init, in steady state
warm_up(){ local n="$1"
  local t0=$SECONDS prev cur rates=() cov
  # hold past a KNOWN init before even looking at stability (see init_secs)
  local floor_s; floor_s=$(init_secs "$n")
  floor_s=$(( floor_s * 3 / 2 ))          # 1.5x: init is slower under contention
  (( floor_s > WARMUP_MIN )) || floor_s=$WARMUP_MIN
  prev=$(grp_mbm "$n" || echo 0)
  while (( SECONDS - t0 < WARMUP_MAX )); do
    sleep 2; cur=$(grp_mbm "$n" || echo 0); rates+=( $(( (cur - prev) / 2 )) ); prev=$cur
    (( ${#rates[@]} > 8 )) && rates=( "${rates[@]: -8}" )
    (( SECONDS - t0 < floor_s )) && continue
    (( ${#rates[@]} >= 8 )) || continue
    # A tenant with essentially NO memory traffic has no mbm signal to
    # stabilize: the CoV test divides noise by ~zero, never converges, burns the
    # whole WARMUP_MAX and then prints a misleading "never reached steady state".
    # npb_ep_e is the case -- 0.003 LLC-loads/KI vs canneal's 2.94, because its
    # working set fits in the 2 MiB L2 and this box's L3 is non-inclusive, so
    # nothing ever reaches the LLC. No LLC traffic also means no cache-level init
    # to wait out, so the init floor already passed above is sufficient.
    if printf '%s\n' "${rates[@]}" | awk '{s+=$1;n++} END{exit !(n && s/n < 1e8)}'; then
      log "   $n steady after $((SECONDS-t0))s (no memory traffic, <0.1 GB/s; init floor ${floor_s}s)"
      return 0; fi
    cov=$(printf '%s\n' "${rates[@]}" | awk '{s+=$1;q+=$1*$1;n++} END{if(n&&s>0){m=s/n;print (sqrt(q/n-m*m)/m)}else print 9}')
    if awk -v c="$cov" -v g="$WARMUP_COV" 'BEGIN{exit !(c<g)}'; then
      log "   $n steady after $((SECONDS-t0))s (mbm CoV=$cov, init floor ${floor_s}s)"; return 0; fi
  done
  log "   !! $n never reached steady state in ${WARMUP_MAX}s -- baseline may be phase-contaminated"; }
