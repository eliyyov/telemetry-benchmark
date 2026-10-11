#!/usr/bin/env bash
# run.sh - interactive benchmark menu, run on VM-Mon:  cd ~/bench && ./run.sh
# Picks agent + load level from the menu, runs the orchestrator on VM-Data over SSH,
# pulls the results back to VM-Mon, exports the cAdvisor cross-check and analyzes.
set -euo pipefail
cd "$(dirname "$0")"
source "$HOME/.bench_env"

ENVV="AGENT_CPUSET=2-3 AGENT_CORES=2 LOGGEN_CPUS=0 SAMPLER_CPUS=1"
echo "=== Telemetry agent benchmark: Vector vs OpenTelemetry Collector ==="
echo "    VM-Data (agent under test) -> results land here on VM-Mon"
echo

PS3=$'\nChoose what to run: '
select scope in \
  "Smoke run (1 rep, 10s warm-up + 30s window, both agents, normal load) - run this first" \
  "One level, full window (1 rep, 30s warm-up + 120s window)" \
  "Full thesis sweep (all 3 levels x reps, ABBA order, ~2.5-3 h)" \
  "Analyze existing results only (no new runs)"; do
  [ -n "$scope" ] && break
done

case "$REPLY" in
  1) MODE=smoke ;;
  2) MODE=level ;;
  3) MODE=sweep ;;
  4) MODE=analyze ;;
  *) echo "invalid"; exit 1 ;;
esac

if [ "$MODE" != analyze ]; then
  PS3=$'\nWhich agent(s)? '
  select a in "Both arms (vector + otel, ABBA order)" "Vector only" "OpenTelemetry Collector only"; do
    [ -n "$a" ] && break
  done
  case "$REPLY" in 1) ARMS=v,o ;; 2) ARMS=v ;; 3) ARMS=o ;; *) echo invalid; exit 1 ;; esac

  PS3=$'\nWhich load level? '
  select l in "normal (5,000 ev/s)" "medium (20,000 ev/s)" "high (50,000 ev/s)" "all levels (normal, medium, high)"; do
    [ -n "$l" ] && break
  done
  case "$REPLY" in 1) LEVELS=normal ;; 2) LEVELS=medium ;; 3) LEVELS=high ;; 4) LEVELS=normal,medium,high ;; *) echo invalid; exit 1 ;; esac
fi

case "$MODE" in
  smoke)  ARGS="--reps 1 --arms $ARMS --levels ${LEVELS:-normal} --warmup 10 --measure 30"
          NOTE="short pipeline check, NOT thesis data";;
  level)  ARGS="--reps 1 --arms $ARMS --levels $LEVELS"
          NOTE="one full-window rep per selected arm (thesis-format data point)";;
  sweep)  read -r -p "Repetitions per cell [5]: " REPS; REPS=${REPS:-5}
          ARGS="--reps $REPS --arms $ARMS --levels $LEVELS"
          NOTE="FULL SWEEP: $REPS reps x $(echo "$LEVELS" | tr ',' '\n' | wc -l) level(s) x $(echo "$ARMS" | tr ',' '\n' | wc -l) arm(s). Long run.";;
  analyze) ARGS=""; NOTE="analysis only";;
esac

echo
echo "Plan: $MODE - $NOTE"
[ -n "$ARGS" ] && echo "orchestrator.py $ARGS"
if [ "$MODE" = sweep ]; then
  read -r -p "This is the full benchmark sweep. Type 'yes' to start: " GO
  [ "$GO" = "yes" ] || { echo "cancelled"; exit 0; }
else
  read -r -p "Proceed? [y/N]: " GO
  case "$GO" in y|Y) ;; *) echo cancelled; exit 0 ;; esac
fi

if [ -n "$ARGS" ]; then
  echo "== syncing bench bundle to VM-Data =="
  rsync -a --exclude logs --exclude results --exclude raw_metrics --exclude __pycache__ ./ data:bench/
  echo "== running on VM-Data (output streams below) =="
  # shellcheck disable=SC2086
  ssh -o BatchMode=yes data "cd bench && $ENVV python3 orchestrator.py $ARGS"
  echo "== pulling results back to VM-Mon =="
  mkdir -p results raw_metrics
  rsync -a data:bench/results/ results/
  rsync -a data:bench/raw_metrics/ raw_metrics/
fi

echo "== cAdvisor cross-check export (V5/V6) =="
python3 export_cadvisor.py || echo "(export skipped - is Prometheus up? run ./setup.sh)"
echo "== analysis =="
python3 analyze.py
echo
echo "Done. Data: results/runs.csv, results/summary.md, raw_metrics/<arm>/<level>/runN/"
