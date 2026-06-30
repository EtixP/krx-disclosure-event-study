from __future__ import annotations

from scripts.run_intraday_walkforward import _mins, classify_entry, INTRADAY_CUTOFF_MIN


def test_mins_parses_hhmm():
    assert _mins("09:05") == 545
    assert _mins("15:30") == 930
    assert _mins(None) is None
    assert _mins("garbage") is None


def test_intraday_event_uses_same_day_close():
    # filed 11:00 -> can buy at the same-day close
    entry, mode = classify_entry(11 * 60, t0_close=100.0, t1_close=110.0)
    assert mode == "intraday"
    assert entry == 100.0


def test_after_close_event_uses_t1_close_no_lookahead():
    # filed 16:24 -> the same-day close happened BEFORE the news; must use T+1
    entry, mode = classify_entry(16 * 60 + 24, t0_close=100.0, t1_close=110.0)
    assert mode == "afterclose"
    assert entry == 110.0


def test_unknown_time_defaults_to_after_close():
    # no filing time -> conservative: cannot assume intraday, use T+1
    entry, mode = classify_entry(None, t0_close=100.0, t1_close=110.0)
    assert mode == "afterclose"
    assert entry == 110.0


def test_cutoff_boundary():
    # exactly at the cutoff -> treated as after-close (need margin to reach close)
    entry, mode = classify_entry(INTRADAY_CUTOFF_MIN, t0_close=100.0, t1_close=110.0)
    assert mode == "afterclose"
    entry, mode = classify_entry(INTRADAY_CUTOFF_MIN - 1, t0_close=100.0, t1_close=110.0)
    assert mode == "intraday"
