"""
多维度综合选股编排层（jq_deep）单元测试（离线，mock 数据层）

运行：
    python3 -m unittest -v test_jq_deep.py
"""

import unittest
from unittest import mock

import pandas as pd

import jq_deep as dp


class TestTradePlan(unittest.TestCase):
    def _levels(self):
        return {"support": 9.5, "resistance": 11.0, "prior_high": 11.5}

    def test_plan_ordering_and_risk_cap(self):
        dims = {"technical": 0.8, "fundamental": 0.8, "chips": 0.6,
                "market": 0.7, "catalyst": 0.6}
        detail = {"technical": {"ma": {"below_ma20": False, "below_ma60": False}}}
        plan = dp.trade_plan(10.0, self._levels(), dims, detail)
        self.assertLess(plan["buy_low"], plan["buy_high"] + 1e-9)
        self.assertLess(plan["stop"], 10.0)
        self.assertLess(plan["target1"], plan["target2"])
        # 止损不超过最大风险（默认 8%）
        self.assertGreaterEqual(plan["stop"], 10.0 * (1 - dp.STOP_MAX_PCT) - 1e-6)
        self.assertIn("持有", plan["hold"])

    def test_no_price(self):
        plan = dp.trade_plan(0, self._levels(), {}, {})
        self.assertIn("note", plan)


class TestHoldingAdvice(unittest.TestCase):
    def test_below_ma60_exit(self):
        detail = {"technical": {"ma": {"below_ma20": True, "below_ma60": True}}}
        adv = dp._holding_advice({"fundamental": 0.9}, detail)
        self.assertIn("趋势走坏", adv)

    def test_below_ma20_warn(self):
        detail = {"technical": {"ma": {"below_ma20": True, "below_ma60": False}}}
        adv = dp._holding_advice({"fundamental": 0.9}, detail)
        self.assertIn("20 日线", adv)

    def test_healthy_strong_fundamental(self):
        detail = {"technical": {"ma": {"below_ma20": False, "below_ma60": False}}}
        adv = dp._holding_advice({"fundamental": 0.8}, detail)
        self.assertIn("持有", adv)
        self.assertIn("中线", adv)

    def test_weak_fundamental_short(self):
        detail = {"technical": {"ma": {"below_ma20": False, "below_ma60": False}}}
        adv = dp._holding_advice({"fundamental": 0.2}, detail)
        self.assertIn("短线", adv)


class TestPositionPct(unittest.TestCase):
    def test_unlock_shrinks_position(self):
        good = {"technical": 0.9, "market": 0.8, "fundamental": 0.8,
                "chips": 0.7, "catalyst": 0.8}
        risky = dict(good, catalyst=0.1)
        self.assertGreater(dp._position_pct(good), dp._position_pct(risky))

    def test_capped(self):
        full = {k: 1.0 for k in ("technical", "market", "fundamental",
                                 "chips", "catalyst")}
        self.assertLessEqual(dp._position_pct(full), dp.PER_POSITION_PCT + 1e-9)


class TestFormatReport(unittest.TestCase):
    def _pick(self):
        return {
            "代码": "688981", "名称": "测试芯", "板块": "科创板",
            "今日主力净占比": 12.0, "连续净流入天数": 3, "综合评分": 0.82,
            "现价": 50.0,
            "维度分": {"technical": 0.8, "market": 0.7, "fundamental": 0.75,
                       "chips": 0.6, "catalyst": 0.6},
            "维度明细": {"technical": {"ma": {"bull_align": True},
                                        "macd": {"golden": True, "above_zero": True},
                                        "volprice": {"divergence": False}},
                          "catalyst": {"unlock_rate": 0.0}},
            "操作计划": {"buy_low": 49.0, "buy_high": 50.5, "stop": 46.5,
                          "target1": 55.0, "target2": 60.0, "support": 47.5,
                          "resistance": 55.0, "risk_pct": 7.0, "reward_pct": 10.0,
                          "position_pct": 0.25, "hold": "站上 20 日均线，趋势健康：持有。"},
        }

    def test_report_contains_key_sections(self):
        rep = dp.format_report([self._pick()])
        for kw in ["综合选股报告", "买入区间", "止损位", "目标价1", "持有建议",
                   "688981", "MACD 金叉", "均线多头排列"]:
            self.assertIn(kw, rep)

    def test_empty(self):
        self.assertIn("观望", dp.format_report([]))

    def test_unlock_warning_shown(self):
        p = self._pick()
        p["维度明细"]["catalyst"] = {"unlock_rate": 0.12, "unlock_date": "2026-07-01"}
        rep = dp.format_report([p])
        self.assertIn("解禁", rep)


class TestScoreOne(unittest.TestCase):
    def test_score_one_with_mocked_data(self):
        closes = [10 + i * 0.2 for i in range(70)]
        bars = pd.DataFrame({"close": closes,
                             "high": [c + 0.2 for c in closes],
                             "low": [c - 0.2 for c in closes],
                             "volume": [1000 + (i % 2) * 400 for i in range(70)],
                             "turnover": [5.0] * 70})
        ctx = {"index_code": "000688.XSHG",
               "index_closes": [3000 + i for i in range(25)],
               "northbound": [1, 2, 3]}
        fundamentals = {"600000": {"rev_yoy": [5, 10, 20], "margin": [-2, 5],
                                   "net_profit": 100, "op_cash_flow": 80}}
        with mock.patch.object(dp.jf, "fetch_daily_bars", return_value=bars):
            total, dims, detail = dp.score_one(
                "600000.XSHG", board="主板(沪)", market_ctx=ctx,
                fundamentals=fundamentals, unlock={})
        self.assertTrue(0.0 <= total <= 1.0)
        self.assertEqual(set(dims), {"technical", "market", "fundamental",
                                     "chips", "catalyst"})
        self.assertGreater(dims["fundamental"], 0.7)
        self.assertIn("levels", detail["technical"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
