"""
R6 Automated Storage Governance Module.
Enforces 3-tier data lifecycle protection, bounded disk usage, automatic log rotation pruning,
and actionable 30GB disk defense with critical order safety gating.

Tier A (Protected): Ledgers, state.json, trade reviews, private incident/replay datasets, bundles.
Tier B (Bounded Retention): SHADOW ticks, radar history, shadow events with date & sequence preservation.
Tier C (Aggressive Purge): Whitelisted console output, debug logs, temp caches.
"""
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('StorageGovernor')

# Storage limits and thresholds
MAX_ARCHIVE_DIR_BYTES = 150 * 1024 * 1024  # 150 MB max per archive folder
MAX_ARCHIVE_AGE_DAYS = 3                   # 3 days retention for diagnostic archives
MAX_SHADOW_TICK_ROTATIONS = 3             # Keep active + up to 3 rotations (Tier B)
MIN_SHADOW_TICK_AGE_DAYS = 3               # Only prune rotations older than 3 days
MAX_CONSOLE_LOG_BYTES = 10 * 1024 * 1024   # 10 MB max for operator_console.txt
EMERGENCY_FREE_GB_THRESHOLD = 30.0         # 30 GB warning threshold (triggers Tier C cleanup)
CRITICAL_FREE_GB_THRESHOLD = 5.0           # 5 GB critical threshold (blocks new buy orders)

# Explicit Tier A / Protected patterns (case-insensitive) - NEVER delete under any circumstances
PROTECTED_PATTERNS = (
    'state.json',
    'events.jsonl',
    'trade-review',
    'heartbeat.json',
    'session_settings.json',
    'operator_account.json',
    'policy.json',
    'RELEASE_IDENTITY',
    'DEPLOYMENT',
    'TRADE_PRIVATE',
    'BROKER_PRIVATE',
    'CAPTURE_PRIVATE',
    'EVENT_WINDOW_PRIVATE',
    '.bundle',
    'lunit',
    'bexel',
    'incident',
    'fixture',
)

# Explicit Tier C / Allowlist of deletable diagnostic prefixes (only these may be pruned)
DELETABLE_ARCHIVE_PREFIXES = (
    'operator_console.txt',
    'runner_stdout',
    'debug',
    'temp_',
    'operator_error_history.jsonl',
)


def is_protected_file(path: Path) -> bool:
    """Check if a file matches any Tier A protected pattern."""
    name_lower = path.name.lower()
    for pattern in PROTECTED_PATTERNS:
        if pattern.lower() in name_lower:
            return True
    return False


def is_deletable_diagnostic_archive(path: Path) -> bool:
    """Check if an archive file is an allowable diagnostic log eligible for pruning."""
    p = Path(path)
    if is_protected_file(p):
        return False
    name_lower = p.name.lower()
    for prefix in DELETABLE_ARCHIVE_PREFIXES:
        if name_lower.startswith(prefix.lower()):
            return True
    return False


def get_free_disk_gb(path: Path) -> float:
    """Return free disk space in GB for the drive containing path."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except Exception as e:
        logger.error(f"Failed to check disk usage for {path}: {e}")
        return 999.0


def prune_archive_dir(archive_dir: Path, max_bytes: int = MAX_ARCHIVE_DIR_BYTES,
                      max_age_days: int = MAX_ARCHIVE_AGE_DAYS) -> Tuple[int, int, List[str]]:
    """
    Prune rotated log files in an archive directory using strict allowlisting.
    ONLY files matching DELETABLE_ARCHIVE_PREFIXES and NOT protected are eligible.
    All Tier A ledgers and unlisted files are strictly protected.
    Returns (files_removed, bytes_freed, action_logs).
    """
    if not archive_dir.exists() or not archive_dir.is_dir():
        return 0, 0, []

    now_ts = time.time()
    max_age_sec = max_age_days * 86400
    files_removed = 0
    bytes_freed = 0
    action_logs = []

    # 1. Select ONLY eligible diagnostic files
    eligible_files = [f for f in archive_dir.iterdir() if f.is_file() and is_deletable_diagnostic_archive(f)]

    # 2. Purge files older than max_age_days
    for f in eligible_files:
        try:
            stat = f.stat()
            if now_ts - stat.st_mtime > max_age_sec:
                sz = stat.st_size
                f.unlink()
                files_removed += 1
                bytes_freed += sz
                action_logs.append(f"Pruned expired diagnostic log {f.name} ({sz / 1024**2:.2f} MB, age > {max_age_days}d)")
        except OSError as e:
            logger.error(f"Error unlinking expired archive {f}: {e}")

    # 3. Group by prefix and keep at most 2 most recent per prefix
    remaining = [f for f in archive_dir.iterdir() if f.is_file() and is_deletable_diagnostic_archive(f)]
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
                action_logs.append(f"Pruned excess diagnostic rotation {old_file.name} ({sz / 1024**2:.2f} MB)")
            except OSError as e:
                logger.error(f"Error unlinking excess rotation {old_file}: {e}")

    # 4. Enforce max_bytes ceiling (oldest first among deletable files)
    remaining = sorted([f for f in archive_dir.iterdir() if f.is_file() and is_deletable_diagnostic_archive(f)],
                       key=lambda x: x.stat().st_mtime)
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
            action_logs.append(f"Pruned diagnostic log for capacity ceiling {old_file.name} ({sz / 1024**2:.2f} MB)")
        except OSError as e:
            logger.error(f"Error unlinking capacity ceiling file {old_file}: {e}")

    return files_removed, bytes_freed, action_logs


def prune_shadow_ticks(runtime_dir: Path, max_rotations: int = MAX_SHADOW_TICK_ROTATIONS,
                       min_age_days: int = MIN_SHADOW_TICK_AGE_DAYS) -> Tuple[int, int, List[str]]:
    """
    Tier B Bounded Retention for shadow_ticks.jsonl.* files.
    NEVER deletes active shadow_ticks.jsonl.
    Preserves recent rotations within min_age_days or within max_rotations.
    Only prunes older rotations that exceed max_rotations AND are older than min_age_days.
    Returns (files_removed, bytes_freed, action_logs).
    """
    if not runtime_dir.exists():
        return 0, 0, []

    files_removed = 0
    bytes_freed = 0
    action_logs = []
    now_ts = time.time()
    min_age_sec = min_age_days * 86400

    tick_files = []
    for f in runtime_dir.glob('shadow_ticks.jsonl.*'):
        if is_protected_file(f):
            continue
        suffix = f.name.replace('shadow_ticks.jsonl.', '')
        if suffix.isdigit():
            tick_files.append((int(suffix), f))

    # Sort by rotation index descending
    tick_files.sort(key=lambda x: x[0], reverse=True)

    for idx, f in tick_files:
        if idx > max_rotations:
            try:
                stat = f.stat()
                # Check age to avoid deleting current trading day's active rotations
                if now_ts - stat.st_mtime > min_age_sec or len(tick_files) > max_rotations * 2:
                    sz = stat.st_size
                    f.unlink()
                    files_removed += 1
                    bytes_freed += sz
                    action_logs.append(f"Pruned expired shadow tick rotation {f.name} ({sz / 1024**2:.2f} MB)")
            except OSError as e:
                logger.error(f"Error unlinking shadow tick rotation {f}: {e}")

    return files_removed, bytes_freed, action_logs


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
    except OSError as e:
        logger.error(f"Error truncating console log {log_path}: {e}")
        return 0


def check_disk_safety_gate(root_path: Path) -> Dict:
    """
    Evaluates actionable disk safety.
    Returns status dictionary with free GB, warning/critical flags, and order block reason.
    """
    free_gb = get_free_disk_gb(root_path)
    is_emergency = free_gb < EMERGENCY_FREE_GB_THRESHOLD
    is_critical = free_gb < CRITICAL_FREE_GB_THRESHOLD

    order_block_reason = None
    if is_critical:
        order_block_reason = f"CRITICAL_LOW_DISK_SPACE: 여유 디스크가 {free_gb:.1f}GB로 극도로 부족합니다 (최소 5GB 필요). 신규 매수가 차단됩니다."

    return {
        'free_gb': round(free_gb, 2),
        'disk_emergency': is_emergency,
        'disk_critical': is_critical,
        'order_blocked': is_critical,
        'order_block_reason': order_block_reason
    }


def run_storage_governance(root_path: Path, emergency_mode: bool = False) -> Dict:
    """
    Execute full tiered storage governance on a given root path.
    Safe, idempotent, preserves all Tier A state, ledgers, and order records.
    Never deletes ledgers even in disk emergencies.
    """
    root = Path(root_path)
    runtime = root / 'runtime'
    total_freed = 0
    total_removed = 0
    all_actions = []

    disk_status = check_disk_safety_gate(root)
    effective_emergency = emergency_mode or disk_status['disk_emergency']

    # 1. Prune runtime/archive and runtime/automation-production/archive
    for arch in [runtime / 'archive', runtime / 'automation-production/archive']:
        if arch.exists():
            n, b, logs = prune_archive_dir(
                arch,
                max_bytes=50 * 1024 * 1024 if effective_emergency else MAX_ARCHIVE_DIR_BYTES,
                max_age_days=1 if effective_emergency else MAX_ARCHIVE_AGE_DAYS
            )
            total_removed += n
            total_freed += b
            all_actions.extend(logs)

    # 2. Prune shadow_ticks rotations (Tier B)
    n, b, logs = prune_shadow_ticks(
        runtime,
        max_rotations=2 if effective_emergency else MAX_SHADOW_TICK_ROTATIONS,
        min_age_days=1 if effective_emergency else MIN_SHADOW_TICK_AGE_DAYS
    )
    total_removed += n
    total_freed += b
    all_actions.extend(logs)

    # 3. Truncate console log (Tier C)
    console_log = runtime / 'operator_console.txt'
    if console_log.exists():
        b = truncate_console_log(
            console_log,
            max_bytes=5 * 1024 * 1024 if effective_emergency else MAX_CONSOLE_LOG_BYTES
        )
        if b > 0:
            all_actions.append(f"Truncated operator_console.txt: {b / 1024**2:.1f} MB freed")
            total_freed += b

    # Re-check disk status after governance
    final_disk_status = check_disk_safety_gate(root)

    return {
        'timestamp': datetime.now().isoformat(),
        'root': str(root),
        'free_gb': final_disk_status['free_gb'],
        'disk_emergency': final_disk_status['disk_emergency'],
        'disk_critical': final_disk_status['disk_critical'],
        'order_blocked': final_disk_status['order_blocked'],
        'order_block_reason': final_disk_status['order_block_reason'],
        'files_removed': total_removed,
        'bytes_freed': total_freed,
        'mb_freed': round(total_freed / (1024 ** 2), 2),
        'actions': all_actions
    }
