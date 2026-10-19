"""
R6 Automated Storage Governance Module.
Enforces bounded disk usage, automatic log rotation pruning, cache TTL, and 30GB disk ceiling defense.
Never deletes core accounting ledgers, state.json, or unhandled positions/orders.
"""
import gzip
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('StorageGovernor')

# Storage limits and thresholds
MAX_ARCHIVE_DIR_BYTES = 150 * 1024 * 1024  # 150 MB max per archive folder
MAX_ARCHIVE_AGE_DAYS = 3                   # 3 days retention for diagnostic archives
MAX_SHADOW_TICK_ROTATIONS = 2             # Keep active + .1 + .2 (max 3 files)
MAX_CONSOLE_LOG_BYTES = 10 * 1024 * 1024   # 10 MB max for operator_console.txt
EMERGENCY_FREE_GB_THRESHOLD = 30.0         # 30 GB minimum free disk space


def get_free_disk_gb(path: Path) -> float:
    """Return free disk space in GB for the drive containing path."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except Exception:
        return 999.0


def prune_archive_dir(archive_dir: Path, max_bytes: int = MAX_ARCHIVE_DIR_BYTES,
                      max_age_days: int = MAX_ARCHIVE_AGE_DAYS) -> Tuple[int, int]:
    """
    Prune rotated log files in an archive directory.
    Retains up to 2 most recent files per log prefix, purges files older than max_age_days,
    and enforces max_bytes ceiling.
    Returns (files_removed, bytes_freed).
    """
    if not archive_dir.exists() or not archive_dir.is_dir():
        return 0, 0

    now_ts = time.time()
    max_age_sec = max_age_days * 86400
    files_removed = 0
    bytes_freed = 0

    all_files = [f for f in archive_dir.iterdir() if f.is_file()]
    # 1. Purge files older than max_age_days
    for f in all_files:
        try:
            stat = f.stat()
            if now_ts - stat.st_mtime > max_age_sec:
                sz = stat.st_size
                f.unlink()
                files_removed += 1
                bytes_freed += sz
        except OSError:
            pass

    # 2. Group by prefix and keep at most 2 most recent per prefix
    remaining = [f for f in archive_dir.iterdir() if f.is_file()]
    by_prefix = {}
    for f in remaining:
        prefix = f.name.split('.')[0] if '.' in f.name else f.name
        by_prefix.setdefault(prefix, []).append(f)

    for prefix, flist in by_prefix.items():
        flist.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for old_file in flist[2:]:
            try:
                sz = old_file.stat().st_size
                old_file.unlink()
                files_removed += 1
                bytes_freed += sz
            except OSError:
                pass

    # 3. Enforce max_bytes ceiling (oldest first)
    remaining = sorted([f for f in archive_dir.iterdir() if f.is_file()], key=lambda x: x.stat().st_mtime)
    total_sz = sum(f.stat().st_size for f in remaining)
    for old_file in remaining:
        if total_sz <= max_bytes:
            break
        try:
            sz = old_file.stat().st_size
            old_file.unlink()
            total_sz -= sz
            files_removed += 1
            bytes_freed += sz
        except OSError:
            pass

    return files_removed, bytes_freed


def prune_shadow_ticks(runtime_dir: Path, max_rotations: int = MAX_SHADOW_TICK_ROTATIONS) -> Tuple[int, int]:
    """
    Prune shadow_ticks.jsonl.* files exceeding max_rotations.
    Returns (files_removed, bytes_freed).
    """
    if not runtime_dir.exists():
        return 0, 0

    files_removed = 0
    bytes_freed = 0
    for f in runtime_dir.glob('shadow_ticks.jsonl.*'):
        try:
            suffix = f.name.replace('shadow_ticks.jsonl.', '')
            if suffix.isdigit() and int(suffix) > max_rotations:
                sz = f.stat().st_size
                f.unlink()
                files_removed += 1
                bytes_freed += sz
        except OSError:
            pass
    return files_removed, bytes_freed


def truncate_console_log(log_path: Path, max_bytes: int = MAX_CONSOLE_LOG_BYTES,
                         keep_bytes: int = 2 * 1024 * 1024) -> int:
    """
    Truncate operator_console.txt if it exceeds max_bytes, keeping the latest keep_bytes.
    Returns bytes_freed.
    """
    if not log_path.exists() or not log_path.is_file():
        return 0

    try:
        sz = log_path.stat().st_size
        if sz <= max_bytes:
            return 0
        with open(log_path, 'rb') as f:
            f.seek(sz - keep_bytes)
            f.readline()  # Seek to next newline boundary
            tail_data = f.read()
        with open(log_path, 'wb') as f:
            f.write(b'[StorageGovernor] Truncated older log records to conserve disk space.\n')
            f.write(tail_data)
        bytes_freed = sz - log_path.stat().st_size
        return max(0, bytes_freed)
    except OSError:
        return 0


def run_storage_governance(root_path: Path, emergency_mode: bool = False) -> Dict:
    """
    Execute full storage governance on a given root path (Desktop or dev directory).
    Safe, idempotent, preserves all state and order records.
    """
    root = Path(root_path)
    runtime = root / 'runtime'
    total_freed = 0
    total_removed = 0
    actions = []

    # 1. Prune runtime/archive and runtime/automation-production/archive
    for arch in [runtime / 'archive', runtime / 'automation-production/archive']:
        if arch.exists():
            n, b = prune_archive_dir(arch,
                                     max_bytes=50 * 1024 * 1024 if emergency_mode else MAX_ARCHIVE_DIR_BYTES,
                                     max_age_days=1 if emergency_mode else MAX_ARCHIVE_AGE_DAYS)
            if n > 0:
                actions.append(f'Pruned {arch.name}: {n} files, {b / 1024**2:.1f} MB freed')
                total_removed += n
                total_freed += b

    # 2. Prune shadow_ticks rotations
    n, b = prune_shadow_ticks(runtime, max_rotations=1 if emergency_mode else MAX_SHADOW_TICK_ROTATIONS)
    if n > 0:
        actions.append(f'Pruned shadow_ticks: {n} files, {b / 1024**2:.1f} MB freed')
        total_removed += n
        total_freed += b

    # 3. Truncate console log
    console_log = runtime / 'operator_console.txt'
    if console_log.exists():
        b = truncate_console_log(console_log, max_bytes=5 * 1024 * 1024 if emergency_mode else MAX_CONSOLE_LOG_BYTES)
        if b > 0:
            actions.append(f'Truncated operator_console.txt: {b / 1024**2:.1f} MB freed')
            total_freed += b

    # 4. Remove duplicate archive_compressed if present
    comp_zip = runtime / 'archive_compressed/archive_20261008.zip'
    if comp_zip.exists():
        try:
            sz = comp_zip.stat().st_size
            comp_zip.unlink()
            actions.append(f'Removed obsolete archive_20261008.zip: {sz / 1024**2:.1f} MB freed')
            total_freed += sz
            total_removed += 1
        except OSError:
            pass

    free_gb = get_free_disk_gb(root)
    return {
        'timestamp': datetime.now().isoformat(),
        'root': str(root),
        'free_gb': round(free_gb, 2),
        'below_30gb_threshold': free_gb < EMERGENCY_FREE_GB_THRESHOLD,
        'files_removed': total_removed,
        'bytes_freed': total_freed,
        'mb_freed': round(total_freed / (1024 ** 2), 2),
        'actions': actions
    }
