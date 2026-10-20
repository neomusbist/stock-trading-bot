"""Bounded exchange-clock skew and independent local receipt freshness.

The one-second lead bound matches the existing receiver admission rule for
second-resolution exchange timestamps. It never adds to the local receive TTL.
"""
import math
import time
from datetime import datetime

MAX_EXCHANGE_LEAD_SECONDS = 1.0


def market_freshness(stamp, asof, stale_seconds, *, received_at=None,
                     received_monotonic=None, monotonic_now=None):
    evidence = {'valid': False, 'fresh': False, 'exchange_age_seconds': None,
                'receive_age_seconds': None, 'receive_elapsed_seconds': None,
                'effective_age_seconds': None,
                'max_exchange_lead_seconds': MAX_EXCHANGE_LEAD_SECONDS}

    def invalid(reason):
        return {**evidence, 'reason': reason}

    def parse(value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    def same_day(value):
        return (isinstance(value, datetime) and value.tzinfo is not None
                and value.astimezone(asof.tzinfo).date() == asof.date())

    try:
        if (not isinstance(asof, datetime) or asof.tzinfo is None
                or type(stale_seconds) not in (int, float)
                or not math.isfinite(stale_seconds) or stale_seconds < 0):
            return invalid('INVALID_ASOF_OR_TTL')
        stamp = parse(stamp)
        if not same_day(stamp):
            return invalid('INVALID_EXCHANGE_DATE')
        age = (asof-stamp).total_seconds()
        evidence['exchange_age_seconds'] = age
        if age < -MAX_EXCHANGE_LEAD_SECONDS:
            return invalid('EXCHANGE_TIME_IN_FUTURE')
        ages = [max(0., age)]
        if received_at is not None:
            received_at = parse(received_at)
            if not same_day(received_at):
                return invalid('INVALID_RECEIPT_DATE')
            receipt_age = (asof-received_at).total_seconds()
            source_age = (received_at-stamp).total_seconds()
            evidence['receive_age_seconds'] = receipt_age
            evidence['exchange_age_at_receipt_seconds'] = source_age
            if min(receipt_age, source_age) < -MAX_EXCHANGE_LEAD_SECONDS:
                return invalid('INVALID_RECEIPT_CLOCK_ORDER')
            ages.extend((max(0., receipt_age), max(0., source_age)))
        if received_monotonic is not None:
            if received_at is None:
                return invalid('RECEIPT_COUNTER_WITHOUT_TIMESTAMP')
            current = time.monotonic() if monotonic_now is None else monotonic_now
            if any(type(v) not in (int, float) or not math.isfinite(v)
                   for v in (received_monotonic, current)):
                return invalid('INVALID_RECEIPT_COUNTER')
            elapsed = current-received_monotonic
            evidence['receive_elapsed_seconds'] = elapsed
            if elapsed < 0:
                return invalid('RECEIPT_COUNTER_IN_FUTURE')
            ages.append(elapsed)
            # Include delay already present at arrival when wall time is later
            # corrected backwards; the monotonic wait must not erase that delay.
            receipt_based_age = max(0., source_age) + elapsed
            evidence['receipt_based_age_seconds'] = receipt_based_age
            ages.append(receipt_based_age)
        effective_age = max(ages)
        return {**evidence, 'valid': True, 'fresh': effective_age <= stale_seconds,
                'effective_age_seconds': effective_age,
                'reason': 'CURRENT' if effective_age <= stale_seconds else 'MARKET_DATA_EXPIRED'}
    except (TypeError, ValueError, OverflowError):
        return invalid('INVALID_MARKET_TIMESTAMP')


def tick_freshness(tick, asof, stale_seconds):
    return market_freshness(getattr(tick, 'time', None), asof, stale_seconds,
                            received_at=getattr(tick, 'received_at', None),
                            received_monotonic=getattr(tick, 'received_monotonic', None))


def book_freshness(tick, asof, stale_seconds):
    """Keep the book's own receipt clock; a later trade cannot refresh it."""
    return market_freshness(getattr(tick, 'book_time', None), asof, stale_seconds,
                            received_at=getattr(tick, 'book_received_at', None),
                            received_monotonic=getattr(tick, 'book_received_monotonic', None))
