# Explanatory experiments

The benchmark sweep answers *how much*. These blocks answer *why* — they exist so
Chapter 4 can explain the ordering instead of only reporting it.

Entry point, on VM-Mon:

```
cd ~/bench
./diagnose --dry-run      # plan only
./diagnose                # all blocks, ~40 min, unattended
./diagnose --only ablation
```

A diagnostic run is **not** thesis data. It never writes into `results/`, and it
never modifies `configs/` — variants are generated into `diag_cfg/` (gitignored)
and each arm is pointed at one by a generated compose file.

## Blocks

| block | cells | what it isolates |
|---|---|---|
| `ablation` | source-only, +parse, full × 2 arms × reps | where each agent spends CPU |
| `crossing` | 25 000 and 36 000 ev/s × 2 arms × reps | whether the predicted crossing is real |
| `poll` | OTel `poll_interval` 200 ms → 50 ms × 2 arms × reps | whether OTel's low-load advantage is batch size |

## Parity applied to every variant

Two asymmetries in the official configs are removed here, so a stage difference
cannot be blamed on side work:

- Vector `host_key: ""` — one per-event attribute, matching OTel's `log.file.name`
  (Vector otherwise also writes the hostname).
- OTel `host_metrics.collectors` restricted to the six Vector collects
  (cpu, memory, disk, network, filesystem, load); the default is eight, adding
  paging and processes.

## Files

- `make_configs.py` — generates `diag_cfg/` from the committed configs (run automatically).
- `runner.py` — runs on VM-Data; imports `orchestrator` and calls the same
  `run_once()` the sweep uses, so sampling, cgroup counting, ABBA order and the
  validity gates are identical. Every cell records the sha256 of its config.
- `diagnose` (in `bench/`) — preflight, sync, block execution, result pull,
  summary table.

## Reading the output

`diagnostics/<timestamp>/` holds one CSV per block plus the exact configs that
were used and the git HEAD. The printed table is a stage breakdown; the per-stage
cost is the difference between consecutive rows, not the row value alone.
