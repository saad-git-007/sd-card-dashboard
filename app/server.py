#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DEFAULT_DESTINATION_DIR = os.environ.get(
    "DEFAULT_DESTINATION_DIR",
    "/media/intelirain/T9/pi-golden",
)
PISHRINK_PATH = os.environ.get("PISHRINK_PATH", "/usr/local/bin/pishrink.sh")

BASE_ENV = os.environ.copy()
BASE_ENV.update({"LANG": "C", "LC_ALL": "C", "LANGUAGE": "C"})

MAX_JOB_LOGS = 160
DD_PROGRESS_PATTERN = re.compile(
    r"(?P<bytes>[\d,]+)\s+bytes.*copied,\s*(?P<seconds>[\d.]+)\s*s"
)
IMAGE_SUFFIXES = (".img", ".img.gz", ".img.xz")
SDCARD_LABEL_PATTERN = re.compile(r"^SDcard(?P<number>\d+)$", re.IGNORECASE)
CPU_SAMPLE_LOCK = threading.Lock()
CPU_SAMPLE = None
DISK_SAMPLE_LOCK = threading.Lock()
DISK_SAMPLE = None
PHYSICAL_DISK_NAME_PATTERN = re.compile(
    r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$"
)


class CommandError(RuntimeError):
    def __init__(self, command, returncode, stdout="", stderr=""):
        super().__init__(self._build_message(command, returncode, stdout, stderr))
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @staticmethod
    def _build_message(command, returncode, stdout, stderr):
        joined = " ".join(command)
        detail = (stderr or stdout or "").strip()
        if detail:
            return f"Command failed ({returncode}): {joined} -> {detail}"
        return f"Command failed ({returncode}): {joined}"


def now_ts():
    return time.time()


def human_bytes(value):
    if value is None:
        return "n/a"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def sanitize_camera_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned or "camera"


def normalize_mountpoints(mountpoints):
    seen = []
    for mountpoint in mountpoints or []:
        if mountpoint and mountpoint not in seen:
            seen.append(mountpoint)
    return seen


def is_image_file(path):
    return any(str(path).endswith(suffix) for suffix in IMAGE_SUFFIXES)


def ensure_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def path_is_under(child_path, parent_path):
    try:
        child = Path(child_path).resolve(strict=False)
        parent = Path(parent_path).resolve(strict=False)
    except OSError:
        return False
    child_text = str(child)
    parent_text = str(parent)
    return child_text == parent_text or child_text.startswith(parent_text.rstrip("/") + "/")


def capacity_bucket(size_bytes):
    if not size_bytes:
        return None
    gib = size_bytes / float(1024 ** 3)
    if 28 <= gib <= 35:
        return "32 GB"
    if 58 <= gib <= 69:
        return "64 GB"
    return None


def read_cpu_sample():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return None

    if not first_line.startswith("cpu "):
        return None

    parts = first_line.split()[1:]
    if not parts:
        return None

    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None

    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return {"idle": idle, "total": sum(values)}


def system_cpu_usage_percent():
    global CPU_SAMPLE

    current_sample = read_cpu_sample()
    if current_sample is None:
        return None

    with CPU_SAMPLE_LOCK:
        previous_sample = CPU_SAMPLE
        CPU_SAMPLE = current_sample

    if previous_sample is None:
        time.sleep(0.1)
        next_sample = read_cpu_sample()
        if next_sample is None:
            return None
        with CPU_SAMPLE_LOCK:
            CPU_SAMPLE = next_sample
        previous_sample = current_sample
        current_sample = next_sample

    total_delta = current_sample["total"] - previous_sample["total"]
    idle_delta = current_sample["idle"] - previous_sample["idle"]
    if total_delta <= 0:
        return None

    usage_percent = (1 - (idle_delta / total_delta)) * 100
    return max(0.0, min(100.0, usage_percent))


def system_memory_snapshot():
    fields = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, raw_value = line.partition(":")
                if not _:
                    continue
                amount = raw_value.strip().split()[0]
                fields[key] = int(amount) * 1024
    except (OSError, ValueError, IndexError):
        return {
            "total_bytes": None,
            "used_bytes": None,
            "available_bytes": None,
            "used_percent": None,
        }

    total_bytes = fields.get("MemTotal")
    available_bytes = fields.get("MemAvailable")
    if available_bytes is None:
        available_bytes = (
            fields.get("MemFree", 0)
            + fields.get("Buffers", 0)
            + fields.get("Cached", 0)
        )

    if total_bytes is None:
        return {
            "total_bytes": None,
            "used_bytes": None,
            "available_bytes": None,
            "used_percent": None,
        }

    used_bytes = max(total_bytes - (available_bytes or 0), 0)
    used_percent = (used_bytes / total_bytes) * 100 if total_bytes else None
    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "available_bytes": available_bytes,
        "used_percent": used_percent,
    }


def read_disk_sample():
    total_sectors = 0
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                if not PHYSICAL_DISK_NAME_PATTERN.fullmatch(name):
                    continue
                read_sectors = int(parts[5])
                write_sectors = int(parts[9])
                total_sectors += read_sectors + write_sectors
    except (OSError, ValueError):
        return None

    return {"sectors": total_sectors, "timestamp": time.time()}


def system_disk_io_bps():
    global DISK_SAMPLE

    current_sample = read_disk_sample()
    if current_sample is None:
        return None

    with DISK_SAMPLE_LOCK:
        previous_sample = DISK_SAMPLE
        DISK_SAMPLE = current_sample

    if previous_sample is None:
        time.sleep(0.1)
        next_sample = read_disk_sample()
        if next_sample is None:
            return None
        with DISK_SAMPLE_LOCK:
            DISK_SAMPLE = next_sample
        previous_sample = current_sample
        current_sample = next_sample

    elapsed = max(current_sample["timestamp"] - previous_sample["timestamp"], 0.001)
    delta_sectors = max(current_sample["sectors"] - previous_sample["sectors"], 0)
    return (delta_sectors * 512) / elapsed


def run_command(command, check=True, capture_output=True):
    try:
        result = subprocess.run(
            command,
            capture_output=capture_output,
            text=True,
            env=BASE_ENV,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc
    if check and result.returncode != 0:
        raise CommandError(command, result.returncode, result.stdout, result.stderr)
    return result


def stream_command(command, on_line):
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=BASE_ENV,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc
    selector = selectors.DefaultSelector()
    buffers = {}

    for source_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is not None:
            selector.register(stream, selectors.EVENT_READ, data=source_name)
            buffers[source_name] = b""

    while selector.get_map():
        events = selector.select(timeout=0.25)
        if not events and process.poll() is not None:
            events = [(key, None) for key in list(selector.get_map().values())]

        for key, _ in events:
            stream = key.fileobj
            source_name = key.data
            chunk = os.read(stream.fileno(), 4096)

            if not chunk:
                remainder = buffers.pop(source_name, b"")
                if remainder:
                    line = remainder.decode("utf-8", errors="replace").strip()
                    if line:
                        on_line(source_name, line)
                selector.unregister(stream)
                continue

            buffer = buffers[source_name] + chunk.replace(b"\r", b"\n")
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                text = raw_line.decode("utf-8", errors="replace").strip()
                if text:
                    on_line(source_name, text)
            buffers[source_name] = buffer

    return process.wait()


def parse_dd_progress(line, total_bytes):
    match = DD_PROGRESS_PATTERN.search(line)
    if not match:
        return None

    processed = int(match.group("bytes").replace(",", ""))
    seconds = max(float(match.group("seconds")), 0.001)
    speed_bps = processed / seconds
    percent = min((processed / total_bytes) * 100, 100.0) if total_bytes else 0.0
    eta_seconds = None
    if total_bytes and speed_bps > 0 and processed <= total_bytes:
        eta_seconds = max((total_bytes - processed) / speed_bps, 0.0)

    return {
        "processed_bytes": processed,
        "total_bytes": total_bytes,
        "speed_bps": speed_bps,
        "eta_seconds": eta_seconds,
        "percent": percent,
    }


def compute_sha256(path, on_progress=None):
    file_path = Path(path)
    total_bytes = file_path.stat().st_size
    processed = 0
    hasher = hashlib.sha256()
    started_at = time.time()
    last_emit = 0.0

    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            processed += len(chunk)

            current_time = time.time()
            if on_progress and (current_time - last_emit >= 0.2 or processed == total_bytes):
                elapsed = max(current_time - started_at, 0.001)
                speed_bps = processed / elapsed
                eta_seconds = (
                    max((total_bytes - processed) / speed_bps, 0.0) if speed_bps else None
                )
                on_progress(
                    {
                        "processed_bytes": processed,
                        "total_bytes": total_bytes,
                        "speed_bps": speed_bps,
                        "eta_seconds": eta_seconds,
                        "percent": min((processed / total_bytes) * 100, 100.0)
                        if total_bytes
                        else 100.0,
                    }
                )
                last_emit = current_time

    return hasher.hexdigest()


def collect_mountpoints(node):
    mountpoints = normalize_mountpoints(node.get("mountpoints"))
    for child in node.get("children", []):
        for mountpoint in collect_mountpoints(child):
            if mountpoint not in mountpoints:
                mountpoints.append(mountpoint)
    return mountpoints


def is_standard_sdcard_partition(partition):
    if not partition:
        return False
    label = partition.get("label") or ""
    fstype = (partition.get("fstype") or "").lower()
    return (
        SDCARD_LABEL_PATTERN.fullmatch(label) is not None
        and fstype in ("vfat", "fat", "fat32")
    )


def discover_devices(destination_dir):
    result = run_command(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,KNAME,PATH,PKNAME,SIZE,TYPE,RM,HOTPLUG,MODEL,VENDOR,SERIAL,TRAN,MOUNTPOINTS,FSTYPE,LABEL",
        ]
    )
    payload = json.loads(result.stdout)
    raw_devices = []

    for device in payload.get("blockdevices", []):
        if device.get("type") != "disk":
            continue

        mountpoints = collect_mountpoints(device)
        is_external = bool(device.get("rm") or device.get("hotplug") or device.get("tran") == "usb")
        system_mounts = [mp for mp in mountpoints if mp in ("/", "/boot", "/boot/efi")]
        destination_matches = [
            mp for mp in mountpoints if destination_dir and path_is_under(destination_dir, mp)
        ]

        partitions = []
        for child in device.get("children", []):
            if child.get("type") != "part":
                continue
            partitions.append(
                {
                    "name": child.get("name"),
                    "path": child.get("path"),
                    "size_bytes": int(child.get("size") or 0),
                    "size_human": human_bytes(int(child.get("size") or 0)),
                    "fstype": child.get("fstype"),
                    "label": child.get("label"),
                    "mountpoints": normalize_mountpoints(child.get("mountpoints")),
                }
            )

        raw_devices.append(
            {
                "name": device.get("name"),
                "path": device.get("path"),
                "model": (device.get("model") or "").strip() or None,
                "vendor": (device.get("vendor") or "").strip() or None,
                "serial": device.get("serial"),
                "transport": device.get("tran"),
                "is_external": is_external,
                "size_bytes": int(device.get("size") or 0),
                "mountpoints": mountpoints,
                "partitions": partitions,
                "system_mounts": system_mounts,
                "destination_matches": destination_matches,
            }
        )

    best_destination_match_length = 0
    for device in raw_devices:
        for mountpoint in device["destination_matches"]:
            best_destination_match_length = max(best_destination_match_length, len(mountpoint))

    devices = []
    for device in raw_devices:
        destination_mounts = [
            mountpoint
            for mountpoint in device["destination_matches"]
            if len(mountpoint) == best_destination_match_length and best_destination_match_length > 0
        ]

        protected_reason = None
        if device["system_mounts"]:
            protected_reason = "Host system disk"
        elif destination_mounts:
            protected_reason = "Current destination directory is on this drive"
        elif not device["is_external"]:
            protected_reason = "Not a removable USB or hot-plug disk"

        has_media = device["size_bytes"] > 0
        has_partitions = bool(device["partitions"])
        has_staged_fat32 = len(device["partitions"]) == 1 and is_standard_sdcard_partition(
            device["partitions"][0]
        )
        bucket = capacity_bucket(device["size_bytes"])
        is_candidate = (
            device["is_external"]
            and has_media
            and not device["system_mounts"]
            and not destination_mounts
        )
        ready_for_flash = (
            is_candidate
            and bucket in ("32 GB", "64 GB")
            and (not has_partitions or has_staged_fat32)
        )

        status_label = "Available"
        if protected_reason:
            status_label = "Protected"
        elif not has_media:
            status_label = "No SD card detected"
        elif is_candidate:
            status_label = "Ready for backup or write"

        visible = device["is_external"] or destination_mounts
        if not visible:
            continue

        devices.append(
            {
                "name": device["name"],
                "path": device["path"],
                "model": device["model"],
                "vendor": device["vendor"],
                "serial": device["serial"],
                "transport": device["transport"],
                "is_external": device["is_external"],
                "is_candidate": is_candidate,
                "is_protected": bool(protected_reason),
                "protected_reason": protected_reason,
                "size_bytes": device["size_bytes"],
                "size_human": human_bytes(device["size_bytes"]),
                "capacity_bucket": bucket,
                "mountpoints": device["mountpoints"],
                "partitions": device["partitions"],
                "has_partitions": has_partitions,
                "has_staged_fat32": has_staged_fat32,
                "recommended_source": is_candidate and has_partitions and not has_staged_fat32,
                "recommended_target": ready_for_flash,
                "status_label": status_label,
            }
        )

    display_index = 1
    for item in devices:
        if item["is_protected"]:
            item["display_index"] = None
            continue
        item["display_index"] = display_index
        display_index += 1
    return devices


def list_images(destination_dir):
    directory = Path(destination_dir)
    if not directory.exists() or not directory.is_dir():
        return []

    images = []
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        if entry.name.endswith(".sha256") or entry.name.endswith(".working.img"):
            continue
        if not is_image_file(entry.name):
            continue
        stat = entry.stat()
        images.append(
            {
                "name": entry.name,
                "path": str(entry.resolve()),
                "size_bytes": stat.st_size,
                "size_human": human_bytes(stat.st_size),
                "modified_at": stat.st_mtime,
            }
        )
    images.sort(key=lambda item: item["modified_at"], reverse=True)
    return images


def device_map(destination_dir):
    return {item["path"]: item for item in discover_devices(destination_dir)}


def pick_unique_stem(directory, requested_stem, final_suffix):
    base = sanitize_camera_name(requested_stem)
    counter = 1
    stem = base
    while True:
        final_path = directory / f"{stem}{final_suffix}"
        working_path = directory / f"{stem}.working.img"
        hash_path = directory / f"{stem}{final_suffix}.sha256"
        if not final_path.exists() and not working_path.exists() and not hash_path.exists():
            return stem
        counter += 1
        stem = f"{base}-{counter}"


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError("This action must run as root inside the container.")


def require_pishrink():
    if not Path(PISHRINK_PATH).exists():
        raise RuntimeError(f"PiShrink was not found at {PISHRINK_PATH}.")


def wait_for_partition_path(device_path, timeout_seconds=20.0):
    deadline = time.time() + timeout_seconds
    predicted_paths = predicted_partition_paths(device_path)
    while time.time() < deadline:
        for path in current_partition_paths(device_path):
            return path
        for path in predicted_paths:
            if Path(path).exists():
                return path
        refresh_kernel_partition_state(device_path, add_partitions=True)
        time.sleep(0.35)
    raise RuntimeError(f"Timed out waiting for a partition to appear on {device_path}.")


def current_partition_paths(device_path):
    result = run_command(["lsblk", "-J", "-o", "PATH,TYPE", device_path])
    payload = json.loads(result.stdout)
    paths = []
    for node in payload.get("blockdevices", []):
        for child in node.get("children", []):
            if child.get("type") == "part" and child.get("path"):
                paths.append(child["path"])
    return paths


def predicted_partition_paths(device_path, limit=4):
    suffix = "p" if device_path[-1].isdigit() else ""
    return [f"{device_path}{suffix}{number}" for number in range(1, limit + 1)]


def refresh_kernel_partition_state(
    device_path,
    log_callback=None,
    drop_existing=False,
    add_partitions=False,
):
    commands = []
    if drop_existing:
        commands.append(["partx", "-d", device_path])
    if add_partitions:
        commands.extend([["partx", "-u", device_path], ["partx", "-a", device_path]])
    commands.extend(
        [
            ["blockdev", "--flushbufs", device_path],
            ["blockdev", "--rereadpt", device_path],
            ["partprobe", device_path],
            ["udevadm", "settle"],
            ["sync"],
        ]
    )

    for command in commands:
        if log_callback:
            log_callback(f"Refreshing kernel state with {' '.join(command)}")
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=BASE_ENV,
            check=False,
        )
    time.sleep(0.8)


def run_parted_mutation(device_path, args, log_callback=None, retries=3):
    command = ["parted", "-s", device_path, *args]
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            run_command(command)
            refresh_kernel_partition_state(device_path, log_callback=log_callback)
            return
        except CommandError as exc:
            last_error = exc
            detail = f"{exc.stderr}\n{exc.stdout}".lower()
            should_retry = (
                "unable to inform the kernel" in detail
                or "remain in use" in detail
                or "device or resource busy" in detail
            )
            if not should_retry or attempt == retries:
                raise
            if log_callback:
                log_callback(
                    f"Kernel still reports {device_path} as busy after {' '.join(args)}; retrying ({attempt}/{retries})"
                )
            refresh_kernel_partition_state(
                device_path,
                log_callback=log_callback,
                drop_existing=True,
            )
    if last_error:
        raise last_error


def is_not_mounted_message(message):
    text = (message or "").strip().lower()
    return "not mounted" in text or "udisks2.error.notmounted" in text


def normalize_reader_key(raw_key):
    value = (raw_key or "").strip()
    if not value:
        return None
    for marker in ("-scsi-", "-ata-", "-nvme-", "-mmc-"):
        if marker in value:
            return value.split(marker, 1)[0]
    return value


def device_reader_key(device_path):
    try:
        result = run_command(
            ["udevadm", "info", "--query=property", "--name", device_path],
            check=False,
        )
    except RuntimeError:
        result = None

    if result and result.returncode == 0:
        properties = {}
        for line in (result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
        for key_name in ("ID_PATH", "ID_PATH_TAG"):
            normalized = normalize_reader_key(properties.get(key_name))
            if normalized:
                return normalized

    block_name = Path(device_path).name
    sysfs_target = Path("/sys/class/block") / block_name / "device"
    try:
        resolved = sysfs_target.resolve(strict=True)
    except OSError:
        return None

    resolved_text = str(resolved)
    match = re.search(r"/usb\d+/(?P<reader>\d+-[\d.]+(?:/\d+-[\d.]+)*(?::\d+\.\d+)?)", resolved_text)
    if match:
        return match.group("reader")
    return resolved_text


def busy_reader_siblings(device_path, job_manager, current_job_id=None):
    reader_key = device_reader_key(device_path)
    if not reader_key:
        return []

    siblings = []
    for other_path, owner in job_manager.busy_resources().items():
        if other_path == device_path or owner == current_job_id:
            continue
        if device_reader_key(other_path) == reader_key:
            siblings.append(other_path)
    return sorted(set(siblings))


def unmount_device(device_info, log_callback=None, refresh_kernel=True):
    partitions = device_info.get("partitions") or []
    seen_paths = set()

    for partition in partitions:
        path = partition["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        if log_callback:
            log_callback(f"Unmounting {path}")

        if shutil.which("udisksctl") is not None:
            result = subprocess.run(
                ["udisksctl", "unmount", "-b", path],
                capture_output=True,
                text=True,
                env=BASE_ENV,
                check=False,
            )
            message = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                if log_callback and message:
                    log_callback(message)
            elif is_not_mounted_message(message):
                if log_callback:
                    log_callback(f"{path} is already unmounted.")
            elif log_callback and message:
                log_callback(f"Error unmounting {path}: {message}")

        fallback = subprocess.run(
            ["umount", "-lf", path],
            capture_output=True,
            text=True,
            env=BASE_ENV,
            check=False,
        )
        fallback_message = (fallback.stdout or fallback.stderr or "").strip()
        if (
            fallback.returncode != 0
            and fallback_message
            and not is_not_mounted_message(fallback_message)
            and log_callback
        ):
            log_callback(f"Fallback unmount for {path}: {fallback_message}")

    if refresh_kernel:
        refresh_kernel_partition_state(device_info["path"], log_callback=log_callback)


def clear_device_partitions(device_info, log_callback=None):
    device_path = device_info["path"]
    for partition_path in current_partition_paths(device_path):
        if log_callback:
            log_callback(f"Clearing partition signatures on {partition_path}")
        subprocess.run(
            ["wipefs", "-a", "-f", partition_path],
            capture_output=True,
            text=True,
            env=BASE_ENV,
            check=False,
        )

    if log_callback:
        log_callback(f"Clearing signatures on {device_path}")
    run_command(["wipefs", "-a", "-f", device_path])
    if log_callback:
        log_callback(f"Creating fresh partition label on {device_path}")
    run_parted_mutation(device_path, ["mklabel", "msdos"], log_callback=log_callback)


def format_device_as_fat32(device_info, label, log_callback=None):
    device_path = device_info["path"]
    if log_callback:
        log_callback(f"Creating FAT32 partition on {device_path}")
    run_parted_mutation(
        device_path,
        ["mkpart", "primary", "fat32", "1MiB", "100%"],
        log_callback=log_callback,
    )
    refresh_kernel_partition_state(device_path, log_callback=log_callback, add_partitions=True)
    partition_path = None
    try:
        partition_path = wait_for_partition_path(device_path, timeout_seconds=20.0)
    except RuntimeError as exc:
        if log_callback:
            log_callback(str(exc))
            log_callback(
                f"Partition node did not appear for {device_path}; formatting the whole device as FAT32 instead."
            )
        run_command(["mkfs.vfat", "-F", "32", "-I", "-n", label, device_path])
        subprocess.run(["sync"], capture_output=True, text=True, env=BASE_ENV, check=False)
        return device_path

    if log_callback:
        log_callback(f"Formatting {partition_path} as FAT32 with label {label}")
    run_command(["mkfs.vfat", "-F", "32", "-n", label, partition_path])
    subprocess.run(["sync"], capture_output=True, text=True, env=BASE_ENV, check=False)
    return partition_path


def eject_device(device_info, log_callback=None, allow_power_off=True, skip_reason=None):
    device_path = device_info["path"]
    unmount_device(device_info, log_callback, refresh_kernel=False)
    if not allow_power_off:
        if log_callback and skip_reason:
            log_callback(skip_reason)
        return {
            "device_path": device_path,
            "powered_off": False,
            "warning": skip_reason or "Reader power-off was skipped.",
        }
    if log_callback:
        log_callback(f"Ejecting {device_path}")
    try:
        result = run_command(["udisksctl", "power-off", "-b", device_path])
    except CommandError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        lowered = detail.lower()
        if "drive in use" in lowered or "is mounted" in lowered or "udisks-error-quark, 14" in lowered:
            if log_callback and detail:
                log_callback(detail)
            if log_callback:
                log_callback(
                    f"{device_path} was unmounted, but the USB reader stayed powered on because another slot or partition is still in use."
                )
            return {
                "device_path": device_path,
                "powered_off": False,
                "warning": "Reader still in use by another mounted slot or partition.",
            }
        raise
    if log_callback and result.stdout.strip():
        log_callback(result.stdout.strip())
    return {"device_path": device_path, "powered_off": True, "warning": None}


def pishrink_progress_hint(line):
    hints = (
        ("Gathering data", 77.0, "Inspecting image layout"),
        ("Checking filesystem", 81.0, "Checking filesystem health"),
        ("Shrinking filesystem", 86.0, "Shrinking filesystem"),
        ("Zeroing any free space left", 90.0, "Zero-filling free space"),
        ("Shrinking partition", 93.0, "Shrinking partition table"),
        ("Checking for unpartitioned space", 95.0, "Checking remaining free space"),
        ("Truncating image", 96.0, "Truncating image"),
        ("Shrunk ", 98.0, "Shrink complete"),
    )
    for marker, percent, message in hints:
        if marker in line:
            return percent, message
    return None


class JobManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}
        self._busy_resources = {}
        self._reserved_sdcard_numbers = set()

    def create_job(self, job_type, title, resources=None, targets=None):
        resources = resources or []
        with self._lock:
            busy = [resource for resource in resources if resource in self._busy_resources]
            if busy:
                owners = ", ".join(
                    f"{resource} ({self._busy_resources[resource]})" for resource in busy
                )
                raise RuntimeError(f"Busy device detected: {owners}")

            job_id = uuid.uuid4().hex[:12]
            for resource in resources:
                self._busy_resources[resource] = job_id

            self._jobs[job_id] = {
                "id": job_id,
                "type": job_type,
                "title": title,
                "status": "queued",
                "created_at": now_ts(),
                "started_at": None,
                "finished_at": None,
                "phase": "Queued",
                "message": "Queued",
                "progress": 0.0,
                "metrics": {},
                "logs": [],
                "error": None,
                "result": {},
                "targets": copy.deepcopy(targets or []),
                "resources": list(resources),
            }
            return job_id

    def start_job(self, job_id, worker, *args):
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, worker, args),
            daemon=True,
            name=f"job-{job_id}",
        )
        thread.start()

    def _run_job(self, job_id, worker, args):
        self.patch_job(
            job_id,
            status="running",
            started_at=now_ts(),
            phase="Starting",
            message="Starting job",
        )
        try:
            worker(job_id, *args)
        except Exception as exc:
            trace = traceback.format_exc(limit=5)
            self.append_log(job_id, trace)
            self.finalize(job_id, "failed", str(exc), error=str(exc))
        finally:
            self.release_resources(job_id)

    def release_resources(self, job_id):
        with self._lock:
            to_release = [
                resource for resource, owner in self._busy_resources.items() if owner == job_id
            ]
            for resource in to_release:
                self._busy_resources.pop(resource, None)

    def release_resource(self, job_id, resource):
        with self._lock:
            if self._busy_resources.get(resource) == job_id:
                self._busy_resources.pop(resource, None)

    def patch_job(self, job_id, **updates):
        with self._lock:
            job = self._jobs[job_id]
            for key, value in updates.items():
                if value is not None:
                    job[key] = value

    def update_progress(self, job_id, percent, message=None, phase=None, metrics=None):
        updates = {"progress": max(0.0, min(float(percent), 100.0))}
        if message is not None:
            updates["message"] = message
        if phase is not None:
            updates["phase"] = phase
        if metrics is not None:
            updates["metrics"] = metrics
        self.patch_job(job_id, **updates)

    def append_log(self, job_id, line):
        with self._lock:
            job = self._jobs[job_id]
            job["logs"].append({"ts": now_ts(), "line": line})
            if len(job["logs"]) > MAX_JOB_LOGS:
                overflow = len(job["logs"]) - MAX_JOB_LOGS
                del job["logs"][:overflow]

    def clear_logs(self, job_id=None):
        with self._lock:
            if job_id is None:
                for job in self._jobs.values():
                    job["logs"] = []
                return
            if job_id not in self._jobs:
                raise RuntimeError(f"Unknown job id: {job_id}")
            self._jobs[job_id]["logs"] = []

    def delete_job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RuntimeError(f"Unknown job id: {job_id}")
            if job.get("status") in ("queued", "running"):
                raise RuntimeError("Cannot delete a job tile while it is still running.")
            self._jobs.pop(job_id, None)

    def update_target(self, job_id, device_path, **updates):
        with self._lock:
            job = self._jobs[job_id]
            for target in job["targets"]:
                if target["device_path"] == device_path:
                    target.update(updates)
                    break
            progresses = [target.get("progress", 0.0) for target in job["targets"]]
            if progresses:
                job["progress"] = sum(progresses) / len(progresses)
            total_targets = len(job["targets"])
            completed = sum(1 for target in job["targets"] if target.get("status") == "completed")
            running = sum(1 for target in job["targets"] if target.get("status") == "running")
            failed = sum(1 for target in job["targets"] if target.get("status") == "failed")
            if running:
                if completed or failed:
                    fragments = []
                    if completed:
                        fragments.append(f"{completed}/{total_targets} completed")
                    if failed:
                        fragments.append(f"{failed} failed")
                    fragments.append(f"{running} still writing")
                    job["phase"] = "Writing remaining cards"
                    job["message"] = ", ".join(fragments)
                else:
                    job["phase"] = "Writing image"
                    job["message"] = f"{running}/{total_targets} card(s) active"
            elif failed and completed:
                job["phase"] = "Completed with errors"
                job["message"] = f"{completed}/{total_targets} finished, {failed} failed"
            elif failed:
                job["phase"] = "Failed"
                job["message"] = f"{failed}/{total_targets} failed"
            elif completed == total_targets and total_targets:
                job["phase"] = "Finished"
                job["message"] = f"All {total_targets} cards finished"

    def finalize(self, job_id, status, message, error=None, result=None):
        updates = {
            "status": status,
            "message": message,
            "finished_at": now_ts(),
        }
        if error is not None:
            updates["error"] = error
        if result is not None:
            updates["result"] = result
        if status in ("completed", "completed_with_errors"):
            updates["progress"] = 100.0
        self.patch_job(job_id, **updates)

    def busy_resources(self):
        with self._lock:
            return dict(self._busy_resources)

    def reserve_sdcard_label(self, destination_dir):
        with self._lock:
            used_numbers = set(self._reserved_sdcard_numbers)
            for device in discover_devices(destination_dir):
                for partition in device.get("partitions", []):
                    label = partition.get("label") or ""
                    match = SDCARD_LABEL_PATTERN.fullmatch(label)
                    if match:
                        used_numbers.add(int(match.group("number")))

            number = 1
            while number in used_numbers:
                number += 1

            self._reserved_sdcard_numbers.add(number)
            return f"SDcard{number}"

    def release_sdcard_label(self, label):
        match = SDCARD_LABEL_PATTERN.fullmatch(label or "")
        if not match:
            return
        with self._lock:
            self._reserved_sdcard_numbers.discard(int(match.group("number")))

    def jobs_snapshot(self):
        with self._lock:
            jobs = copy.deepcopy(list(self._jobs.values()))

        for job in jobs:
            job.pop("resources", None)
        jobs.sort(key=lambda item: item["created_at"], reverse=True)
        return jobs


def build_system_snapshot(job_manager, destination_dir):
    warnings = []
    if os.geteuid() != 0:
        warnings.append("Container is not running as root. Imaging actions will fail.")
    if not Path(PISHRINK_PATH).exists():
        warnings.append(
            f"PiShrink is missing at {PISHRINK_PATH}. Mount the host script into the container."
        )
    if not Path(destination_dir).exists():
        warnings.append(
            f"Destination directory {destination_dir} does not exist yet. It will be created on first capture."
        )
    if shutil.which("mkfs.vfat") is None:
        warnings.append("mkfs.vfat is missing. FAT32 reformat actions will fail.")
    if shutil.which("udisksctl") is None:
        warnings.append("udisksctl is missing. Card eject actions will fail.")

    memory = system_memory_snapshot()
    cpu_usage_percent = system_cpu_usage_percent()
    disk_io_bps = system_disk_io_bps()

    return {
        "running_as_root": os.geteuid() == 0,
        "pishrink_available": Path(PISHRINK_PATH).exists(),
        "pishrink_path": PISHRINK_PATH,
        "default_destination_dir": DEFAULT_DESTINATION_DIR,
        "destination_dir": destination_dir,
        "destination_exists": Path(destination_dir).exists(),
        "busy_resources": job_manager.busy_resources(),
        "warnings": warnings,
        "inside_container": Path("/.dockerenv").exists(),
        "cpu_usage_percent": cpu_usage_percent,
        "memory": memory,
        "disk_io_bps": disk_io_bps,
    }


def build_state(job_manager, destination_dir):
    devices = discover_devices(destination_dir)
    busy_resources = job_manager.busy_resources()
    for device in devices:
        device["busy_job_id"] = busy_resources.get(device["path"])
    return {
        "system": build_system_snapshot(job_manager, destination_dir),
        "devices": devices,
        "images": list_images(destination_dir),
        "jobs": job_manager.jobs_snapshot(),
        "now": now_ts(),
    }


def validate_candidate_device(device_path, destination_dir):
    devices = device_map(destination_dir)
    info = devices.get(device_path)
    if info is None:
        raise RuntimeError(f"{device_path} is not a detected removable device.")
    if not info.get("is_candidate"):
        reason = info.get("protected_reason") or "Device is not safe to use."
        raise RuntimeError(f"{device_path} is not available: {reason}")
    return info


def create_golden_image(job_id, source_device_path, camera_name, destination_dir, job_manager):
    require_root()
    require_pishrink()

    source_info = validate_candidate_device(source_device_path, destination_dir)
    ensure_directory(destination_dir)

    destination = Path(destination_dir)
    stem = pick_unique_stem(destination, camera_name, "_shrunk.img")
    working_image = destination / f"{stem}.working.img"
    final_image = destination / f"{stem}_shrunk.img"
    hash_file = destination / f"{stem}_shrunk.img.sha256"

    job_manager.patch_job(
        job_id,
        phase="Preparing source card",
        message=f"Preparing {source_device_path}",
        result={
            "source_device": source_device_path,
            "working_image_path": str(working_image),
            "final_image_path": str(final_image),
            "sha256_path": str(hash_file),
        },
    )

    unmount_device(source_info, lambda line: job_manager.append_log(job_id, line))
    job_manager.update_progress(job_id, 0.0, "Source card unmounted", "Preparing source card")

    total_bytes = source_info["size_bytes"]
    dd_command = [
        "dd",
        f"if={source_device_path}",
        f"of={working_image}",
        "bs=4M",
        "status=progress",
        "conv=fsync",
        "iflag=fullblock",
    ]
    job_manager.append_log(job_id, "Starting backup image capture with dd")

    def on_dd_line(_source, line):
        job_manager.append_log(job_id, line)
        progress = parse_dd_progress(line, total_bytes)
        if progress:
            job_manager.update_progress(
                job_id,
                progress["percent"],
                f"Reading {source_device_path} into {working_image.name}",
                "Creating backup image",
                progress,
            )

    if stream_command(dd_command, on_dd_line) != 0:
        raise RuntimeError("dd failed while creating the backup image.")

    subprocess.run(["sync"], capture_output=True, text=True, env=BASE_ENV, check=False)
    job_manager.update_progress(
        job_id,
        100.0,
        f"Raw image created at {working_image.name}",
        "Creating backup image",
        {"processed_bytes": total_bytes, "total_bytes": total_bytes},
    )

    pishrink_command = [PISHRINK_PATH, "-n", "-v", str(working_image)]
    job_manager.append_log(job_id, "Starting PiShrink")

    def on_pishrink_line(_source, line):
        job_manager.append_log(job_id, line)
        hint = pishrink_progress_hint(line)
        if hint:
            percent, message = hint
            job_manager.update_progress(job_id, 100.0, message, "Shrinking backup image")

    if stream_command(pishrink_command, on_pishrink_line) != 0:
        raise RuntimeError(
            f"PiShrink failed. The working image was left behind at {working_image} for inspection."
        )

    working_image.rename(final_image)
    final_size = final_image.stat().st_size
    job_manager.update_progress(
        job_id,
        100.0,
        f"Shrink complete: {final_image.name}",
        "Hashing backup image",
    )

    def on_hash_progress(_progress):
        job_manager.update_progress(
            job_id,
            100.0,
            f"Hashing {final_image.name}",
            "Hashing backup image",
        )

    digest = compute_sha256(final_image, on_hash_progress)
    hash_file.write_text(f"{digest}  {final_image}\n", encoding="utf-8")

    job_manager.finalize(
        job_id,
        "completed",
        f"Backup image ready: {final_image.name}",
        result={
            "source_device": source_device_path,
            "image_path": str(final_image),
            "sha256_path": str(hash_file),
            "size_bytes": final_size,
            "size_human": human_bytes(final_size),
        },
    )


def clear_card(job_id, device_path, destination_dir, label, job_manager):
    require_root()
    try:
        device_info = validate_candidate_device(device_path, destination_dir)
        job_manager.update_progress(job_id, 5.0, f"Unmounting {device_path}", "Unmounting")
        unmount_device(device_info, lambda line: job_manager.append_log(job_id, line))

        job_manager.update_progress(
            job_id,
            35.0,
            f"Deleting existing partitions on {device_path}",
            "Deleting partitions",
        )
        clear_device_partitions(device_info, lambda line: job_manager.append_log(job_id, line))

        job_manager.update_progress(
            job_id,
            70.0,
            f"Creating FAT32 volume {label}",
            "Formatting FAT32",
        )
        partition_path = format_device_as_fat32(
            device_info,
            label,
            lambda line: job_manager.append_log(job_id, line),
        )
        job_manager.finalize(
            job_id,
            "completed",
            f"{device_path} reformatted as FAT32 ({label})",
            result={"device_path": device_path, "partition_path": partition_path, "label": label},
        )
    finally:
        job_manager.release_sdcard_label(label)


def eject_cards(job_id, device_paths, destination_dir, job_manager):
    require_root()
    total_devices = len(device_paths)
    warnings = []
    for index, device_path in enumerate(device_paths, start=1):
        device_info = validate_candidate_device(device_path, destination_dir)
        sibling_busy_paths = busy_reader_siblings(device_path, job_manager, current_job_id=job_id)
        job_manager.update_progress(
            job_id,
            ((index - 1) / total_devices) * 100.0,
            f"Ejecting {device_path}",
            "Ejecting cards",
        )
        skip_reason = None
        if sibling_busy_paths:
            skip_reason = (
                f"{device_path} was unmounted, but reader power-off was skipped because "
                f"other slot(s) are still active: {', '.join(sibling_busy_paths)}."
            )
        result = eject_device(
            device_info,
            lambda line: job_manager.append_log(job_id, line),
            allow_power_off=not sibling_busy_paths,
            skip_reason=skip_reason,
        )
        if result and result.get("warning"):
            warnings.append(result)

    message = f"Ejected {total_devices} card(s)"
    if warnings:
        warning_count = len(warnings)
        message = (
            f"Ejected {total_devices} card(s); {warning_count} reader"
            f"{'s' if warning_count != 1 else ''} remained powered because another slot is in use"
        )

    job_manager.finalize(
        job_id,
        "completed",
        message,
        result={"device_paths": list(device_paths), "warnings": warnings},
    )


def flash_targets(job_id, targets, destination_dir, job_manager):
    require_root()

    source_devices = device_map(destination_dir)

    prepared_targets = []
    for target in targets:
        image_path = target.get("image_path")
        if not image_path:
            raise RuntimeError(f"Choose an image for {target['device_path']} before flashing.")
        image = Path(image_path)
        if not image.exists() or not image.is_file():
            raise RuntimeError(f"Image file not found: {image}")
        if image.suffix != ".img":
            raise RuntimeError(f"Only raw .img files can be flashed with dd: {image.name}")
        image_size = image.stat().st_size

        info = source_devices.get(target["device_path"])
        if info is None:
            raise RuntimeError(f"Device disappeared: {target['device_path']}")
        if not info.get("is_candidate"):
            raise RuntimeError(
                f"{target['device_path']} is not available: {info.get('protected_reason')}"
            )
        prepared_targets.append(
            {
                "device_path": info["path"],
                "device_name": info["name"],
                "label": info["capacity_bucket"] or info["size_human"],
                "size_bytes": info["size_bytes"],
                "wipe_first": bool(target.get("wipe_first")),
                "image_path": str(image),
                "image_name": image.name,
                "image_size": image_size,
                "status": "queued",
                "phase": "Queued",
                "message": f"Queued image file {image.name}",
                "progress": 0.0,
                "metrics": {},
            }
        )

    unique_images = sorted({target["image_name"] for target in prepared_targets})
    job_manager.patch_job(
        job_id,
        phase="Preparing flash job",
        message=f"Preparing to flash {len(prepared_targets)} card(s)",
        targets=prepared_targets,
        result={
            "targets": [
                {"device_path": target["device_path"], "image_path": target["image_path"]}
                for target in prepared_targets
            ]
        },
    )

    lock = threading.Lock()

    def update_target(target_device_path, **updates):
        with lock:
            job_manager.update_target(job_id, target_device_path, **updates)

    def worker(target_info):
        device_path = target_info["device_path"]
        device_name = target_info["device_name"]
        image = Path(target_info["image_path"])
        image_size = target_info["image_size"]
        try:
            if target_info["size_bytes"] < image_size:
                raise RuntimeError(
                    f"{device_path} is too small for {image.name} ({human_bytes(image_size)})."
                )

            live_info = validate_candidate_device(device_path, destination_dir)
            update_target(
                device_path,
                status="running",
                phase="Preparing target",
                message=f"Unmounting target card for {image.name}",
                progress=0.0,
            )
            unmount_device(live_info, lambda line: job_manager.append_log(job_id, f"[{device_name}] {line}"))

            if target_info["wipe_first"]:
                update_target(
                    device_path,
                    phase="Deleting partitions",
                    message="Deleting existing partitions",
                    progress=0.0,
                )
                clear_device_partitions(
                    live_info,
                    lambda line: job_manager.append_log(job_id, f"[{device_name}] {line}"),
                )

            update_target(
                device_path,
                phase="Writing image",
                message=f"Writing image file {image.name}",
                progress=0.0,
            )
            job_manager.append_log(job_id, f"[{device_name}] Starting dd flash")

            dd_command = [
                "dd",
                f"if={image}",
                f"of={device_path}",
                "bs=4M",
                "status=progress",
                "conv=fsync",
                "iflag=fullblock",
            ]

            def on_dd_line(_source, line):
                job_manager.append_log(job_id, f"[{device_name}] {line}")
                progress = parse_dd_progress(line, image_size)
                if progress:
                    update_target(
                        device_path,
                        phase="Writing image",
                        message=f"Writing image file {image.name}",
                        progress=progress["percent"],
                        metrics=progress,
                    )

            if stream_command(dd_command, on_dd_line) != 0:
                raise RuntimeError(f"dd failed while writing to {device_path}.")

            update_target(
                device_path,
                status="completed",
                phase="Finished",
                message=f"Image file {image.name} written successfully",
                progress=100.0,
                metrics={
                    "processed_bytes": image_size,
                    "total_bytes": image_size,
                    "percent": 100.0,
                },
            )
        except Exception as exc:
            update_target(
                device_path,
                status="failed",
                phase="Failed",
                message=str(exc),
                error=str(exc),
            )
            job_manager.append_log(job_id, f"[{device_name}] ERROR: {exc}")
        finally:
            job_manager.release_resource(job_id, device_path)

    threads = []
    for target_info in prepared_targets:
        thread = threading.Thread(target=worker, args=(target_info,), daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

    snapshot = [item for item in job_manager.jobs_snapshot() if item["id"] == job_id][0]
    successes = [target for target in snapshot["targets"] if target["status"] == "completed"]
    failures = [target for target in snapshot["targets"] if target["status"] == "failed"]

    if failures and not successes:
        job_manager.finalize(
            job_id,
            "failed",
            f"Flashing failed on all {len(failures)} target card(s)",
            error="All flash operations failed.",
            result={"targets": [{"device_path": target["device_path"], "image_path": target["image_path"]} for target in prepared_targets]},
        )
    elif failures:
        job_manager.finalize(
            job_id,
            "completed_with_errors",
            f"Flashed {len(successes)} card(s), {len(failures)} failed",
            result={"targets": [{"device_path": target["device_path"], "image_path": target["image_path"]} for target in prepared_targets]},
        )
    else:
        completion_message = (
            f"Flashed {len(successes)} card(s) with {unique_images[0]}"
            if len(unique_images) == 1
            else f"Flashed {len(successes)} card(s) with {len(unique_images)} selected backups"
        )
        job_manager.finalize(
            job_id,
            "completed",
            completion_message,
            result={"targets": [{"device_path": target["device_path"], "image_path": target["image_path"]} for target in prepared_targets]},
        )


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SDCardDashboard/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            params = parse_qs(parsed.query)
            destination_dir = params.get("destination_dir", [DEFAULT_DESTINATION_DIR])[0]
            return self.send_json(HTTPStatus.OK, build_state(self.server.job_manager, destination_dir))
        if parsed.path == "/healthz":
            return self.send_json(HTTPStatus.OK, {"ok": True})
        return self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/golden":
                return self.handle_create_golden(payload)
            if parsed.path == "/api/wipe":
                return self.handle_wipe(payload)
            if parsed.path == "/api/flash":
                return self.handle_flash(payload)
            if parsed.path == "/api/logs/clear":
                return self.handle_clear_logs(payload)
            if parsed.path == "/api/jobs/delete":
                return self.handle_delete_job(payload)
            if parsed.path == "/api/eject":
                return self.handle_eject(payload)
            if parsed.path == "/api/eject-all":
                return self.handle_eject_all(payload)
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown API route"})
        except CommandError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        except RuntimeError as exc:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc), "trace": traceback.format_exc(limit=3)},
            )

    def handle_create_golden(self, payload):
        source_device = payload.get("source_device")
        camera_name = payload.get("camera_name")
        destination_dir = payload.get("destination_dir") or DEFAULT_DESTINATION_DIR

        if not source_device:
            raise ValueError("A source SD card must be selected.")
        if not camera_name:
            raise ValueError("Enter a backup image name before capturing a backup image.")

        validate_candidate_device(source_device, destination_dir)
        title = f"Create backup image from {source_device}"
        job_id = self.server.job_manager.create_job(
            "golden",
            title,
            resources=[source_device],
        )
        self.server.job_manager.start_job(
            job_id,
            create_golden_image,
            source_device,
            camera_name,
            destination_dir,
            self.server.job_manager,
        )
        self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def handle_wipe(self, payload):
        device_path = payload.get("device")
        destination_dir = payload.get("destination_dir") or DEFAULT_DESTINATION_DIR

        if not device_path:
            raise ValueError("A device path is required.")

        validate_candidate_device(device_path, destination_dir)
        label = self.server.job_manager.reserve_sdcard_label(destination_dir)
        try:
            title = f"Reformat {device_path} as FAT32 ({label})"
            job_id = self.server.job_manager.create_job(
                "wipe",
                title,
                resources=[device_path],
            )
        except Exception:
            self.server.job_manager.release_sdcard_label(label)
            raise
        self.server.job_manager.start_job(
            job_id,
            clear_card,
            device_path,
            destination_dir,
            label,
            self.server.job_manager,
        )
        self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def handle_flash(self, payload):
        default_image_path = payload.get("image_path")
        targets = payload.get("targets") or []
        destination_dir = payload.get("destination_dir") or DEFAULT_DESTINATION_DIR

        if not targets:
            raise ValueError("Select at least one target SD card.")

        resource_paths = []
        image_names = set()
        for target in targets:
            device_path = target.get("device_path")
            if not device_path:
                raise ValueError("Each target must include a device path.")
            image_path = target.get("image_path") or default_image_path
            if not image_path:
                raise ValueError(f"Choose an image for {device_path} before flashing.")
            target["image_path"] = image_path
            image_names.add(Path(image_path).name)
            validate_candidate_device(device_path, destination_dir)
            resource_paths.append(device_path)

        if len(image_names) == 1:
            title = f"Flash {next(iter(image_names))} to {len(targets)} card(s)"
        else:
            title = f"Flash {len(image_names)} selected backups to {len(targets)} card(s)"
        job_id = self.server.job_manager.create_job(
            "flash",
            title,
            resources=resource_paths,
        )
        self.server.job_manager.start_job(
            job_id,
            flash_targets,
            targets,
            destination_dir,
            self.server.job_manager,
        )
        self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def handle_clear_logs(self, payload):
        job_id = payload.get("job_id")
        self.server.job_manager.clear_logs(job_id=job_id)
        self.send_json(HTTPStatus.OK, {"ok": True})

    def handle_delete_job(self, payload):
        job_id = payload.get("job_id")
        if not job_id:
            raise ValueError("A job id is required.")
        self.server.job_manager.delete_job(job_id)
        self.send_json(HTTPStatus.OK, {"ok": True})

    def handle_eject(self, payload):
        device_path = payload.get("device")
        destination_dir = payload.get("destination_dir") or DEFAULT_DESTINATION_DIR
        if not device_path:
            raise ValueError("A device path is required.")

        validate_candidate_device(device_path, destination_dir)
        title = f"Eject {device_path}"
        job_id = self.server.job_manager.create_job(
            "eject",
            title,
            resources=[device_path],
        )
        self.server.job_manager.start_job(
            job_id,
            eject_cards,
            [device_path],
            destination_dir,
            self.server.job_manager,
        )
        self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def handle_eject_all(self, payload):
        destination_dir = payload.get("destination_dir") or DEFAULT_DESTINATION_DIR
        busy_resources = self.server.job_manager.busy_resources()
        devices = [
            device["path"]
            for device in discover_devices(destination_dir)
            if device.get("is_candidate") and device["path"] not in busy_resources
        ]
        if not devices:
            raise ValueError("No removable SD cards are available to eject.")

        title = f"Eject {len(devices)} card(s)"
        job_id = self.server.job_manager.create_job(
            "eject-all",
            title,
            resources=devices,
        )
        self.server.job_manager.start_job(
            job_id,
            eject_cards,
            devices,
            destination_dir,
            self.server.job_manager,
        )
        self.send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def read_json(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def serve_static(self, raw_path):
        relative = raw_path.lstrip("/") or "index.html"
        if relative == "":
            relative = "index.html"

        requested = (STATIC_DIR / relative).resolve()
        static_root = STATIC_DIR.resolve()
        if not str(requested).startswith(str(static_root)):
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid path"})

        if requested.is_dir():
            requested = requested / "index.html"
        if not requested.exists():
            requested = STATIC_DIR / "index.html"

        try:
            content = requested.read_bytes()
        except FileNotFoundError:
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})

        content_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        return


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        self.job_manager = JobManager()


def main():
    server = DashboardServer((HOST, PORT), DashboardHandler)
    print(f"SD card backup dashboard running on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
