# Panduan Data Benchmark — Vector vs OpenTelemetry Collector

Cara membaca setiap file hasil benchmark, arti tiap angka, dan cara menelusuri balik
klaim apa pun ke data mentah. Ditulis untuk testbed equal-work (2026-10-12).

---

## 1. Gambaran besar: apa isi eksperimennya

Dua telemetry agent menjalankan job yang sama di VM-Data, satu per satu:

| arm | agent | bahasa | memory model |
|---|---|---|---|
| `vector` | Vector 0.59.0 | Rust | tanpa GC (ownership) |
| `otel` | OpenTelemetry Collector contrib 0.162.0 | Go | garbage-collected |

**Job yang sama** artinya, per event (per baris log), kedua arm mengerjakan hal yang persis sama:

1. baca satu baris dari `/logs/bench.log`
2. parse sebagai JSON (`parse_json!` / `ParseJSON`)
3. tambah satu field: `processed: true`
4. buang event-nya (`blackhole` / `nop`)
5. di luar itu: keduanya menyimpan file-read checkpoint (Vector `data_dir`, OTel `file_storage`),
   keduanya mengekspos self-telemetry lewat Prometheus, keduanya juga mengumpulkan host metrics

Load tier: `normal` = 5.000 ev/s · `medium` = 20.000 · `high` = 50.000 (event per detik,
digenerate `loggen` dari baris NDJSON `corpus/template.json`).

Batas resource per agent: 2 CPU core terpin (`cpuset 2-3`, tanpa CPU quota), cap RAM 1.536 MiB.
Pembagian core di VM-Data 4 vCPU: core 0 = loggen, core 1 = cAdvisor + sampler, core 2-3 = agent.

Protokol run: teardown → reset log → agent up → ready-check → warm-up (30 dtk) →
measurement window (120 dtk) → stop → teardown → agregasi. Arm bergantian dengan urutan
**ABBA** (rep1: vector,otel · rep2: otel,vector · ...) supaya drift lingkungan yang pelan
tidak bisa berpihak pada agent yang kebetulan jalan duluan.

---

## 2. Segitiga pengukuran

Tiap run menghasilkan 5 file mentah plus satu baris CSV. Kepercayaan datang dari tiga sisi
yang saling memeriksa:

```
loggen  ──mengklaim─►  "Saya menulis X baris"          (loggen_status.json / window.written di run.json)
agent   ──mengklaim─►  "Saya memproses Y event"        (agent_metrics.txt / window.counter di run.json)
kernel  ──mengklaim─►  "Biayanya Z CPU/RAM"            (cgroup_samples.csv)
cAdvisor ──memeriksa─► "secara independen, juga Z ±5%" (cadvisor_samples.csv)
```

- V1 membandingkan loggen terhadap target tier (offered rate).
- V3 membandingkan agent terhadap loggen (apakah agent sanggup mengikuti?).
- V5/V6 membandingkan kernel terhadap cAdvisor (apakah alat ukurnya sendiri sehat?).

Kalau ada sisi yang bedanya melewati toleransi, run-nya ditandai, diulang, atau dibuang.
Tidak pernah diam-diam masuk rata-rata.

---

## 3. Struktur direktori

```
<root data run>/
  results/
    runs.csv          # satu baris per run — tabel utama
    summary.md        # agregasi turunan, rasio, statistik, cross-check
  raw_metrics/
    <arm>/<level>/run<N>/     # contoh: vector/normal/run1/
      run.json                # flight recorder: batas window + angka turunan
      loggen_status.json      # rekap generator
      cgroup_samples.csv      # sampel resource kernel 1 Hz (meter utama)
      cadvisor_samples.csv    # sampel independen 5 dtk (meter cross-check)
      agent_metrics.txt       # scrape Prometheus self-telemetry agent
```

Riwayat data lokal (di mesin analisis):
`~/thesis/data/smoke-2026-10-11/` (config lama, kerja tidak sama) dan
`~/thesis/data/equalwork-2026-10-12/` (equal-work + equal-durability).

---

## 4. `runs.csv` — kolom per kolom

Satu baris per run. Urutan grup dari kiri ke kanan:

### Identitas
| kolom | arti |
|---|---|
| `arm` | `vector` atau `otel` |
| `level` | load tier: normal / medium / high |
| `rep` | nomor repetisi dalam cell (1..5; rep ada supaya statistik bisa jalan) |
| `attempt` | 1 = langsung lolos; 2 = baris retry setelah gagal validity check |

### Workload (blok "apakah bebannya benar-benar terjadi")
| kolom | arti | sumber |
|---|---|---|
| `rate_target` | rate tier yang diminta ke generator (5.000) | desain eksperimen |
| `offered_eps` | baris yang benar-benar ditulis loggen per detik selama window | Δ `window.written` / 120 dtk |
| `consumed_eps` | event yang dihitung sendiri oleh agent per detik | Δ `window.counter` / 120 dtk |
| `consumed_ratio` | consumed ÷ offered (0.9977 = agent mengikuti 99,77%) | turunan |

### Biaya resource (data inti skripsi)
| kolom | arti |
|---|---|
| `cpu_mean_pct` | rata-rata CPU selama window — **persen dari SATU core** (200 = kedua core sibuk penuh) |
| `cpu_max_pct` | sampel 1 detik terburuk dalam window |
| `cpu_p95_pct` | persentil ke-95 — bukti tail/spikiness |
| `mem_mean_window_mib` | rata-rata RSS selama window |
| `mem_peak_window_mib` | puncak RSS selama window |
| `mem_peak_lifetime_mib` | puncak sejak container start (termasuk warm-up; ≥ puncak window) |
| `throttled_delta` | event CPU throttling selama window (harusnya 0 — tidak ada quota) |
| `oom_kill` | OOM kill kernel (harusnya 0 — cap 1536 MiB tidak dirancang untuk membatasi) |

### Validity flag (dari MEASUREMENT_SPEC)
| flag | aturan | kalau gagal |
|---|---|---|
| `v1_ok` | offered dalam 2% dari target tier | run tidak valid, ulangi |
| `v3_ok` | consumed_ratio ≥ 0,98 | kalau V4 saturated → simpan sebagai **lower bound**; kalau tidak, ulangi |
| `v4_saturated` | agent memakai ≥ 190% dari satu core | **hanya dilaporkan**, tidak pernah jadi alasan reject |
| `v10_ok` | tidak ada CPU throttling selama window | ulangi |
| `v11_ok` | tidak ada OOM selama window | ulangi |

Kolom `status`: `valid` / `lower-bound` / `error`.

### Catatan administrasi
`samples` (120 = 1 dtk × 120 dtk window, cocok dengan spec C7), `agent_cpuset` (harus `2-3`),
`started`, `finished` (UTC).

---

## 5. `summary.md` — bagian per bagian

1. **Per level and arm** — n, mean/median CPU, mean puncak memori, offered/consumed eps.
   Tabel untuk melihat sekilas siapa unggul di apa.
2. **Ratios + exact Mann–Whitney U** — rasio mean (otel/vector), uji U exact two-sided
   per level. Cara bacanya:
   - ratio: 0,59× CPU artinya "OTel memakai 59% CPU Vector"; 3,97× mem artinya
     "OTel memakai ~4× memori Vector"
   - `p = 0.1000` pada n=3 vs 3 adalah p-value **terkecil yang bisa dicapai** pada ukuran
     sampel itu, bukan "hampir signifikan". Lihat pemisahannya saja (misal: tidak ada
     tumpang tindih antar arm).
   - pada n=5 vs 5 p minimumnya 0,0079; klaim dibuat di n=5.
3. **V5/V6 cross-check** — sampler cgroup vs cAdvisor per run, toleransi 5%.
   `FAIL (repeat <=2x, then exclude)`: run diulang maksimal dua kali; kalau tetap
   tidak cocok, cell itu dibuang dan dilaporkan sebagai demikian.
4. **Validity counts** — per cell berapa run lolos tiap flag; `excluded runs`;
   total `cross-check failures`.

---

## 6. File mentah

### `run.json` — flight recorder
Semua yang ada di baris runs.csv, plus jejak auditnya:
- `window.start` / `window.end` — batas measurement window 120 dtk (timestamp unix)
- `window.written` `[t0, t1]` — counter baris loggen saat window mulai/akhir →
  `(t1-t0)/120 = offered_eps`
- `window.counter` `[t0, t1]` — counter event milik agent → `consumed_eps`
- `loggen` — rekap generator sepanjang run (pengecekan wajar di sisi offered)

**Angka runs.csv apa pun bisa dihitung ulang dari dua counter ini.**

### `loggen_status.json`
`{"written": N, "elapsed": S}` — pengakuan generator. Kalau loggen meleset dari target
rate, V1 gagal dan run-nya batal **sebelum** agent mana pun disalahkan.

### `cgroup_samples.csv` — meter resource utama (1 Hz, ~135 baris/run)
Dibaca dari cgroup v2 (akunting kernel) oleh sampler yang dipin ke core 1:

| kolom | arti |
|---|---|
| `ts`, `elapsed_s` | timestamp unix / detik sejak run mulai |
| `container` | `v-vector` / `o-otel` |
| `cpu_usage_usec` | akumulasi waktu CPU (µs) — delta-nya memberi biaya per detik |
| `cpu_pct_of_one_core` | delta CPU 1 dtk dalam % satu core (sumber mentah kolom CPU) |
| `mem_current_bytes`, `mem_peak_bytes` | RSS live dan puncak yang dicatat kernel |
| `nr_throttled`, `throttled_usec` | counter throttling → `throttled_delta` |
| `oom_kill` | event OOM |

Hanya baris di dalam `window.start..end` yang dianalisis (baris warm-up/teardown dibuang).

### `cadvisor_samples.csv` — saksi independen (5 dtk, ~26 baris/run)
Container yang sama, jalur pengukuran yang sama sekali terpisah (cAdvisor 0.60.6 di VM-Data):

| kolom | arti |
|---|---|
| `ts` | timestamp unix (kelipatan 5 dtk) |
| `cpu_usage_seconds_total` | CPU container kumulatif versi cAdvisor (dtk) → delta / 5 dtk = % satu core |
| `mem_usage_bytes` | pembacaan memori container versi cAdvisor |

Dibandingkan dengan angka cgroup pada window yang sama → cross-check V5/V6.

### `agent_metrics.txt` — laporan diri agent
Scrape Prometheus di akhir run dari endpoint telemetry masing-masing agent. Catat
asimetri ini: **Vector `:9598`** menggabungkan sumber `host` + `self`; **OTel `:8888`**
hanya self-telemetry (pipeline host-metrics-nya diekspor di `:9599`, tidak ikut ter-scrape
ke file mentah). Ini tidak memengaruhi angka CPU/mem (kedua exporter jalan di kedua arm),
tapi memengaruhi apa yang bisa kamu audit per arm.

**Vector (82 metric family), per kelompok:**
- *consumption (jangkar V2)*: `vector_component_received_events_total{component_id="logs"}`,
  `vector_component_received_bytes_total`, `vector_component_received_event_bytes_total`,
  `vector_component_received_events_count`
- *alir per tahap*: `vector_component_sent_*`, `vector_component_latency_seconds`,
  `vector_component_latency_mean_seconds`
- *buffer*: `vector_buffer_*` (size, utilization, send duration) plus per tahap
  `vector_source_buffer_*`, `vector_transform_buffer_utilization*` — tekanan pipeline
- *perilaku source*: `vector_source_lag_time_seconds` (seberapa jauh pembaca file tertinggal
  — sinyal "sanggup mengikuti" yang sesungguhnya), `vector_source_send_latency_seconds`,
  `vector_source_send_batch_latency_seconds`
- *durability*: `vector_checkpoints_total` (tulis checkpoint = kerja durability opsi-b),
  `vector_files_added_total`, `vector_open_files`
- *self/infra*: `vector_uptime_seconds`, `vector_utilization`, `vector_build_info`,
  `vector_started_total`, `vector_http_server_*`, `vector_internal_metrics_cardinality*`
- *sinyal host (C10)*: `host_cpu_seconds_total`, `host_memory_*`, `host_disk_*`,
  `host_network_*`, `host_filesystem_*`, `host_load1/5/15`, `host_logical_cpus`,
  `host_physical_cpus`, `host_memory_oom_kill_events_total`

**OTel (22 metric family), per kelompok:**
- *consumption (jangkar V2)*: `otelcol_receiver_accepted_log_records{receiver="file_log"}`,
  plus `otelcol_receiver_failed_log_records` dan `otelcol_receiver_refused_log_records`
  (failed = error parse/emit; refused = ditolak downstream)
- *alir per tahap*: `otelcol_processor_incoming_items` / `outgoing_items` (processor
  `transform/enrich` — parse JSON equal-work mendarat di sini),
  `otelcol_exporter_in_flight_requests`, `otelcol_exporter_sent_metric_points`
- *file/checkpointing*: `otelcol_fileconsumer_open_files`, `otelcol_fileconsumer_reading_files`
  (perilaku checkpoint file_storage terlihat di sini)
- *runtime/bukti GC*: `otelcol_process_runtime_heap_alloc_bytes`,
  `otelcol_process_runtime_total_alloc_bytes`,
  `otelcol_process_runtime_total_sys_memory_bytes`, `otelcol_process_cpu_seconds`,
  `otelcol_process_memory_rss`, `otelcol_process_uptime` — angka heap Go di balik
  cerita memori
- *scraper host-metrics*: `otelcol_scraper_scraped_metric_points`,
  `otelcol_scraper_errored_metric_points`, `otelcol_receiver_accepted/failed_metric_points`
- *lainnya*: `promhttp_metric_handler_errors_total`, `target_info`

---

## 7. Kerangka metrik (ID MEASUREMENT_SPEC)

Spec memberi nomor pada setiap metrik. Arm sekarang dipetakan ke penamaan asli spec sebagai
berikut: **U1 → arm `vector`** (agent unified), **F1/F2 → arm `otel`** (spec lebih tua
dari formasi Vector-vs-OTel dan masih menuliskan Filebeat/Fluentd di sana). Baris yang
menyebut C4 (Vector logs-only) dan node_exporter (D5) bagian dari studi terfragmentasi
yang sudah dibuang dan **tidak bisa dihitung di desain 2-arm sekarang** — spec perlu
diubah menyesuaikan.

### Primary outcome (hipotesis diuji di sini)
| ID | nama | definisi | satuan | sumber |
|---|---|---|---|---|
| O1 | `cpu_mean_pct` | mean(Δ`usage_usec` / (Δt × 10⁶) × 100) selama window | % satu core | cgroup `cpu.stat` @ 1 Hz |
| O2 | `mem_peak_mb` | max(`memory.peak`) | MiB | cgroup `memory.peak` @ 1 Hz |

### Secondary observation
| ID | nama | definisi |
|---|---|---|
| S1 | `cpu_max_pct` | sampel CPU 1 dtk tertinggi dalam window |
| S2 | `mem_mean_mb` | mean(`memory.current`) |
| S3 | split per container | O1/O2 per container (legacy: log agent vs node_exporter) |

### Validity check (rangkaian V)
| ID | yang dicek | aturan |
|---|---|---|
| V1 | offered rate | dalam 2% dari target tier |
| V2 | `agent_throughput_eps` | Δ counter event agent / 120 dtk (metrik jangkar `agent_metrics.txt`) |
| V3 | consumption ratio V2/V1 | ≥ 0,98; kalau di bawah & saturated → label lower-bound; kalau tidak, ulangi |
| V4 | saturation | S1 ≥ 190% (95% dari 2 core); dilaporkan, tidak pernah reject |
| V5 | kecocokan CPU cAdvisor | \|cAdvisor − sampler\| / sampler ≤ 5% (Δ`container_cpu_usage_seconds_total`/120 dtk × 100 vs Δ`usage_usec`) |
| V6 | kecocokan memori cAdvisor | toleransi 5% yang sama untuk memori |
| V10 | tanpa CPU throttling | `throttled_delta` = 0 |
| V11 | tanpa OOM | `oom_kill` = 0 |

### Derived value (dihitung per tier dari 5 run per arm)
| ID | nilai | definisi | status di desain sekarang |
|---|---|---|---|
| D1 | CPU ratio | mean O1(otel) / mean O1(vector) | ✅ terhitung (summary.md "CPU ratio") |
| D2 | memory ratio | mean O2(otel) / mean O2(vector) | ✅ terhitung ("mem ratio") |
| D3 | CPU per 1k ev/s | O1 / (V2 / 1000) | ✅ bisa dihitung |
| D4 | marginal cost sinyal ke-2 | O1(logs+metrics) − O1(logs-only) | ⚠️ butuh arm logs-only (sudah dibuang) |
| D5 | biaya agent metrik terpisah | nilai S3 node_exporter | ⚠️ legacy (desain terfragmentasi yang dibuang) |
| D6 | baseline gap | \|O1(F1) − O1(F2)\| / mean | ⚠️ legacy (dua agent terfragmentasi) |
| D7 | memory growth | O2(t3) − O2(t1) per arm | ✅ bisa dihitung (window t1→t3) |

### Hipototesis (seperti tertulis di spec; H2/H4 butuh arm legacy)
| ID | yang diuji | didukung jika |
|---|---|---|
| H1 consolidation | D1, D2 | bedanya signifikan (p < .05) di setiap tier |
| H2 marginal cost | D4 vs D5 | ⚠️ arm legacy |
| H3 memory follows runtime | O2, D7 | arm tanpa GC punya O2 terendah dan D7 terkecil |
| H4 impl vs architecture | D6 vs D1 | ⚠️ arm legacy |

---

## 8. Cara mengaudit klaim apa pun (resep)

- *"Vector memakai CPU 6,68%"* → buka `vector/normal/run1/cgroup_samples.csv`, ambil
  baris `cpu_pct_of_one_core` dalam `window` di `run.json`, hitung rata-ratanya ≈ 6,69.
- *"OTel memakai memori 4×"* → bandingkan mean `mem_mean_window_mib` antar arm di runs.csv,
  atau hitung ulang dari kolom `mem_current_bytes`.
- *"consumed_ratio 0,9977"* → `(639800-39800)/120 ÷ (646900-45500)/120` dari `run.json`.
- *"spikiness GC"* → deret `cpu_pct_of_one_core` per detik: stdev Vector 0,27 vs
  OTel 1,06 (equal-work, tier normal). Plot saja — sawtooth-nya kelihatan.
- *"pengukurannya sehat"* → tabel V5/V6 di summary.md, toleransi 5% antar meter.

## 9. Catatan statistik

- Uji: exact two-sided Mann–Whitney U per level (CPU dan memori terpisah).
- n=3 (pilot sekarang): p minimum yang bisa dicapai = 0,1000. n=5 (sweep rencana): p = 0,0079.
- Effect size dilaporkan sebagai rasio mean di samping p.
- Urutan arm ABBA + teardown per run membuat run nyaris independen.
- Exclusion (validity atau cross-check) selalu dilaporkan, tidak pernah diam-diam dibuang.

## 10. Apa yang dikatakan data equal-work sekarang (2026-10-12, tier normal, n=3)

- OTel ~1,7× lebih hemat CPU (3,92% vs 6,68% dari satu core), Vector ~4× lebih hemat
  memori (20 vs 80 MiB).
- CPU OTel lebih bergerigi (max/mean 1,51×, stdev 1,06) vs Vector datar (1,16×, 0,27) —
  tanda tangan GC.
- Keenam run valid; 1 cross-check batas toleransi (vector/run3 mem 5,2% vs batas 5%).
- V4 saturation tidak pernah true di 5k — agent masih santai; tier medium/high menguji
  scaling, bukan saturation.
