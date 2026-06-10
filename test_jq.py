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
import jq_main as jm
import jq_analyst as ana


class TestCodeConversion(unittest.TestCase):
    def test_to_jq_code(self):
        self.assertEqual(jd.to_jq_code("600519"), "600519.XSHG")
        self.assertEqual(jd.to_jq_code("000001"), "000001.XSHE")
        self.assertEqual(jd.to_jq_code("300750"), "300750.XSHE")
        self.assertEqual(jd.to_jq_code("688981"), "688981.XSHG")
        self.assertEqual(jd.to_jq_code("830799"), "830799.BJSE")
        self.assertEqual(jd.to_jq_code("920819"), "920819.BJSE")  # 北交所新代码 9 开头
        self.assertEqual(jd.to_jq_code("430047"), "430047.BJSE")
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
        self.assertEqual(jd.get_board("920819"), "北交所")   # 9 开头新代码
        self.assertEqual(jd.get_board("430047"), "北交所")


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


class TestLastPriceFallback(unittest.TestCase):
    def test_falls_back_to_get_price_when_tick_unavailable(self):
        """无实时 tick 权限时应降级到 get_price(分钟/日线)"""
        called = {"price": 0}

        def fake_get_price(code, **kwargs):
            called["price"] += 1
            return pd.DataFrame({"close": [12.34]})

        with mock.patch.object(jd, "ensure_auth", lambda: None), \
             mock.patch.object(jd.jq, "get_current_tick",
                               side_effect=Exception("no realtime permission")), \
             mock.patch.object(jd.jq, "get_price", side_effect=fake_get_price):
            px = jd.get_last_price("000001.XSHE")
        self.assertEqual(px, 12.34)
        self.assertGreaterEqual(called["price"], 1)

    def test_uses_tick_when_available(self):
        tick_df = pd.DataFrame({"current": [10.5]}, index=[0])
        with mock.patch.object(jd, "ensure_auth", lambda: None), \
             mock.patch.object(jd.jq, "get_current_tick", return_value=tick_df):
            px = jd.get_last_price("000001.XSHE")
        self.assertEqual(px, 10.5)


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
             "net_amount_main": [5000.0, 100.0]},
            index=["600000.XSHG", "000002.XSHE"],
        )
        # 价格/涨跌停：第一只温和上涨可操作
        px = pd.DataFrame(
            {"change_pct": [3.0], "is_paused": [False], "is_limit_up": [False],
             "near_limit_up": [False], "is_limit_down": [False]},
            index=["600000.XSHG"],
        )
        hist = {"600000.XSHG": [100.0, 200.0, -50.0, 300.0, 400.0]}

        with mock.patch.object(sel.jd, "get_universe", return_value=uni), \
             mock.patch.object(sel.jd, "filter_universe",
                               side_effect=lambda df, **k: df.loc[["600000.XSHG", "000002.XSHE"]]), \
             mock.patch.object(sel.jd, "get_valuation_oneday", return_value=val), \
             mock.patch.object(sel.jd, "get_money_flow_oneday", return_value=mf), \
             mock.patch.object(sel.jd, "get_price_oneday", return_value=px), \
             mock.patch.object(sel.jd, "get_money_flow_history", return_value=hist), \
             mock.patch.object(sel.jd, "get_security_name", side_effect=lambda c: "测试股"), \
             mock.patch.object(sel, "JQ_MIN_NET_PCT_MAIN", 5.0), \
             mock.patch.object(sel, "JQ_MAX_NET_PCT_MAIN", 25.0), \
             mock.patch.object(sel, "JQ_MIN_MARKET_CAP", 50.0), \
             mock.patch.object(sel, "JQ_MAX_MARKET_CAP", 1000.0), \
             mock.patch.object(sel, "JQ_MIN_TURNOVER", 2.0), \
             mock.patch.object(sel, "JQ_MAX_TURNOVER", 30.0):
            cands = sel.select_candidates(date="2026-06-10")

        # 只剩第一只（市值&净占比&可操作都达标）
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["代码"], "600000")
        self.assertEqual(c["今日主力净占比"], 12.0)
        self.assertEqual(c["今日涨跌幅(%)"], 3.0)
        self.assertEqual(c["连续净流入天数"], 2)  # 末两日 300,400 >0
        self.assertIn("综合评分", c)

    def test_limit_up_filtered_out(self):
        """涨停/接近涨停的票应被剔除（买不进、追高）"""
        uni = self._universe()
        val = pd.DataFrame(
            {"market_cap": [100.0], "turnover_ratio": [10.0],
             "circulating_market_cap": [80.0], "pe_ratio": [20], "pb_ratio": [2]},
            index=["600000.XSHG"],
        )
        mf = pd.DataFrame(
            {"net_pct_main": [20.0], "net_amount_main": [9000.0]},
            index=["600000.XSHG"],
        )
        # 涨停：is_limit_up=True → 应被剔除
        px = pd.DataFrame(
            {"change_pct": [10.0], "is_paused": [False], "is_limit_up": [True],
             "near_limit_up": [True], "is_limit_down": [False]},
            index=["600000.XSHG"],
        )
        with mock.patch.object(sel.jd, "get_universe", return_value=uni), \
             mock.patch.object(sel.jd, "filter_universe",
                               side_effect=lambda df, **k: df.loc[["600000.XSHG"]]), \
             mock.patch.object(sel.jd, "get_valuation_oneday", return_value=val), \
             mock.patch.object(sel.jd, "get_money_flow_oneday", return_value=mf), \
             mock.patch.object(sel.jd, "get_price_oneday", return_value=px), \
             mock.patch.object(sel.jd, "get_money_flow_history", return_value={}), \
             mock.patch.object(sel.jd, "get_security_name", side_effect=lambda c: "测试股"):
            cands = sel.select_candidates(date="2026-06-10")
        self.assertEqual(cands, [])  # 涨停被剔除，无候选

    def test_default_returns_final_picks(self):
        """默认只返回 JQ_FINAL_PICKS 只（如 3 只），而非整个候选池"""
        n = 8
        codes = [f"60000{i}.XSHG" for i in range(n)]
        uni = pd.DataFrame(
            {"display_name": [f"股{i}" for i in range(n)],
             "name": [f"S{i}" for i in range(n)],
             "start_date": ["2010-01-01"] * n, "end_date": ["2200-01-01"] * n},
            index=codes,
        )
        val = pd.DataFrame(
            {"market_cap": [100.0] * n, "turnover_ratio": [10.0] * n},
            index=codes,
        )
        mf = pd.DataFrame(
            {"net_pct_main": [10.0 + i for i in range(n)],  # 各不相同便于排序
             "net_amount_main": [1000.0] * n},
            index=codes,
        )
        px = pd.DataFrame(
            {"change_pct": [3.0] * n, "is_paused": [False] * n,
             "is_limit_up": [False] * n, "near_limit_up": [False] * n,
             "is_limit_down": [False] * n},
            index=codes,
        )
        with mock.patch.object(sel.jd, "get_universe", return_value=uni), \
             mock.patch.object(sel.jd, "filter_universe", side_effect=lambda df, **k: df), \
             mock.patch.object(sel.jd, "get_valuation_oneday", return_value=val), \
             mock.patch.object(sel.jd, "get_money_flow_oneday", return_value=mf), \
             mock.patch.object(sel.jd, "get_price_oneday", return_value=px), \
             mock.patch.object(sel.jd, "get_money_flow_history", return_value={}), \
             mock.patch.object(sel.jd, "get_security_name", side_effect=lambda c: "测试股"), \
             mock.patch.object(sel, "JQ_FINAL_PICKS", 3):
            cands = sel.select_candidates(date="2026-06-10")
        self.assertEqual(len(cands), 3)
        # 可显式指定数量覆盖默认
        with mock.patch.object(sel.jd, "get_universe", return_value=uni), \
             mock.patch.object(sel.jd, "filter_universe", side_effect=lambda df, **k: df), \
             mock.patch.object(sel.jd, "get_valuation_oneday", return_value=val), \
             mock.patch.object(sel.jd, "get_money_flow_oneday", return_value=mf), \
             mock.patch.object(sel.jd, "get_price_oneday", return_value=px), \
             mock.patch.object(sel.jd, "get_money_flow_history", return_value={}), \
             mock.patch.object(sel.jd, "get_security_name", side_effect=lambda c: "测试股"):
            cands5 = sel.select_candidates(date="2026-06-10", top_n=5)
        self.assertEqual(len(cands5), 5)


class TestCliCodes(unittest.TestCase):
    """自定义股票池命令行解析"""

    def test_parse_codes_inline(self):
        self.assertEqual(jm._parse_codes(["--select", "--codes", "600000,000001"]),
                         ["600000", "000001"])

    def test_parse_codes_equals_and_fullwidth_comma(self):
        self.assertEqual(jm._parse_codes(["--codes=600000，300750"]),
                         ["600000", "300750"])

    def test_parse_codes_none(self):
        self.assertIsNone(jm._parse_codes(["--select"]))

    def test_read_watchlist(self):
        import tempfile, os
        content = "# 我的自选\n600000 000001\n300750,\n# 注释行\n688981\n"
        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            codes = jm._parse_codes(["--watchlist", path])
        finally:
            os.remove(path)
        self.assertEqual(codes, ["600000", "000001", "300750", "688981"])


class TestAnalystReport(unittest.TestCase):
    """操作报告：Claude 推送 + 不可用时本地规则化降级"""

    def _cands(self):
        return [{
            "代码": "600000", "jq代码": "600000.XSHG", "名称": "测试股",
            "板块": "主板(沪)", "总市值(亿)": 120.0, "换手率(%)": 8.0,
            "今日涨跌幅(%)": 3.0, "今日主力净占比": 12.0,
            "今日主力净流入(万)": 5000.0, "连续净流入天数": 3,
            "综合评分": 0.8, "近N日主力流向": "+100、+200、+300",
        }]

    def test_local_report_has_levels(self):
        rep = ana.local_report(self._cands(), final_picks=3,
                               price_func=lambda c: 10.0)
        self.assertIn("操作报告", rep)
        self.assertIn("买入区间", rep)
        self.assertIn("止损位", rep)
        self.assertIn("600000", rep)

    def test_local_report_no_price(self):
        rep = ana.local_report(self._cands(), price_func=lambda c: 0.0)
        self.assertIn("无法获取现价", rep)

    def test_generate_report_falls_back_without_key(self):
        with mock.patch.object(ana, "ANTHROPIC_API_KEY", ""):
            rep = ana.generate_report(self._cands(), final_picks=3,
                                      price_func=lambda c: 10.0)
        self.assertIn("本地规则化", rep)

    def test_use_claude_switch_off(self):
        with mock.patch.object(ana, "USE_CLAUDE", False):
            self.assertIsNone(ana.analyze_with_claude(self._cands()))

    def test_claude_failure_logs_and_falls_back(self):
        with mock.patch.object(ana, "USE_CLAUDE", True), \
             mock.patch.object(ana, "ANTHROPIC_API_KEY", "sk-test"), \
             mock.patch("anthropic.Anthropic",
                        side_effect=Exception("connection error")):
            rep = ana.generate_report(self._cands(), price_func=lambda c: 10.0)
        # 调用失败应降级到本地报告
        self.assertIn("本地规则化", rep)

    def test_generate_report_uses_claude_when_available(self):
        with mock.patch.object(ana, "analyze_with_claude",
                               return_value="CLAUDE_REPORT_OK"):
            rep = ana.generate_report(self._cands(), price_func=lambda c: 10.0)
        self.assertEqual(rep, "CLAUDE_REPORT_OK")

    def test_generate_report_empty(self):
        self.assertIn("无候选股", ana.generate_report([]))


class TestStrategyScoring(unittest.TestCase):
    """多因子评分与可操作性过滤"""

    def test_change_score_band(self):
        self.assertEqual(sel._change_score(3.0), 1.0)     # 温和上涨最佳
        self.assertEqual(sel._change_score(9.5), 0.0)     # 过热
        self.assertEqual(sel._change_score(-5.0), 0.0)    # 大跌
        self.assertGreater(sel._change_score(0.5), 0.0)

    def test_turnover_score_band(self):
        self.assertEqual(sel._turnover_score(8.0), 1.0)
        self.assertLess(sel._turnover_score(1.0), 1.0)    # 流动性差
        self.assertLess(sel._turnover_score(25.0), 1.0)   # 过热

    def test_inflow_and_consec_caps(self):
        self.assertEqual(sel._inflow_score(50.0), 1.0)    # 20% 封顶
        self.assertEqual(sel._consec_score(99), 1.0)      # 5 天封顶

    def test_composite_prefers_healthy(self):
        # 温和上涨 + 健康换手 + 连续流入，应高于追涨停的极端票
        healthy = sel.composite_score(10.0, 3, 4.0, 8.0)
        extreme = sel.composite_score(20.0, 0, 9.8, 25.0)
        self.assertGreater(healthy, extreme)

    def test_is_tradable(self):
        up = {"is_paused": False, "is_limit_up": True, "near_limit_up": True,
              "is_limit_down": False}
        ok = {"is_paused": False, "is_limit_up": False, "near_limit_up": False,
              "is_limit_down": False}
        self.assertFalse(sel.is_tradable(up))
        self.assertTrue(sel.is_tradable(ok))
        self.assertTrue(sel.is_tradable(up, exclude_near_limit=False))


class TestMoneyFlowProFallback(unittest.TestCase):
    """get_money_flow_pro 兜底：由 inflow/outflow/netflow 推导主力净额/净占比"""

    def _pro_df(self):
        # 单位：元。主力 = 超大单(xl) + 大单(l)
        return pd.DataFrame({
            "code": ["600000.XSHG"],
            "inflow_xl": [6_000_000.0], "inflow_l": [2_000_000.0],
            "inflow_m": [1_000_000.0], "inflow_s": [1_000_000.0],
            "outflow_xl": [1_000_000.0], "outflow_l": [1_000_000.0],
            "outflow_m": [4_000_000.0], "outflow_s": [4_000_000.0],
            "netflow_xl": [5_000_000.0], "netflow_l": [1_000_000.0],
        })

    def test_pro_main_amount_in_wan(self):
        s = jd._pro_main_amount(self._pro_df())
        # (5,000,000 + 1,000,000) 元 = 600 万元
        self.assertAlmostEqual(s.iloc[0], 600.0)

    def test_pro_to_main_pct(self):
        g = jd._pro_to_main(self._pro_df())
        self.assertAlmostEqual(g["net_amount_main"].iloc[0], 600.0)
        # 主力净额 600 万 / 总成交额 2000 万 * 100 = 30%
        self.assertAlmostEqual(g["net_pct_main"].iloc[0], 30.0)

    def test_pro_to_main_zero_total(self):
        df = self._pro_df()
        for c in ["inflow_xl", "inflow_l", "inflow_m", "inflow_s",
                  "outflow_xl", "outflow_l", "outflow_m", "outflow_s"]:
            df[c] = 0.0
        g = jd._pro_to_main(df)
        self.assertEqual(g["net_pct_main"].iloc[0], 0.0)  # 不除零


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
