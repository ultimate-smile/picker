from datetime import date, timedelta

import pandas as pd

from jq_seven_day_strategy import run_strategy


def make_bars(closes, opens=None, start=date(2026, 1, 1)):
    if opens is None:
        opens = closes
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(len(closes))],
        "open": opens,
        "close": closes,
    })


def test_buy_after_seven_down_days_when_day8_open_not_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.6]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.3]
    result = run_strategy(make_bars(closes, opens), code="000001", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 9), initial_cash=100000)

    assert len(result.trades) == 1
    assert result.trades[0].side == "buy"
    assert result.trades[0].date == date(2026, 1, 9)
    assert result.trades[0].price == 9.3
    assert result.shares == 10700


def test_skip_buy_when_day8_open_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.6]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.29]
    result = run_strategy(make_bars(closes, opens), code="000001", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 9), initial_cash=100000)

    assert result.trades == []
    assert result.final_equity == 100000


def test_sell_when_holding_open_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.6, 9.7, 9.8]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.3, 9.6, 9.69]
    result = run_strategy(make_bars(closes, opens), code="000001", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 11), initial_cash=100000)

    assert [t.side for t in result.trades] == ["buy", "sell"]
    assert result.trades[1].date == date(2026, 1, 11)
    assert result.trades[1].price == 9.69
    assert result.shares == 0


def test_sell_after_seven_consecutive_up_days():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 10.0]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9]
    result = run_strategy(make_bars(closes, opens), code="000001", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 15), initial_cash=100000)

    assert [t.side for t in result.trades] == ["buy", "sell"]
    assert result.trades[1].date == date(2026, 1, 15)
    assert result.trades[1].price == 10.0
    assert result.trades[1].reason == "连续上涨7个交易日"
