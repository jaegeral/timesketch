# Timesketch Bulk Sketch Export Utility

## Overview
This utility automates the large-scale export of sketches from a Timesketch instance. It is designed to handle thousands of sketches while protecting the underlying OpenSearch cluster from overload and managing different sketch states (Ready/Archived/Deleted).

## Key Features
- **Direct Index Management**: Automatically opens OpenSearch indices for archived/deleted sketches before export and closes them afterwards. This is done **directly** on the OpenSearch cluster without modifying the database status, ensuring your instance metadata remains untouched.
- **Shared Index Protection**: Smart index closing—indices are only closed if no other non-archived sketches in the database are currently using them.
- **Detailed Startup Insights**: At launch, the script provides a comprehensive breakdown of the instance state:
    - Total sketches in the database.
    - Number of sketches matching your status filters.
    - Number of sketches already processed (found in `manifest.csv`).
    - Explicit list of active filters (limit, ID range, etc.) explaining why the target count is lower.
- **High-Signal Console Output**:
    - **Colored Status**: SUCCESS (Green), NOOP (Yellow), and FAILED (Red) labels for immediate visual feedback.
    - **Detailed Success Reporting**: Displays absolute file paths and human-readable file sizes (e.g., 1.50 GB) for every successful export.
    - **Real-time Progress monitoring**: Subprocess output (including progress bars) is streamed directly to the console as it happens.
- **Actionable Failure Diagnostics**: If an export fails, the script:
    - Identifies the likely cause (e.g., empty sketch, index timeout).
    - Provides a specific **recommendation** directly in the console and the manifest.
    - Automatically executes and displays `tsctl sketch-info` to provide deep context for debugging the failure.
- **Smart NOOP Handling**: Detects and skips sketches with no timelines or 0 events across all indices, marking them as `NOOP` to save time and cluster resources.
- **State Recovery**: Detects interrupted runs via a `.bulk_export_state.json` file and automatically ensures OpenSearch indices are closed before proceeding.
- **Graceful Shutdown**: Handles `Ctrl+C` (SIGINT) and `SIGTERM` by finishing the current sketch and cleaning up indices before exiting.
- **Clean CLI Experience**: Suppresses noisy library warnings (like SQLAlchemy 2.0 deprecations).
- **Cluster Protection**: 
    - Monitored unarchiving (waits for indices to become ready).
    - **JVM Heap Guard**: Pauses if JVM heap usage on any node exceeds a safety threshold (default 85%).
    - **Relocation Guard**: Pauses if the cluster is undergoing heavy shard relocation.
    - Shard limit protection (pauses if the cluster is near its shard limit).
    - Cluster health checks (pauses if the cluster status is RED).
    - Settle periods after closing indices to allow for cluster cleanup.
- **Data Integrity**: Verifies that generated ZIP files are valid, meet minimum size requirements, and calculates a **SHA256 checksum** for each export to provide an audit trail.

## Design & Strategy
For a detailed breakdown of the technical approach, resource guards, and phase-by-phase implementation details, see [DESIGN.md](DESIGN.md).

## Configuration & CLI Arguments
The script supports several command-line arguments:
- `--export-dir`: Destination for ZIP files and logs (Default: `/usr/local/src/timesketch/exports`).
- `--tmp-dir`: Temporary build directory (Default: `/tmp`).
- `--min-disk-gb`: Hard stop threshold for free space (Default: 50).
- `--settle-delay`: Wait time after closing indices (Default: 60).
- `--include-deleted`: If set, sketches in the `deleted` state will also be exported.
- `--all-statuses`: If set, sketches will be exported regardless of their current status.
- `--annotated-only`: If set, only events with annotations (labels, stars, comments) will be exported.
- `--include-legacy`: If set, legacy events (missing `__ts_timeline_id`) will be included in the export.
- `--start-id`: Process sketches starting from this ID.
- `--end-id`: Process sketches up to this ID.
- `--limit`: Max number of sketches to process in this run.
- `--log-file`: Explicit path to the log file (Default: `bulk_export.log` in export directory).
- `--jvm-threshold`: JVM heap usage threshold as a float (0.0-1.0, Default: 0.85).
- `--shard-threshold`: Shard count threshold as a float (0.0-1.0, Default: 0.9).
- `--max-shards-per-node`: Manual override for shards per node limit.
- `--ignore-shard-limit`: Disable the shard limit safety check.
- `--ignore-cluster-checks`: Disable JVM pressure, shard limit, and cluster health checks entirely.
- `--ignore-index-wait`: Skip waiting for OpenSearch indices to be ready after opening (use with caution).

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
    
    # Run with arguments (e.g., include deleted sketches and limit to 100)
    docker exec timesketch-web python3 /usr/local/src/timesketch/contrib/bulk_export/bulk_export.py --include-deleted --limit 100
    ```

### Running from a Shell (Inside Container/Server)

If you are already in a terminal session on the Timesketch server, you can run the script directly. It automatically initializes the necessary Flask application context:

```bash
# Navigate to the project root
cd /usr/local/src/timesketch

# Run the script directly
python3 contrib/bulk_export/bulk_export.py --export-dir /path/to/exports
```

### Monitoring Progress
The script provides detailed console logging and writes to `bulk_export.log`. You can also monitor the `manifest.csv` in real-time:

```bash
tail -f /path/to/exports/manifest.csv
```

### Handling Retries
The script will **skip** any sketch ID that already exists in the `manifest.csv`. 

To retry a specific sketch:
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
