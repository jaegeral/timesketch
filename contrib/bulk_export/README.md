# Timesketch Bulk Sketch Export Utility

## Overview
This utility automates the large-scale export of sketches from a Timesketch instance. It is designed to handle thousands of sketches while protecting the underlying OpenSearch cluster from overload and managing different sketch states (Ready/Archived/Deleted).

## Key Features
- **Newest-First by Default**: Automatically processes sketches from highest ID to lowest ID to prioritize recent investigations. Use `--oldest-first` to switch to ascending order.
- **Windowed Batching & Concurrency**: Specifically designed for high-shard clusters. Processes metadata operations (Open/Close) in "windows" to minimize expensive Global Cluster State updates, while performing data extraction in **parallel** via thread pools.
- **High-Speed Skip Logic**:
    - **Instant ZIP Discovery**: Bypasses all OpenSearch operations if a valid export ZIP already exists in the destination folder.
    - **Memory-Resident Manifest**: Uses a high-speed set for nanosecond lookups of already processed sketches.
- **Smart "Interestingness" Skip**: For triage-only runs (`--annotated-only`), the script verifies event counts in OpenSearch before starting the heavy compression phase, marking empty results as `NOOP`.
- **Direct Index Management**: Automatically opens OpenSearch indices for archived/deleted sketches before export and closes them afterwards. This is done **directly** on the OpenSearch cluster without modifying the database status, ensuring your instance metadata remains untouched.
- **Shared Index Protection**: Smart index closing—indices are only closed if no other non-archived sketches in the database are currently using them.
- **Detailed Startup Insights**: At launch, the script provides a comprehensive breakdown of the instance state:
    - Total sketches in the database.
    - Number of sketches matching your status filters.
    - Number of sketches already processed (found in `manifest.csv`).
    - Explicit list of active filters (limit, ID range, etc.) explaining why the target count is lower.
- **High-Signal Console Output**:
    - **Colored Status**: SUCCESS (Green), NOOP (Yellow), and FAILED (Red) labels for immediate visual feedback.
    - **Detailed Success Reporting**: Displays absolute file paths, human-readable file sizes (e.g., 1.50 GB), and total event/timeline counts for every successful export.
    - **Real-time Progress monitoring**: Subprocess output (including progress bars) is streamed directly to the console as it happens.
- **Actionable Failure Diagnostics**: If an export fails, the script:
    - Identifies the likely cause (e.g., empty sketch, index timeout).
    - Provides a specific **recommendation** directly in the console and the manifest.
    - Automatically executes and displays `tsctl sketch-info` to provide deep context for debugging the failure.
- **Smart NOOP Handling**: Detects and skips sketches with no timelines or 0 events across all indices, marking them as `NOOP` to save time and cluster resources.
- **State Recovery**: Detects interrupted runs via a `.bulk_export_state.json` file and automatically ensures OpenSearch indices are closed before proceeding.
- **Graceful Shutdown**: Handles `Ctrl+C` (SIGINT) and `SIGTERM` by finishing the current batch and cleaning up indices before exiting.
- **Clean CLI Experience**: Suppresses noisy library warnings and internal OpenSearch library tracebacks.
- **Cluster Protection**: 
    - Monitored unarchiving (waits for indices to become ready).
    - **JVM Heap Guard**: Pauses if JVM heap usage on any node exceeds a safety threshold (default 85%).
    - **Relocation Guard**: Pauses if the cluster is undergoing heavy shard relocation.
    - Shard limit protection (pauses if the cluster is near its shard limit).
    - Cluster health checks (pauses if the cluster status is RED).
    - Settle periods after closing indices to allow for cluster cleanup.
- **Data Integrity**: Verifies that generated ZIP files are valid, meet minimum size requirements, and calculates a **SHA256 checksum** for each export to provide an audit trail.

## Design & Strategy
For a detailed breakdown of the technical approach, resource guards, and architectural options for high-scale clusters (including the proposed Pipeline Pipeline Architecture), see [DESIGN.md](DESIGN.md).

## Configuration & CLI Arguments
The script supports several command-line arguments:
- `--export-dir`: Destination for ZIP files and logs (Default: `/usr/local/src/timesketch/exports`).
- `--tmp-dir`: Temporary build directory (Default: `/tmp`).
- `--min-disk-gb`: Hard stop threshold for free space (Default: 50).
- `--min-ram-gb`: Pause processing if available system RAM falls below this threshold in GB (Default: 10).
- `--settle-delay`: Wait time after closing indices (Default: 60).
- `--include-deleted`: If set, sketches in the `deleted` state will also be exported.
- `--all-statuses`: If set, sketches will be exported regardless of their current status.
- `--annotated-only`: If set, only events with annotations (labels, stars, comments) will be exported.
- `--include-legacy`: If set, legacy events (missing `__ts_timeline_id`) will be included in the export.
- `--force`: Redo export even if a valid ZIP file already exists.
- `--oldest-first`: Process sketches from oldest to newest (ascending ID). Default is newest first.
- `--concurrency`: Number of sketches to process in a single metadata window (Default: 1).
- `--pipeline`: Enable Pipeline Architecture for maximum performance in high-shard clusters. This decouples metadata management (opening/closing indices) from data extraction.
- `--start-id`: Process sketches starting from this ID.
- `--end-id`: Process sketches up to this ID.
- `--limit`: Max number of sketches to process in this run.
- `--retry-failed`: If set, the script will automatically remove entries from `manifest.csv` that are marked as `Failed` and delete their associated ZIP files, allowing them to be retried in the current run.
- `--log-file`: Explicit path to the log file (Default: `bulk_export.log` in export directory).
- `--jvm-threshold`: JVM heap usage threshold as a float (0.0-1.0, Default: 0.85).
- `--shard-threshold`: Shard count threshold as a float (0.0-1.0, Default: 0.9).
- `--max-shards-per-node`: Manual override for shards per node limit.
- `--ignore-cluster-checks`: Disable JVM pressure, shard limit, and cluster health checks entirely.
- `--ignore-index-wait`: Skip waiting for OpenSearch indices to be ready after opening (use with caution).
- `--ignore-event-count`: Skip precise event counting and verification. This significantly speeds up the export process on large clusters by bypassing the pre-count and spot-check phases.

## Prerequisites (Critical)
For this utility to function correctly in high-scale environments, you **must** apply the following patches to your Timesketch installation. These patches fix core library bugs related to missing indices and system-level search operations.

**Run these commands from your host machine:**
```bash
# 1. Patch OpenSearch library (Fixes search crashes for system tools)
docker cp timesketch/lib/datastores/opensearch.py <CONTAINER>:/opt/venv/lib/python3.12/site-packages/timesketch/lib/datastores/opensearch.py

# 2. Patch Story Fetcher library (Fixes 403 Forbidden on Story exports)
docker cp timesketch/lib/stories/api_fetcher.py <CONTAINER>:/opt/venv/lib/python3.12/site-packages/timesketch/lib/stories/api_fetcher.py

# 3. Patch tsctl tool (Allows exporting archived sketches and resilient stories)
docker cp timesketch/tsctl.py <CONTAINER>:/usr/local/src/timesketch/timesketch/tsctl.py
```

## Performance Tuning

### Option 1: Pipeline Mode (Recommended for Speed)
Best for large migrations where "Open Index" latency is high. Decouples index management from data extraction.
```bash
python3 bulk_export.py --pipeline --concurrency 4 --all-statuses
```
- **Recommended Concurrency**: 4 to 8 (depending on cluster CPU/RAM).

### Option 2: Windowed Batching (Default)
Safe and predictable. Processes sketches in small batches.
```bash
python3 bulk_export.py --concurrency 2
```

## Usage

### Running in Docker

To execute the script within a Timesketch Docker container:

1.  **Copy the script into the container**:
    ```bash
    docker cp contrib/bulk_export/bulk_export.py timesketch-web:/usr/local/src/timesketch/contrib/bulk_export/
    ```

2.  **Ensure the Export Directory exists**:
    ```bash
    docker exec timesketch-web mkdir -p /usr/local/src/timesketch/exports
    ```

3.  **Execute the script**:
    ```bash
    # Basic run
    docker exec timesketch-web python3 /usr/local/src/timesketch/contrib/bulk_export/bulk_export.py
    
    # Run with arguments (e.g., enable pipeline and limit to 100)
    docker exec timesketch-web python3 /usr/local/src/timesketch/contrib/bulk_export/bulk_export.py --pipeline --concurrency 4 --limit 100
    ```

### Running from a Shell (Inside Container/Server)

If you are already in a terminal session on the Timesketch server, you can run the script directly. It automatically initializes the necessary Flask application context:

```bash
# Navigate to the project root
cd /usr/local/src/timesketch

# Run the script directly
python3 contrib/bulk_export/bulk_export.py --export-dir /path/to/exports
```

### Running in the Background (tmux)
For large-scale exports that take hours or days, it is highly recommended to run the script inside a `tmux` session to prevent the process from being killed if your SSH connection drops.

1.  **Start a new named session**:
    ```bash
    tmux new -s timesketch-export
    ```
2.  **Run your export command**:
    ```bash
    python3 bulk_export.py --export-dir /mnt/sketch_export/ --pipeline --concurrency 4 --ignore-event-count
    ```
3.  **Detach from the session**: Press `Ctrl + B`, then `D`.
4.  **Re-attach later**:
    ```bash
    tmux attach -t timesketch-export
    ```

### Monitoring Progress
The script provides detailed real-time logging. In **Pipeline Mode**, tasks are asynchronous, so IDs will finish out of order.

- **Console**: Observe parallel `SUCCESS` messages and progress bars. All log lines are prefixed with `[Sketch {id}]` for easy identification in parallel runs.
- **Manifest**: Watch the source of truth grow:
  ```bash
  tail -f /path/to/exports/manifest.csv
  ```
- **OpenSearch**: Monitor active export tasks:
  ```bash
  curl -XGET "http://localhost:9200/_cat/tasks?v&actions=*search*"
  ```

### Handling Retries
The script will **skip** any sketch ID that already exists in the `manifest.csv`. 

#### Automated Retries (Recommended)
Use the `--retry-failed` flag to automatically clear failed entries and retry them in a single run:
```bash
python3 bulk_export.py --retry-failed
```

#### Manual Retries
To manually retry a specific sketch:
1. Open `manifest.csv`.
2. Delete the row corresponding to the `sketch_id`.
3. Re-run the script.

## Manifest Schema
The `manifest.csv` contains the following fields:
- `sketch_id`: Unique identifier of the sketch.
- `name`: Name of the sketch at time of export.
- `status`: Original status before processing (ready/archived/deleted).
- `export_status`: Outcome (Success/Failed/NOOP).
- `error_msg`: Detailed error description if the export failed.
- `recommendation`: Actionable advice for resolving failures.
- `output_file`: Filename of the resulting ZIP archive.
- `size_bytes`: Final size of the exported file.
- `sha256`: SHA256 checksum of the exported ZIP.
