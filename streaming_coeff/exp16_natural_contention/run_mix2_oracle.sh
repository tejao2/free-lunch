#!/usr/bin/env bash
# run_mix2_oracle.sh -- mix2: every LLC way-partition, measured live.
#
# WHAT IT IS. SpiderSense's persistent-load ALGORITHM 2 run exhaustively
# (spidersense_style.py --search exhaustive, via run_certify.sh's spider_chunk
# arm): 5 tenants x 15 ways, every tenant >=1 way => C(14,4) = 1001 contiguous
# partitions, 1 s each (their Table 2 cadence). The same scan is two things:
#   - the SpiderSense arm (argmax of summed IPC, then a runoff -> `converged`)
#   - the ORACLE MAP: per-tenant IPC under every partition, so the ceiling
#     ("best achievable victim recovery") and the rank of the victim-optimal
#     partition under their objective both fall out (grade_spider.py).
#
# PIPELINE (each step strictly after the previous; skipped if already done):
#   1. iso      one tenant alone, scene core order, WIN=180 -> results_iso/$FAM
#   2. teeth    victims-only + noisy, no arms -> abort unless >=1 tenant is
#               LLC-critical (>10% below iso, SpiderSense Fig 9)
#   3. sweep    spider_chunk, relaunching the scene until the state converges
#   4. grade    grade_spider.py + certify.py
#
# INSTANCE GATE = warn, NOT block (decided 2026-09-15). The gate's 3% threshold
# was calibrated on LOOPING BATCH tenants (canneal restarting into a different
# program). mix2's victims are LC servers and single-invocation tenants that
# never restart, and their fingerprints drift 3-9% from load/timing alone
# (memcached, sphinx, stream_l in the mix2 noisy runs). Blocking would reject
# healthy chunks indefinitely; widening 3% after seeing the drift would be
# post-hoc. So every chunk still RECORDS its fingerprints, and per-tenant
# thresholds are set from the iso repeat (results_iso/mix2 vs mix2r) BEFORE
# the sweep is graded; an uncalibratable tenant is reported "instance unguarded".
#
# PRIORITY: run_certify.sh:write_cfg gives every tenant priority 2 (no ladder),
# per the user. The exhaustive arm writes disjoint masks directly, so the ladder
# does not affect this sweep anyway -- only the c4/coeffd arms.
#
# Usage (env AFTER sudo -- sudo resets the environment):
#   sudo ./run_mix2_oracle.sh
#   sudo SPIDER_BUDGET=1100 MAXTRY=6 FAM=mix2r ./run_mix2_oracle.sh
#   sudo FORCE=1 ./run_mix2_oracle.sh      # sweep even if step 2 finds no victim
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

FAM="${FAM:-mix2r}"
read -r -a VICS <<< "${VICS:-sphinx memcached pr}"
POL="${POL:-stream_l npb_is_d600}"
POLNC="${POLNC:-4}"
ISO_WIN="${ISO_WIN:-180}"
# 1001 configs x ~1.02 s ~= 17 min of probing. One chunk fits every tenant's
# lifetime (shortest: stream_l ~3700 s alone) including ~10 min of scene setup,
# so the default budget covers the whole space; the loop still handles a split.
SPIDER_BUDGET="${SPIDER_BUDGET:-1100}"
MAXTRY="${MAXTRY:-6}"
FORCE="${FORCE:-0}"

# RUN_TAG: an independent repeat of the sweep on the SAME iso/teeth (e.g. RUN_TAG=_r2).
# D3 PROTOCOL NOTE: D3's sweep ran in 2 chunks, so its 8 anchors were measured in
# 2 scene incarnations and gave a real noise floor (0.099). A 1-chunk sweep has
# no anchor spread and reports floor 0 / "resolvable" vacuously -- force >=2
# chunks with SPIDER_BUDGET below the probing time (364 configs: ~190 s).
RUN_TAG="${RUN_TAG:-}"
STATE="${SCRIPT_DIR}/results_certify/${FAM}_spider${RUN_TAG}_state.json"
SWEEP_AS="${FAM}_spider${RUN_TAG}"
NOISY_AS="${NOISY_AS:-${FAM}_teeth}"   # reuse an existing noisy run, e.g. mix2r_is
LOGD="/tmp/${FAM}_oracle"; mkdir -p "$LOGD"

log(){ echo "[$(date '+%H:%M:%S')] $*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo"
[[ "$STATE" != */results_certify/spider_state.json ]] || die "refusing D3's state file"
log "mix2 oracle: FAM=$FAM victims=(${VICS[*]}) donors=($POL) POLNC=$POLNC"
log "  logs: $LOGD   state: $STATE"

converged(){ [[ -f "$STATE" ]] && python3 -c \
  "import json,sys; sys.exit(0 if json.load(open('$STATE')).get('converged') else 1)" 2>/dev/null; }

# ── 1. iso ────────────────────────────────────────────────────────────────
ISO_JSON="${SCRIPT_DIR}/results_iso/${FAM}/iso.json"
if [[ -f "$ISO_JSON" ]]; then
  log "1/4 iso: exists ($ISO_JSON) -- skipped"
else
  log "1/4 iso: ${#VICS[@]} victims + donors, WIN=$ISO_WIN"
  AS="$FAM" WIN="$ISO_WIN" POL="$POL" POLNC="$POLNC" ./run_iso.sh "${VICS[@]}" \
      2>&1 | tee "$LOGD/1_iso.log"
  (( PIPESTATUS[0] == 0 )) || die "run_iso.sh failed (see $LOGD/1_iso.log)"
  python3 iso_report.py "results_iso/$FAM" 2>&1 | tee -a "$LOGD/1_iso.log"
  [[ -f "$ISO_JSON" ]] || die "iso_report produced no $ISO_JSON"
fi

# ── 2. teeth ──────────────────────────────────────────────────────────────
if [[ -d "results_certify/$NOISY_AS/phase_noisy" ]]; then
  log "2/4 teeth: results_certify/$NOISY_AS exists -- re-grading only"
else
  log "2/4 teeth: victims-only + noisy, no arms"
  AS="$NOISY_AS" POL="$POL" POLNC="$POLNC" ARMS= CTRLS= RUNC4=0 RUNDSOLO=0 \
      ./run_certify.sh "${VICS[@]}" 2>&1 | tee "$LOGD/2_teeth.log"
  (( PIPESTATUS[0] == 0 )) || die "teeth run failed (see $LOGD/2_teeth.log)"
fi
python3 certify.py "results_certify/$NOISY_AS" > "$LOGD/2_teeth_grade.txt" 2>&1
sed -n '/tenant            iso IPC/,/fence moved/p' "$LOGD/2_teeth_grade.txt"
# LLC-critical rows in the PEER-STANDARD table (column 5 == YES)
ncrit=$(sed -n '/tenant            iso IPC/,/scene GB\/s/p' "$LOGD/2_teeth_grade.txt" \
        | awk '$5=="YES"{c++} END{print c+0}')
if (( ncrit == 0 )); then
  (( FORCE )) || die "no tenant >10% below iso -- no teeth, sweep skipped (FORCE=1 to override)"
  log "  !! no LLC-critical tenant, continuing because FORCE=1"
else
  log "  teeth: $ncrit LLC-critical tenant(s)"
fi

# ── 3. sweep ──────────────────────────────────────────────────────────────
try=0
until converged; do
  try=$((try+1))
  (( try <= MAXTRY )) || die "not converged after $MAXTRY scene launches (state: $STATE)"
  log "3/4 sweep: scene launch $try/$MAXTRY, budget ${SPIDER_BUDGET}s"
  AS="$SWEEP_AS" ISO_AS="$FAM" POL="$POL" POLNC="$POLNC" \
  CTRLS=spider_chunk ARMS= RUNC4=0 RUNDSOLO=0 \
  SPIDER_STATE="$STATE" SPIDER_BUDGET="$SPIDER_BUDGET" SPIDER_FP_MODE=warn \
      ./run_certify.sh "${VICS[@]}" 2>&1 | tee -a "$LOGD/3_sweep.log"
  rc=${PIPESTATUS[0]}
  if (( rc == 3 )); then log "  chunk rejected by instance gate -- relaunching"
  elif (( rc != 0 )); then die "run_certify.sh exited $rc (see $LOGD/3_sweep.log)"; fi
  [[ -f "$STATE" ]] && python3 -c "import json;d=json.load(open('$STATE'));\
print(f\"  state: {len(d.get('done',{}))} probed, chunks={d.get('chunks',0)}, converged={bool(d.get('converged'))}\")"
done
log "3/4 sweep: converged"

# PROBE HEALTH -- added after the mix2r sweep "converged" on 229/356 probes with
# nsamp=0 (perf stalled by an RR-99 LC tenant; see bench_lib LC_RT). A probe with
# no complete perf interval reads IPC 0 for every tenant and silently enters the
# argmax. Refuse to grade unless every anchor and >=98% of probes carry samples.
python3 - "$STATE" <<'EOF' || die "sweep data UNUSABLE -- see probe health above; do not grade it"
import json, sys
s = json.load(open(sys.argv[1]))
done = s.get("done", {})
empty = sum(1 for v in done.values() if not v.get("nsamp"))
anch = [o for obs in s.get("anchor", {}).values() for o in obs]
anch0 = sum(1 for o in anch if o.get("sum_ipc", 0) <= 0.01)
frac = empty / max(1, len(done))
print(f"  probe health: {empty}/{len(done)} probes with no perf samples ({100*frac:.1f}%), "
      f"{anch0}/{len(anch)} anchors reading zero")
sys.exit(0 if frac <= 0.02 and anch0 == 0 else 1)
EOF

# ── 4. grade ──────────────────────────────────────────────────────────────
log "4/4 grade"
python3 grade_spider.py "results_certify/$SWEEP_AS" --state "$STATE" 2>&1 | tee "$LOGD/4_grade_spider.txt"
python3 certify.py "results_certify/$SWEEP_AS" > "$LOGD/4_certify.txt" 2>&1
if [[ -n "${SUDO_UID:-}" ]]; then
  chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "results_certify/$SWEEP_AS" "results_certify/$NOISY_AS" \
      "$STATE" "results_iso/$FAM" "$LOGD" 2>/dev/null || true
fi
log "DONE. grade: $LOGD/4_grade_spider.txt   ladder: $LOGD/4_certify.txt"
log "NEXT (separate runs, one arm each): c4, then spider_conv with SPIDER_CONV=$STATE"
