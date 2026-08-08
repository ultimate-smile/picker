import unittest
from unittest import mock

import pandas as pd

import shizixi_strategy as sz


def sample_df(n=120):
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = pd.Series(range(100, 100 + n), dtype=float)
    df = pd.DataFrame({
        "date": dates,
        "open": close - 0.5,
        "high": close + 0.2,
        "low": close - 1.0,
        "close": close,
        "volume": [1000.0] * (n - 1) + [3000.0],
        "amount": close * 1000,
    })
    return df


class TestShizixiStrategy(unittest.TestCase):
    def test_fetch_uses_params_and_bars(self):
        payload = {"errors": [], "items": [{"code": "688188", "bars": sample_df(3).assign(date=lambda d: d["date"].astype(str)).to_dict("records")}]} 
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        with mock.patch.object(sz.requests, "get", return_value=resp) as get:
            data = sz.fetch_kline_batch(["688188"], "2020-12-29", "2026-08-07", adjust="qfq")
        self.assertIn("688188", data)
        self.assertEqual(get.call_args.kwargs["params"]["adjust"], "qfq")
        self.assertEqual(get.call_args.kwargs["params"]["codes"], "688188")

    def test_backtest_outputs_buy_advice(self):
        df = sample_df(120)
        result = sz.backtest("688188", df)
        self.assertEqual(result.last_signal, "BUY")
        self.assertGreaterEqual(result.trades, 0)
        self.assertIn("下一交易日", result.suggestion)

    def test_rank_prefers_higher_return_then_win_rate(self):
        a = sz.BacktestResult("A", 1, .9, .1, .1, -.1, 1, 1, "HOLD", "", None, None, None)
        b = sz.BacktestResult("B", 1, .5, .2, .2, -.2, 1, 1, "HOLD", "", None, None, None)
        self.assertEqual(sz.rank_results([a, b])[0].code, "B")


if __name__ == "__main__":
    unittest.main()
