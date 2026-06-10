"""
聚宽版（jqdatasdk）选股 + 交易 单元测试（全部离线，mock 掉网络/SDK）

运行：
    python3 -m unittest -v test_jq.py
"""

import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

import jq_data as jd
import jq_selector as sel
import jq_trader as trd


class TestCodeConversion(unittest.TestCase):
    def test_to_jq_code(self):
        self.assertEqual(jd.to_jq_code("600519"), "600519.XSHG")
        self.assertEqual(jd.to_jq_code("000001"), "000001.XSHE")
        self.assertEqual(jd.to_jq_code("300750"), "300750.XSHE")
        self.assertEqual(jd.to_jq_code("688981"), "688981.XSHG")
        self.assertEqual(jd.to_jq_code("830799"), "830799.BJSE")
        self.assertEqual(jd.to_jq_code("000001.XSHE"), "000001.XSHE")
        self.assertEqual(jd.to_jq_code("1"), "000001.XSHE")

    def test_from_jq_code(self):
        self.assertEqual(jd.from_jq_code("600519.XSHG"), "600519")
        self.assertEqual(jd.from_jq_code("000001"), "000001")

    def test_get_board(self):
        self.assertEqual(jd.get_board("600519.XSHG"), "主板(沪)")
        self.assertEqual(jd.get_board("000001.XSHE"), "主板(深)")
        self.assertEqual(jd.get_board("300750"), "创业板")
        self.assertEqual(jd.get_board("688981"), "科创板")
        self.assertEqual(jd.get_board("830799"), "北交所")


class TestConsecutiveInflow(unittest.TestCase):
    def test_counts_from_latest(self):
        self.assertEqual(jd.consecutive_inflow_days([1, -1, 2, 3, 4]), 3)
        self.assertEqual(jd.consecutive_inflow_days([-1, -2]), 0)
        self.assertEqual(jd.consecutive_inflow_days([1, 2, 3]), 3)
        self.assertEqual(jd.consecutive_inflow_days([]), 0)
        # 最新一天（列表末位）是无效值 → 0
        self.assertEqual(jd.consecutive_inflow_days([5, "x"]), 0)
        # 最新一天有效、其前一天无效 → 1
        self.assertEqual(jd.consecutive_inflow_days(["x", 5]), 1)


class TestSelector(unittest.TestCase):
    def _universe(self):
        return pd.DataFrame(
            {"display_name": ["甲", "乙ST", "丙"], "name": ["A", "B", "C"],
             "start_date": ["2010-01-01", "2010-01-01", "2010-01-01"],
             "end_date": ["2200-01-01", "2200-01-01", "2200-01-01"]},
            index=["600000.XSHG", "600001.XSHG", "000002.XSHE"],
        )

    def test_filter_universe_excludes_st(self):
        df = jd.filter_universe(self._universe(), exclude_st=True,
                                exclude_new_days=0, ref_date="2026-06-10")
        self.assertIn("600000.XSHG", df.index)
        self.assertNotIn("600001.XSHG", df.index)  # ST 被剔除

    def test_select_candidates_end_to_end(self):
        uni = self._universe()
        val = pd.DataFrame(
            {"market_cap": [100.0, 2000.0],   # 第二只市值超上限，应被剔除
             "turnover_ratio": [10.0, 10.0],
             "circulating_market_cap": [80.0, 1500.0],
             "pe_ratio": [20, 30], "pb_ratio": [2, 3]},
            index=["600000.XSHG", "000002.XSHE"],
        )
        mf = pd.DataFrame(
            {"net_pct_main": [12.0, 3.0],     # 第二只低于阈值，应被剔除
             "net_amount_main": [5000.0, 100.0],
             "change_pct": [3, 1]},
            index=["600000.XSHG", "000002.XSHE"],
        )
        hist = {"600000.XSHG": [100.0, 200.0, -50.0, 300.0, 400.0]}

        with mock.patch.object(sel.jd, "get_universe", return_value=uni), \
             mock.patch.object(sel.jd, "filter_universe",
                               side_effect=lambda df, **k: df.loc[["600000.XSHG", "000002.XSHE"]]), \
             mock.patch.object(sel.jd, "get_valuation_oneday", return_value=val), \
             mock.patch.object(sel.jd, "get_money_flow_oneday", return_value=mf), \
             mock.patch.object(sel.jd, "get_money_flow_history", return_value=hist), \
             mock.patch.object(sel.jd, "get_security_name", side_effect=lambda c: "测试股"), \
             mock.patch.object(sel, "JQ_MIN_NET_PCT_MAIN", 5.0), \
             mock.patch.object(sel, "JQ_MIN_MARKET_CAP", 50.0), \
             mock.patch.object(sel, "JQ_MAX_MARKET_CAP", 1000.0), \
             mock.patch.object(sel, "JQ_MAX_TURNOVER", 30.0):
            cands = sel.select_candidates(date="2026-06-10")

        # 只剩第一只（市值&净占比都达标）
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["代码"], "600000")
        self.assertEqual(c["今日主力净占比"], 12.0)
        self.assertEqual(c["连续净流入天数"], 2)  # 末两日 300,400 >0


class TestPositionSizing(unittest.TestCase):
    def test_rounds_to_lot(self):
        # 现金10万，30%仓位=3万，价10元 → 3000股
        self.assertEqual(trd.position_size(100000, 10.0, 0.3), 3000)

    def test_insufficient_for_one_lot(self):
        self.assertEqual(trd.position_size(500, 10.0, 1.0), 0)  # 一手1000元>500

    def test_absolute_budget(self):
        # pct>1 视为绝对金额
        self.assertEqual(trd.position_size(100000, 10.0, 20000), 2000)

    def test_never_exceeds_cash(self):
        shares = trd.position_size(10000, 10.0, 5.0)  # 预算5万但只有1万
        self.assertLessEqual(shares * 10.0, 10000)


class TestRiskExit(unittest.TestCase):
    def _pos(self, entry=10.0, highest=10.0):
        return trd.Position(code="x", name="x", shares=100,
                            entry_price=entry, highest_price=highest)

    def test_stop_loss(self):
        r = trd.decide_exit(self._pos(), 9.5, take_profit=0.08,
                            stop_loss=0.04, trail_stop=0.03)
        self.assertIn("止损", r)

    def test_take_profit(self):
        r = trd.decide_exit(self._pos(), 10.9, take_profit=0.08,
                            stop_loss=0.04, trail_stop=0.03)
        self.assertIn("止盈", r)

    def test_trail_stop(self):
        # 最高12，现价11.5（仍浮盈），回撤=0.5/12=4.2%>3% → 移动止盈
        r = trd.decide_exit(self._pos(highest=12.0), 11.5, take_profit=0.20,
                            stop_loss=0.10, trail_stop=0.03)
        self.assertIn("移动止盈", r)

    def test_hold(self):
        r = trd.decide_exit(self._pos(), 10.1, take_profit=0.08,
                            stop_loss=0.04, trail_stop=0.03)
        self.assertIsNone(r)


class TestTradingHours(unittest.TestCase):
    def test_in_hours(self):
        self.assertTrue(trd.in_trading_hours(datetime(2026, 6, 10, 10, 0)))
        self.assertTrue(trd.in_trading_hours(datetime(2026, 6, 10, 14, 0)))
        self.assertFalse(trd.in_trading_hours(datetime(2026, 6, 10, 12, 0)))
        self.assertFalse(trd.in_trading_hours(datetime(2026, 6, 10, 8, 0)))

    def test_near_close(self):
        self.assertTrue(trd.near_market_close(datetime(2026, 6, 10, 14, 56), minutes=5))
        self.assertFalse(trd.near_market_close(datetime(2026, 6, 10, 14, 50), minutes=5))


class TestPaperBroker(unittest.TestCase):
    def test_buy_sell_cycle(self):
        b = trd.PaperBroker(cash=100000, min_commission=5, commission_rate=0.00025,
                            stamp_tax=0.001)
        self.assertTrue(b.buy("600000.XSHG", "甲", 1000, 10.0))
        self.assertIn("600000.XSHG", b.get_positions())
        self.assertLess(b.get_cash(), 100000)
        # 涨到 11 卖出，应盈利
        self.assertTrue(b.sell("600000.XSHG", 1000, 11.0))
        self.assertNotIn("600000.XSHG", b.get_positions())
        self.assertGreater(b.get_cash(), 100000)

    def test_buy_blocked_when_insufficient_cash(self):
        b = trd.PaperBroker(cash=1000)
        self.assertFalse(b.buy("x", "x", 1000, 10.0))  # 需1万

    def test_total_equity(self):
        b = trd.PaperBroker(cash=100000)
        b.buy("600000.XSHG", "甲", 1000, 10.0)
        eq = b.total_equity(lambda c: 12.0)  # 现价12
        self.assertGreater(eq, 100000)


class TestLiveBroker(unittest.TestCase):
    def test_raises(self):
        with self.assertRaises(NotImplementedError):
            trd.LiveBroker()


class TestIntradayTrader(unittest.TestCase):
    def test_open_and_exit(self):
        b = trd.PaperBroker(cash=100000)
        prices = {"600000.XSHG": 10.0}

        trader = trd.IntradayTrader(
            broker=b, price_func=lambda c: prices.get(c, 0.0),
            max_positions=2, per_position_pct=0.3, poll_seconds=0,
        )
        candidates = [{"jq代码": "600000.XSHG", "名称": "甲", "今日主力净占比": 12.0}]
        trader.open_positions(candidates)
        self.assertIn("600000.XSHG", b.get_positions())

        # 价格跳涨触发止盈，check_exits 应卖出
        prices["600000.XSHG"] = 11.0
        trader.check_exits()
        self.assertNotIn("600000.XSHG", b.get_positions())

    def test_run_demo_loop(self):
        b = trd.PaperBroker(cash=100000)
        prices = {"600000.XSHG": 10.0}
        trader = trd.IntradayTrader(
            broker=b, price_func=lambda c: prices.get(c, 0.0),
            max_positions=1, per_position_pct=0.3, poll_seconds=0,
        )
        cands = [{"jq代码": "600000.XSHG", "名称": "甲", "今日主力净占比": 12.0}]
        # respect_hours=False 让其在任意时间跑一次循环
        trader.run(cands, max_loops=1, respect_hours=False)
        self.assertTrue(len(b.trades) >= 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
