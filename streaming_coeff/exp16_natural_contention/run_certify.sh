#!/usr/bin/env bash
# run_certify.sh -- multi-tenant scene: does the free lunch exist, can we take it,
# and who pays? 3-5 tenants (the field's centre of mass), four phases, every
# tenant measured in every phase.
#
# WHY THE SCENE NEEDS >=2 CONDITIONS (measured 2026-08-08, cg_d20 vs mg_d).
# The first attempt failed for a precise reason worth keeping written down:
#   noisy: mg_d held 34.1 MB at hit 0.015   cg_d20 held 20.8 MB at hit 0.937
#   cat:   mg_d  5.0 MB                     cg_d20 45.7 MB, hit STILL 0.937
# The fence worked perfectly -- it moved 29 MB off the streamer for a cost of
# 1.5% -- and the victim gained NOTHING, because cg_d20's knee is 6 ways (24 MB)
# and unmanaged sharing had already left it 20.8 MB. It was sitting at its knee
# before we did anything. So a fence scene needs BOTH:
#   (1) the streamer holds reclaimable cache   -- mg_d: 34 MB at hit 0.015, yes
#   (2) the victims' unmanaged share falls BELOW their knee -- cg_d20: no
# COEFFD4_DESIGN states (1) only ("free-lunch magnitude = the streamer's
# reclaimable footprint"); (2) is what actually bounds recoverable harm.
# Condition (2) is why this script takes MULTIPLE victims: aggregate victim
# demand is what pushes each one under its own knee.
#
# WARMUP. The first run also measured cg_d20's matrix-GENERATION phase as its
# solo baseline (hit 0.17, ips 45e9 for 40 s, solver only from t=56 s), making
# "degradation" a comparison of init against steady state. Every tenant is now
# warmed to a stable mbm rate before the solo phase is taken.
#
# Phases: solo (all victims, no polluters) -> noisy -> cat (polluters fenced to
# POLWAYS, victims own the rest) -> mba (polluters throttled). Aggressor cost is
# reported in every phase -- the accounting whose absence forced the exp16
# DNH-oracle retraction.
#
# Usage: sudo [POL="npb_mg_d stream"] [POLNC=4] [NC=4] ./run_certify.sh <victim> [victim2 ...]
#   e.g. sudo POL="npb_mg_d stream" POLNC=4 ./run_certify.sh npb_cg_d20 canneal
#   NOTE: env goes AFTER sudo -- sudo resets the environment.
#   then: python3 certify.py results_certify/<name>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/bench_lib.sh"
require_turbo_off
RESCTRL=/sys/fs/resctrl; NODE=0
WIN=${WIN:-45}; SETTLE=${SETTLE:-15}
C4WIN=${C4WIN:-400}      # controller arm: how long coeffd4 is allowed to RUN and
                        # ACTUATE before we stop it and grade. It must cover the
                        # daemon's whole convergence, not just part of it:
                        #   15 s  perf warmup before the first decide cycle
                        # + 30 s  Change-1 baseline (BASELINE_CYCLES x DECIDE_SECS;
                        #         up to 120 s if a tenant is slow to settle)
                        # + 20 s  PER VERIFIED STEP (apply, then verify next cycle)
                        # + 60 s  a settled tail for CONTROLLER_TAIL=6 scans
                        # D3-v7 took 10 steps = 200 s and was STILL climbing when
                        # the 240 s window closed -- canneal rose monotonically
                        # 1.022 -> 1.123 IPC and never plateaued, and the ELIMINATE
                        # step was applied but never verified. 400 s covers ~15
                        # steps; raise it further if a scene has more donors.
RUNC4=${RUNC4:-1}       # 0 = static arms only
POL="${POL:-npb_mg_d}"         # squeeze-free donors (measured: mg_d -1.5% to fence)
POLMBA="${POLMBA:-10}"
POLWAYS="${POLWAYS:-2}"
POLNC="${POLNC:-4}"            # cores per polluter (OMP streamers need >1)
WARMUP_MIN="${WARMUP_MIN:-20}"   # never trust the first N seconds
WARMUP_MAX="${WARMUP_MAX:-300}"  # give up waiting for steady state after this
WARMUP_COV="${WARMUP_COV:-0.12}" # mbm CoV over the sliding window => steady
# ── canneal MUST outlast the whole scene, not just a phase (2026-09-04).
# canneal loops at ~215-250 s per invocation under contention, while solo->dsolo
# spans ~23 min, so a scene contains 3-4 invocation boundaries. Measured: canneal
# comes back from a restart with materially DIFFERENT memory behaviour --
# LLC-loads/KI 2.90 -> 3.45 (+19%), constant for the whole process lifetime --
# and in D3-v3 that flip landed mid-`cat`, so cat_cap and mba were divided by a
# solo/noisy baseline from a different process instance. That alone produced the
# 58pp CAPrec and 48pp MBArec swings (the recovery denominator is only 0.148 IPC,
# so ~1% of canneal's IPC is ~8pp of recovery).
# Sizing is AFFINE: ~3.8 s setup + 3.6 ms/step, and ~1.6x slower under contention.
# 300000 steps ~= 1080 s solo ~= 1700 s contended > the ~1380 s scene.
export CANNEAL_STEPS="${CANNEAL_STEPS:-300000}"
# ── cat_cap arm: fence the donor AND cap the ABSORBER at its pre-fence share.
# This is step 4 of the nested-elimination loop executed STATICALLY, so the
# elimination claim can be tested before any controller code exists. Measured
# motivation (npb_cg_d40+canneal+npb_ep): the fence released 20.7 MB and
# npb_cg_d40 took 10.3 MB of it -- MORE than canneal's 9.4 -- while converting
# 0.026 %/MB against canneal's 0.84, i.e. 32x worse. Roughly half the free lunch
# went to a tenant that could not use it. cat_cap denies it the WINDFALL ONLY:
# the cap equals its own `noisy` occupancy, never less. NOTE 2026-09-03: that
# guarantee holds in MB and NOT in IPC -- D3-v2 honoured the cap (14.9 -> 15.2 MB)
# and still left the absorber 7.2% below unmanaged, because a 4-way mask is
# 4-way ASSOCIATIVE regardless of capacity. See CERTIFY_DESIGN.md "FALSIFIED".
# Isolated-baseline map for the arms that need one (CoPart, SATORI). Defaults
# to the roster's iso run; harmless when absent for CoPart, which then falls
# back to its own in-band profiling as the paper does -- but SATORI cannot
# start without it (it needs isolated IPC for EVERY tenant, which is why it is
# labelled "profiled" in the comparison).
#
# AS-vs-MIX TRAP (cost us the 2026-09-12 D3_satori run, which died at t=0 and
# burned the whole window): the one-controller-per-run convention names runs
# AS=D3_satori / AS=D3_palloc, but the iso profiles live under the MIX name,
# results_iso/D3/. The bare ${AS} default therefore pointed at a directory that
# does not exist and cfg.json silently omitted solo_ipc. Fall back to the AS
# name with a trailing _<controller> suffix stripped, so the per-controller run
# names resolve to the mix's profiles. An explicit SOLO_IPC= always wins.
SOLO_IPC="${SOLO_IPC:-${SCRIPT_DIR}/results_iso/${AS:-none}/solo_ipc.json}"
if [[ ! -f "$SOLO_IPC" && -n "${AS:-}" ]]; then
  _as_mix="${AS%_*}"           # D3_satori -> D3 ; D3 -> D3 (no separator)
  if [[ "$_as_mix" != "$AS" && -f "${SCRIPT_DIR}/results_iso/${_as_mix}/solo_ipc.json" ]]; then
    SOLO_IPC="${SCRIPT_DIR}/results_iso/${_as_mix}/solo_ipc.json"
    echo "note: solo_ipc for AS=$AS resolved via mix name '$_as_mix' -> $SOLO_IPC" >&2
  fi
fi
CAP="${CAP:-}"                   # tenant to cap; empty => the cat_cap arm is skipped
WAY_MB="${WAY_MB:-4.0}"          # 60 MB L3 / 15 ways on this box; override per machine
CAPWAYS=""                       # derived from the `noisy` phase, never hand-set

(( $# >= 1 )) || { echo "usage: sudo $0 <victim> [victim2 ...]"; exit 1; }
VICS=("$@")
OUT="${SCRIPT_DIR}/results_certify/${AS:-$(IFS=+; echo "${VICS[*]}")}"

log(){ echo "[$(date '+%H:%M:%S')] $*"; }; die(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo"
for v in "${VICS[@]}"; do [[ -x "${BIN[$v]:-}" ]] || die "missing binary for victim $v"; done
for p in $POL; do [[ -x "${BIN[$p]:-}" ]] || die "missing polluter binary for $p"; done
# PREFLIGHT: a stale tenant from a previous scene silently contaminates this one
# and is invisible in the results (D3v8 ran with two llamas). Refuse to start.
# Match on the RESOLVED EXECUTABLE (/proc/PID/exe), never on cmdline text: a
# `pgrep -f <name>` also matches the shell running the check, so the guard
# fires on itself and no run can ever start.
declare -A _tenant_exe=()
for _b in "${BIN[@]}"; do
  [[ -n "$_b" ]] || continue
  _tenant_exe["$(readlink -f "$_b" 2>/dev/null || echo "$_b")"]=1
done
_stale=()
for _d in /proc/[0-9]*; do
  _p=${_d#/proc/}
  [[ "$_p" == "$$" ]] && continue
  _e=$(readlink -f "$_d/exe" 2>/dev/null) || continue
  [[ -n "$_e" ]] || continue
  if [[ -n "${_tenant_exe[$_e]:-}" ]]; then
    _stale+=("$_p  $_e")
  elif [[ "${_e##*/}" == python* ]]; then
    # `|| true` is REQUIRED: under `set -e` a bare x=$(cmd) aborts the script when
    # cmd fails, and 2>/dev/null does not help because the failure is the shell's
    # own redirection setup. /proc/<pid>/cmdline disappears the instant a process
    # exits, and this scans ~1500 entries six times over the ladder.
    _c=$(tr '\0' ' ' < "$_d/cmdline" 2>/dev/null) || true
    [[ "$_c" == *coeffd3.py* || "$_c" == *coeffd4.py* || "$_c" == *coeffd4.1.py* ]] && _stale+=("$_p  ${_c:0:70}")
  fi
done
if (( ${#_stale[@]} > 0 )); then
  echo "ERROR: ${#_stale[@]} tenant process(es) already running -- a previous scene did"
  echo "       not clean up. This would contaminate the run. Offending processes:"
  printf '         %s\n' "${_stale[@]}"
  echo "       Clear them, then re-run:  sudo kill -9 ${_stale[*]%% *}"
  exit 1
fi

mkdir -p "$OUT"

FULL_HEX=$(cat "$RESCTRL/info/L3/cbm_mask")
NBITS=$(python3 -c "print(int('$FULL_HEX',16).bit_length())")
(( NBITS > POLWAYS )) || die "POLWAYS=$POLWAYS leaves nothing for the victims"

declare -A GRPOF=() CORESOF=() NCOF=() PIDOF=()
ISPOL(){ local n="$1" p; for p in $POL; do [[ "$p" == "$n" ]] && return 0; done; return 1; }

# core plan: victims from core 2 upward, then polluters
NEXT=2
for v in "${VICS[@]}"; do
  nc=${NC:-$(default_ncores "$v")}
  NCOF[$v]=$nc; CORESOF[$v]="$NEXT-$((NEXT+nc-1))"; GRPOF[$v]="$RESCTRL/cert_${v}"
  NEXT=$((NEXT+nc))
done
for p in $POL; do
  NCOF[$p]=$POLNC; CORESOF[$p]="$NEXT-$((NEXT+POLNC-1))"; GRPOF[$p]="$RESCTRL/cert_${p}"
  NEXT=$((NEXT+POLNC))
done
(( NEXT <= 32 )) || die "scene needs $NEXT cores, node 0 has 32"
log "scene: $((${#VICS[@]}+$(echo $POL|wc -w))) tenants, $((NEXT-2)) cores"
for n in "${!CORESOF[@]}"; do log "   $n cores=${CORESOF[$n]} $(ISPOL "$n" && echo '(donor)' || echo '(victim)')"; done

CPID=""
# KILL THE WHOLE TREE, NOT THE LOOP SHELL. Tenants are launched as
#   ( ... bash -c "while true; do BIN; done" ) & WPID=$!
# so $WPID is the loop SHELL and the workload is its CHILD. `kill -9 $WPID`
# reaps the shell and ORPHANS the running binary, which is reparented to init
# and keeps burning the tenant cores. Measured 2026-09-04: D3-v7's llama-cli
# survived its own cleanup and was still running 58 minutes later, THROUGH the
# whole of D3-v8 -- so that scene ran with two llamas on cores 18-21, llama's
# IPC read 0.296 instead of 2.379, everything ran slow, and npb_cg_d80's init
# was pushed past the solo window. The corrupted baseline we diagnosed as a
# warmup problem had this underneath it.
kill_tree(){ local p="$1" c
    for c in $(pgrep -P "$p" 2>/dev/null); do kill_tree "$c"; done
    kill -9 "$p" 2>/dev/null || true; }
hand_back(){ # results are written as root; the analysis scripts run as the user
    [[ -n "${SUDO_UID:-}" && -n "${OUT:-}" && -d "${OUT:-}" ]] || return 0
    chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT" 2>/dev/null || true; }
cleanup(){ [[ -n "$CPID" ]] && kill_tree "$CPID" || true
    for n in "${!PIDOF[@]}"; do kill_tree "${PIDOF[$n]}" || true; done
    for lp in ${LOAD_PIDS[@]:-}; do kill -9 "$lp" 2>/dev/null || true; done
    pkill -9 -f "coeffd3.py" 2>/dev/null || true; pkill -9 -f "perf stat" 2>/dev/null || true
    pkill -9 -f "redis-benchmark|memtier_benchmark" 2>/dev/null || true
    hand_back
    for n in "${!GRPOF[@]}"; do [[ -d "${GRPOF[$n]}" ]] && rmdir "${GRPOF[$n]}" 2>/dev/null || true; done
    printf 'L3:0=%s;1=%s\n' "$FULL_HEX" "$FULL_HEX" > "$RESCTRL/schemata" 2>/dev/null || true; }
trap cleanup EXIT INT TERM HUP

POL_FENCE_MASK=$(( (1<<POLWAYS)-1 ))
VIC_FENCE_MASK=$(( ((1<<NBITS)-1) ^ POL_FENCE_MASK ))
set_phase(){ local phase="$1"
  local n g
  for n in "${!GRPOF[@]}"; do g="${GRPOF[$n]}"; [[ -d "$g" ]] || continue
    case "$phase" in
      solo|noisy) printf 'L3:0=%s;1=%s\nMB:0=100;1=100\n' "$FULL_HEX" "$FULL_HEX" > "$g/schemata" ;;
      cat) if ISPOL "$n"; then printf 'L3:0=%x;1=%s\nMB:0=100;1=100\n' "$POL_FENCE_MASK" "$FULL_HEX" > "$g/schemata"
           else printf 'L3:0=%x;1=%s\nMB:0=100;1=100\n' "$VIC_FENCE_MASK" "$FULL_HEX" > "$g/schemata"; fi ;;
      mba) if ISPOL "$n"; then printf 'L3:0=%s;1=%s\nMB:0=%s;1=100\n' "$FULL_HEX" "$FULL_HEX" "$POLMBA" > "$g/schemata"
           else printf 'L3:0=%s;1=%s\nMB:0=100;1=100\n' "$FULL_HEX" "$FULL_HEX" > "$g/schemata"; fi ;;
      cat_cap)
           # Same fence as `cat`, plus the absorber pinned to its pre-fence
           # share. Three disjoint contiguous regions, low to high:
           #   [0, POLWAYS)                  donor  (fenced)
           #   [POLWAYS, +CAPWAYS)           absorber (capped at its noisy occupancy)
           #   [POLWAYS+CAPWAYS, NBITS)      everyone else -- receives the windfall
           if ISPOL "$n"; then printf 'L3:0=%x;1=%s\nMB:0=100;1=100\n' "$POL_FENCE_MASK" "$FULL_HEX" > "$g/schemata"
           elif [[ "$n" == "$CAP" ]]; then printf 'L3:0=%x;1=%s\nMB:0=100;1=100\n' "$CAP_MASK" "$FULL_HEX" > "$g/schemata"
           else printf 'L3:0=%x;1=%s\nMB:0=100;1=100\n' "$REST_MASK" "$FULL_HEX" > "$g/schemata"; fi ;;
      spider_conv)
           # SpiderSense's ANSWER, replayed as a static arm. Legitimate because
           # their persistent strategy converges and HOLDS (p.11) -- at steady
           # state their controller IS a static partition. Measuring it this way
           # costs ~75 s instead of a ~430 s controller arm, and keeps the
           # graded window identical in length to every other static arm. The
           # search cost is reported separately, not folded in here.
           printf 'L3:0=%s;1=%s\nMB:0=100;1=100\n' "${SPIDER_MASK[$n]:-$FULL_HEX}" "$FULL_HEX" > "$g/schemata" ;;
      sat_conv|cm_nest|nest_dp|band_dp)
           # STATIC REPLAY of a published mask set (load_replay). sat_conv =
           # SATORI's converged answer, same treatment as spider_conv. cm_nest /
           # nest_dp / band_dp = the Cacheman overlap-vs-depth test
           # (mask_tests/README.md). load_replay already refused to run with a
           # missing tenant, so the FULL_HEX fallback is unreachable by design.
           printf 'L3:0=%s;1=%s\nMB:0=100;1=100\n' "${REPLAY_MASK[$phase|$n]:-$FULL_HEX}" "$FULL_HEX" > "$g/schemata" ;;
      eq)  # equal DISJOINT partition -- PaLLOC's baseline, CoPart's Fig.12 normalizer.
           # Not the same as `noisy` (everyone holds the full mask and the LRU decides);
           # they differ exactly when one tenant is a cache hog, i.e. in every scene we run.
           printf 'L3:0=%x;1=%s\nMB:0=100;1=100\n' "$(eq_mask_for "$n")" "$FULL_HEX" > "$g/schemata" ;;
    esac
  done; }

# Load the converged SpiderSense masks published by the search (see
# spidersense_style.py "converged.json"). Refuses to guess: a missing or
# incomplete file is a hard error, because silently falling back to full masks
# would grade `spider_conv` as if it were `noisy` and report a null result as a
# real one.
declare -A SPIDER_MASK=()
load_spider_conv(){
  # the search checkpoint itself carries the converged block, so one file is
  # both the resume state and the published answer
  local f="${SPIDER_CONV:-$SPIDER_STATE}"
  [[ -f "$f" ]] || die "spider_conv: no converged config at $f (run the search first, or set SPIDER_CONV)"
  local n m
  while IFS=$'\t' read -r n m; do
    [[ -n "$n" ]] && SPIDER_MASK[$n]="$m"
  done < <(python3 -c "
import json,sys
d=json.load(open('$f'))
masks=d.get('masks') or (d.get('converged') or {}).get('masks')
if not masks: sys.exit('no masks in $f')
for k,v in masks.items(): print(k+chr(9)+v)
")
  local t
  for t in "${ALLNAMES[@]}"; do
    [[ -n "${SPIDER_MASK[$t]:-}" ]] || die "spider_conv: no mask for tenant '$t' in $f"
  done
  log "  spider_conv masks: $(for t in "${ALLNAMES[@]}"; do printf '%s=%s ' "$t" "${SPIDER_MASK[$t]}"; done)"
}

# Generic static-replay loader: any JSON with {"masks": {tenant: hex}} (or a
# {"converged": {"masks": ...}} block). Same refuse-to-guess contract as
# load_spider_conv: a missing file, a missing tenant, or a non-contiguous /
# out-of-range CBM is a hard error -- silently falling back to the full mask
# would grade the arm as `noisy` and publish a null result as a real one.
declare -A REPLAY_MASK=()
load_replay(){ local arm="$1" f="$2"
  [[ -f "$f" ]] || die "$arm: no mask file at $f"
  local n m
  while IFS=$'\t' read -r n m; do
    [[ -n "$n" ]] && REPLAY_MASK["$arm|$n"]="$m"
  done < <(python3 -c "
import json,sys
d=json.load(open('$f'))
masks=d.get('masks') or (d.get('converged') or {}).get('masks')
if not masks: sys.exit('no masks in $f')
nb=$NBITS
for k,v in masks.items():
    x=int(str(v).lower().replace('0x',''),16)
    lo=(x & -x).bit_length()-1 if x else -1
    if not (0 < x < (1<<nb)) or ((x>>lo) & ((x>>lo)+1)):
        sys.exit('bad CBM for %s: %s (must be non-empty, contiguous, < %d ways)' % (k,v,nb))
    print(k+chr(9)+format(x,'x'))
")
  local t
  for t in "${ALLNAMES[@]}"; do
    [[ -n "${REPLAY_MASK[$arm|$t]:-}" ]] || die "$arm: no mask for tenant '$t' in $f"
  done
  log "  $arm masks ($f): $(for t in "${ALLNAMES[@]}"; do printf '%s=%s ' "$t" "${REPLAY_MASK[$arm|$t]}"; done)"
}

# contiguous disjoint slice per tenant, in declaration order
eq_mask_for(){ local want="$1" i=0 per idx
  per=$(( NBITS / ${#ALLNAMES[@]} )); (( per < 1 )) && per=1
  for n in "${ALLNAMES[@]}"; do
    if [[ "$n" == "$want" ]]; then idx=$i; break; fi; i=$((i+1)); done
  echo $(( ((1<<per)-1) << (idx*per) )); }

# Derive the absorber's cap from what it actually held under unmanaged sharing.
# Sets CAPWAYS / CAP_MASK / REST_MASK as globals -- deliberately NOT via $(),
# which would run it in a subshell and throw the assignments away.
# Rounding is UP (ceil): the absorber gets at least its pre-fence share, never
# less, so any recovery the converters show cannot be an artefact of shorting it.
plan_cap(){
  local mb
  mb=$(python3 - "$OUT/phase_noisy/decisions.jsonl" "$CAP" <<'PY'
import json,sys,statistics as st
v=[]
for line in open(sys.argv[1]):
    try: o=json.loads(line)
    except ValueError: continue
    if o.get("kind")!="scan": continue
    t=o.get("tenants",{}).get(sys.argv[2])
    if t and t.get("cmt_mb") is not None: v.append(t["cmt_mb"])
print("%.2f"%st.median(v) if v else "")
PY
)
  if [[ -z "$mb" ]]; then
    log "  !! no noisy occupancy for CAP=$CAP -- cat_cap SKIPPED"; CAP=""; return 0; fi
  CAPWAYS=$(awk -v m="$mb" -v w="$WAY_MB" 'BEGIN{n=int(m/w); if(m/w>n)n++; if(n<1)n=1; print n}')
  if (( POLWAYS + CAPWAYS >= NBITS )); then
    log "  !! CAP=$CAP needs $CAPWAYS ways + donor $POLWAYS >= $NBITS -- no room for converters, cat_cap SKIPPED"
    CAP=""; return 0; fi
  CAP_MASK=$(( ((1<<CAPWAYS)-1) << POLWAYS ))
  REST_MASK=$(( ((1<<(NBITS-POLWAYS-CAPWAYS))-1) << (POLWAYS+CAPWAYS) ))
  log "  cap plan: $CAP held ${mb} MB unmanaged -> $CAPWAYS ways (mask $(printf '0x%x' "$CAP_MASK"));"
  log "            converters get $((NBITS-POLWAYS-CAPWAYS)) ways (mask $(printf '0x%x' "$REST_MASK"))"; }

# grp_mbm() and warm_up() now live in bench_lib.sh -- run_iso.sh needs them too
# and a second copy is a second thing to forget to fix.


start_tenant(){ local n="$1"
  local g="${GRPOF[$n]}"
  [[ -d "$g" ]] || mkdir "$g"
  log "  launch $n on cores ${CORESOF[$n]}"
  launch_tenant "$n" "${CORESOF[$n]}" "$g" "$OUT/$n" "${NCOF[$n]}"
  PIDOF[$n]="$LAUNCH_PID"
  local dl=$((SECONDS+300))
  while ! tenant_ready "$n" "$OUT/$n" "${PIDOF[$n]}"; do
    (( SECONDS > dl )) && die "timeout waiting for $n"; sleep 2; done
  assign_tenant "${PIDOF[$n]}" "$g"; }

write_cfg(){ local f="$OUT/cfg.json"
  local n first=1 lat
  { printf '{"tenants": ['
    for n in "${!PIDOF[@]}"; do
      (( first )) || printf ', '; first=0
      lat=""; has_lat_feed "$n" && lat=",\"lat_file\":\"$OUT/$n/run/live.txt\""
      printf '{"name":"%s","pid":%s,"grp":"%s","priority":2,"cores":"%s"%s}' \
        "$n" "${PIDOF[$n]}" "${GRPOF[$n]}" "${CORESOF[$n]}" "$lat"
    done; printf ']'
    # copart_style.py reads cfg["solo_ipc"] -> {tenant: isolated IPC}, the
    # denominator of its Eq.1 slowdown. Other controllers ignore the key.
    [[ -n "$SOLO_IPC" && -f "$SOLO_IPC" ]] && printf ', "solo_ipc": "%s"' "$SOLO_IPC"
    printf '}'; } > "$f"; }

measure(){ local phase="$1"
  local od="$OUT/phase_${phase}"; mkdir -p "$od"
  write_cfg; set_phase "$phase"; sleep "$SETTLE"
  python3 "$COEFFD3" --config "$OUT/cfg.json" --out "$od" --observe --perf-attach cores \
      > "$od/ctrl.log" 2>&1 & CPID=$!
  sleep $((SETTLE+WIN))
  kill -TERM "$CPID" 2>/dev/null || true
  for _ in $(seq 1 8); do kill -0 "$CPID" 2>/dev/null || break; sleep 1; done
  # kill_tree + a SCOPED perf sweep: a controller killed with SIGKILL orphans its
  # own `perf stat` children, which keep sampling into the NEXT phase of the same
  # scene. cleanup()'s blanket pkill only runs at final script exit. Every
  # controller passes `perf stat ... -o <file under $od>`, so this path match hits
  # exactly this phase and nothing else.
  kill_tree "$CPID" 2>/dev/null || true; wait "$CPID" 2>/dev/null || true; CPID=""
  pkill -9 -f "perf stat.*${od}" 2>/dev/null || true
  local ns; ns=$(grep -c '"kind": *"scan"' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  log "  phase $phase: $ns scans"
  (( ns > 0 )) || log "  !! WARNING: no scans in phase $phase"; }

# ── controller arm. Unlike every phase above, the masks are NOT set by us: the
# scene starts UNMANAGED (exactly the `noisy` configuration) and coeffd4 decides
# what to do, one verified step at a time. That is the whole point -- the static
# arms show what a lever CAN buy, this shows what a controller with no oracle
# knowledge actually takes. It runs without --observe, so it actuates.
# ── CONTROLLER ARMS. Every controller gets IDENTICAL treatment, because the
# comparison is only sound if the policy is the only variable (PRIOR_ART_
# COMPARISON.md "Comparison validity"): same scene, same tenant INSTANCES, same
# levers, same window, each starting from the same unmanaged state and restoring
# it afterwards so no arm inherits its predecessor's masks.
#   $1 = arm name (also the phase dir), $2 = path to the controller script
#   $3 = perf-file prefix the controller writes (coeffd=perf, copart=cp, ...)
#   $4 = extra args (only ours take --perf-attach; the peer arms attach -C cores
#        by construction, which is the same thing, so passing it would error)
measure_ctrl(){ local arm="$1" ctl="$2" pfx="${3:-perf}" extra="${4:-}" win="${5:-$C4WIN}" envpfx="${6:-}"
  local od="$OUT/phase_${arm}"; mkdir -p "$od"
  # START FROM UNMANAGED: the controller must discover the scene, not inherit a
  # partition some earlier arm left behind. This is what makes arm ORDER
  # defensible -- without it, arm N is graded on arm N-1's leftovers.
  write_cfg; set_phase noisy; sleep "$SETTLE"
  # $envpfx is an optional "VAR=val" prefix for THIS command only (via `env`),
  # e.g. satori's PYTHONPATH for its vendored scikit-optimize -- it must not
  # leak into the shell's own environment or any other arm.
  # shellcheck disable=SC2086 -- $extra/$envpfx are intentional word-split args
  env $envpfx python3 "$ctl" --config "$OUT/cfg.json" --out "$od" $extra \
      > "$od/ctrl.log" 2>&1 & CPID=$!
  # EQUAL WALL CLOCK for every controller. The ONE exception is the spider arm
  # (see measure_spider): SpiderSense's ALGORITHM 2 is an exhaustive sweep that
  # cannot produce an answer inside C4WIN, so it is given search_time + C4WIN
  # and rotates its own counters at convergence, leaving the GRADED window at
  # exactly C4WIN of its converged configuration -- same measurement length as
  # everyone else, with the search cost reported as a separate axis.
  if [[ "${CTRL_WAIT_EXIT:-0}" == 1 ]]; then
    # a chunked search arm exits on its own when its budget is spent; sleeping
    # the full window would idle the scene for whatever time it did not need
    local _dl=$((SECONDS+win))
    while kill -0 "$CPID" 2>/dev/null && (( SECONDS < _dl )); do sleep 2; done
  else
    # POLL, do not blind-sleep. The guard below is what stops a dead arm from
    # publishing, but checking only AFTER the full window means a controller
    # that dies at t=0 still burns C4WIN before anyone is told (2026-09-12:
    # satori died instantly on a missing solo_ipc and the run sat idle). Poll
    # so the failure surfaces in seconds; sleep the remainder when healthy.
    local _dl=$((SECONDS+win))
    while (( SECONDS < _dl )); do
      kill -0 "$CPID" 2>/dev/null || break
      sleep 2
    done
  fi
  # FAIL LOUD IF THE CONTROLLER DIED EARLY. A controller that exits mid-window
  # (crash, SystemExit from its own safety net, unreadable counters) leaves the
  # scene UNMANAGED for the rest of the window, and certify would grade that
  # unmanaged stretch as the controller's result -- a false number that looks
  # like a real arm. The one arm allowed to exit on its own is the chunked
  # search (CTRL_WAIT_EXIT=1), which is finished when it exits.
  if [[ "${CTRL_WAIT_EXIT:-0}" != 1 ]] && ! kill -0 "$CPID" 2>/dev/null; then
    # `wait` on a failed child returns its rc, which under `set -e` would abort
    # this script BEFORE the message below -- capture it in the || branch.
    local _rc=0; wait "$CPID" 2>/dev/null || _rc=$?
    echo "DIED rc=$_rc arm=$arm" > "$od/CONTROLLER_DIED"
    die "controller '$arm' exited BEFORE its window ended (rc=$_rc) -- the scene
       ran unmanaged after that, so this phase is NOT a result. See $od/ctrl.log"
  fi
  kill -TERM "$CPID" 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 "$CPID" 2>/dev/null || break; sleep 1; done
  # kill_tree + a SCOPED perf sweep: a controller killed with SIGKILL orphans its
  # own `perf stat` children, which keep sampling into the NEXT phase of the same
  # scene. cleanup()'s blanket pkill only runs at final script exit. Every
  # controller passes `perf stat ... -o <file under $od>`, so this path match hits
  # exactly this phase and nothing else.
  kill_tree "$CPID" 2>/dev/null || true; wait "$CPID" 2>/dev/null || true; CPID=""
  pkill -9 -f "perf stat.*${od}" 2>/dev/null || true
  local ns na nr nb
  ns=$(grep -cE '"kind": *"(scan|hold)"' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  # Each controller names its own record kinds. coeffd: apply/rollback.
  # dcat: apply. copart: match/explore (+idle = did nothing). spidersense:
  # shift/keep/revert. Counting only coeffd's names printed "0 actuations" for
  # copart and spidersense even while they were moving masks, which would hide
  # a frozen arm from the operator during an irrepeatable run.
  na=$(grep -cE '"kind": *"(apply|match|explore|shift|probe|converged)"' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  nr=$(grep -cE '"kind": *"(rollback|revert)"' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  ni=$(grep -c '"kind": *"idle"' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  nb=$(grep -c 'sanctioned_breach' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  log "  phase $arm: $ns scans, $na actuations, $nr rollbacks/reverts, $ni idle, $nb sanctioned breaches"
  if (( ns > 20 && na == 0 )); then
    log "  !! $arm made NO actuation in $ns scans -- arm may be frozen, check before trusting it"
  fi
  # NORMALIZE the perf filenames. Each controller names its own perf output
  # (coeffd perf_X.json, copart cp_X.json, dcat dc_X.json, spidersense
  # ssperf_X.json); certify.py grades every arm from perf_<tenant>.json, so an
  # un-normalized peer arm reads as "no data" rather than as a result.
  if [[ "$pfx" != "perf" ]]; then
    local _n
    for _n in "${ALLNAMES[@]}"; do
      [[ -f "$od/${pfx}_${_n}.json" ]] && cp -f "$od/${pfx}_${_n}.json" "$od/perf_${_n}.json"
    done
  fi
  # restore before anything downstream measures: the controller leaves its own
  # masks and MBA levels in place, and phase order must not inherit them
  set_phase noisy
  sleep "$SETTLE"; }                    # let the restore settle before the next arm

measure_c4(){ measure_ctrl c4 "$COEFFD4" perf "--perf-attach cores"; }
measure_c3(){ measure_ctrl c3 "$COEFFD3" perf "--perf-attach cores"; }
# TIME-MATCHED CONTROL (CONTROLLER_COMPARISON.md 14d). coeffd3 --observe is a
# no-op: it measures and logs but never writes a mask or an MBA level. So this
# arm is the scene left UNMANAGED for the full C4WIN, graded on the same 60 s
# tail as every controller arm.
#
# Why it is needed: the `noisy` baseline is a 60 s window that ends ~400 s
# BEFORE a controller arm's graded tail. A tenant that drifts over those 400 s
# (measured: llama does; canneal, npb_cg_d80 and npb_ep_e do not) has that
# drift folded into its "controller cost". This arm gives every tenant a
# baseline at the SAME wall-clock offset as the arms, so drift cancels.
#
# It doubles as the methodology's own noise floor: `null` recovery SHOULD be
# ~0 for every tenant. Whatever it actually reads is the drift+noise floor that
# any other arm's digits must clear to mean anything.
measure_null(){ measure_ctrl null "$COEFFD3" perf "--observe --perf-attach cores"; }
# ── PEER-STYLE ARMS. Faithful reimplementations of the published POLICY onto
# OUR lever set, which is the accepted practice (CLITE did exactly this for
# PARTIES) and is what isolates policy as the only variable. See each script's
# header for its fidelity notes, and CONTROLLER_COMPARISON.md for the protocol.
measure_copart(){ measure_ctrl copart "${SCRIPT_DIR}/copart_style.py" cp; }
# DCAT_EXTRA passes dCat's paper-swept thresholds through, e.g.
#   DCAT_EXTRA="--ipc-imp-thr 0.03"
# dCat's Fig.9 sweeps ipc_imp_thr over 3-40% and picks 5% as its default; on
# D3 that default is load-bearing (canneal gains only 2.79-2.82% per way, so
# dCat stops after ONE way and leaves 3 ways unused for 350 s). Reporting dCat
# at its best PUBLISHED setting, not just its default, is what keeps this a
# sensitivity result rather than a strawman. See CONTROLLER_COMPARISON.md 9g.
measure_dcat(){   measure_ctrl dcat   "${SCRIPT_DIR}/dcat_style.py"   dc "${DCAT_EXTRA:-}"; }
# Cacheman (PPoPP'26) -- occupancy-fairness LLC controller. Python, writes its
# own decisions.jsonl and takes the standard cfg, so plain measure_ctrl works.
# CACHEMAN_CORE_DENOM picks the baseline denominator: "managed" (PI-decided
# default, Sigma over the tenants' own cores) vs "socket" (paper-literal, the
# documented sensitivity arm -- run it with CACHEMAN_CORE_DENOM=socket).
#
# GRADING OBLIGATION (CACHEMAN_AUDIT.md S9/B2 -- do not drop this): both of
# this controller's inertness guards are provably unreachable while its
# pool_full gate is open, which on this box is every cycle (N2). The arm can
# therefore complete cleanly, exit 0, and still have sat at level 0 (the
# unmanaged 15-way mask) for the whole graded window. certify.py reads the
# `activity-summary` record and refuses to print a recovery digit for an arm
# with inert_tail; this function additionally surfaces it in the run log so a
# live operator sees it without waiting for grading.
# pfx="perf": cacheman_style.py writes perf_<tenant>.json DIRECTLY (its own
# measure_ctrl contract, cacheman_style.py:221-223), unlike copart/dcat/spider
# which use their own prefix and need the normalize-copy step. Passing a
# non-"perf" prefix here would send that step hunting for files that never
# exist -- silently copying nothing.
measure_cacheman(){ measure_ctrl cacheman "${SCRIPT_DIR}/cacheman_style.py" perf
  local _as="$OUT/phase_cacheman/decisions.jsonl"
  if [[ -f "$_as" ]] && grep -q '"inert_tail": *true' "$_as"; then
    echo "!! WARNING cacheman: INERT TAIL -- every tenant sat at level 0 (the" >&2
    echo "   unmanaged mask) for the graded window. This arm took no action;" >&2
    echo "   it must be reported as 'took no action', NEVER as a recovery" >&2
    echo "   number. See CACHEMAN_AUDIT.md S9." >&2
  fi
  # explicit: this function's last statement is an `if` whose condition fails
  # in the GOOD case (arm not inert). bash returns 0 for a false-condition
  # `if`, so `set -e` is not tripped -- but this file has been bitten by
  # exit-status subtleties before, so do not leave it implicit.
  return 0; }

# PaLLOC (Bai et al., JSA 2025) -- the authors' REAL artifact, not a rendition.
# Needs its own arm because (1) it is C++ and writes no perf files, so this
# function starts the grading perf itself, (2) libpqos os_alloc_prep()
# (third_party/intel-cmt-cat/lib/os_allocation.c:230-257) mkdirs COS1..COS(n-1)
# at init and dies if ANY clos is taken, so every cert_* group must be handed
# back first (rmdir moves its tasks to the root group; tenants keep running),
# and (3) it manages PIDs, which on this harness means the workload CHILD of the
# `while true` wrapper. See CONTROLLER_COMPARISON.md 12a-12f for the patches,
# the seed decision and the measured smoke evidence.
measure_palloc(){
  local od="$OUT/phase_palloc"; mkdir -p "$od"
  local pdir="${PALLOC_DIR:-/home/tejendra/litmus/PaLLOC}"
  local pbin="$pdir/PaLLOC"
  [[ -x "$pbin" ]] || die "PaLLOC not built: $pbin (make test=1 in $pdir)"
  write_cfg; set_phase noisy; sleep "$SETTLE"
  local _n _rp _pids="" _nt
  for _n in "${ALLNAMES[@]}"; do
    _rp=$(pgrep -P "${PIDOF[$_n]}" 2>/dev/null | head -1) || true
    [[ -n "$_rp" ]] || die "palloc: no live workload child under wrapper ${PIDOF[$_n]} ($_n)"
    _nt=$(ls "/proc/$_rp/task" 2>/dev/null | wc -l) || true
    log "  palloc: $_n wrapper=${PIDOF[$_n]} workload=$_rp threads=${_nt:-?}"
    _pids+="${_rp},"
  done
  _pids="${_pids%,}"
  # grading perf: 2 s intervals, same as the coeffd arms. certify grades the
  # last CONTROLLER_TAIL*10 SECONDS per file (certify.py _tail_s), so the
  # interval only has to be small relative to that window.
  for _n in "${ALLNAMES[@]}"; do
    ( timeout -k 5 $((C4WIN+60)) perf stat -j -o "$od/perf_${_n}.json" -I 2000 \
        -e instructions,cycles,LLC-loads -C "${CORESOF[$_n]}" -- sleep $((C4WIN+30)) ) >/dev/null 2>&1 &
  done
  for _n in "${ALLNAMES[@]}"; do [[ -d "${GRPOF[$_n]}" ]] && rmdir "${GRPOF[$_n]}"; done
  local _left
  _left=$(find "$RESCTRL" -mindepth 1 -maxdepth 1 -type d ! -name info ! -name mon_groups ! -name mon_data | wc -l)
  (( _left == 0 )) || die "palloc: $_left resctrl control group(s) still held -- PaLLOC needs every CLOS"
  ( cd "$pdir" && exec env \
      PALLOC_SELF_CPU="${PALLOC_SELF_CPU:-22}" PALLOC_EXPECT_SOCKET="${PALLOC_EXPECT_SOCKET:-0}" \
      PALLOC_MAX_MB="${PALLOC_MAX_MB:-107765}" PALLOC_SEED_ATTACH="${PALLOC_SEED_ATTACH:-1}" \
      PALLOC_DIAG_LOG="$od/palloc_diag.log" \
      LD_LIBRARY_PATH="$pdir/third_party/intel-cmt-cat/lib:$pdir/third_party/pcm/build/lib:${LD_LIBRARY_PATH:-}" \
      "$pbin" -a 1 -P 100 -i "${PALLOC_INTERVAL_MS:-10}" -m 1 -p "$_pids" \
      > "$od/palloc.stdout.log" 2>&1 ) & CPID=$!
  sleep 3
  kill -0 "$CPID" 2>/dev/null || die "palloc exited immediately -- see $od/palloc.stdout.log"
  log "  palloc pid $CPID (seed=${PALLOC_SEED_ATTACH:-1}, -i ${PALLOC_INTERVAL_MS:-10} ms, window ${C4WIN}s)"
  sleep "$C4WIN"
  kill -INT "$CPID" 2>/dev/null || true
  for _ in $(seq 1 10); do kill -0 "$CPID" 2>/dev/null || break; sleep 1; done
  kill_tree "$CPID" 2>/dev/null || true; wait "$CPID" 2>/dev/null || true; CPID=""
  # hand the CLOS back: drop PaLLOC's COS* groups, recreate ours, reassign every
  # tid (wrapper AND the live child, which may have restarted).
  local _g
  for _g in "$RESCTRL"/COS*; do [[ -d "$_g" ]] && rmdir "$_g" 2>/dev/null || true; done
  for _n in "${ALLNAMES[@]}"; do
    [[ -d "${GRPOF[$_n]}" ]] || mkdir "${GRPOF[$_n]}"
    assign_tenant "${PIDOF[$_n]}" "${GRPOF[$_n]}"
    _rp=$(pgrep -P "${PIDOF[$_n]}" 2>/dev/null | head -1) || true
    [[ -n "$_rp" ]] && assign_tenant "$_rp" "${GRPOF[$_n]}"
    log "  palloc restore: $_n -> $(wc -l < "${GRPOF[$_n]}/tasks" 2>/dev/null || echo '?') tasks"
  done
  pkill -9 -f "perf stat.*${od}" 2>/dev/null || true
  set_phase noisy; sleep "$SETTLE"
}
# SATORI (Roy/Patel/Tiwari, ISCA'21): BO over the SAME 1001-partition LLC
# space the spider/oracle sweep uses (CONTROLLER_COMPARISON.md #13). Its own
# scikit-optimize is not in the system site-packages and this script runs
# under sudo, which cannot see the invoking user's --user site anyway, so it
# is vendored into a local target dir (see satori_style.py header / #13a);
# PYTHONPATH is scoped to ONLY this one command via measure_ctrl's $envpfx.
SATORI_PYDEPS="${SATORI_PYDEPS:-/home/tejendra/litmus/SATORI_artifact/pydeps}"
measure_satori(){
  # SATORI is the one arm that CANNOT run unprofiled: it needs isolated IPC for
  # every tenant (hence its "profiled" label in the comparison). Check before
  # launching -- satori_style.py already fails loud, but measure_ctrl only
  # notices a dead controller AFTER sleeping the whole window, so an unchecked
  # start costs a full C4WIN and yields an empty phase dir (2026-09-12 run).
  [[ -n "$SOLO_IPC" && -f "$SOLO_IPC" ]] || die "satori needs isolated IPC for
     every tenant but SOLO_IPC='$SOLO_IPC' does not exist. Point it at the MIX's
     iso run, e.g. SOLO_IPC=${SCRIPT_DIR}/results_iso/D3/solo_ipc.json"
  measure_ctrl satori "${SCRIPT_DIR}/satori_style.py" st "" "$C4WIN" \
    "PYTHONPATH=$SATORI_PYDEPS"; }
# SpiderSense ALGORITHM 2 is an EXHAUSTIVE live sweep over every way-partition:
# C(NBITS-1, N-1) configurations = 1001 for this scene's 5 tenants on 15 ways,
# ~17 min at their published 1 s Sampling Time Interval (Table 2). The arm
# therefore gets search + C4WIN; spidersense_style.py rotates its perf counters
# at convergence so the graded window is C4WIN of the CONVERGED config only.
# SS_SETTLE=0 reproduces their cadence exactly; SS_SETTLE=1.0 is the generous
# variant that gives each config a settle window before it is scored. Running
# both answers whether their 1 s hyperparameter decides on post-mask transients.
SS_INTERVAL="${SS_INTERVAL:-1.0}"
SS_SETTLE="${SS_SETTLE:-0.0}"
measure_spider(){
  local n m per search win
  n=${#ALLNAMES[@]}
  m=$(python3 -c "from math import comb; print(comb($NBITS-1, $n-1))")
  per=$(python3 -c "print($SS_INTERVAL + $SS_SETTLE)")
  search=$(python3 -c "print(int($m * $per))")
  win=$(( search + C4WIN + 60 ))
  log "  spider: ALGORITHM 2 over $m configs @ ${per}s = $((search/60)) min search," \
      "then ${C4WIN}s graded hold (arm window ${win}s)"
  measure_ctrl spider "${SCRIPT_DIR}/spidersense_style.py" ssperf \
    "--search exhaustive --interval $SS_INTERVAL --settle $SS_SETTLE" "$win"
}

# ── CHUNKED SEARCH ARM. ALGORITHM 2 needs ~1001 s of probing on this scene,
# which outlives the shortest tenant (npb_mg_d300, 1216 s solo) once warmup and
# the rest of the ladder are added. Rather than re-size the roster (which would
# invalidate the D3 iso baselines), the sweep is CHUNKED: each invocation probes
# a slice under $SPIDER_BUDGET, checkpoints to $SPIDER_STATE, and exits so the
# scene can be relaunched fresh for the next slice. Every configuration is then
# measured against a tenant instance that has NOT restarted mid-probe.
#
# The confound this introduces -- configurations measured in different scene
# incarnations -- is handled by the anchor set: $SS_ANCHORS configurations are
# re-measured in EVERY chunk, giving a per-chunk offset to subtract and a
# residual noise floor to test the argmax against. Validated offline: a drift
# 2.9x larger than the top-3 spread was removed and the true optimum recovered.
#
# Drive it from outside, relaunching the scene each time:
#   until grep -q '"remaining": 0' /tmp/spider_chunks.log; do
#     sudo CTRLS=spider_chunk ARMS= RUNC4=0 RUNDSOLO=0 AS=D3spider \
#          ./run_certify.sh <victims> | tee -a /tmp/spider_chunks.log
#   done
SPIDER_STATE="${SPIDER_STATE:-${SCRIPT_DIR}/results_certify/spider_state.json}"
SPIDER_BUDGET="${SPIDER_BUDGET:-340}"     # seconds of PROBING per chunk
SS_ANCHORS="${SS_ANCHORS:-8}"
# INSTANCE GATE. The iso baseline names WHICH instance of each tenant the whole
# comparison is keyed to (canneal is bistable across relaunches: 2.90 vs 3.45
# LLC-loads/KI). A chunk that came up on the other instance is discarded rather
# than folded in. ISO_AS names the ROSTER the baseline belongs to (D3), which is
# not the same as AS (the scene/run label, e.g. D3spider).
SPIDER_ISO="${SPIDER_ISO:-${SCRIPT_DIR}/results_iso/${ISO_AS:-${AS:-none}}/iso.json}"
measure_spider_chunk(){
  local per win cid
  per=$(python3 -c "print($SS_INTERVAL + $SS_SETTLE)")
  # chunk wall clock = anchor pass + budget + slack. The budget governs BODY
  # probing only, so the anchors must be added, not assumed inside it.
  win=$(python3 -c "print(int($SS_ANCHORS*$per + $SPIDER_BUDGET + 120))")
  cid=0
  [[ -f "$SPIDER_STATE" ]] && cid=$(python3 -c \
     "import json;print(json.load(open('$SPIDER_STATE')).get('chunks',0))" 2>/dev/null || echo 0)
  log "  spider chunk $cid: budget=${SPIDER_BUDGET}s anchors=$SS_ANCHORS window=${win}s"
  log "  state: $SPIDER_STATE"
  local isoarg="" vlist=""
  # Gate on the VICTIMS only. certify.py scopes the instance guard to tenants
  # whose MBA level is constant, and measurement agrees: on D3v9 canneal held
  # 0.9% and npb_cg_d80 0.3% against iso, while the donors llama (16.7%) and
  # npb_mg_d300 (3.9%) moved for real. Gating on a donor rejects healthy chunks.
  vlist=$(IFS=,; echo "${VICS[*]}")
  if [[ -f "$SPIDER_ISO" ]]; then
    isoarg="--iso $SPIDER_ISO --fp-tenants $vlist --fp-mode ${SPIDER_FP_MODE:-block}"
    log "  instance gate ON against $SPIDER_ISO (victims only: $vlist)"
  else
    log "  !! instance gate OFF -- no iso baseline at $SPIDER_ISO."
    log "     Set ISO_AS=<roster> (e.g. D3) or SPIDER_ISO=<path>. Without it a"
    log "     chunk that relaunched canneal into its other steady state is"
    log "     folded into the sweep silently."
  fi
  # shellcheck disable=SC2086
  CTRL_WAIT_EXIT=1 measure_ctrl "spider_c${cid}" "${SCRIPT_DIR}/spidersense_style.py" ssperf \
    "--search exhaustive --interval $SS_INTERVAL --settle $SS_SETTLE --state $SPIDER_STATE --budget $SPIDER_BUDGET --anchors $SS_ANCHORS --no-rotate-perf $isoarg" "$win"
  # the gate refuses the chunk by exiting 3; surface that as this script's exit
  # code so the driver loop relaunches the scene instead of counting a chunk
  if grep -q '"kind": *"fp_gate_failed"' "$OUT/phase_spider_c${cid}/decisions.jsonl" 2>/dev/null; then
    log "  chunk REJECTED by the instance gate -- relaunch and retry"
    SPIDER_GATE_FAILED=1
  fi
}

# ── CHUNKED SEARCH ARM (SATORI). Mirrors measure_spider_chunk above almost
# exactly: satori_style.py's start_bo_engine() needs up to n_calls=1000
# samples (its own VERBATIM hyperparameters), which the 2026-09-12 D3_satori
# run showed does NOT fit in one C4WIN-sized window -- it hit
# "Total BO Engine Timeout Reached" via the harness's own SIGALRM/SIGTERM
# after 298 samples / 249 distinct configs of the 1001-config space (the
# ARTIFACT's own get_weights bug, §13c, is already fixed; this timeout is
# purely a WINDOW SIZE problem). Reporting that partial search as SATORI's
# result would strawman the baseline, exactly the reasoning that produced
# measure_spider_chunk. Chunked the same way: each invocation probes for
# $SATORI_BUDGET seconds, checkpoints (config_idx, objective) observations +
# anchor probes to $SATORI_STATE, and exits so the scene can be relaunched
# fresh for the next slice. See satori_style.py's run_chunked_bo module
# docstring for the warm-start (x0/y0) and offset-correction mechanics --
# and for what is and is NOT preserved faithfully across a resume.
#
# Drive it the same way as spider_chunk, relaunching the scene each time:
#   until grep -q '"remaining": 0' /tmp/satori_chunks.log; do
#     sudo CTRLS=satori_chunk ARMS= RUNC4=0 RUNDSOLO=0 AS=D3satori \
#          ./run_certify.sh <victims> | tee -a /tmp/satori_chunks.log
#   done
SATORI_STATE="${SATORI_STATE:-${SCRIPT_DIR}/results_certify/satori_state.json}"
SATORI_BUDGET="${SATORI_BUDGET:-340}"     # seconds of BO PROBING per chunk
SATORI_ANCHORS="${SATORI_ANCHORS:-8}"
# Same iso.json format/roster as SPIDER_ISO (shared instance-fingerprint
# baseline, not controller-specific) -- see run_chunked_bo's _fp_gate.
SATORI_ISO="${SATORI_ISO:-${SCRIPT_DIR}/results_iso/${ISO_AS:-${AS:-none}}/iso.json}"
measure_satori_chunk(){
  # SATORI is the one arm that CANNOT run unprofiled -- same precondition as
  # measure_satori, checked here too so an unchecked start does not burn a
  # full chunk window on a controller that dies at t=0 (2026-09-12 finding).
  [[ -n "$SOLO_IPC" && -f "$SOLO_IPC" ]] || die "satori needs isolated IPC for
     every tenant but SOLO_IPC='$SOLO_IPC' does not exist. Point it at the MIX's
     iso run, e.g. SOLO_IPC=${SCRIPT_DIR}/results_iso/D3/solo_ipc.json"
  local win cid
  # chunk wall clock = anchor pass + budget + slack. Anchors are probed via a
  # direct objective() call (time_sampling=0.1s each, no GP-refit cost), so a
  # generous flat 2s/anchor covers the perf-poll budget too.
  win=$(python3 -c "print(int($SATORI_ANCHORS*2 + $SATORI_BUDGET + 120))")
  cid=0
  [[ -f "$SATORI_STATE" ]] && cid=$(python3 -c \
     "import json;print(json.load(open('$SATORI_STATE')).get('chunks',0))" 2>/dev/null || echo 0)
  log "  satori chunk $cid: budget=${SATORI_BUDGET}s anchors=$SATORI_ANCHORS window=${win}s"
  log "  state: $SATORI_STATE"
  local isoarg="" vlist=""
  # Gate on the VICTIMS only -- same rationale as measure_spider_chunk: the
  # donors' MBA/CAT levels move for real between chunks, gating on them
  # would reject healthy chunks.
  vlist=$(IFS=,; echo "${VICS[*]}")
  if [[ -f "$SATORI_ISO" ]]; then
    isoarg="--iso $SATORI_ISO --fp-tenants $vlist --fp-mode ${SATORI_FP_MODE:-block}"
    log "  instance gate ON against $SATORI_ISO (victims only: $vlist)"
  else
    log "  !! instance gate OFF -- no iso baseline at $SATORI_ISO."
    log "     Set ISO_AS=<roster> (e.g. D3) or SATORI_ISO=<path>. Without it a"
    log "     chunk that relaunched a tenant into its other steady state is"
    log "     folded into the surrogate's training data silently."
  fi
  # shellcheck disable=SC2086
  CTRL_WAIT_EXIT=1 measure_ctrl "satori_c${cid}" "${SCRIPT_DIR}/satori_style.py" st \
    "--state $SATORI_STATE --budget $SATORI_BUDGET --anchors $SATORI_ANCHORS $isoarg" \
    "$win" "PYTHONPATH=$SATORI_PYDEPS"
  # the gate refuses the chunk by exiting 3; surface that as this script's exit
  # code so the driver loop relaunches the scene instead of counting a chunk
  if grep -q '"kind": *"fp_gate_failed"' "$OUT/phase_satori_c${cid}/decisions.jsonl" 2>/dev/null; then
    log "  chunk REJECTED by the instance gate -- relaunch and retry"
    SATORI_GATE_FAILED=1
  fi
}

# ── solo: all victims together, no polluters (this is the counterfactual the
# do-no-harm floor is defined against -- unmanaged sharing among the paying
# tenants, not single-tenant isolation)
log "=== certify ${VICS[*]} vs donors: $POL ==="
for v in "${VICS[@]}"; do start_tenant "$v"; done
log "warming victims to steady state"
for v in "${VICS[@]}"; do warm_up "$v"; done
measure solo

# ── + polluters
for p in $POL; do start_tenant "$p"; done
log "warming donors"
for p in $POL; do warm_up "$p"; done
ALLNAMES=( "${VICS[@]}" ); for p in $POL; do ALLNAMES+=( "$p" ); done
measure noisy

# cat_cap's cap is defined against UNMANAGED occupancy, so it can only be
# planned once `noisy` exists. Plan it before `cat` so an unplannable cap is
# reported early rather than after another ~2 minutes of measurement.
if [[ -n "$CAP" ]]; then
  if ISPOL "$CAP"; then die "CAP=$CAP is a donor; the cap targets an ABSORBER among the victims"; fi
  [[ -n "${GRPOF[$CAP]:-}" ]] || die "CAP=$CAP is not a tenant in this scene"
fi

# ── STATIC ARMS ARE NOW OPT-IN (2026-09-04). Measured on D3-v8, the four static
# arms cost 312 s of a 1270 s run (25%); the dominant costs are warmup and c4.
# Default keeps only `cat`, for two reasons this project paid to learn:
#   * `solo` (always run) is the scene's ONLY sanity check -- D3-v8's corrupted
#     baseline was visible solely because solo showed npb_cg_d80 at 0.9 MB
#     instead of 29 MB. Without it a broken scene reads as a quiet one.
#   * `cat` is the reference coeffd4 is graded against, and the comparison is
#     only sound WITHIN one run -- cross-run comparison is what the canneal
#     instance-flip showed we cannot trust.
# `eq` (peer baseline), `cat_cap` (falsified, n=2, and it harms a tenant by
# design) and `mba` (asymmetry replicated n=5) are established; run them with
# ARMS="eq cat cat_cap mba" when producing paper figures.
# NOTE ${ARMS-cat}, not ${ARMS:-cat}: an explicitly EMPTY ARMS="" must mean
# "no static arms", and :- would silently substitute the default back in.
ARMS="${ARMS-cat}"
for _a in $ARMS; do
  case "$_a" in
    cat_cap) if [[ -n "$CAP" ]]; then plan_cap; measure cat_cap; fi ;;
    spider_conv) load_spider_conv; measure spider_conv ;;
    # SATORI's converged answer. SATORI_CONV must be given explicitly: the
    # converged.json lives in whichever chunk converged, and guessing the chunk
    # would risk replaying a stale or partial search.
    sat_conv) [[ -n "${SATORI_CONV:-}" ]] || die "sat_conv needs SATORI_CONV=<path to converged.json>"
              load_replay sat_conv "$SATORI_CONV"; measure sat_conv ;;
    cm_nest|nest_dp|band_dp)
              load_replay "$_a" "${MASK_TEST_DIR:-${SCRIPT_DIR}/mask_tests}/$_a.json"; measure "$_a" ;;
    *)       measure "$_a" ;;
  esac
done

# ── CONTROLLER ARMS, after the static ones: static masks are then provably ours,
# and each controller starts from a restored unmanaged state regardless.
# CTRLS is an ORDERED list. Order is a real confound -- later arms see tenants
# further into their lifetime -- so rotate it across runs (CTRLS="c4 c3" then
# "c3 c4") and check the two orderings agree before quoting a difference.
CTRLS="${CTRLS-$( (( RUNC4 )) && echo c4 )}"
for _c in $CTRLS; do
  case "$_c" in
    c4) measure_c4 ;;
    c3) measure_c3 ;;
    copart) measure_copart ;;
    dcat)   measure_dcat ;;
    spider) measure_spider ;;
    spider_chunk) measure_spider_chunk ;;
    satori) measure_satori ;;
    satori_chunk) measure_satori_chunk ;;
    palloc) measure_palloc ;;
    cacheman) measure_cacheman ;;
    null)   measure_null ;;
    *)  die "unknown controller arm '$_c' (known: c4 c3 copart dcat spider spider_chunk satori satori_chunk palloc cacheman)" ;;
  esac
done

# a gate-rejected search chunk must not look like a successful run
if [[ "${SPIDER_GATE_FAILED:-0}" == 1 || "${SATORI_GATE_FAILED:-0}" == 1 ]]; then
  log "=== chunk rejected by the instance gate; exiting 3 ==="
  exit 3
fi

# ── dsolo: the DONORS' own counterfactual, taken LAST.
# Until now donors existed only from `noisy` onward, so the scene could say what
# a lever COST them but not how much co-location had already cost them -- an
# asymmetry with the victims, who get `solo`. That is the same shape of gap as
# the missing aggressor column that forced the exp16 DNH-oracle retraction: the
# aggressor was in the scene but not in the accounting.
# Taken at the END rather than the start so the donors are never relaunched: the
# victims are being torn down anyway, so killing them early costs nothing and
# leaves the donors in ONE continuous execution phase for the whole scene (the
# npb_mg_d300 sizing depends on that).
if (( ${RUNDSOLO:-1} )) && [[ -n "$POL" ]]; then
  log "dsolo: tearing down victims, measuring donors alone"
  set_phase noisy                       # full masks: this is the UNMANAGED donor baseline
  # kill_tree, NOT `kill -9 $PIDOF`: PIDOF is the loop SHELL and the workload is
  # its child, so a bare kill orphans the binary. D3-v9 measured `dsolo` with all
  # three victims still running at ~400% CPU on 12 cores -- the phase claims the
  # donors are ALONE, so a bare kill silently inverts its meaning.
  for v in "${VICS[@]}"; do
    kill_tree "${PIDOF[$v]}" || true
    unset 'PIDOF['"$v"']'
  done
  sleep 5
  # VERIFY the teardown rather than assume it: dsolo is only meaningful if the
  # victims are actually gone.
  _surv=()
  for _d in /proc/[0-9]*; do
    _e=$(readlink -f "$_d/exe" 2>/dev/null) || continue
    for v in "${VICS[@]}"; do
      [[ "$_e" == "$(readlink -f "${BIN[$v]}" 2>/dev/null)" ]] && _surv+=("${_d#/proc/}:$v")
    done
  done
  if (( ${#_surv[@]} > 0 )); then
    log "  !! dsolo teardown INCOMPLETE, survivors: ${_surv[*]} -- killing and retrying"
    for _s in "${_surv[@]}"; do kill -9 "${_s%%:*}" 2>/dev/null || true; done
    sleep 5
  fi
  for p in $POL; do warm_up "$p"; done
  measure dsolo
fi

hand_back
log "DONE. Analyze: python3 certify.py $OUT"
