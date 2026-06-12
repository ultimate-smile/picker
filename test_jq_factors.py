"""
多维度因子评分 + 综合选股 单元测试（全部离线，不联网/不依赖 jqdatasdk 数据）

运行：
    python3 -m unittest -v test_jq_factors.py
"""

import unittest
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

import jq_factors as jf


def _bars(closes, highs=None, lows=None, vols=None, turns=None):
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1000.0] * n
    turns = turns or [5.0] * n
    return pd.DataFrame({"close": closes, "high": highs, "low": lows,
                         "volume": vols, "turnover": turns})


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        self.assertAlmostEqual(jf.sma([1, 2, 3, 4, 5], 5), 3.0)
        self.assertTrue(np.isnan(jf.sma([1, 2], 5)))

    def test_macd_shapes(self):
        closes = list(range(1, 60))
        dif, dea, hist = jf.macd(closes)
        self.assertEqual(len(dif), len(closes))
        self.assertEqual(len(dea), len(closes))
        # 单调上涨 → DIF 最终为正
        self.assertGreater(dif[-1], 0)


class TestMAScore(unittest.TestCase):
    def test_uptrend_bull_alignment_high(self):
        closes = [10 + i * 0.3 for i in range(70)]
        s, d = jf.ma_score(closes)
        self.assertTrue(d["bull_align"])
        self.assertGreaterEqual(s, 0.9)

    def test_downtrend_below_ma60_low(self):
        closes = [40 - i * 0.3 for i in range(70)]
        s, d = jf.ma_score(closes)
        self.assertTrue(d["below_ma60"])
        self.assertLessEqual(s, 0.2)


class TestVolPrice(unittest.TestCase):
    def test_up_volume_healthy(self):
        closes = [10, 10.2, 10.1, 10.4, 10.3, 10.6]
        vols = [1000, 2000, 800, 2200, 700, 2400]   # 涨天放量、跌天缩量
        s, d = jf.volprice_score(closes, vols, lookback=5)
        self.assertGreater(s, 0.5)

    def test_divergence_penalized(self):
        # 价格持续创新高但近段量能萎缩 → 背离降分
        closes = [10, 10.5, 11, 11.5, 12, 12.5]
        vols = [3000, 3000, 3000, 800, 700, 600]
        s, d = jf.volprice_score(closes, vols, lookback=5)
        self.assertTrue(d["divergence"])


class TestMACDScore(unittest.TestCase):
    def test_golden_cross_recovery(self):
        # V 形：长跌后近端强升 → 近 5 日内金叉
        closes = [30 - i * 0.4 for i in range(56)] + [10 + i * 1.2 for i in range(5)]
        s, d = jf.macd_score(closes)
        self.assertTrue(d["golden"])
        self.assertGreater(s, 0.6)

    def test_death_cross_topping(self):
        closes = [10 + i * 0.4 for i in range(56)] + [32 - i * 1.2 for i in range(5)]
        s, d = jf.macd_score(closes)
        self.assertTrue(d["death"])
        self.assertLess(s, 0.5)


class TestLevels(unittest.TestCase):
    def test_round_step(self):
        self.assertEqual(jf._round_step(8), 1.0)
        self.assertEqual(jf._round_step(33), 5.0)
        self.assertEqual(jf._round_step(77), 10.0)
        self.assertEqual(jf._round_step(250), 50.0)
        self.assertEqual(jf._round_step(1200), 100.0)

    def test_high_volume_node(self):
        closes = [10, 11, 10.5, 10.5, 10.5, 12]
        vols = [100, 100, 5000, 5000, 5000, 100]   # 量集中在 10.5
        node = jf._high_volume_node(pd.Series(closes), pd.Series(vols))
        self.assertTrue(10.0 <= node <= 11.0)

    def test_key_levels_and_score(self):
        closes = [10 + (i % 5) * 0.2 for i in range(60)]
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.3 for c in closes]
        vols = [1000] * 60
        lv = jf.key_levels(highs, lows, closes, vols)
        self.assertIsNotNone(lv["support"])
        self.assertIsNotNone(lv["resistance"])
        self.assertLess(lv["support"], lv["resistance"])
        s, d = jf.level_score(closes[-1], lv)
        self.assertTrue(0.0 <= s <= 1.0)


class TestTechnical(unittest.TestCase):
    def test_uptrend_high(self):
        closes = [10 + i * 0.2 for i in range(70)]
        vols = [1000 + (i % 2) * 500 for i in range(70)]
        s, d = jf.technical_score(_bars(closes, vols=vols))
        self.assertGreater(s, 0.55)
        self.assertIn("levels", d)

    def test_no_data_neutral(self):
        s, d = jf.technical_score(pd.DataFrame())
        self.assertEqual(s, 0.5)


class TestFundamental(unittest.TestCase):
    def test_revenue_acceleration(self):
        self.assertEqual(jf.revenue_accel_score([5, 10, 20]), 1.0)
        self.assertLess(jf.revenue_accel_score([20, 10, 5]), 0.6)
        self.assertEqual(jf.revenue_accel_score([]), 0.5)

    def test_margin_turn_positive(self):
        self.assertEqual(jf.margin_score([-5, 3]), 1.0)   # 毛利率转正
        self.assertGreater(jf.margin_score([10, 15]), 0.7)
        self.assertLess(jf.margin_score([20, 10]), 0.5)

    def test_quality(self):
        self.assertEqual(jf.quality_score(100, 50), 1.0)
        self.assertEqual(jf.quality_score(-100, 50), 0.45)
        self.assertEqual(jf.quality_score(-100, -50), 0.2)

    def test_fundamental_combine(self):
        s, d = jf.fundamental_score([5, 10, 20], [-2, 5], 100, 80)
        self.assertGreater(s, 0.8)


class TestChips(unittest.TestCase):
    def test_distribution_and_score(self):
        closes = [10.0] * 100
        s, d = jf.chip_score(closes, closes, closes, [5.0] * 100)
        self.assertTrue(0.0 <= s <= 1.0)
        self.assertAlmostEqual(d["avg_cost"], 10.0, places=1)

    def test_near_cost_better_than_far(self):
        base = [10.0] * 100
        near, _ = jf.chip_score(base, base, base, [5.0] * 100)
        # 现价远高于成本（最后一日拉到 14）→ 获利盘高、偏离成本 → 分更低
        far_closes = [10.0] * 99 + [14.0]
        far, _ = jf.chip_score([c * 1.01 for c in far_closes],
                               [c * 0.99 for c in far_closes],
                               far_closes, [5.0] * 100)
        self.assertGreaterEqual(near, far)


class TestMarket(unittest.TestCase):
    def test_index_trend(self):
        up = [3000 + i * 5 for i in range(25)]
        self.assertEqual(jf.index_trend_score(up), 1.0)
        down = [3000 - i * 5 for i in range(25)]
        self.assertLessEqual(jf.index_trend_score(down), 0.3)

    def test_relative_strength(self):
        idx = [3000 + i for i in range(25)]
        strong = [10 + i * 0.1 for i in range(25)]    # 个股涨幅远大于指数
        self.assertGreater(jf.relative_strength_score(strong, idx), 0.5)

    def test_northbound(self):
        self.assertGreater(jf.northbound_score([1, 2, 3, 1, 2]), 0.5)
        self.assertLess(jf.northbound_score([-1, -2, -3]), 0.5)
        self.assertEqual(jf.northbound_score([]), 0.5)

    def test_market_combine(self):
        idx = [3000 + i * 5 for i in range(25)]
        stock = [10 + i * 0.1 for i in range(25)]
        s, d = jf.market_score(idx, stock, northbound_net=[1, 2, 3])
        self.assertGreater(s, 0.6)


class TestCatalyst(unittest.TestCase):
    def test_big_unlock_veto(self):
        s, d = jf.catalyst_score(unlock_rate=0.2, days_to_unlock=10)
        self.assertLessEqual(s, 0.1)

    def test_small_unlock_partial(self):
        s, _ = jf.catalyst_score(unlock_rate=0.03)
        self.assertLess(s, jf.CATALYST_BASE)
        self.assertGreater(s, 0.1)

    def test_override_boost(self):
        s, _ = jf.catalyst_score(override=0.3)
        self.assertGreater(s, jf.CATALYST_BASE)

    def test_no_unlock_base(self):
        s, _ = jf.catalyst_score()
        self.assertAlmostEqual(s, jf.CATALYST_BASE)


class TestAggregate(unittest.TestCase):
    def test_weighted_mean(self):
        dims = {"technical": 1.0, "market": 0.0, "fundamental": 0.0,
                "chips": 0.0, "catalyst": 0.0}
        w = {"technical": 0.5, "market": 0.5}
        self.assertAlmostEqual(jf.aggregate(dims, w), 0.5)

    def test_all_one(self):
        dims = {k: 1.0 for k in jf.DIM_WEIGHTS}
        self.assertAlmostEqual(jf.aggregate(dims), 1.0)


class TestQuarterlyStatdates(unittest.TestCase):
    def test_generates_descending_then_reversed(self):
        sds = jf.quarterly_statdates(date(2025, 5, 10), n=3)
        self.assertEqual(len(sds), 3)
        # 升序，且不含尚未发布的 2025q2（5月时最新已出多为 2025q1）
        self.assertEqual(sds[-1], "2025q1")
        self.assertTrue(sds[0] < sds[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
