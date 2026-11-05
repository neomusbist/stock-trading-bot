"""Bounded asynchronous diagnostic evidence. Never an order or fill ledger."""
from collections import OrderedDict, deque
from datetime import datetime
import json
import shutil
import threading


class AsyncDailyDecisionLog:
    def __init__(self, folder, *, clock=None, max_file_bytes=4*1024*1024,
                 min_free_bytes=5*1024**3, dedup_seconds=15., max_queue_rows=1024,
                 max_queue_bytes=1024*1024, file_prefix='core-decisions',
                 max_total_bytes=512*1024**2):
        self.folder = folder
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.max_file_bytes = max_file_bytes
        self.min_free_bytes = min_free_bytes
        self.dedup_seconds = dedup_seconds
        self.max_queue_rows = max_queue_rows
        self.max_queue_bytes = max_queue_bytes
        self.file_prefix = file_prefix
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.pending = deque()
        self.pending_bytes = 0
        self.last_sig = OrderedDict()
        self.damaged_paths = set()
        self.omission_intervals = deque(maxlen=32)
        self.written = self.omitted = self.errors = 0
        self.blocked = False
        self.block_reason = None
        self.last_error = ''
        self.bytes_written = 0
        self.day = None

    def _omit_locked(self, reason, count=1):
        at = self.clock().isoformat()
        self.omitted += count
        self.blocked, self.block_reason = True, reason
        if self.omission_intervals and self.omission_intervals[-1]['reason'] == reason and self.omission_intervals[-1]['open']:
            interval = self.omission_intervals[-1]
            interval['last_detected_at'] = at
            interval['rows'] += count
        else:
            self.omission_intervals.append({'reason': reason, 'first_detected_at': at,
                                            'last_detected_at': at, 'rows': count, 'open': True,
                                            'upstream_feed_loss': 'UNKNOWN'})

    def record(self, row, symbol=None, force=False):
        try:
            stamp = self.clock()
            day = stamp.strftime('%Y%m%d')
            sig = (symbol, row.get('setup'), row.get('decision'), row.get('primary_reason_code'),
                   row.get('entry_price'), row.get('structural_stop'), row.get('emergency_stop'),
                   row.get('target'), row.get('quantity'), row.get('observation_gap'))
            payload = (json.dumps(row, ensure_ascii=False, default=str, allow_nan=False,
                                  separators=(',', ':')) + '\n').encode('utf8')
            with self.lock:
                previous = self.last_sig.get(symbol) if symbol else None
                if (not force and row.get('decision') != 'BUY' and previous and previous[0] == sig
                        and 0 <= stamp.timestamp() - previous[1] < self.dedup_seconds):
                    return True
                if len(self.pending) >= self.max_queue_rows or self.pending_bytes + len(payload) > self.max_queue_bytes:
                    self._omit_locked('DIAGNOSTIC_QUEUE_LIMIT')
                    return False
                self.pending.append((day, payload))
                self.pending_bytes += len(payload)
                if symbol:
                    self.last_sig[symbol] = (sig, stamp.timestamp())
                    self.last_sig.move_to_end(symbol)
                    if len(self.last_sig) > 2048:
                        self.last_sig.popitem(last=False)
                return True
        except Exception as error:
            with self.lock:
                self.errors += 1
                self.last_error = type(error).__name__
                self._omit_locked('DIAGNOSTIC_ENCODE_ERROR')
            return False

    def enqueue_encoded(self, payload):
        """Enqueue an already-frozen JSONL evidence row without producer I/O."""
        try:
            day = self.clock().strftime('%Y%m%d')
            with self.lock:
                if (not isinstance(payload, bytes) or len(payload) > 32768 or
                        len(self.pending) >= self.max_queue_rows or
                        self.pending_bytes + len(payload) > self.max_queue_bytes):
                    self._omit_locked('DIAGNOSTIC_QUEUE_LIMIT')
                    return False
                self.pending.append((day, payload))
                self.pending_bytes += len(payload)
                return True
        except Exception as error:
            with self.lock:
                self.errors += 1
                self.last_error = type(error).__name__
                self._omit_locked('DIAGNOSTIC_ENQUEUE_ERROR')
            return False

    def flush(self):
        # Only the diagnostic worker calls this in production. Disk operations
        # never hold the producer lock and never touch the critical ledger.
        with self.writer_lock:
            with self.lock:
                batch = list(self.pending)
                self.pending.clear()
                self.pending_bytes = 0
            try:
                total = sum(p.stat().st_size for p in self.folder.glob(self.file_prefix + '-????????.jsonl'))
            except OSError as error:
                with self.lock:
                    self.errors += 1
                    self.last_error = type(error).__name__
                    self._omit_locked('DIAGNOSTIC_BUDGET_UNKNOWN',len(batch))
                return
            groups = []
            for day, payload in batch:
                if groups and groups[-1][0] == day and groups[-1][2] + len(payload) <= 65536:
                    groups[-1][1].append(payload)
                    groups[-1][2] += len(payload)
                else:
                    groups.append([day, [payload], len(payload)])
            for day, rows, _ in groups:
                payload = b''.join(rows)
                row_count = len(rows)
                path = self.folder / (self.file_prefix + '-' + day + '.jsonl')
                try:
                    current = path.stat().st_size if path.exists() else 0
                    reason = None
                    if path in self.damaged_paths:
                        reason = 'DIAGNOSTIC_UNREPAIRED_PARTIAL_WRITE'
                    elif current + len(payload) > self.max_file_bytes:
                        reason = 'DIAGNOSTIC_DAILY_BYTE_CAP'
                    elif total + len(payload) > self.max_total_bytes:
                        reason = 'DIAGNOSTIC_TOTAL_BYTE_CAP'
                    elif self.min_free_bytes and shutil.disk_usage(self.folder).free - len(payload) < self.min_free_bytes:
                        reason = 'DIAGNOSTIC_FREE_SPACE_BELOW_SAFETY_LIMIT'
                    if reason:
                        with self.lock:
                            self._omit_locked(reason,row_count)
                        continue
                    try:
                        with path.open('ab') as out:
                            if out.write(payload) != len(payload):
                                raise OSError('SHORT_DIAGNOSTIC_WRITE')
                    except OSError:
                        try:
                            if path.exists():
                                with path.open('r+b') as out:
                                    out.truncate(current)
                        except OSError:
                            self.damaged_paths.add(path)
                        raise
                    with self.lock:
                        self.written += row_count
                        total += len(payload)
                        self.total_bytes = total
                        self.bytes_written = current + len(payload)
                        self.day = day
                        self.blocked, self.block_reason = False, None
                        for interval in self.omission_intervals:
                            interval['open'] = False
                except Exception as error:
                    with self.lock:
                        self.errors += 1
                        self.last_error = type(error).__name__
                        self._omit_locked('DIAGNOSTIC_IO_ERROR:' + self.last_error,row_count)

    def stats(self):
        with self.lock:
            return {'path': self.file_prefix + '-YYYYMMDD.jsonl', 'day': self.day,
                    'written_rows': self.written, 'omitted_rows': self.omitted,
                    'write_errors': self.errors, 'last_error': self.last_error,
                    'omission_intervals': list(self.omission_intervals),
                    'blocked': self.blocked, 'block_reason': self.block_reason,
                    'bytes_written': self.bytes_written, 'max_file_bytes': self.max_file_bytes,
                    'total_bytes': self.total_bytes, 'max_total_bytes': self.max_total_bytes,
                    'min_free_bytes': self.min_free_bytes, 'queue_rows': len(self.pending),
                    'queue_bytes': self.pending_bytes, 'coverage': 'UNKNOWN_IF_OMITTED_OR_UNFLUSHED',
                    'critical_ledger': False}


class SampledResearchLog(AsyncDailyDecisionLog):
    """Optional coarse research tape; never represents continuous feed coverage."""
    def __init__(self, folder, *, sample_seconds=5., **kwargs):
        kwargs.setdefault('max_file_bytes', 200*1024*1024)
        kwargs.setdefault('max_total_bytes', 2*1024**3)
        kwargs.setdefault('min_free_bytes', 32*1024**3)
        kwargs.setdefault('file_prefix', 'research-sampled')
        super().__init__(folder, **kwargs)
        self.sample_seconds = sample_seconds
        self.samples = OrderedDict()
        self.sampled_out = 0

    def record(self, row, symbol=None, force=False):
        if not symbol:
            with self.lock:
                self._omit_locked('RESEARCH_SYMBOL_UNKNOWN')
            return False
        stamp = self.clock().timestamp()
        key = (symbol, row.get('event'))
        with self.lock:
            previous = self.samples.get(key)
            if previous is not None and 0 <= stamp - previous < self.sample_seconds:
                self.sampled_out += 1
                return False
            self.samples[key] = stamp
            self.samples.move_to_end(key)
            if len(self.samples) > 4096:
                self.samples.popitem(last=False)
        return super().record({**row, 'symbol': symbol, 'coverage': 'SAMPLED_NOT_CONTINUOUS'}, symbol, force=True)

    def stats(self):
        result = super().stats()
        with self.lock:
            result['sampled_out_rows'] = self.sampled_out
        result['coverage'] = 'SAMPLED_NOT_CONTINUOUS'
        return result
