"""
R6 Automated Storage Governance Module.
Enforces 3-tier data lifecycle protection, bounded disk usage, automatic log rotation pruning,
and actionable 30GB disk defense with critical order safety gating.

Tier A (Protected): Ledgers, state.json, trade reviews, private incident/replay datasets, bundles.
Tier B (Bounded Retention): SHADOW ticks, radar history, shadow events with date & sequence preservation.
Tier C (Aggressive Purge): Whitelisted console output, debug logs, temp caches.
"""
import hashlib
import json
import logging
import os
import shutil
import time
import zipfile
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


def get_free_disk_gb(path: Path) -> Optional[float]:
    """Return free disk space in GB for the drive containing path. Returns None on failure."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except Exception as e:
        logger.error(f"Failed to check disk usage for {path}: {e}")
        return None


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
        try:
            stat = f.stat()
            file_age_sec = now_ts - stat.st_mtime
            # STRICT PROTECTION: Recent trading days within min_age_days are 100% PROTECTED regardless of file count
            if file_age_sec <= min_age_sec:
                continue
            if idx > max_rotations:
                sz = stat.st_size
                f.unlink()
                files_removed += 1
                bytes_freed += sz
                action_logs.append(f"Pruned expired shadow tick rotation {f.name} ({sz / 1024**2:.2f} MB, age {file_age_sec / 86400:.1f}d > {min_age_days}d)")
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


def compress_file_with_verification(src_path: Path) -> Tuple[bool, int, str]:
    """
    Compress a single file into a .zip archive using a 5-stage fail-safe verification pipeline:
    1. Read and compute SHA-256 of src_path.
    2. Write to a temporary .zip.tmp archive using ZIP_DEFLATED (level 9).
    3. Verify zip archive CRC32 with testzip().
    4. Read back decompressed content from zip, verify SHA-256 matches original exactly.
    5. Atomically replace .zip.tmp -> .zip and unlink src_path.

    Returns (success, bytes_freed, message).
    """
    if not src_path.exists() or not src_path.is_file():
        return False, 0, f"Source file does not exist: {src_path}"

    orig_size = src_path.stat().st_size
    if orig_size == 0:
        return False, 0, f"Source file is empty, skipping compression: {src_path}"

    zip_final = src_path.with_name(src_path.name + '.zip')
    if zip_final.exists():
        return False, 0, f"Target zip already exists: {zip_final}"

    zip_tmp = src_path.with_name(src_path.name + '.zip.tmp')
    try:
        # Step 1: Compute SHA-256 hash of source
        h = hashlib.sha256()
        with open(src_path, 'rb') as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        expected_sha256 = h.hexdigest()

        # Step 2: Write compressed archive
        with zipfile.ZipFile(zip_tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(src_path, arcname=src_path.name)

        # Step 3: Test zip CRC32
        with zipfile.ZipFile(zip_tmp, 'r') as zf:
            corrupt = zf.testzip()
            if corrupt is not None:
                raise ValueError(f"Zip CRC32 check failed on member {corrupt}")

        # Step 4: Decompress and verify bit-for-bit SHA-256
        with zipfile.ZipFile(zip_tmp, 'r') as zf:
            with zf.open(src_path.name, 'r') as member:
                h_verify = hashlib.sha256()
                while chunk := member.read(1024 * 1024):
                    h_verify.update(chunk)
                actual_sha256 = h_verify.hexdigest()
                if actual_sha256 != expected_sha256:
                    raise ValueError(f"SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}")

        # Step 5: Commit archive and safe remove source
        zip_tmp.replace(zip_final)
        compressed_size = zip_final.stat().st_size
        bytes_freed = orig_size - compressed_size
        src_path.unlink()
        return True, max(0, bytes_freed), f"Compressed {src_path.name} -> {zip_final.name} ({orig_size / 1024**2:.2f} MB -> {compressed_size / 1024**2:.2f} MB, saved {bytes_freed / 1024**2:.2f} MB)"

    except Exception as e:
        if zip_tmp.exists():
            try:
                zip_tmp.unlink()
            except OSError:
                pass
        logger.error(f"Failed to safely compress {src_path}: {e}")
        return False, 0, f"Error compressing {src_path.name}: {e}"


def compress_historical_archives(runtime_dir: Path, min_age_days: int = 2) -> Tuple[int, int, List[str]]:
    """
    Compress inactive historical archives older than min_age_days without any data loss.
    Targets rotated files in:
      - runtime/archive/
      - runtime/automation-production/archive/
      - runtime/automation-production/trade-review/
    NEVER touches active files:
      - state.json, events.jsonl, heartbeat.json, operator_account.json, policy.json
      - active unrotated shadow_ticks.jsonl
      - trade-review file for today's session
      - any file modified within min_age_days
      - any file already ending in .zip or .gz
    Returns (files_compressed, bytes_freed, action_logs).
    """
    if not runtime_dir.exists():
        return 0, 0, []

    files_compressed = 0
    total_freed = 0
    action_logs = []
    now_ts = time.time()
    min_age_sec = min_age_days * 86400
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_compact = datetime.now().strftime('%Y%m%d')

    ACTIVE_NAMES = {
        'state.json', 'events.jsonl', 'heartbeat.json', 'operator_account.json',
        'session_settings.json', 'policy.json', 'RELEASE_IDENTITY.json',
        'shadow_ticks.jsonl', 'operator_console.txt'
    }

    target_dirs = [
        runtime_dir / 'archive',
        runtime_dir / 'automation-production' / 'archive',
        runtime_dir / 'automation-production' / 'trade-review',
    ]

    for d in target_dirs:
        if not d.exists() or not d.is_dir():
            continue

        candidates = []
        for f in d.iterdir():
            if not f.is_file():
                continue
            name = f.name.lower()
            if name.endswith('.zip') or name.endswith('.gz') or name.endswith('.tmp'):
                continue
            if name in ACTIVE_NAMES:
                continue
            if 'trade-review' in d.parts:
                if today_str in name or today_compact in name:
                    continue

            try:
                stat = f.stat()
                age_sec = now_ts - stat.st_mtime
                if age_sec >= min_age_sec:
                    candidates.append((stat.st_mtime, f))
            except OSError:
                continue

        candidates.sort(key=lambda x: x[0])

        for _, f in candidates:
            success, freed, msg = compress_file_with_verification(f)
            if success:
                files_compressed += 1
                total_freed += freed
                action_logs.append(msg)

    return files_compressed, total_freed, action_logs


def check_disk_safety_gate(root_path: Path) -> Dict:
    """
    Evaluates actionable disk safety.
    Conservative policy: If disk space is unknown or query fails, blocks new orders.
    """
    free_gb = get_free_disk_gb(root_path)
    if free_gb is None:
        return {
            'free_gb': None,
            'disk_emergency': True,
            'disk_critical': True,
            'order_blocked': True,
            'order_block_reason': 'DISK_CAPACITY_UNKNOWN: 디스크 여유 공간 조회 실패로 신규매수를 보수적으로 차단합니다.'
        }

    is_emergency = free_gb < EMERGENCY_FREE_GB_THRESHOLD
    is_critical = free_gb < CRITICAL_FREE_GB_THRESHOLD

    order_block_reason = None
    if is_critical:
        order_block_reason = f"CRITICAL_LOW_DISK_SPACE: 여유 디스크가 {free_gb:.1f}GB로 극도로 부족합니다 (최소 {CRITICAL_FREE_GB_THRESHOLD:.1f}GB 필요). 신규 매수가 차단됩니다."

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

    # 1. Compress historical archives older than 2 days (or 1 day in emergency)
    files_compressed, comp_freed, comp_logs = compress_historical_archives(
        runtime,
        min_age_days=1 if effective_emergency else 2
    )
    total_freed += comp_freed
    all_actions.extend(comp_logs)

    # 2. Prune runtime/archive and runtime/automation-production/archive
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

    # 3. Prune shadow_ticks rotations (Tier B)
    n, b, logs = prune_shadow_ticks(
        runtime,
        max_rotations=2 if effective_emergency else MAX_SHADOW_TICK_ROTATIONS,
        min_age_days=1 if effective_emergency else MIN_SHADOW_TICK_AGE_DAYS
    )
    total_removed += n
    total_freed += b
    all_actions.extend(logs)

    # 4. Truncate console log (Tier C)
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
        'files_compressed': files_compressed,
        'bytes_freed': total_freed,
        'bytes_compressed_freed': comp_freed,
        'mb_freed': round(total_freed / (1024 ** 2), 2),
        'actions': all_actions
    }


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Run tiered storage governance and safe compression.")
    parser.add_argument('root', nargs='?', default='.', help="Root directory containing runtime/ (default: .)")
    parser.add_argument('--emergency', action='store_true', help="Force emergency mode (aggressive compression & pruning)")
    args = parser.parse_args()

    root_dir = Path(args.root).resolve()
    print(f"Running Storage Governor on: {root_dir}")
    res = run_storage_governance(root_dir, emergency_mode=args.emergency)
    print(json.dumps(res, indent=2, ensure_ascii=False))
