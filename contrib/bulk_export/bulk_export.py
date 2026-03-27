"""Batch export utility for Timesketch sketches.

This script automates the process of exporting multiple sketches from a
Timesketch instance. It handles active, archived, and deleted sketches,
maintaining a manifest of progress to allow for easy resumption.

Key features:
- Priority-based export (ready sketches first).
- Windowed metadata management (Open/Close batches) for high-shard clusters.
- Pipeline Architecture: Decoupled metadata management and parallel extraction.
- Parallel extraction and zipping via thread pools.
- Resource guards for disk space, JVM heap, and OpenSearch shard limits.
- ZIP integrity verification and SHA256 hashing.
- Manifest-driven tracking for robust batch processing.
- Database-level "Fast-Skip" for triage-only exports.
- Graceful shutdown via signal handling.
"""

import argparse
import click
import csv
import gc
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple, Optional

# Silence SQLAlchemy 2.0 deprecation warnings
os.environ["SQLALCHEMY_SILENCE_UBER_WARNING"] = "1"

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
is_shutting_down = False
orchestrator = None

# Configure logging
logger = logging.getLogger("bulk_export")


def setup_logging(export_dir: str, log_path: Optional[str] = None) -> None:
    """Configures logging to both console and a file.

    Args:
        export_dir: Path to the directory where logs should be saved.
        log_path: Optional explicit path to the log file.
    """
    os.makedirs(export_dir, exist_ok=True)
    if log_path:
        log_file = log_path
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
    else:
        log_file = os.path.join(export_dir, "bulk_export.log")

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False

    logging.getLogger("opensearch").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def get_file_sha256(file_path: str) -> str:
    """Calculates the SHA256 hash of a file.

    Args:
        file_path: Path to the file to hash.

    Returns:
        Hexadecimal representation of the SHA256 hash.
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(1048576), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def save_state(export_dir: str, sketch_ids: List[int]) -> None:
    """Saves the current batch of in-flight sketches to a file.

    Args:
        export_dir: Path to the export directory.
        sketch_ids: List of IDs currently being processed.
    """
    state_path = os.path.join(export_dir, STATE_FILE_NAME)
    with open(state_path, "w") as f:
        json.dump({"sketch_ids": sketch_ids}, f)


def clear_state(export_dir: str) -> None:
    """Removes the state file.

    Args:
        export_dir: Path to the export directory.
    """
    state_path = os.path.join(export_dir, STATE_FILE_NAME)
    if os.path.exists(state_path):
        os.remove(state_path)


# Global lock for OpenSearch metadata operations (Open/Close)
# This prevents multiple threads from hammering the Master node simultaneously
metadata_lock = threading.Lock()


def open_sketch_indices(sketch_id: int, datastore: OpenSearchDataStore) -> bool:
    """Opens OpenSearch indices for a sketch with Master node protection.

    Args:
        sketch_id: The ID of the sketch to open indices for.
        datastore: OpenSearch data store instance.

    Returns:
        True if all indices were opened successfully, False otherwise.
    """
    sketch = Sketch.query.get(sketch_id)
    if not sketch:
        return False

    search_indexes = {t.searchindex for t in sketch.timelines if t.searchindex}
    if not search_indexes:
        return True

    # Use the metadata lock to ensure we don't pile up requests
    with metadata_lock:
        # Final pre-check: Is the cluster healthy enough for a metadata change?
        try:
            h = datastore.client.cluster.health(request_timeout=30)
            if h.get("status") == "red":
                logger.warning("Cluster is RED. Aborting index open.")
                return False
            
            unassigned = h.get("unassigned_shards", 0)
            if unassigned > 10:
                logger.warning(
                    "Cluster in recovery (%d unassigned). Pausing open...",
                    unassigned
                )
                time.sleep(60)
                return False

            if h.get("number_of_pending_tasks", 0) > 2:
                logger.info("Master node busy. Waiting before open...")
                time.sleep(30)
        except Exception:
            pass

        for search_index in search_indexes:
            try:
                logger.info(
                    "  [Sketch %d] Opening OpenSearch index: %s", 
                    sketch_id, 
                    search_index.index_name
                )
                datastore.client.indices.open(index=search_index.index_name)
                # Mandatory cooldown after every single index command
                time.sleep(10)
            except Exception as e:
                if "index_not_closed_exception" in str(e).lower():
                    continue
                logger.error(
                    "  [Sketch %d] Failed to open index %s: %s", 
                    sketch_id, 
                    search_index.index_name, 
                    e
                )
                return False

    return True


def close_sketch_indices(sketch_id: int, datastore: OpenSearchDataStore) -> bool:
    """Closes OpenSearch indices for a sketch where safe.

    This implements Shared Index Protection and Master node safety.

    Args:
        sketch_id: The ID of the sketch to close indices for.
        datastore: OpenSearch data store instance.

    Returns:
        True if successful, False otherwise.
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
            if not timeline.sketch:
                continue
            status_obj = timeline.sketch.get_status
            status = status_obj.status if status_obj else "unknown"
            if status not in ("archived", "deleted"):
                can_be_closed = False
                break

        if can_be_closed:
            with metadata_lock:
                try:
                    logger.info(
                        "  Closing OpenSearch index: %s",
                        search_index.index_name,
                    )
                    datastore.client.indices.close(
                        index=search_index.index_name, ignore=[400, 404]
                    )
                    # Mandatory cooldown to let cluster map update
                    time.sleep(10)
                except Exception as e:
                    logger.error("  Failed to close index: %s", e)
    return True


def run_tsctl(command: List[str]) -> Tuple[bool, str]:
    """Executes a tsctl command and streams output in real-time.

    Args:
        command: The command and arguments to run (excluding 'tsctl').

    Returns:
        A tuple of (success_boolean, combined_output_string).
    """
    global is_shutting_down
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    full_cmd = ["tsctl"] + command
    
    combined_output = []
    process = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
        start_new_session=True, # Allows us to kill the whole group
    )

    try:
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                if is_shutting_down:
                    logger.warning("Shutdown detected. Terminating child tsctl...")
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    break
                
                clean_line = line.strip()
                if clean_line:
                    logger.info("      > %s", clean_line)
                    click.echo(f"      > {clean_line}")
                    combined_output.append(clean_line)
    except Exception as e:
        logger.error("Error reading tsctl output: %s", e)
    finally:
        if is_shutting_down and process.poll() is None:
            # Final attempt to kill if still alive
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                pass

    process.wait()
    return (process.returncode == 0), "\n".join(combined_output)


def recover_state(export_dir: str, datastore: OpenSearchDataStore) -> None:
    """Restores sketch state after an interrupted run."""
    state_path = os.path.join(export_dir, STATE_FILE_NAME)
    if not os.path.exists(state_path):
        return
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
            for sid in state.get("sketch_ids", []):
                logger.info("Recovering Sketch %d...", sid)
                close_sketch_indices(sid, datastore)
    except Exception:
        pass
    finally:
        clear_state(export_dir)


def signal_handler(sig: int, frame: Any) -> None:
    """Handles SIGINT/SIGTERM for graceful shutdown."""
    global is_shutting_down
    if is_shutting_down:
        sys.exit(1)
    is_shutting_down = True
    logger.warning("Shutdown signal received. Finishing current operations...")


def check_disk_space(path: str) -> int:
    """Returns free space in GB."""
    _, _, free = shutil.disk_usage(path)
    return free // (2**30)


def check_system_memory() -> int:
    """Returns available system memory in GB from /proc/meminfo.

    Returns:
        int: The amount of available memory in Gigabytes. Returns 999
            on failure to allow the script to continue.
    """
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if "MemAvailable" in line:
                    # Line looks like: MemAvailable:   12345678 kB
                    parts = line.split()
                    return int(parts[1]) // (1024 * 1024)
    except Exception:
        return 999  # Fallback to safe value if we can't read
    return 0


def check_shard_limit(
    datastore: OpenSearchDataStore, threshold: float, manual_limit: Optional[int] = None
) -> Tuple[bool, int, int]:
    """Checks cluster shard limits."""
    stats = datastore.client.cluster.stats(request_timeout=10)
    current = stats["indices"]["shards"]["total"]
    limit_per_node = manual_limit or 1000
    total_limit = limit_per_node * stats["_nodes"]["total"]
    return (current < (total_limit * threshold)), current, total_limit


def check_jvm_pressure(datastore: OpenSearchDataStore, threshold: float) -> bool:
    """Checks JVM heap pressure."""
    nodes_stats = datastore.client.nodes.stats(metric="jvm", request_timeout=10)
    for node_id, stats in nodes_stats["nodes"].items():
        if stats["jvm"]["mem"]["heap_used_percent"] > (threshold * 100):
            return False
    return True


def check_master_latency(datastore: OpenSearchDataStore, threshold_ms: int) -> Tuple[bool, int]:
    """Checks the Master node task queue latency.

    Args:
        datastore: OpenSearch data store instance.
        threshold_ms: Max allowed wait time in milliseconds.

    Returns:
        tuple: (bool indicating if healthy, actual latency in ms).
    """
    try:
        health = datastore.client.cluster.health(request_timeout=10)
        latency = health.get("task_max_waiting_in_queue_millis", 0)
        return (latency < threshold_ms), latency
    except Exception:
        return False, 999999


def get_processed_ids(manifest_path: str) -> set[int]:
    """Loads processed IDs from manifest."""
    processed_ids = set()
    if not os.path.exists(manifest_path):
        return processed_ids
    try:
        with open(manifest_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                processed_ids.add(int(row["sketch_id"]))
    except Exception:
        pass
    return processed_ids


def get_recommendation(error_msg: str) -> str:
    """Provides human-readable fix suggestions."""
    if not error_msg:
        return ""
    low_error = error_msg.lower()
    if "no timelines" in low_error: return "Delete empty sketch."
    if "timeout" in low_error: return "Increase wait time."
    if "integrity" in low_error: return "Verify OS data existence."
    return "Check logs."


def write_to_manifest(manifest_path: str, data: Dict[str, Any]) -> None:
    """Appends results to manifest."""
    file_exists = os.path.exists(manifest_path)
    fieldnames = [
        "sketch_id", "name", "status", "export_status", "error_msg",
        "recommendation", "output_file", "size_bytes", "sha256"
    ]
    with open(manifest_path, mode="a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists: writer.writeheader()
        writer.writerow(data)


def retry_failed(manifest_path: str, export_dir: str) -> None:
    """Removes failed entries from manifest and deletes associated ZIPs."""
    if not os.path.exists(manifest_path):
        return

    fieldnames = [
        "sketch_id", "name", "status", "export_status", "error_msg",
        "recommendation", "output_file", "size_bytes", "sha256"
    ]
    retry_ids = []
    rows_to_keep = []

    try:
        with open(manifest_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("export_status") == "Failed":
                    retry_ids.append(int(row["sketch_id"]))
                    # Delete the ZIP if it exists
                    out_file = row.get("output_file")
                    if out_file:
                        full_path = os.path.join(export_dir, out_file)
                        if os.path.exists(full_path):
                            logger.info("Deleting failed ZIP: %s", full_path)
                            os.remove(full_path)
                else:
                    rows_to_keep.append(row)

        if retry_ids:
            logger.info("Removing %d failed entries from manifest...", len(retry_ids))
            with open(manifest_path, mode="w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows_to_keep)
    except Exception as e:
        logger.error("Failed to clean manifest for retry: %s", e)


def wait_for_indices_ready(sketch_id: int, datastore: OpenSearchDataStore) -> bool:
    """Waits for sketch shards to initialize with Master node protection.

    Args:
        sketch_id: The ID of the sketch to wait for.
        datastore: OpenSearch data store instance.

    Returns:
        True if indices became ready within timeout, False otherwise.
    """
    sketch = Sketch.query.get(sketch_id)
    indices = [
        t.searchindex.index_name for t in sketch.timelines if t.searchindex
    ]
    if not indices:
        return True

    start = time.time()
    while time.time() - start < UNARCHIVE_TIMEOUT:
        if is_shutting_down:
            return False
        try:
            # Check cluster-wide stress first
            h_cluster = datastore.client.cluster.health(request_timeout=30)
            pending = h_cluster.get("number_of_pending_tasks", 0)
            status = h_cluster.get("status")
            unassigned = h_cluster.get("unassigned_shards", 0)

            if status == "red":
                logger.warning("Cluster is RED! Pausing all operations...")
                time.sleep(60)
                continue

            if unassigned > 10:
                logger.warning(
                    "Cluster is in recovery (%d unassigned shards). Waiting...",
                    unassigned,
                )
                time.sleep(60)
                continue

            if pending > 2:
                logger.info(
                    "Master node busy (%d tasks). Waiting for queue...", pending
                )
                time.sleep(30)
                continue

            # Now check the specific indices
            all_ready = True
            for idx in indices:
                try:
                    # Request minimal info for speed
                    h = datastore.client.cluster.health(
                        index=idx, request_timeout=30, level="indices"
                    )
                    # proceed on yellow (primaries up)
                    if h["status"] == "red":
                        all_ready = False
                        break
                except Exception:
                    all_ready = False
                    break

            if all_ready:
                return True

        except Exception as e:
            logger.warning("Health check request failed: %s", e)

        time.sleep(30)  # Increased interval for cluster safety
    return False


def get_open_indices(datastore: OpenSearchDataStore) -> set[str]:
    """Gets a set of all currently open indices in the cluster.

    Args:
        datastore: OpenSearch data store instance.

    Returns:
        A set of open index names.
    """
    try:
        logger.info("Fetching index list from OpenSearch (this may take a moment)...")
        # Use column filtering (h) to keep payload small, but avoid 's' (sort)
        res = datastore.client.cat.indices(format="json", h="index,status")
        open_indices = {i["index"] for i in res if i.get("status") == "open"}
        logger.info("Found %d open indices.", len(open_indices))
        return open_indices
    except Exception as e:
        logger.warning("Failed to get open indices list: %s", e)
        return set()


def human_readable_size(size_bytes: int) -> str:
    """Formats bytes."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} TB"


def process_sketch_wrapper(sketch_id: int, args: Any, datastore: OpenSearchDataStore):
    """Worker function for parallel export execution."""
    start_total = time.time()
    sketch = Sketch.query.get(sketch_id)
    status_obj = sketch.get_status
    orig_status = status_obj.status if status_obj else "unknown"

    indices = [
        t.searchindex.index_name for t in sketch.timelines if t.searchindex
    ]
    result = {
        "sketch_id": sketch_id,
        "name": sketch.name,
        "status": orig_status,
        "export_status": "Failed",
        "error_msg": "",
        "sha256": "",
        "output_file": f"sketch_{sketch_id}.zip",
        "size_bytes": 0,
        "events": 0,
        "timelines": len(sketch.timelines),
        "duration_total": 0.0,
        "duration_export": 0.0,
    }

    output_path = os.path.join(args.export_dir, result["output_file"])

    try:
        # Check existing file
        if (
            not args.force
            and os.path.exists(output_path)
            and zipfile.is_zipfile(output_path)
        ):
            result.update(
                {
                    "export_status": "Success",
                    "sha256": get_file_sha256(output_path),
                    "size_bytes": os.path.getsize(output_path),
                    "duration_total": time.time() - start_total,
                }
            )
            return result

        # Temp flip status for export
        if orig_status != "ready":
            sketch.set_status(status="ready")
            db_session.commit()

        # Run Export
        start_export = time.time()
        cmd = [
            "export-sketch",
            str(sketch_id),
            "--method",
            "direct",
            "--filename",
            output_path,
        ]
        if args.annotated_only:
            cmd.append("--annotated-only")
        if args.include_legacy:
            cmd.append("--include-legacy")
        if args.ignore_event_count:
            cmd.append("--ignore-event-count")

        success, output = run_tsctl(cmd)
        result["duration_export"] = time.time() - start_export

        if (
            success
            and os.path.exists(output_path)
            and zipfile.is_zipfile(output_path)
        ):
            # Parse event count from output
            event_match = re.search(r"Verification: (\d+) events", output)
            events_found = int(event_match.group(1)) if event_match else 0

            result.update(
                {
                    "export_status": "Success",
                    "sha256": get_file_sha256(output_path),
                    "size_bytes": os.path.getsize(output_path),
                    "events": events_found,
                }
            )
        else:
            result["error_msg"] = "tsctl failed or integrity check failed."

    except Exception as e:
        result["error_msg"] = str(e)
    finally:
        if orig_status != "ready":
            try:
                sketch.set_status(status=orig_status)
                db_session.commit()
            except Exception:
                pass

    result["duration_total"] = time.time() - start_total
    return result


class ExportOrchestrator:
    """Orchestrates the export pipeline.

    Metadata Opening -> Parallel Export -> Metadata Closing.
    """

    def __init__(
        self,
        sketches: List[Sketch],
        args: Any,
        datastore: OpenSearchDataStore,
        manifest_path: str,
        open_os_indices: set[str],
    ):
        self.sketches = sketches
        self.args = args
        self.datastore = datastore
        self.manifest_path = manifest_path

        self.pending_queue = queue.Queue()
        self.ready_queue = queue.Queue()
        self.instant_ready_queue = queue.Queue()  # Buffer for already-open sketches
        self.cleanup_queue = queue.Queue()

        self.open_indices_count = 0
        self.lock = threading.Lock()
        self.producer_done = False

        self.total_to_process = len(sketches)
        self.processed_in_session = 0

        self.success_count = 0
        self.noop_count = 0
        self.total_bytes = 0
        self.success_events = 0
        self.success_timelines = 0

        # Timing data: sketch_id -> {phase -> duration}
        self.timings = {}

        for s in sketches:
            indices = {
                t.searchindex.index_name for t in s.timelines if t.searchindex
            }
            # If all indices for this sketch are already open, go to buffer
            if indices and indices.issubset(open_os_indices):
                logger.info(
                    "  Sketch %d indices are already open. Buffering for READY.",
                    s.id,
                )
                self.instant_ready_queue.put(s.id)
            else:
                self.pending_queue.put(s.id)

    def producer_thread(self):
        """Sequentially opens indices for sketches."""
        global is_shutting_down
        try:
            while (not self.pending_queue.empty() or not self.instant_ready_queue.empty()) and not is_shutting_down:
                
                # 1. Exhaust instant_ready_queue first (fast path)
                # These are already open, so they don't count towards our 
                # "opening budget". We feed them to the workers instantly.
                while not self.instant_ready_queue.empty():
                    sketch_id = self.instant_ready_queue.get()
                    logger.info("  Sketch %d (ALREADY OPEN) -> READY", sketch_id)
                    self.ready_queue.put(sketch_id)
                    self.timings[sketch_id] = {"duration_open": 0.0}

                # 2. Check if we have anything else to do
                if self.pending_queue.empty():
                    break

                # 3. Buffer control for Pending (Archived) sketches
                # Only block if the ready_queue is sufficiently full to prevent worker starvation.
                if self.ready_queue.qsize() >= (self.args.concurrency * 2):
                    time.sleep(5)
                    continue
                
                # If we are strictly opening archived ones, respect the concurrency limit
                if self.open_indices_count >= self.args.concurrency and not self.args.open_indices_only:
                    time.sleep(5)
                    continue

                # Resource Guards
                for path in [self.args.export_dir, self.args.tmp_dir]:
                    if check_disk_space(path) < self.args.min_disk_gb:
                        logger.critical("LOW DISK SPACE on %s. Stopping.", path)
                        is_shutting_down = True
                        return

                # RAM Guard: Pause if system RAM is low
                if check_system_memory() < self.args.min_ram_gb:
                    logger.warning(
                        "LOW SYSTEM RAM (Below %d GB). Pausing metadata production...",
                        self.args.min_ram_gb
                    )
                    time.sleep(30)
                    continue

                if not self.args.ignore_cluster_checks:
                    if not check_jvm_pressure(self.datastore, self.args.jvm_threshold):
                        logger.warning("JVM pressure high. Waiting...")
                        time.sleep(60)
                        continue
                    
                    is_ok, latency = check_master_latency(self.datastore, self.args.max_master_wait)
                    if not is_ok:
                        logger.warning(
                            "Master node queue latency high (%d ms). Waiting...", 
                            latency
                        )
                        time.sleep(30)
                        continue

                    safe, _, _ = check_shard_limit(
                        self.datastore,
                        self.args.shard_threshold,
                        self.args.max_shards_per_node,
                    )
                    if not safe:
                        logger.warning("Shard count high. Waiting...")
                        time.sleep(60)
                        continue

                sketch_id = self.pending_queue.get()
                sketch = Sketch.query.get(sketch_id)
                status_obj = sketch.get_status
                orig_status = status_obj.status if status_obj else "unknown"

                # Check if we can skip before opening
                output_path = os.path.join(
                    self.args.export_dir, f"sketch_{sketch_id}.zip"
                )
                if (
                    not self.args.force
                    and os.path.exists(output_path)
                    and zipfile.is_zipfile(output_path)
                ):
                    self.ready_queue.put(sketch_id)
                    continue

                start_open = time.time()
                if orig_status != "ready":
                    if not open_sketch_indices(sketch_id, self.datastore):
                        logger.error("Failed to open indices for %d", sketch_id)
                    else:
                        if not self.args.ignore_index_wait:
                            wait_for_indices_ready(sketch_id, self.datastore)
                        with self.lock:
                            self.open_indices_count += 1

                self.timings[sketch_id] = {
                    "duration_open": time.time() - start_open
                }
                self.ready_queue.put(sketch_id)
        finally:
            self.producer_done = True
            logger.info("Producer thread finished.")

    def worker_thread(self):
        """Parallel data extraction."""
        global is_shutting_down
        logger.info("Worker thread starting...")
        while not is_shutting_down:
            try:
                # Use a short timeout to allow checking shutdown flag
                sketch_id = self.ready_queue.get(timeout=10)
            except queue.Empty:
                if not self.producer_done:
                    # Producer is still working, just wait
                    continue
                else:
                    # Everything is done
                    break

            # Heartbeat & Session Progress
            with self.lock:
                self.processed_in_session += 1
                current_count = self.processed_in_session

            logger.info(
                "Session Progress: [%d/%d] picked up Sketch %d.",
                current_count, self.total_to_process, sketch_id
            )
            
            # Safety check: Don't export if the cluster is RED
            # (Quick check, no timeout needed as producer manages health)
            try:
                h = self.datastore.client.cluster.health(request_timeout=5)
                if h.get("status") == "red":
                    logger.warning("Cluster RED. Worker for %d waiting...", sketch_id)
                    time.sleep(30)
                    self.ready_queue.put(sketch_id)
                    continue
            except Exception:
                # If health check fails, proceed anyway - tsctl will handle it
                pass

            res = process_sketch_wrapper(sketch_id, self.args, self.datastore)

            # Record result
            with self.lock:
                if res["export_status"] == "Success":
                    self.success_count += 1
                    self.total_bytes += res["size_bytes"]
                    self.success_events += res["events"]
                    self.success_timelines += res["timelines"]
                    click.echo(
                        click.style(f"  SUCCESS: Sketch {res['sketch_id']}", fg="green")
                    )
                elif res["export_status"] == "NOOP":
                    self.noop_count += 1
                    click.echo(
                        click.style(
                            f"  NOOP: Sketch {res['sketch_id']} ({res['error_msg']})",
                            fg="yellow",
                        )
                    )
                else:
                    click.echo(
                        click.style(
                            f"  FAILED: Sketch {res['sketch_id']} - {res['error_msg']}",
                            fg="red",
                        )
                    )

                # Store worker timings
                if sketch_id in self.timings:
                    self.timings[sketch_id].update(
                        {
                            "duration_total": res["duration_total"],
                            "duration_export": res["duration_export"],
                        }
                    )

                write_to_manifest(
                    self.manifest_path,
                    {
                        "sketch_id": res["sketch_id"],
                        "name": res["name"],
                        "status": res["status"],
                        "export_status": res["export_status"],
                        "error_msg": res["error_msg"],
                        "recommendation": get_recommendation(res["error_msg"]),
                        "output_file": (
                            res["output_file"]
                            if res["export_status"] == "Success"
                            else ""
                        ),
                        "size_bytes": res["size_bytes"],
                        "sha256": res["sha256"],
                    },
                )

            self.cleanup_queue.put(sketch_id)
            
            # Prevent memory creep in long-running threads
            gc.collect()

    def cleaner_thread(self):
        """Sequentially closes indices."""
        global is_shutting_down
        logger.info("Cleaner thread starting...")
        while not is_shutting_down:
            try:
                sketch_id = self.cleanup_queue.get(timeout=5)
            except queue.Empty:
                if self.producer_done and self.ready_queue.empty():
                    # Check one last time if worker is finished with cleanup_queue
                    if self.cleanup_queue.empty():
                        break
                continue

            sketch = Sketch.query.get(sketch_id)
            status_obj = sketch.get_status
            orig_status = status_obj.status if status_obj else "unknown"

            # Master Latency Guard: Pause if Master is struggling with metadata
            if not self.args.ignore_cluster_checks:
                while True:
                    is_ok, latency = check_master_latency(
                        self.datastore, self.args.max_master_wait
                    )
                    if is_ok:
                        break
                    logger.warning(
                        "Master node queue latency high (%d ms). Cleaner pausing...", 
                        latency
                    )
                    time.sleep(30)

            start_close = time.time()
            if orig_status != "ready":
                logger.info(
                    "  Closing indices for Sketch %d because database status is '%s'.",
                    sketch_id, orig_status
                )
                close_sketch_indices(sketch_id, self.datastore)
                time.sleep(5)  # Settle cluster

            # Always release the slot, even if we didn't close indices
            with self.lock:
                self.open_indices_count -= 1

            # Final timing log
            t = self.timings.get(sketch_id, {})
            duration_close = time.time() - start_close
            timing_msg = (
                f"  [Timing] Sketch {sketch_id} | "
                f"Total: {t.get('duration_total', 0):.1f}s | "
                f"Open: {t.get('duration_open', 0):.1f}s | "
                f"Export: {t.get('duration_export', 0):.1f}s | "
                f"Close: {duration_close:.1f}s"
            )
            logger.info(timing_msg)

    def run(self):
        threads = []
        t_prod = threading.Thread(target=self.producer_thread)
        t_clean = threading.Thread(target=self.cleaner_thread)
        threads.extend([t_prod, t_clean])

        for _ in range(self.args.concurrency):
            t_work = threading.Thread(target=self.worker_thread)
            threads.append(t_work)

        for t in threads:
            t.start()
        for t in threads:
            t.join()


def run_export() -> None:
    """Main execution loop."""
    parser = argparse.ArgumentParser(description="Bulk Sketch Export Tool")
    parser.add_argument("--export-dir", default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--tmp-dir", default=DEFAULT_TMP_DIR)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--retry-failed", action="store_true", help="Retry failed exports from manifest.csv"
    )
    parser.add_argument(
        "--pipeline", action="store_true", help="Enable Pipeline Architecture"
    )
    parser.add_argument(
        "--min-disk-gb", type=int, default=MIN_DISK_SPACE_GB
    )
    parser.add_argument(
        "--min-ram-gb", type=int, default=10, help="Pause if available RAM is below this GB"
    )
    parser.add_argument(
        "--max-master-wait", type=int, default=10000, 
        help="Pause if Master node queue wait time exceeds this ms (Default: 10000)"
    )
    parser.add_argument(
        "--settle-delay", type=int, default=SETTLE_DELAY
    )
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--all-statuses", action="store_true")
    parser.add_argument("--annotated-only", action="store_true")
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--oldest-first",
        action="store_true",
        help="Process sketches from oldest to newest (ascending ID)",
    )
    parser.add_argument("--start-id", type=int)
    parser.add_argument("--end-id", type=int)
    parser.add_argument(
        "--jvm-threshold", type=float, default=MAX_JVM_THRESHOLD
    )
    parser.add_argument(
        "--shard-threshold", type=float, default=MAX_SHARDS_THRESHOLD
    )
    parser.add_argument("--max-shards-per-node", type=int)
    parser.add_argument("--ignore-shard-limit", action="store_true")
    parser.add_argument("--ignore-cluster-checks", action="store_true")
    parser.add_argument("--ignore-index-wait", action="store_true")
    parser.add_argument("--ignore-event-count", action="store_true")
    parser.add_argument(
        "--open-indices-only",
        action="store_true",
        help="Only export sketches that already have all indices open in OpenSearch.",
    )
    parser.add_argument("--log-file")
    args = parser.parse_args()

    setup_logging(args.export_dir, args.log_file)
    manifest_path = os.path.join(args.export_dir, "manifest.csv")

    if args.retry_failed:
        retry_failed(manifest_path, args.export_dir)

    datastore = OpenSearchDataStore()
    recover_state(args.export_dir, datastore)
    signal.signal(signal.SIGINT, signal_handler)

    processed_ids = get_processed_ids(manifest_path)
    q = Sketch.query
    if not args.all_statuses:
        st = ["ready", "archived", "active", "open", "new"]
        if args.include_deleted: st.append("deleted")
        q = q.filter(Sketch.status.any(Sketch.Status.status.in_(st)))
    if args.start_id: q = q.filter(Sketch.id >= args.start_id)
    if args.oldest_first: q = q.order_by(Sketch.id.asc())
    else: q = q.order_by(Sketch.id.desc())
    
    to_process = [s for s in q.all() if s.id not in processed_ids]
    
    # Fast path: Only process sketches already open in OpenSearch
    if args.open_indices_only:
        logger.info("Filtering for sketches with ALREADY OPEN indices...")
        open_os_indices = get_open_indices(datastore)
        filtered_process = []
        for s in to_process:
            indices = {t.searchindex.index_name for t in s.timelines if t.searchindex}
            if indices and indices.issubset(open_os_indices):
                filtered_process.append(s)
        to_process = filtered_process
        logger.info("Filtered down to %d sketches.", len(to_process))

    if args.limit: to_process = to_process[:args.limit]

    logger.info("Targeting %d sketches. Pipeline: %s", len(to_process), args.pipeline)

    if args.pipeline:
        open_os_indices = get_open_indices(datastore)
        orch = ExportOrchestrator(
            to_process, args, datastore, manifest_path, open_os_indices
        )
        orch.run()
        success_count = orch.success_count
        noop_count = orch.noop_count
        total_bytes = orch.total_bytes
        success_events = orch.success_events
        success_timelines = orch.success_timelines
    else:
        # Standard Windowed Batching
        global is_shutting_down
        success_count = 0
        total_bytes = 0
        success_events = 0
        success_timelines = 0
        noop_count = 0
        for i in range(0, len(to_process), args.concurrency):
            if is_shutting_down:
                break
            batch = to_process[i : i + args.concurrency]
            batch_ids = [s.id for s in batch]
            save_state(args.export_dir, batch_ids)

            # Resource Guards
            for path in [args.export_dir, args.tmp_dir]:
                if check_disk_space(path) < args.min_disk_gb:
                    logger.critical("LOW DISK SPACE on %s. Stopping.", path)
                    clear_state(args.export_dir)
                    return

            if not args.ignore_cluster_checks:
                while not check_jvm_pressure(datastore, args.jvm_threshold):
                    logger.warning("JVM pressure high. Pausing...")
                    time.sleep(120)

                safe, current, limit = check_shard_limit(
                    datastore, args.shard_threshold, args.max_shards_per_node
                )
                if not safe:
                    logger.warning(
                        "Shard count high (%d/%d). Pausing...", current, limit
                    )
                    time.sleep(300)
                    # Retry same batch
                    i -= args.concurrency
                    continue

                try:
                    health = datastore.client.cluster.health(request_timeout=10)
                    if health["status"] == "red":
                        logger.warning("Cluster status RED. Pausing...")
                        time.sleep(300)
                        i -= args.concurrency
                        continue
                except Exception:
                    pass

            opened_ids = []
            for s in batch:
                st_obj = s.get_status
                if (st_obj.status if st_obj else "unknown") != "ready":
                    if open_sketch_indices(s.id, datastore): opened_ids.append(s.id)
            if opened_ids:
                time.sleep(10)
                if not args.ignore_index_wait:
                    for oid in opened_ids:
                        wait_for_indices_ready(oid, datastore)
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                results = list(
                    executor.map(
                        lambda sid: process_sketch_wrapper(
                            sid, args, datastore
                        ),
                        batch_ids,
                    )
                )
            for res in results:
                if res["export_status"] == "Success":
                    success_count += 1
                    total_bytes += res["size_bytes"]
                    success_events += res.get("events", 0)
                    success_timelines += res.get("timelines", 0)
                    click.echo(
                        click.style(
                            f"  SUCCESS: Sketch {res['sketch_id']}", fg="green"
                        )
                    )
                elif res["export_status"] == "NOOP":
                    noop_count += 1
                    click.echo(
                        click.style(
                            f"  NOOP: Sketch {res['sketch_id']} ({res['error_msg']})",
                            fg="yellow",
                        )
                    )
                else:
                    click.echo(
                        click.style(
                            f"  FAILED: Sketch {res['sketch_id']} - {res['error_msg']}",
                            fg="red",
                        )
                    )
                write_to_manifest(
                    manifest_path,
                    {
                        "sketch_id": res["sketch_id"],
                        "name": res["name"],
                        "status": res["status"],
                        "export_status": res["export_status"],
                        "error_msg": res["error_msg"],
                        "recommendation": get_recommendation(res["error_msg"]),
                        "output_file": (
                            res["output_file"]
                            if res["export_status"] == "Success"
                            else ""
                        ),
                        "size_bytes": res["size_bytes"],
                        "sha256": res["sha256"],
                    },
                )
                if res["status"] != "ready":
                    close_sketch_indices(res["sketch_id"], datastore)
            clear_state(args.export_dir)

    summary_line = (
        f"BULK EXPORT SUMMARY: Success: {success_count} | "
        f"NOOP: {noop_count} | Vol: {total_bytes / (1024**3):.2f} GB"
    )
    detail_line = f"Events: {success_events:,} | Timelines: {success_timelines}"

    logger.info(summary_line)
    logger.info(detail_line)

    click.echo(click.style("-" * 50, fg="white"))
    click.echo(click.style("BULK EXPORT SUMMARY", bold=True))
    click.echo(click.style(summary_line, fg="green"))
    click.echo(click.style(detail_line, fg="cyan"))
    click.echo(click.style("-" * 50, fg="white"))

if __name__ == "__main__":
    app = create_app()
    with app.app_context(): run_export()
