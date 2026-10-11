# EC2 testbed - Vector vs OpenTelemetry Collector

Two VMs in one VPC/subnet/AZ. Only VM-Mon is reachable from the internet (SSH from your
IP). VM-Data accepts SSH + scrape ports from VM-Mon only.

| VM      | Instance            | Runs                                                                                      |
| ------- | ------------------- | ----------------------------------------------------------------------------------------- |
| VM-Data | c6a.xlarge (4 vCPU) | agent under test (cores 2-3), loggen (core 0), cAdvisor + cgroup sampler (core 1), Docker |
| VM-Mon  | c6a.large (2 vCPU)  | Prometheus + Grafana (localhost only), all scripts, results                               |

Arms: `timberio/vector:0.59.0-debian` vs `otel/opentelemetry-collector-contrib:0.162.0`.
Both pipelines are equal by construction: file tail -> discard (no parsing), 6 host-metric
collectors (cpu, memory, disk, network, filesystem, load) every 15 s, per-agent consumed
counter, default configs (the parity knobs are only the mounts/paths/ports both need).
Load tiers: normal 5k / medium 20k / high 50k events/s, 5 reps each, ABBA order.

## 1. Build the infrastructure (from your machine)

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # optional: edit region / allowed_ssh_cidr
terraform init
terraform plan                                 # review the plan
terraform apply                                # YOU run this - it creates billable resources
```

Terraform prints `ssh_mon` - that is your entry point. It also uploads `bench/` to
VM-Mon and writes `operator_key.pem` next to the .tf files. To destroy everything at the
end: `terraform destroy`.

## 2. One-time setup (on VM-Mon)

```bash
ssh -i operator_key.pem ubuntu@<mon_public_ip>
cd bench && ./setup.sh
```

Checks both VMs, starts Prometheus + Grafana (VM-Mon) and cAdvisor (VM-Data, core 1),
pulls the agent images, and verifies every link. Idempotent.

## 3. Run the benchmark (on VM-Mon)

```bash
./run.sh
```

### Menu parameters

The menu asks for everything tunable. Start with the **smoke run** - one short rep that
proves the pipeline; it is not thesis data. The full sweep needs a typed `yes`.

| Parameter           | What it decides          | Meaning                                                                                                                 | Default            |
| ------------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------ |
| Mode                | what kind of session    | *smoke*: quick pipeline check (not thesis data) · *custom*: you pick reps/window · *sweep*: the real experiment · *analyze only*: re-generate stats from existing data, no new runs | -                  |
| Agent(s)            | who is tested           | *both*: Vector and OTel alternately (ABBA: rep 1 V,O - rep 2 O,V ...) so host drift hits both equally · one arm for debugging | -                  |
| Load level(s)       | how fast logs are written | *normal* 5,000 ev/s · *medium* 20,000 · *high* 50,000 · *all* runs the three in order                                   | sweep: all         |
| Reps per cell       | how many repeats        | each agent x level combination is repeated n times; n = 5 makes the exact Mann-Whitney test able to reach p < 0.05       | smoke/custom: 1, sweep: 5 |
| Warm-up seconds     | settle time             | agent starts and catches up on the log file; this period is thrown away so startup noise stays out of the data           | 10 (smoke) / 30    |
| Measurement window  | the measured period     | CPU and memory figures are computed over exactly this window (O1/O2)                                                     | 30 (smoke) / 120   |
| Resume mode         | continue after a break  | skips cells already recorded valid - use when a sweep got interrupted (reboot, dropped SSH) instead of redoing it all    | n                  |

Fixed on purpose (controlled variables, not selectable): tier rates 5k/20k/50k ev/s,
core pinning (agent cores 2-3, loggen core 0, sampler + cAdvisor core 1), ABBA arm
order, and the 2x validity-retry rule.

Results stream live, then land in:

- `results/runs.csv` - one row per run (O1/O2, offered vs consumed eps, validity flags)
- `results/summary.md` - per-tier table, ratios, exact Mann-Whitney, V5/V6 cross-check
- `raw_metrics/<arm>/<level>/runN/` - cgroup_samples.csv, cadvisor_samples.csv,
  run.json, agent_metrics.txt (supplementary material)

## 4. Grafana (optional, from your machine)

```bash
ssh -i operator_key.pem -L 3000:127.0.0.1:3000 ubuntu@<mon_public_ip>
# http://localhost:3000 - admin / $(cat ~/.grafana_admin_password) on VM-Mon
```

## Notes

- Cost: c6a.xlarge + c6a.large on-demand, roughly US$0.23/h combined (check current
  pricing in your region). `terraform destroy` when finished.
- Cloud-init disabled unattended-upgrades and snapd on VM-Data so nothing updates during
  a measurement window.
- A run is repeated at most twice on validity failure (V1 offered rate, V10 throttling,
  V11 OOM, V5/V6 cross-check > 5%), then excluded and reported.
- LXC rehearsal numbers are pipeline proof only; EC2 is the thesis dataset.
