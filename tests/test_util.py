"""Time/unit helpers - the part that used to silently break claims."""

from __future__ import annotations

import time

from itlg_keeper.util import as_bool, as_float, humanize, pick_amount, to_epoch_seconds


def test_milliseconds_are_detected():
    assert to_epoch_seconds(1_760_000_000_000) == 1_760_000_000.0


def test_seconds_pass_through():
    assert to_epoch_seconds(1_760_000_000) == 1_760_000_000.0


def test_strings_and_garbage():
    assert to_epoch_seconds("1760000000000") == 1_760_000_000.0
    assert to_epoch_seconds("") is None
    assert to_epoch_seconds(None) is None
    assert to_epoch_seconds("soon") is None
    assert to_epoch_seconds(0) is None
    assert to_epoch_seconds(-5) is None


def test_humanize():
    assert humanize(None) == "-"
    assert humanize(-10) == "sekarang"
    assert humanize(45) == "45s"
    assert humanize(125) == "2m 05s"
    assert humanize(3 * 3600 + 12 * 60) == "3j 12m"
    assert humanize(2 * 86400 + 5 * 3600) == "2h 5j"


def test_coercions():
    assert as_float("12.5") == 12.5
    assert as_float("x", 3.0) == 3.0
    assert as_bool("true") is True
    assert as_bool(0) is False
    assert as_bool("nope") is False


def test_pick_amount_finds_nested_reward():
    payload = {"data": {"claim": {"reward": 3.25}}}
    assert pick_amount(payload) == 3.25


def test_pick_amount_ignores_zero_and_absent():
    assert pick_amount({"amount": 0}) is None
    assert pick_amount({"nothing": "here"}) is None
    assert pick_amount(None) is None


def test_now_is_sane():
    assert abs(time.time() - to_epoch_seconds(int(time.time() * 1000))) < 2
