"""Unit tests for gpu.py helpers that don't require an actual OpenCL device."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import pytest

from hash256_miner.gpu import (
    DEFAULT_GLOBAL_FLOOR,
    DEFAULT_LOCAL_CEILING,
    DEFAULT_NONCES_PER_ITEM,
    DEFAULT_OVER_SUBSCRIBE,
    MAX_NONCES_PER_ITEM,
    _resolve_nonces_per_item,
    auto_work_size,
)


@dataclass
class _FakeDevice:
    max_work_group_size: int = 1024
    max_compute_units: int = 20


def test_auto_work_size_defaults_use_local_ceiling_when_mwg_is_large():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=20)
    local, global_size = auto_work_size(dev)
    assert local == DEFAULT_LOCAL_CEILING
    # adaptive = 20 * 256 * 256 = 1.31M, below floor → floor applies.
    assert global_size == DEFAULT_GLOBAL_FLOOR


def test_auto_work_size_high_cu_scales_beyond_floor():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=128)
    local, global_size = auto_work_size(dev)
    assert local == 256
    expected = 128 * 256 * DEFAULT_OVER_SUBSCRIBE
    assert global_size == expected
    assert global_size > DEFAULT_GLOBAL_FLOOR


def test_auto_work_size_small_mwg_caps_local():
    dev = _FakeDevice(max_work_group_size=128, max_compute_units=20)
    local, global_size = auto_work_size(dev)
    assert local == 128
    # Floor applies; ensure divisibility.
    assert global_size >= DEFAULT_GLOBAL_FLOOR
    assert global_size % local == 0


def test_auto_work_size_local_override_respected():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=20)
    local, global_size = auto_work_size(dev, local_override=128)
    assert local == 128
    assert global_size % 128 == 0
    assert global_size >= DEFAULT_GLOBAL_FLOOR


def test_auto_work_size_global_override_respected():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=20)
    local, global_size = auto_work_size(dev, global_override=1 << 24)
    assert local == DEFAULT_LOCAL_CEILING
    assert global_size == 1 << 24


def test_auto_work_size_global_rounded_to_local_multiple():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=20)
    # Override global to a value not a multiple of local; we should round down.
    _, global_size = auto_work_size(dev, local_override=200, global_override=1 << 22)
    assert global_size % 200 == 0
    assert global_size <= (1 << 22)


def test_auto_work_size_local_exceeds_mwg_raises():
    dev = _FakeDevice(max_work_group_size=64, max_compute_units=20)
    with pytest.raises(ValueError, match="exceeds device"):
        auto_work_size(dev, local_override=128)


def test_auto_work_size_global_below_local_raises():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=20)
    with pytest.raises(ValueError, match="must be >="):
        auto_work_size(dev, local_override=256, global_override=128)


def test_auto_work_size_invalid_local_raises():
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=20)
    with pytest.raises(ValueError, match=">= 1"):
        auto_work_size(dev, local_override=0)


def test_auto_work_size_env_over_subscribe(monkeypatch):
    monkeypatch.setenv("HASH256_OVER_SUBSCRIBE", "512")
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=128)
    _, global_size = auto_work_size(dev)
    assert global_size == 128 * 256 * 512


def test_auto_work_size_env_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("HASH256_OVER_SUBSCRIBE", "not-a-number")
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=128)
    _, global_size = auto_work_size(dev)
    # Falls back to default 256
    assert global_size == 128 * 256 * DEFAULT_OVER_SUBSCRIBE


def test_auto_work_size_env_negative_falls_back(monkeypatch):
    monkeypatch.setenv("HASH256_OVER_SUBSCRIBE", "-5")
    dev = _FakeDevice(max_work_group_size=1024, max_compute_units=128)
    _, global_size = auto_work_size(dev)
    assert global_size == 128 * 256 * DEFAULT_OVER_SUBSCRIBE


def test_auto_work_size_missing_attrs_uses_defaults():
    class BareDevice:
        pass

    local, global_size = auto_work_size(BareDevice())
    assert local == DEFAULT_LOCAL_CEILING
    assert global_size >= DEFAULT_GLOBAL_FLOOR
    assert global_size % local == 0


# ---------------------------------------------------------------------------
# _resolve_nonces_per_item
# ---------------------------------------------------------------------------

def test_nonces_per_item_default(monkeypatch):
    monkeypatch.delenv("HASH256_NONCES_PER_ITEM", raising=False)
    assert _resolve_nonces_per_item() == DEFAULT_NONCES_PER_ITEM


def test_nonces_per_item_empty_string_is_default(monkeypatch):
    monkeypatch.setenv("HASH256_NONCES_PER_ITEM", "")
    assert _resolve_nonces_per_item() == DEFAULT_NONCES_PER_ITEM


@pytest.mark.parametrize("n", [1, 2, 4, 8, 16, 32, 64, 128, 256])
def test_nonces_per_item_valid_powers_of_two(monkeypatch, n: int):
    monkeypatch.setenv("HASH256_NONCES_PER_ITEM", str(n))
    assert _resolve_nonces_per_item() == n


def test_nonces_per_item_rejects_zero(monkeypatch):
    monkeypatch.setenv("HASH256_NONCES_PER_ITEM", "0")
    with pytest.raises(ValueError, match=r"in \[1, "):
        _resolve_nonces_per_item()


def test_nonces_per_item_rejects_too_large(monkeypatch):
    monkeypatch.setenv("HASH256_NONCES_PER_ITEM", str(MAX_NONCES_PER_ITEM * 2))
    with pytest.raises(ValueError, match=r"in \[1, "):
        _resolve_nonces_per_item()


def test_nonces_per_item_rejects_non_power_of_two(monkeypatch):
    monkeypatch.setenv("HASH256_NONCES_PER_ITEM", "48")
    with pytest.raises(ValueError, match="power of two"):
        _resolve_nonces_per_item()


def test_nonces_per_item_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("HASH256_NONCES_PER_ITEM", "abc")
    with pytest.raises(ValueError, match="integer"):
        _resolve_nonces_per_item()
