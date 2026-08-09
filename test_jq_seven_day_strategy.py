from datetime import date, timedelta
from unittest import mock

import pandas as pd

from jq_seven_day_strategy import fetch_daily_bars, normalise_bars, run_strategy


def make_bars(closes, opens=None, start=date(2026, 1, 1)):
    if opens is None:
        opens = closes
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(len(closes))],
        "open": opens,
        "close": closes,
    })


def test_normalise_bars_reads_shizixi_payload_bars():
    payload = {
        "items": [{
            "code": "688188",
            "bars": [
                {"date": "2026-01-06T00:00:00", "open": 9.9, "close": 9.8},
                {"date": "2026-01-05T00:00:00", "open": 10.0, "close": 9.9},
            ],
        }]
    }

    bars = normalise_bars(payload, "688188")

    assert bars["date"].tolist() == [date(2026, 1, 5), date(2026, 1, 6)]
    assert bars["open"].tolist() == [10.0, 9.9]
    assert bars["close"].tolist() == [9.9, 9.8]


def test_fetch_daily_bars_calls_shizixi_api_and_parses_response():
    response = mock.Mock()
    response.__enter__ = mock.Mock(return_value=response)
    response.__exit__ = mock.Mock(return_value=None)
    response.read.return_value = b'{"errors": [], "items": [{"code": "688188", "bars": [{"date": "2026-01-05T00:00:00", "open": 10, "close": 9.9}]}]}'

    opener = mock.Mock()
    opener.open.return_value = response
    with mock.patch("jq_seven_day_strategy.build_opener", return_value=opener):
        bars = fetch_daily_bars("688188.XSHG", "2026-01-05", "2026-08-07", api_url="https://example.test/kline")

    called_url = opener.open.call_args.args[0]
    assert called_url.startswith("https://example.test/kline?")
    assert "codes=688188" in called_url
    assert "period=daily" in called_url
    assert "adjust=qfq" in called_url
    assert "since=2026-01-05" in called_url
    assert "to=2026-08-07" in called_url
    assert bars.iloc[0]["date"] == date(2026, 1, 5)


def test_buy_after_seven_down_days_when_day8_open_not_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.6]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.3]
    result = run_strategy(make_bars(closes, opens), code="688188", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 9), initial_cash=100000)

    assert len(result.trades) == 1
    assert result.trades[0].side == "buy"
    assert result.trades[0].date == date(2026, 1, 9)
    assert result.trades[0].price == 9.3
    assert result.shares == 10700


def test_skip_buy_when_day8_open_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.6]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.29]
    result = run_strategy(make_bars(closes, opens), code="688188", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 9), initial_cash=100000)

    assert result.trades == []
    assert result.final_equity == 100000


def test_sell_when_holding_open_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.6, 9.7, 9.8]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.3, 9.6, 9.69]
    result = run_strategy(make_bars(closes, opens), code="688188", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 11), initial_cash=100000)

    assert [t.side for t in result.trades] == ["buy", "sell"]
    assert result.trades[1].date == date(2026, 1, 11)
    assert result.trades[1].price == 9.69
    assert result.shares == 0


def test_sell_after_seven_following_opens_not_below_previous_close():
    closes = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 10.0, 10.1]
    opens = [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 10.0]
    result = run_strategy(make_bars(closes, opens), code="688188", start_date=date(2026, 1, 1),
                          end_date=date(2026, 1, 16), initial_cash=100000)

    assert [t.side for t in result.trades] == ["buy", "sell"]
    assert result.trades[1].date == date(2026, 1, 16)
    assert result.trades[1].price == 10.0
    assert result.trades[1].reason == "连续7个交易日开盘价不低于前收盘价"
