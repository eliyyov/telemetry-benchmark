#!/usr/bin/env bash
# run.sh - interactive benchmark menu, run on VM-Mon:  cd ~/bench && ./run.sh
# Every tunable is asked in the terminal: mode, agent, load level, reps, warm-up,
# measurement window, resume. Fixed by design (controlled variables, not selectable):
#   tier rates 5k/20k/50k ev/s, core pinning (agent 2-3, loggen 0, sampler 1),
#   ABBA arm order, validity retries (2x).
# Runs the orchestrator on VM-Data over SSH, pulls results back to VM-Mon,
# exports the cAdvisor cross-check and analyzes.
set -euo pipefail
cd "$(dirname "$0")"
source "$HOME/.bench_env"

ENVV="AGENT_CPUSET=2-3 AGENT_CORES=2 LOGGEN_CPUS=0 SAMPLER_CPUS=1"
echo "=== Telemetry agent benchmark: Vector vs OpenTelemetry Collector ==="
echo "    VM-Data (agent under test) -> results land here on VM-Mon"
echo

PS3=$'\nChoose what to run: '
select scope in \
  "Smoke run - pipeline check only, NOT thesis data" \
  "Custom run - your own reps and window" \
  "Full thesis sweep (normal, medium, high x reps, ABBA order)" \
  "Analyze existing results only (no new runs)"; do
  [ -n "$scope" ] && break
done
case "$REPLY" in
  1) MODE=smoke ;;  2) MODE=custom ;;  3) MODE=sweep ;;  4) MODE=analyze ;;
  *) echo invalid; exit 1 ;;
esac

ARGS=""
if [ "$MODE" != analyze ]; then
  PS3=$'\nWhich agent(s)? '
  select a in "Both arms (vector + otel, ABBA order)" "Vector only" "OpenTelemetry Collector only"; do
    [ -n "$a" ] && break
  done
  case "$REPLY" in 1) ARMS=v,o ;; 2) ARMS=v ;; 3) ARMS=o ;; *) echo invalid; exit 1 ;; esac

  if [ "$MODE" = sweep ]; then LEVELS=normal,medium,high; else
    PS3=$'\nWhich load level? '
    select l in "normal (5,000 ev/s)" "medium (20,000 ev/s)" "high (50,000 ev/s)" "all levels (normal, medium, high)"; do
      [ -n "$l" ] && break
    done
    case "$REPLY" in 1) LEVELS=normal ;; 2) LEVELS=medium ;; 3) LEVELS=high ;; 4) LEVELS=normal,medium,high ;; *) echo invalid; exit 1 ;; esac
  fi

  # reps per cell
  case "$MODE" in
    smoke)  read -r -p "Reps per cell [1]: " REPS; REPS=${REPS:-1} ;;
    custom) read -r -p "Reps per cell [1]: " REPS; REPS=${REPS:-1} ;;
    sweep)  read -r -p "Reps per cell [5]: " REPS; REPS=${REPS:-5} ;;
  esac

  # warm-up / measurement window
  case "$MODE" in
    smoke)  DEF_W=10; DEF_M=30 ;;
    *)      DEF_W=30; DEF_M=120 ;;
  esac
  read -r -p "Warm-up seconds [$DEF_W]: " WARM;  WARM=${WARM:-$DEF_W}
  read -r -p "Measurement window seconds [$DEF_M]: " MEAS; MEAS=${MEAS:-$DEF_M}

  # resume: skip runs already valid (continues an interrupted sweep)
  read -r -p "Resume mode - skip cells already valid? [y/N]: " RES
  case "$RES" in y|Y) RESUME="--resume" ;; *) RESUME="" ;; esac

  ARGS="--reps $REPS --arms $ARMS --levels $LEVELS --warmup $WARM --measure $MEAS $RESUME"
fi

NLEV=$(echo "$LEVELS" | tr ',' '\n' | grep -c . || true)
NARM=$(echo "$ARMS"  | tr ',' '\n' | grep -c . || true)
if [ -n "$ARGS" ]; then
  NRUNS=$((NLEV * NARM * REPS))
  ESTMIN=$(python3 -c "print(f'{$NRUNS * ($WARM + $MEAS + 30) / 60:.0f}')")
  echo
  echo "Plan: $MODE - $NRUNS runs, est. ${ESTMIN} min (plus retries)"
  echo "orchestrator.py $ARGS"
  if [ "$MODE" = sweep ]; then
    read -r -p "This is the full benchmark sweep. Type 'yes' to start: " GO
    [ "$GO" = "yes" ] || { echo cancelled; exit 0; }
  else
    read -r -p "Proceed? [y/N]: " GO
    case "$GO" in y|Y) ;; *) echo cancelled; exit 0 ;; esac
  fi

  echo "== syncing bench bundle to VM-Data =="
  rsync -a --exclude logs --exclude results --exclude raw_metrics --exclude __pycache__ ./ data:bench/
  echo "== running on VM-Data (output streams below) =="
  # shellcheck disable=SC2086
  ssh -o BatchMode=yes data "cd bench && $ENVV python3 orchestrator.py $ARGS"
  echo "== pulling results back to VM-Mon =="
  mkdir -p results raw_metrics
  rsync -a data:bench/results/ results/
  rsync -a data:bench/raw_metrics/ raw_metrics/
else
  echo
  echo "Analysis only."
fi

echo "== cAdvisor cross-check export (V5/V6) =="
python3 export_cadvisor.py || echo "(export skipped - is Prometheus up? run ./setup.sh)"
echo "== analysis =="
python3 analyze.py
echo
echo "Done. Data: results/runs.csv, results/summary.md, raw_metrics/<arm>/<level>/runN/"
