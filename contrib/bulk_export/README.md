# Timesketch Bulk Export Tool

A high-performance, resilient utility for exporting thousands of Timesketch sketches to forensic-grade ZIP archives. Optimized for large-scale OpenSearch clusters (50k+ shards).

## 🚀 Key Features

- **Pipeline Architecture**: Decouples index management from data streaming for maximum throughput.
- **Auto-Recovery**: Automatically handles cluster instability and Master node lag.
- **Resource Guards**: Proactively monitors System RAM (OOM prevention) and OpenSearch JVM pressure.
- **Fast Pathing**: Use `--open-indices-only` to export active data without Master node overhead.
- **Hybrid Concurrency**: Dynamically allows parallel streaming for already-active sketches while serializing metadata changes for archived ones.
- **Instant Shutdown**: Cleanly kills all child workers on `Ctrl+C` to prevent orphaned processes.
- **Audit Trails**: Every export includes a detailed performance breakdown and SHA256 integrity verification.

## 📋 Prerequisites

- **Persistence**: Always run in a `tmux` session.
- **Storage**: Ensure the export directory has enough space (approx 100MB to 5GB per sketch).
- **Environment**: Must be run on a system with direct access to the Timesketch database and OpenSearch cluster.

## 🛠️ Usage

### The "Golden Path" (Fastest & Safest)
Only export sketches that are already open in OpenSearch, skipping slow pre-counts and Master node waits. This is the **strongly recommended** command for busy clusters:
```bash
python3 bulk_export.py \
  --export-dir /mnt/sketch_export/annotated \
  --pipeline \
  --concurrency 2 \
  --open-indices-only \
  --ignore-event-count \
  --retry-failed \
  --min-ram-gb 25
```
*(Note: You can safely increase `--concurrency` in this mode as it places zero pressure on the Master node.)*

### Full System Export
Iterate through all sketches (including archived ones) with serialized opening:
```bash
python3 bulk_export.py \
  --export-dir /mnt/sketch_export/all \
  --pipeline \
  --concurrency 1 \
  --ignore-event-count \
  --all-statuses
```

## ⚙️ Configuration & CLI Arguments

### Core Arguments
- `--export-dir`: Destination for ZIP files and `manifest.csv`.
- `--concurrency`: Number of parallel exports (Default: 1). **Recommended: 1-2 for high-shard clusters.**
- `--pipeline`: Enable the producer/worker architecture (Highly recommended).
- `--limit`: Stop after processing this many sketches.

### Filtering
- `--open-indices-only`: Only export sketches that are already active in OpenSearch.
- `--all-statuses`: Include archived, deleted, and ready sketches.
- `--start-id` / `--end-id`: Limit processing to a specific ID range.
- `--annotated-only`: Only export events with stars, comments, or labels.
- `--include-legacy`: Include events missing the `__ts_timeline_id` field.

### Safety & Stability
- `--min-ram-gb`: Pause if available system RAM falls below this threshold (Default: 25).
- `--max-master-wait`: Pause if Master node task queue wait time exceeds this ms (Default: 10000).
- `--ignore-event-count`: Bypasses the expensive pre-export search query. 
- `--retry-failed`: Cleans up manifest and deletes corrupted files from previous failed runs.

## 📊 Monitoring

### Interactive Dashboard (Highly Recommended)
Run the dedicated monitoring script to see cluster health, RAM usage, and live session progress in one view:
```bash
./monitor_export.sh
```

### Troubleshooting
- **Cluster turns RED**: The script will automatically pause. Check the Master node latency via:
  `curl -s "http://MASTER_IP:9200/_cluster/health" | jq ".task_max_waiting_in_queue_millis"`
- **OOM Errors**: If you see `LOW SYSTEM RAM`, the script is waiting for memory to be released. Ensure no zombie `tsctl` processes are running via `ps aux | grep tsctl`.
