#!/usr/bin/env bash
# run_iso.sh -- ISOLATED per-tenant baselines. One tenant on the box at a time.
#
# WHY THIS EXISTS. run_certify.sh's `solo` phase runs ALL VICTIMS TOGETHER with
# no donors. That is not isolation, and the peer literature means isolation by
# the word "solo": CoPart (IPS_full/IPS_solo), Themis (solo-normalized progress),
# CLITE ("solo-isolation"), PIVOT ("run-alone"). Alita lists `solo` and
# `Default (free-share)` as SEPARATE rungs -- our phase is a fourth thing.
# Consequence: our degradation figure is harm attributable TO THE DONORS, which
# is strictly smaller than solo-normalized slowdown because victim-on-victim
# contention is hidden inside the baseline. See CERTIFY_DESIGN.md
# "OUR `solo` IS NOT THE PEERS' `solo`".
#
# WHY A SEPARATE SCRIPT, NOT A PHASE. An isolated baseline depends only on the
# tenant, so unlike the victims-only phase it is valid ACROSS runs: measure once
# per roster, reuse in every scene. That is also why it must be fingerprinted --
# see below.
#
# THE REUSE CATCH. canneal has two steady states (LLC-loads/KI 2.90 vs 3.45,
# constant for a process lifetime), so "canneal isolated" is not a single number.
# Every record is keyed by its instance fingerprint; certify.py refuses an iso
# baseline whose fingerprint does not match the tenant in the scene.
#
# CORE PARITY. Cores are allocated by the SAME rule as run_certify.sh (victims
# from core 2 up, then donors), so pass the tenants in the SAME order and with
# the same NC/POLNC/POL as the scene. Each tenant then runs isolated on exactly
# the cores it will occupy in the scene, removing core placement as a confound.
#
# Usage: sudo [POL="npb_mg_d300 llama"] [POLNC=4] [AS=D3] ./run_iso.sh <t1> [t2 ...]
#   e.g. sudo AS=D3 POL="npb_mg_d300 llama" POLNC=4 \
#           ./run_iso.sh canneal npb_cg_d80 npb_ep_e
#   NOTE: env goes AFTER sudo -- sudo resets the environment.
#   then: python3 iso_report.py results_iso/D3
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/bench_lib.sh"
require_turbo_off
RESCTRL=/sys/fs/resctrl; NODE=0
WIN=${WIN:-45}; SETTLE=${SETTLE:-15}
POL="${POL:-}"
POLNC="${POLNC:-4}"
WARMUP_MIN="${WARMUP_MIN:-20}"
WARMUP_MAX="${WARMUP_MAX:-300}"
WARMUP_COV="${WARMUP_COV:-0.12}"
export CANNEAL_STEPS="${CANNEAL_STEPS:-300000}"

log(){ echo "[$(date '+%H:%M:%S')] $*"; }; die(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo"
(( $# >= 1 )) || { echo "usage: sudo $0 <tenant> [tenant2 ...]"; exit 1; }

VICS=("$@")
ALL=( "${VICS[@]}" ); for p in $POL; do ALL+=( "$p" ); done
for t in "${ALL[@]}"; do [[ -x "${BIN[$t]:-}" ]] || die "missing binary for $t"; done

# PREFLIGHT: match on the RESOLVED EXECUTABLE, never on cmdline text -- a
# `pgrep -f <name>` also matches the shell running the check, so the guard fires
# on itself and no run can ever start.
declare -A _tenant_exe=()
for _b in "${BIN[@]}"; do
  [[ -n "$_b" ]] || continue
  _tenant_exe["$(readlink -f "$_b" 2>/dev/null || echo "$_b")"]=1
done
_stale=()
for _d in /proc/[0-9]*; do
  _p=${_d#/proc/}; [[ "$_p" == "$$" ]] && continue
  _e=$(readlink -f "$_d/exe" 2>/dev/null) || continue
  [[ -n "$_e" ]] || continue
  if [[ -n "${_tenant_exe[$_e]:-}" ]]; then _stale+=("$_p  $_e")
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
  echo "ERROR: ${#_stale[@]} tenant process(es) already running -- would contaminate"
  echo "       the isolation baseline, which is the ONE measurement that must be clean."
  printf '         %s\n' "${_stale[@]}"
  echo "       Clear them, then re-run:  sudo kill -9 ${_stale[*]%% *}"
  exit 1
fi

OUT="${SCRIPT_DIR}/results_iso/${AS:-$(IFS=+; echo "${ALL[*]}")}"
mkdir -p "$OUT"
FULL_HEX=$(cat "$RESCTRL/info/L3/cbm_mask")

# ── core plan: IDENTICAL rule to run_certify.sh so each tenant is measured on
# the cores it will actually occupy in the scene.
declare -A GRPOF=() CORESOF=() NCOF=()
NEXT=2
for v in "${VICS[@]}"; do
  nc=${NC:-$(default_ncores "$v")}
  NCOF[$v]=$nc; CORESOF[$v]="$NEXT-$((NEXT+nc-1))"; GRPOF[$v]="$RESCTRL/iso_${v}"
  NEXT=$((NEXT+nc))
done
for p in $POL; do
  NCOF[$p]=$POLNC; CORESOF[$p]="$NEXT-$((NEXT+POLNC-1))"; GRPOF[$p]="$RESCTRL/iso_${p}"
  NEXT=$((NEXT+POLNC))
done
(( NEXT <= 32 )) || die "roster needs $NEXT cores, node 0 has 32"

log "=== iso baselines: ${#ALL[@]} tenants, one at a time -> $OUT ==="
for n in "${ALL[@]}"; do log "   $n cores=${CORESOF[$n]} nc=${NCOF[$n]}"; done

CPID=""; CURPID=""; CURGRP=""
kill_tree(){ local p="$1" c
    for c in $(pgrep -P "$p" 2>/dev/null); do kill_tree "$c"; done
    kill -9 "$p" 2>/dev/null || true; }
cleanup(){ [[ -n "$CPID" ]] && kill_tree "$CPID" || true
    [[ -n "$CURPID" ]] && kill_tree "$CURPID" || true
    pkill -9 -f "coeffd3.py" 2>/dev/null || true; pkill -9 -f "perf stat" 2>/dev/null || true
    for n in "${!GRPOF[@]}"; do [[ -d "${GRPOF[$n]}" ]] && rmdir "${GRPOF[$n]}" 2>/dev/null || true; done
    printf 'L3:0=%s;1=%s\n' "$FULL_HEX" "$FULL_HEX" > "$RESCTRL/schemata" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

mountpoint -q "$RESCTRL" || mount -t resctrl resctrl "$RESCTRL" || die "cannot mount resctrl"

# ── VERIFY the teardown rather than assume it. An iso baseline contaminated by a
# survivor from the PREVIOUS tenant is the worst possible failure here: it is
# silent, and every downstream number is divided by it.
teardown_verify(){ local t="$1" d e surv=()
  [[ -n "$CURPID" ]] && kill_tree "$CURPID" || true
  CURPID=""; sleep 4
  for d in /proc/[0-9]*; do
    e=$(readlink -f "$d/exe" 2>/dev/null) || continue
    [[ "$e" == "$(readlink -f "${BIN[$t]}" 2>/dev/null)" ]] && surv+=("${d#/proc/}")
  done
  if (( ${#surv[@]} > 0 )); then
    log "  !! $t teardown incomplete, survivors ${surv[*]} -- killing and retrying"
    for s in "${surv[@]}"; do kill -9 "$s" 2>/dev/null || true; done
    sleep 4
  fi
  [[ -n "$CURGRP" && -d "$CURGRP" ]] && rmdir "$CURGRP" 2>/dev/null || true
  CURGRP=""; }

for t in "${ALL[@]}"; do
  log "--- $t alone on cores ${CORESOF[$t]} ---"
  g="${GRPOF[$t]}"; mkdir -p "$g"; CURGRP="$g"
  # full mask: isolation means the whole cache is available, no partitioning
  printf 'L3:0=%s;1=%s\nMB:0=100;1=100\n' "$FULL_HEX" "$FULL_HEX" > "$g/schemata"

  launch_tenant "$t" "${CORESOF[$t]}" "$g" "$OUT/$t" "${NCOF[$t]}"
  CURPID="$LAUNCH_PID"
  while ! tenant_ready "$t" "$OUT/$t" "$CURPID"; do sleep 2; done
  assign_tenant "$CURPID" "$g"

  declare -A PIDOF=(); declare -A _keepgrp=()
  PIDOF[$t]="$CURPID"
  warm_up "$t"

  od="$OUT/iso_$t"; mkdir -p "$od"
  lat=""; has_lat_feed "$t" && lat=",\"lat_file\":\"$OUT/$t/run/live.txt\""
  printf '{"tenants": [{"name":"%s","pid":%s,"grp":"%s","priority":2,"cores":"%s"%s}]}' \
      "$t" "$CURPID" "$g" "${CORESOF[$t]}" "$lat" > "$od/cfg.json"

  sleep "$SETTLE"
  python3 "$COEFFD3" --config "$od/cfg.json" --out "$od" --observe --perf-attach cores \
      > "$od/ctrl.log" 2>&1 & CPID=$!
  sleep $((SETTLE+WIN))
  kill -TERM "$CPID" 2>/dev/null || true
  for _ in $(seq 1 8); do kill -0 "$CPID" 2>/dev/null || break; sleep 1; done
  kill -9 "$CPID" 2>/dev/null || true; wait "$CPID" 2>/dev/null || true; CPID=""
  ns=$(grep -c '"kind": *"scan"' "$od/decisions.jsonl" 2>/dev/null || echo 0)
  log "  $t iso: $ns scans"
  (( ns > 0 )) || log "  !! WARNING: no scans for $t -- baseline UNUSABLE"

  teardown_verify "$t"
  unset PIDOF
done

# results are written as root; hand them back so the analysis scripts (run
# as the user) can write their summary JSON next to the data.
if [[ -n "${SUDO_UID:-}" ]]; then chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT" 2>/dev/null || true; fi
log "DONE. Analyze: python3 iso_report.py $OUT"
