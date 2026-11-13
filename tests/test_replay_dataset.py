"""The archived training set (docs/DATA.md) and the CSV path of replay_history.py.

Pinned here:
* the committed CSV still matches the checksum in its .meta.json (nobody edited it by hand)
* the generator is deterministic for a pinned end time, so the archive is reproducible
* export -> read round-trips every column with the right types
* loading shifts by whole weeks only, so weekday and hour-of-day survive
* the dataset is fault-free, as the anomaly detector's baseline must be
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "ml/datasets/mojaloop_replay_28d.csv"
META = DATASET.with_suffix(".meta.json")


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("replay_history", ROOT / "scripts/replay_history.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_history"] = module
    spec.loader.exec_module(module)
    return module


def test_archived_csv_matches_its_recorded_checksum(replay):
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert replay.sha256_of(DATASET) == meta["sha256"]
    assert meta["rows"] == sum(meta["rows_per_service"].values())


def test_archived_csv_is_fault_free_and_complete(replay):
    rows = list(replay.read_csv(DATASET))
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert len(rows) == meta["rows"]
    assert {r["service"] for r in rows} == set(meta["rows_per_service"])
    # A clean baseline: no error burst anywhere (the live alert threshold is 1%).
    assert max(r["error_ratio"] for r in rows) < 0.01
    assert all(json.loads(r["labels"])["source"] == "replay" for r in rows)


def test_export_is_reproducible_for_a_pinned_end(replay, tmp_path):
    end = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    a = replay.export_csv(tmp_path / "a.csv", days=1, end=end)
    b = replay.export_csv(tmp_path / "b.csv", days=1, end=end)
    assert a["sha256"] == b["sha256"]
    assert a["rows"] == 1 * 24 * 60 * 5  # one day, 1-minute fine tier, five services
    assert "--end 2026-01-05T12:00:00+00:00" in a["generator"]["reproduce"]


def test_export_then_read_round_trips(replay, tmp_path):
    end = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    path = tmp_path / "rt.csv"
    replay.export_csv(path, days=1, end=end)
    original = list(
        replay.generate_rows(
            days=1, end=end, base_tps=25.0, seasonality=True, platform="mojaloop"
        )
    )
    loaded = list(replay.read_csv(path))
    assert len(loaded) == len(original)
    for want, got in zip(original[:50], loaded[:50], strict=True):
        for key in replay.METRIC_COLUMNS:
            if key == "labels":
                continue
            assert got[key] == want[key], key
        assert isinstance(got["request_count"], int)
        labels = json.loads(got["labels"])
        assert labels["source"] == "replay"
        assert labels["dataset"] == "rt.csv"


@pytest.mark.parametrize("gap_days", [0, 1, 6, 7, 8, 30])
def test_week_shift_preserves_weekday_and_hour(replay, gap_days):
    last = datetime(2026, 11, 13, 11, 59, tzinfo=UTC)
    target = last + timedelta(days=gap_days, hours=3)
    shift = replay.week_shift(last, target)
    moved = last + shift
    assert shift % timedelta(weeks=1) == timedelta(0)
    assert moved <= target
    assert target - moved < timedelta(weeks=1)
    assert (moved.weekday(), moved.hour, moved.minute) == (last.weekday(), last.hour, last.minute)
