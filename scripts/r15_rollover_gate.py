#!/usr/bin/env python3
"""Avoid starting a long portfolio generation immediately before 08:00 Moscow.

The underlying R15 day-boundary assertion remains authoritative; this gate
prevents predictable wasted runs without publishing stale or cross-day picks.
"""
import argparse
import datetime as dt

MSK = dt.timezone(dt.timedelta(hours=3))


def defer_generation(now: dt.datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("An aware timestamp is required")
    local = now.astimezone(MSK)
    rollover = local.replace(hour=8, minute=0, second=0, microsecond=0)
    return local < rollover and (rollover - local) <= dt.timedelta(minutes=30)


def self_test() -> None:
    for hour, minute, expected in [
        (7, 29, False),
        (7, 30, True),
        (7, 45, True),
        (7, 59, True),
        (8, 0, False),
        (8, 1, False),
        (23, 59, False),
    ]:
        sample = dt.datetime(2026, 10, 23, hour, minute, tzinfo=MSK)
        assert defer_generation(sample) is expected, (sample, expected)
    print("R15_ROLLOVER_GATE_REGRESSION=GREEN")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--should-defer", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.should_defer:
        raise SystemExit(0 if defer_generation(dt.datetime.now(dt.timezone.utc)) else 1)
    else:
        parser.error("Choose --self-test or --should-defer")
