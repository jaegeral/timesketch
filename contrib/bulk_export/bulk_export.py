# Copyright 2026 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Batch export utility for Timesketch sketches.

This script automates the process of exporting multiple sketches from a
Timesketch instance. It handles active, archived, and deleted sketches,
maintaining a manifest of progress to allow for easy resumption.

Key features:
- Priority-based export (ready sketches first).
- Automatic unarchiving and restoration using tsctl commands.
- Resource guards for disk space, JVM heap, and OpenSearch shard limits.
- ZIP integrity verification and SHA256 hashing.
- Manifest-driven tracking for robust batch processing.
- Graceful shutdown via signal handling.
- State recovery for interrupted operations.
"""

import argparse
import click
import csv
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile

from typing import Any, Dict, List, Tuple, Optional

# Silence SQLAlchemy 2.0 deprecation warnings
os.environ["SQLALCHEMY_SILENCE_UBER_WARNING"] = "1"

from sqlalchemy import and_
from timesketch.app import create_app
from timesketch.models.sketch import Sketch
from timesketch.models import db_session
from timesketch.lib.datastores.opensearch import OpenSearchDataStore

# Default Configuration
DEFAULT_EXPORT_DIR = "/usr/local/src/timesketch/exports"
DEFAULT_TMP_DIR = "/tmp"
SETTLE_DELAY = 60  # Seconds to wait after re-archiving
UNARCHIVE_TIMEOUT = 300  # 5 minutes
MIN_DISK_SPACE_GB = 50  # Hard stop if less than this is available
MAX_SHARDS_THRESHOLD = 0.9  # Pause if shard count exceeds 90% of limit
MAX_JVM_THRESHOLD = 0.85  # Pause if JVM heap usage exceeds 85%
STATE_FILE_NAME = ".bulk_export_state.json"

# Global state for signal handling
current_processing_sketch_id: Optional[int] = None
original_sketch_status: Optional[str] = None
is_shutting_down = False

# Configure logging
logger = logging.getLogger("bulk_export")


def setup_logging(export_dir: str) -> None:
    """Configures logging to both console and a file in the export directory.

    Args:
        export_dir (str): Path to the directory where logs should be saved.
    """
    os.makedirs(export_dir, exist_ok=True)
    log_file = os.path.join(export_dir, "bulk_export.log")

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # File Handler
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)

    # Stream Handler
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)


def get_file_sha256(file_path: str) -> str:
    """Calculates the SHA256 hash of a file.

    Args:
        file_path (str): Path to the file to hash.

    Returns:
        str: Hexadecimal representation of the SHA256 hash.
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in 1MB chunks
        for byte_block in iter(lambda: f.read(1048576), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def save_state(export_dir: str, sketch_id: int, status: str) -> None:
    """Saves the current processing state to a file.

    Args:
        export_dir (str): Path to the export directory.
        sketch_id (int): The ID of the sketch currently being processed.
        status (str): The original status of the sketch.
    """
    state_path = os.path.join(export_dir, STATE_FILE_NAME)
    with open(state_path, "w") as f:
        json.dump({"sketch_id": sketch_id, "original_status": status}, f)


def clear_state(export_dir: str) -> None:
    """Removes the state file.

    Args:
        export_dir (str): Path to the export directory.
    """
    state_path = os.path.join(export_dir, STATE_FILE_NAME)
    if os.path.exists(state_path):
        os.remove(state_path)


def open_sketch_indices(sketch_id: int, datastore: OpenSearchDataStore) -> bool:
    """Opens OpenSearch indices for a sketch without updating DB status.

    Args:
        sketch_id (int): The ID of the sketch to open indices for.
        datastore (OpenSearchDataStore): OpenSearch data store instance.

    Returns:
        bool: True if all indices were opened successfully, False otherwise.
    """
    sketch = Sketch.query.get(sketch_id)
    if not sketch:
        logger.error("  Sketch %d not found.", sketch_id)
        return False

    search_indexes = {t.searchindex for t in sketch.timelines if t.searchindex}
    for search_index in search_indexes:
        try:
            logger.info("  Opening OpenSearch index: %s", search_index.index_name)
            datastore.client.indices.open(
                index=search_index.index_name, ignore=[400, 404]
            )
        except Exception as e:
            logger.error("  Failed to open index %s: %s", search_index.index_name, e)
            return False
    return True


def close_sketch_indices(sketch_id: int, datastore: OpenSearchDataStore) -> bool:
    """Closes OpenSearch indices for a sketch where safe (Shared Index Protection).

    Args:
        sketch_id (int): The ID of the sketch to close indices for.
        datastore (OpenSearchDataStore): OpenSearch data store instance.

    Returns:
        bool: True if successful, False otherwise.
    """
    sketch = Sketch.query.get(sketch_id)
    if not sketch:
        return False

    search_indexes = {t.searchindex for t in sketch.timelines if t.searchindex}
    for search_index in search_indexes:
        can_be_closed = True
        for timeline in search_index.timelines:
            if timeline.sketch_id == sketch_id:
                continue
            if timeline.sketch.get_status.status not in ("archived", "deleted"):
                can_be_closed = False
                logger.info(
                    "  Index %s is used by active sketch %d. Keeping open.",
                    search_index.index_name,
                    timeline.sketch_id,
                )
                break

        if can_be_closed:
            try:
                logger.info("  Closing OpenSearch index: %s", search_index.index_name)
                datastore.client.indices.close(
                    index=search_index.index_name, ignore=[400, 404]
                )
            except Exception as e:
                logger.error(
                    "  Failed to close index %s: %s", search_index.index_name, e
                )
    return True


def run_tsctl(command: List[str]) -> Tuple[bool, str]:
    """Executes a tsctl command and logs the output.

    Args:
        command (List[str]): The command and arguments to run.

    Returns:
        Tuple[bool, str]: A tuple (success_boolean, combined_output).
    """
    env = os.environ.copy()
    env["SQLALCHEMY_SILENCE_UBER_WARNING"] = "1"

    full_cmd = ["tsctl"] + command
    logger.info("  Running: %s", " ".join(full_cmd))
    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    combined_output = f"{result.stdout.strip()}\n{result.stderr.strip()}".strip()
    if result.returncode != 0:
        logger.error("  tsctl command failed: %s", result.stderr.strip())
        return False, combined_output
    return True, combined_output


def recover_state(export_dir: str, datastore: OpenSearchDataStore) -> None:
    """Restores sketch state (closes indices) after an interrupted run.

    Args:
        export_dir (str): Path to the export directory.
        datastore (OpenSearchDataStore): OpenSearch data store instance.
    """
    state_path = os.path.join(export_dir, STATE_FILE_NAME)
    if not os.path.exists(state_path):
        return

    try:
        with open(state_path, "r") as f:
            state = json.load(f)
            sketch_id = state.get("sketch_id")
            original_status = state.get("original_status")

            if sketch_id and original_status in ["archived", "deleted"]:
                logger.info(
                    "Found interrupted run. Closing Sketch %d indices...", sketch_id
                )
                close_sketch_indices(sketch_id, datastore)
                logger.info("  Interrupted state recovery attempt finished.")
    except Exception as e:
        logger.error("Failed to recover state: %s", str(e))
    finally:
        clear_state(export_dir)


def signal_handler(sig: int, frame: Any) -> None:
    """Handles SIGINT/SIGTERM for graceful shutdown.

    Args:
        sig (int): Signal number.
        frame (Any): Current stack frame.
    """
    global is_shutting_down
    if is_shutting_down:
        logger.critical("Forced shutdown requested. Exiting immediately.")
        sys.exit(1)

    is_shutting_down = True
    logger.warning(
        "Shutdown signal received. Finishing current sketch and restoring state..."
    )


def check_disk_space(path: str) -> int:
    """Calculates free disk space in Gigabytes.

    Args:
        path (str): The path to check disk space for.

    Returns:
        int: Free space in Gigabytes.
    """
    _, _, free = shutil.disk_usage(path)
    return free // (2**30)


def check_shard_limit(
    datastore: OpenSearchDataStore, threshold: float
) -> Tuple[bool, int, int]:
    """Verifies that the current shard count is within safe limits.

    Args:
        datastore (OpenSearchDataStore): OpenSearch data store instance.
        threshold (float): Percentage threshold of the shard limit.

    Returns:
        Tuple[bool, int, int]: A tuple containing (is_safe, current_shards, limit).
    """
    stats = datastore.client.cluster.stats()
    current_shards = stats["indices"]["shards"]["total"]
    try:
        settings = datastore.client.cluster.get_settings(include_defaults=True)
        limit_per_node = int(settings["defaults"]["cluster"]["max_shards_per_node"])
    except Exception:
        limit_per_node = 1000
    total_limit = limit_per_node * stats["_nodes"]["total"]
    return current_shards < (total_limit * threshold), current_shards, total_limit


def check_jvm_pressure(datastore: OpenSearchDataStore, threshold: float) -> bool:
    """Checks JVM heap usage across all nodes.

    Args:
        datastore (OpenSearchDataStore): OpenSearch data store instance.
        threshold (float): Percentage threshold of the JVM heap.

    Returns:
        bool: True if heap usage is within safe limits, False otherwise.
    """
    nodes_stats = datastore.client.nodes.stats(metric="jvm")
    for node_id, stats in nodes_stats["nodes"].items():
        heap_used = stats["jvm"]["mem"]["heap_used_percent"]
        if heap_used > (threshold * 100):
            logger.warning("Node %s JVM heap usage high: %d%%", node_id, heap_used)
            return False
    return True


def is_in_manifest(manifest_path: str, sketch_id: int) -> bool:
    """Checks if a specific sketch has already been processed.

    Args:
        manifest_path (str): Path to the manifest CSV file.
        sketch_id (int): The ID of the sketch to check.

    Returns:
        bool: True if the sketch is in the manifest, False otherwise.
    """
    if not os.path.exists(manifest_path):
        return False
    try:
        with open(manifest_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["sketch_id"] == str(sketch_id):
                    return True
    except Exception as e:
        logger.error("Failed to read manifest file: %s", str(e))
    return False


def get_recommendation(error_msg: str, sketch: Sketch) -> str:
    """Provides actionable recommendations based on common failure modes.

    Args:
        error_msg (str): The error message captured during export.
        sketch (Sketch): The sketch object that failed to export.

    Returns:
        str: A string containing a recommendation for resolution.
    """
    if not error_msg:
        return ""
    low_error = error_msg.lower()
    if "sketch has no timelines" in low_error:
        return "Verify if this investigation is active or should be deleted."
    if "indices failed to become ready" in low_error:
        return (
            "OpenSearch shards took too long to initialize. "
            "Try increasing UNARCHIVE_TIMEOUT."
        )
    if "no open indices" in low_error:
        return "All indices for this sketch appear to be missing from OpenSearch."
    if "low disk space" in low_error:
        return "Critical: Destination or /tmp volume is full."
    if "integrity check failed" in low_error:
        return (
            "The export ZIP is missing, corrupt, or nearly empty (<= 1KB). "
            "Check if the sketch has any data/events."
        )
    if "no mapping found for [datetime]" in low_error:
        return (
            "The OpenSearch index exists but has no data or no mapping for "
            "the datetime field. This sketch is effectively empty."
        )
    if "tsctl failed" in low_error:
        return "The underlying CLI command failed. Check logs for details."
    return "Check logs for details and verify sketch accessibility in the UI."


def write_to_manifest(manifest_path: str, data: Dict[str, Any]) -> None:
    """Appends a processing result to the manifest CSV.

    Args:
        manifest_path (str): Path to the manifest CSV file.
        data (Dict[str, Any]): A dictionary containing the result data.
    """
    file_exists = os.path.exists(manifest_path)
    fieldnames = [
        "sketch_id",
        "name",
        "status",
        "export_status",
        "error_msg",
        "recommendation",
        "output_file",
        "size_bytes",
        "sha256",
    ]
    try:
        with open(manifest_path, mode="a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)
    except Exception as e:
        logger.error("Failed to write to manifest: %s", str(e))


def wait_for_indices_ready(sketch_id: int, datastore: OpenSearchDataStore) -> bool:
    """Polls OpenSearch until all indices for a sketch are ready.

    Args:
        sketch_id (int): The ID of the sketch to wait for.
        datastore (OpenSearchDataStore): OpenSearch data store instance.

    Returns:
        bool: True if indices became ready within timeout, False otherwise.
    """
    sketch = Sketch.query.get(sketch_id)
    all_indices = list(
        {t.searchindex.index_name for t in sketch.timelines if t.searchindex}
    )
    if not all_indices:
        return True

    start_time = time.time()
    while time.time() - start_time < UNARCHIVE_TIMEOUT:
        cluster_health = datastore.client.cluster.health()
        relocating = cluster_health.get("relocating_shards", 0)
        if relocating > 10:
            logger.info("  Cluster busy relocating (%d). Waiting...", relocating)
        else:
            all_ready = True
            for index_name in all_indices:
                try:
                    health = datastore.client.cluster.health(index=index_name)
                    if health["status"] == "red":
                        all_ready = False
                        break
                except Exception:
                    all_ready = False
                    break
            if all_ready:
                return True
        time.sleep(15)
    return False


def human_readable_size(size_bytes: int) -> str:
    """Formats bytes into a human-readable string.

    Args:
        size_bytes (int): Number of bytes.

    Returns:
        str: Formatted string (e.g., '1.50 GB').
    """
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1
    return f"{size_bytes:.2f} {units[i]}"


def run_export() -> None:
    """Main execution loop for the bulk export process."""
    global current_processing_sketch_id, original_sketch_status

    parser = argparse.ArgumentParser(description="Bulk Sketch Export Tool")
    parser.add_argument(
        "--export-dir", default=DEFAULT_EXPORT_DIR, help="Destination directory"
    )
    parser.add_argument(
        "--tmp-dir", default=DEFAULT_TMP_DIR, help="Temporary build directory"
    )
    parser.add_argument(
        "--min-disk-gb",
        type=int,
        default=MIN_DISK_SPACE_GB,
        help="Minimum free space in GB",
    )
    parser.add_argument(
        "--settle-delay", type=int, default=SETTLE_DELAY, help="Settle period"
    )
    parser.add_argument(
        "--include-deleted", action="store_true", help="Include deleted sketches"
    )
    parser.add_argument("--start-id", type=int, help="Process starting from this ID")
    parser.add_argument("--end-id", type=int, help="Process up to this ID")
    parser.add_argument("--limit", type=int, help="Limit number of sketches")
    parser.add_argument(
        "--jvm-threshold",
        type=float,
        default=MAX_JVM_THRESHOLD,
        help="JVM usage threshold",
    )
    parser.add_argument(
        "--shard-threshold",
        type=float,
        default=MAX_SHARDS_THRESHOLD,
        help="Shard count threshold",
    )
    args = parser.parse_args()

    setup_logging(args.export_dir)
    manifest_path = os.path.join(args.export_dir, "manifest.csv")

    datastore = OpenSearchDataStore()
    recover_state(args.export_dir, datastore)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Query Sketches
    total_in_db = Sketch.query.count()

    query = Sketch.query
    statuses = ["ready", "archived"]
    if args.include_deleted:
        statuses.append("deleted")

    query = query.filter(Sketch.status.any(Sketch.Status.status.in_(statuses)))

    # Pre-filtering count for status
    total_matching_status = query.count()

    if args.start_id:
        query = query.filter(Sketch.id >= args.start_id)
    if args.end_id:
        query = query.filter(Sketch.id <= args.end_id)
    query = query.order_by(Sketch.id)

    # We load everything matching criteria (except limit) to calculate exclusions
    all_eligible = query.all()

    # Filter out what's already in the manifest
    to_process = [s for s in all_eligible if not is_in_manifest(manifest_path, s.id)]
    already_done_count = len(all_eligible) - len(to_process)

    # Apply limit if specified
    if args.limit and len(to_process) > args.limit:
        to_process = to_process[: args.limit]
        limit_applied = True
    else:
        limit_applied = False

    # Priority: Ready first
    to_process.sort(key=lambda s: 0 if s.get_status.status == "ready" else 1)

    total_target = len(to_process)
    processed_count = 0
    success_count = 0
    noop_count = 0
    total_bytes = 0

    logger.info("Instance Status:")
    logger.info("  Total sketches in database: %d", total_in_db)
    logger.info(
        "  Sketches matching status filter %s: %d", statuses, total_matching_status
    )
    logger.info("  Already processed: %d", already_done_count)

    filter_reasons = []
    if not args.include_deleted:
        msg = "deleted sketches excluded (use --include-deleted to include)"
        filter_reasons.append(msg)

    if args.start_id or args.end_id:
        msg = f"ID range filter active (start: {args.start_id}, end: {args.end_id})"
        filter_reasons.append(msg)

    if limit_applied:
        filter_reasons.append(f"Limit of {args.limit} applied")

    if filter_reasons:
        logger.info("Filters active:")
        for reason in filter_reasons:
            logger.info("  - %s", reason)

    start_msg = f"Starting bulk export for {total_target} sketches..."
    click.echo(click.style(start_msg, fg="cyan", bold=True))
    logger.info(start_msg)

    for sketch in to_process:
        if is_shutting_down:
            break

        processed_count += 1
        current_processing_sketch_id = sketch.id
        original_sketch_status = sketch.get_status.status
        save_state(args.export_dir, sketch.id, original_sketch_status)

        logger.info("Processing Sketch %d: %s", sketch.id, sketch.name)

        # Initialize result variables
        error_msg = ""
        export_status = "Failed"
        sha256_val = ""
        output_filename = f"sketch_{sketch.id}.zip"
        full_output_path = os.path.join(args.export_dir, output_filename)
        cmd_output = ""

        if not sketch.timelines:
            error_msg = "Sketch has no timelines."
            export_status = "NOOP"
            logger.warning("  %s skipping.", error_msg)
        else:
            # Resource Guards
            for path in [args.export_dir, args.tmp_dir]:
                if check_disk_space(path) < args.min_disk_gb:
                    logger.critical("LOW DISK SPACE on %s. Stopping.", path)
                    clear_state(args.export_dir)
                    return

            while not check_jvm_pressure(datastore, args.jvm_threshold):
                logger.warning("JVM pressure high. Pausing...")
                time.sleep(120)

            safe, current, limit = check_shard_limit(datastore, args.shard_threshold)
            if not safe:
                logger.warning(
                    "Shard count too high (%d/%d). Pausing...", current, limit
                )
                time.sleep(300)
                processed_count -= 1
                continue

            if datastore.client.cluster.health()["status"] == "red":
                logger.warning("Cluster status is RED. Pausing...")
                time.sleep(300)
                processed_count -= 1
                continue

            try:
                # Open indices directly
                if original_sketch_status in ["archived", "deleted"]:
                    logger.info("  Opening indices for sketch...")
                    if not open_sketch_indices(sketch.id, datastore):
                        raise RuntimeError("Failed to open OpenSearch indices.")

                    if not wait_for_indices_ready(sketch.id, datastore):
                        raise TimeoutError("Indices failed to become ready.")

                # Check if there are actually any events to export
                index_names = list(
                    {
                        t.searchindex.index_name
                        for t in sketch.timelines
                        if t.searchindex
                    }
                )
                total_events, _ = datastore.count(index_names)
                if total_events == 0:
                    error_msg = "Sketch indices are empty (0 events)."
                    export_status = "NOOP"
                    logger.warning("  %s skipping.", error_msg)
                    if original_sketch_status in ["archived", "deleted"]:
                        close_sketch_indices(sketch.id, datastore)
                else:
                    # Execution
                    cmd = [
                        "export-sketch",
                        str(sketch.id),
                        "--method",
                        "direct",
                        "--filename",
                        full_output_path,
                    ]
                    success, cmd_output = run_tsctl(cmd)
                    if not success:
                        error_msg = "tsctl export-sketch failed."
                    else:
                        if (
                            os.path.exists(full_output_path)
                            and zipfile.is_zipfile(full_output_path)
                            and os.path.getsize(full_output_path) > 1024
                        ):
                            export_status = "Success"
                            sha256_val = get_file_sha256(full_output_path)
                            total_bytes += os.path.getsize(full_output_path)
                        else:
                            error_msg = "Integrity check failed."

                    # Close indices directly
                    if original_sketch_status in ["archived", "deleted"]:
                        logger.info("  Closing indices to release resources...")
                        close_sketch_indices(sketch.id, datastore)
                        time.sleep(args.settle_delay)

            except Exception as e:
                error_msg = str(e)
                logger.error("  Error processing sketch %d: %s", sketch.id, e)
                if original_sketch_status in ["archived", "deleted"]:
                    try:
                        close_sketch_indices(sketch.id, datastore)
                    except Exception:
                        pass

        # Final Status Reporting
        if export_status == "Success":
            success_count += 1
            msg = f"  Result: SUCCESS for Sketch {sketch.id}"
            click.echo(click.style(msg, fg="green", bold=True))

            size_str = human_readable_size(os.path.getsize(full_output_path))
            click.echo(f"    Path: {full_output_path}")
            click.echo(f"    Size: {size_str}")

            if cmd_output:
                click.echo("    Command Output:")
                for line in cmd_output.splitlines():
                    click.echo(f"      > {line}")

            logger.info("  Result: SUCCESS for Sketch %d", sketch.id)
        elif export_status == "NOOP":
            noop_count += 1
            msg = f"  Result: NOOP for Sketch {sketch.id} ({error_msg})"
            click.echo(click.style(msg, fg="yellow", bold=True))
            logger.info("  Result: NOOP for Sketch %d", sketch.id)
        else:
            msg = (
                f"  Result: FAILED for Sketch {sketch.id}. "
                f"Reason: {error_msg or 'Unknown error'}"
            )
            click.echo(click.style(msg, fg="red", bold=True))

            if error_msg:
                rec = get_recommendation(error_msg, sketch)
                click.echo(f"    Recommendation: {rec}")
                logger.info("  Recommendation: %s", rec)

            # Provide sketch info to help debugging
            _, info_output = run_tsctl(["sketch-info", str(sketch.id)])
            if info_output:
                click.echo("    Sketch Info:")
                for line in info_output.splitlines():
                    click.echo(f"      | {line}")

            if cmd_output:
                click.echo("    Command Output:")
                for line in cmd_output.splitlines():
                    click.echo(f"      > {line}")

            logger.error(
                "  Result: FAILED for Sketch %d. Reason: %s",
                sketch.id,
                error_msg or "Unknown error",
            )

        write_to_manifest(
            manifest_path,
            {
                "sketch_id": sketch.id,
                "name": sketch.name,
                "status": original_sketch_status,
                "export_status": export_status,
                "error_msg": error_msg,
                "recommendation": get_recommendation(error_msg, sketch),
                "output_file": output_filename if export_status == "Success" else "",
                "size_bytes": (
                    os.path.getsize(full_output_path)
                    if os.path.exists(full_output_path)
                    else 0
                ),
                "sha256": sha256_val,
            },
        )
        clear_state(args.export_dir)

    failed_count = processed_count - success_count - noop_count
    summary_line = (
        f"Total: {total_target} | Processed: {processed_count} | "
        f"Success: {success_count} | NOOP: {noop_count} | "
        f"Failed: {failed_count} | Volume: {total_bytes / (1024**3):.2f} GB"
    )
    click.echo(click.style("-" * 50, fg="white"))
    click.echo(click.style("BULK EXPORT SUMMARY", bold=True))
    summary_color = "green" if success_count == total_target else "yellow"
    click.echo(click.style(summary_line, fg=summary_color))
    click.echo(click.style("-" * 50, fg="white"))

    logger.info("--------------------------------------------------")
    logger.info("BULK EXPORT SUMMARY")
    logger.info(
        "Total: %d | Processed: %d | Success: %d | NOOP: %d | Failed: %d | Volume: %.2f GB",
        total_target,
        processed_count,
        success_count,
        noop_count,
        failed_count,
        total_bytes / (1024**3),
    )
    logger.info("--------------------------------------------------")


if __name__ == "__main__":
    flask_app = create_app()
    with flask_app.app_context():
        run_export()
