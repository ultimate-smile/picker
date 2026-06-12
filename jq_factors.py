"""
多维度因子评分层
================
把用户的五维选股框架量化为可计算、可配置的评分（每维 0~1）：

  1. 技术面（technical）：均线系统(5/10/20/60 多头排列)、量价配合(涨放量/跌缩量/背离)、
     MACD(金叉/死叉、零轴上下)、关键价位(前高/前低/整数关口/密集成交区)。
  2. 基本面（fundamental）：营收增速(最好连续 3 季加速)、毛利率变化(改善/转正)、
     净利润+经营性现金流(盈利且现金流为正更抗跌)。
  3. 筹码结构（chips）：用历史成交近似筹码成本分布 → 获利盘比例 / 成本区 / 集中度。
  4. 大盘与板块（market）：相关指数趋势、个股相对强弱、北向资金（市场层面，取不到则中性）。
  5. 消息面催化（catalyst）：解禁利空规避（次新股战略配售解禁）+ 自定义催化覆盖。

设计原则：
- **纯函数优先**：所有打分逻辑都是输入数值 → 输出 (score, detail) 的纯函数，便于离线单测；
  数据获取（依赖 jqdatasdk）单独放在带 `fetch_` / `compute_` 前缀的封装里，内部惰性导入 jq_data。
- **规则可配置**：权重与阈值全部从 config.py 读取（缺键回退到此处默认值）。
- **稳健降级**：任一数据缺失（无权限/非交易日）时该子项给中性分，绝不让整体崩溃。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    import config as _cfg
except ImportError:
    _cfg = None


def _cfg_get(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


# ── 配置（缺键回退默认）──
DIM_WEIGHTS = _cfg_get("JQ_DIM_WEIGHTS", {
    "technical": 0.35, "market": 0.25, "fundamental": 0.20,
    "chips": 0.10, "catalyst": 0.10,
})
TECH_WEIGHTS = _cfg_get("JQ_TECH_WEIGHTS", {
    "ma": 0.35, "volprice": 0.20, "macd": 0.25, "level": 0.20})
MA_PERIODS = _cfg_get("JQ_MA_PERIODS", [5, 10, 20, 60])
LEVEL_LOOKBACK = _cfg_get("JQ_LEVEL_LOOKBACK", 60)
VOLPRICE_LOOKBACK = _cfg_get("JQ_VOLPRICE_LOOKBACK", 5)
MACD_CROSS_LOOKBACK = _cfg_get("JQ_MACD_CROSS_LOOKBACK", 5)

FUND_WEIGHTS = _cfg_get("JQ_FUND_WEIGHTS", {
    "rev_accel": 0.4, "margin": 0.3, "quality": 0.3})
FUND_QUARTERS = _cfg_get("JQ_FUND_QUARTERS", 5)

CHIP_LOOKBACK = _cfg_get("JQ_CHIP_LOOKBACK", 100)
CHIP_DECAY = _cfg_get("JQ_CHIP_DECAY", 0.94)

MARKET_WEIGHTS = _cfg_get("JQ_MARKET_WEIGHTS", {
    "index": 0.5, "rs": 0.3, "northbound": 0.2})
BOARD_INDEX = _cfg_get("JQ_BOARD_INDEX", {
    "科创板": "000688.XSHG", "创业板": "399006.XSHE", "主板(沪)": "000001.XSHG",
    "主板(深)": "399001.XSHE", "北交所": "899050.BJSE", "其他": "000001.XSHG"})
MARKET_TREND_LOOKBACK = _cfg_get("JQ_MARKET_TREND_LOOKBACK", 20)
NORTHBOUND_LOOKBACK = _cfg_get("JQ_NORTHBOUND_LOOKBACK", 5)
USE_NORTHBOUND = _cfg_get("JQ_USE_NORTHBOUND", True)

UNLOCK_WARN_DAYS = _cfg_get("JQ_UNLOCK_WARN_DAYS", 30)
UNLOCK_RATE_VETO = _cfg_get("JQ_UNLOCK_RATE_VETO", 0.10)
CATALYST_BASE = _cfg_get("JQ_CATALYST_BASE", 0.6)
CATALYST_OVERRIDES = _cfg_get("JQ_CATALYST_OVERRIDES", {})


def _clip01(x: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(x):
        return 0.0
    return max(0.0, min(1.0, x))


# ═════════════════════════════════════════════════════════════════
# 指标计算（纯函数）
# ═════════════════════════════════════════════════════════════════

def sma(values, n: int) -> float:
    """最后 n 个值的简单均值；不足 n 个返回 nan。"""
    s = pd.Series(values, dtype="float64").dropna()
    if len(s) < n or n <= 0:
        return float("nan")
    return float(s.iloc[-n:].mean())


def ema_series(values, span: int) -> pd.Series:
    s = pd.Series(values, dtype="float64")
    return s.ewm(span=span, adjust=False).mean()


def macd(closes, fast=12, slow=26, signal=9):
    """返回 (dif, dea, hist) 三个等长 numpy 数组。hist = 2*(dif-dea)（常见画法）。"""
    s = pd.Series(closes, dtype="float64")
    if len(s) == 0:
        empty = np.array([])
        return empty, empty, empty
    dif = ema_series(s, fast) - ema_series(s, slow)
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2.0
    return dif.to_numpy(), dea.to_numpy(), hist.to_numpy()


# ═════════════════════════════════════════════════════════════════
# 1. 技术面
# ═════════════════════════════════════════════════════════════════

def ma_score(closes, periods=None):
    """均线系统打分：站上 5/10/20/60、多头排列加分；跌破 20/60 显著降分。"""
    periods = periods or MA_PERIODS
    s = pd.Series(closes, dtype="float64").dropna()
    detail = {"mas": {}, "bull_align": False, "below_ma20": None, "below_ma60": None}
    if len(s) < 2:
        return 0.5, detail
    close = float(s.iloc[-1])
    mas = {p: sma(s, p) for p in periods}
    detail["mas"] = {p: (round(v, 3) if not math.isnan(v) else None) for p, v in mas.items()}

    # 站上短中期均线（5/10/20 各 0.2）
    score = 0.0
    for p, w in zip([5, 10, 20], [0.2, 0.2, 0.2]):
        m = mas.get(p)
        if m is not None and not math.isnan(m) and close >= m:
            score += w

    # 多头排列（MA5>MA10>MA20>MA60）+0.4
    ordered = [mas.get(p) for p in [5, 10, 20, 60] if mas.get(p) is not None
               and not math.isnan(mas.get(p))]
    bull = len(ordered) >= 2 and all(ordered[i] > ordered[i + 1] for i in range(len(ordered) - 1))
    detail["bull_align"] = bull
    if bull:
        score += 0.4

    # 警戒：跌破 MA20 打折；跌破 MA60 趋势走坏，封顶很低
    m20, m60 = mas.get(20), mas.get(60)
    if m20 is not None and not math.isnan(m20):
        detail["below_ma20"] = close < m20
        if close < m20:
            score *= 0.6
    if m60 is not None and not math.isnan(m60):
        detail["below_ma60"] = close < m60
        if close < m60:
            score = min(score, 0.2)
    return _clip01(score), detail


def volprice_score(closes, volumes, lookback=None):
    """量价配合：涨放量、跌缩量为健康；价升量缩(背离)降分。"""
    lookback = lookback or VOLPRICE_LOOKBACK
    c = pd.Series(closes, dtype="float64").dropna()
    v = pd.Series(volumes, dtype="float64").dropna()
    detail = {"divergence": False}
    if len(c) < lookback + 1 or len(v) < lookback + 1:
        return 0.5, detail
    rets = c.diff().iloc[-lookback:]
    vols = v.iloc[-lookback:]
    up_vol = vols[rets > 0].mean()
    down_vol = vols[rets < 0].mean()

    score = 0.5
    if not math.isnan(up_vol) and not math.isnan(down_vol) and down_vol > 0:
        ratio = up_vol / down_vol            # 涨天量 / 跌天量
        # ratio>1 健康（涨放量、跌缩量）：1→0.5, 2→1.0 线性封顶
        score = _clip01(0.5 + 0.5 * (ratio - 1.0))
    elif not math.isnan(up_vol) and (math.isnan(down_vol) or down_vol == 0):
        score = 0.8                          # 区间内几乎只涨不跌

    # 量价背离：近段价格创新高但量能均值较前段萎缩 → 见顶风险，降分
    half = max(2, lookback // 2)
    if len(c) >= lookback and len(v) >= lookback:
        price_up = c.iloc[-1] >= c.iloc[-lookback:].max() - 1e-9
        vol_recent = v.iloc[-half:].mean()
        vol_prev = v.iloc[-lookback:-half].mean()
        if price_up and not math.isnan(vol_prev) and vol_prev > 0 and vol_recent < vol_prev * 0.8:
            detail["divergence"] = True
            score *= 0.6
    detail["score"] = round(score, 3)
    return _clip01(score), detail


def macd_score(closes, cross_lookback=None):
    """MACD：近 N 日金叉(尤其零轴上方)加分，死叉降分；柱状放大次之。"""
    cross_lookback = cross_lookback or MACD_CROSS_LOOKBACK
    dif, dea, hist = macd(closes)
    detail = {"dif": None, "dea": None, "golden": False, "death": False, "above_zero": None}
    if len(dif) < 2:
        return 0.5, detail
    detail["dif"] = round(float(dif[-1]), 4)
    detail["dea"] = round(float(dea[-1]), 4)
    above_zero = dif[-1] > 0 and dea[-1] > 0
    detail["above_zero"] = bool(above_zero)

    n = min(cross_lookback, len(dif) - 1)
    golden = death = False
    for i in range(len(dif) - n, len(dif)):
        if i <= 0:
            continue
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            golden = True
        if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            death = True
    detail["golden"], detail["death"] = golden, death

    if golden and not death:
        score = 1.0 if above_zero else 0.75       # 零轴上方金叉信号更强
    elif death and not golden:
        score = 0.15 if not above_zero else 0.3   # 死叉（高位死叉更弱）
    else:
        # 无新近交叉：看多空位置与柱状趋势
        if dif[-1] > dea[-1]:
            rising = len(hist) >= 2 and hist[-1] > hist[-2]
            score = 0.65 if rising else 0.55
            if above_zero:
                score += 0.05
        else:
            score = 0.35
    return _clip01(score), detail


def key_levels(highs, lows, closes, volumes, lookback=None):
    """计算关键价位：支撑/压力（前高前低 + 整数关口 + 密集成交区）。

    返回 dict：support / resistance / hv_node(密集成交价) / prior_high / prior_low /
    round_below / round_above。价位均为绝对价格，供买卖定价与打分使用。
    """
    lookback = lookback or LEVEL_LOOKBACK
    h = pd.Series(highs, dtype="float64").dropna()
    low = pd.Series(lows, dtype="float64").dropna()
    c = pd.Series(closes, dtype="float64").dropna()
    v = pd.Series(volumes, dtype="float64").fillna(0.0)
    out = {"support": None, "resistance": None, "hv_node": None,
           "prior_high": None, "prior_low": None,
           "round_below": None, "round_above": None}
    if len(c) == 0:
        return out
    close = float(c.iloc[-1])
    win = min(lookback, len(c))

    # 前高/前低（不含当日）
    if len(h) > 1:
        out["prior_high"] = round(float(h.iloc[-win:-1].max()), 3)
    if len(low) > 1:
        out["prior_low"] = round(float(low.iloc[-win:-1].min()), 3)

    # 整数关口（按价位量级取步长）
    step = _round_step(close)
    out["round_below"] = round(math.floor(close / step) * step, 3)
    out["round_above"] = round(math.ceil(close / step + 1e-9) * step, 3)

    # 密集成交区（成交量加权价格直方图的峰值价位）
    out["hv_node"] = _high_volume_node(c.iloc[-win:], v.iloc[-win:])

    # 支撑 = 现价下方最近的（前低 / 密集区 / 整数关口）里最高者
    below = [x for x in [out["prior_low"], out["hv_node"], out["round_below"]]
             if x is not None and x < close]
    out["support"] = round(max(below), 3) if below else round(close * 0.95, 3)
    # 压力 = 现价上方最近的（前高 / 密集区 / 整数关口）里最低者
    above = [x for x in [out["prior_high"], out["hv_node"], out["round_above"]]
             if x is not None and x > close]
    out["resistance"] = round(min(above), 3) if above else round(close * 1.08, 3)
    return out


def _round_step(price: float) -> float:
    """按价位量级给整数关口步长：<10→1, <50→5, <100→10, <500→50, 否则 100。"""
    p = abs(float(price))
    if p < 10:
        return 1.0
    if p < 50:
        return 5.0
    if p < 100:
        return 10.0
    if p < 500:
        return 50.0
    return 100.0


def _high_volume_node(closes, volumes, bins=20):
    """成交量加权价格直方图的峰值价位（密集成交区近似）。"""
    c = pd.Series(closes, dtype="float64").reset_index(drop=True)
    v = pd.Series(volumes, dtype="float64").reset_index(drop=True)
    if len(c) < 3 or v.sum() <= 0:
        return None
    lo, hi = float(c.min()), float(c.max())
    if hi <= lo:
        return round(float(c.iloc[-1]), 3)
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(c.to_numpy(), edges) - 1, 0, bins - 1)
    weight = np.zeros(bins)
    for i, w in zip(idx, v.to_numpy()):
        weight[i] += w
    peak = int(np.argmax(weight))
    return round(float((edges[peak] + edges[peak + 1]) / 2.0), 3)


def level_score(close, levels):
    """关键价位打分：进场性价比 = 上方空间充足 + 下方支撑临近。"""
    detail = {"upside": None, "downside": None}
    if not levels or close is None or close <= 0:
        return 0.5, detail
    sup = levels.get("support")
    res = levels.get("resistance")
    if not sup or not res or res <= sup:
        return 0.5, detail
    upside = (res - close) / close          # 到压力的空间
    downside = (close - sup) / close        # 到支撑的距离
    detail["upside"] = round(upside, 4)
    detail["downside"] = round(downside, 4)
    # 上方空间越大越好（8% 封顶记满），离支撑越近越好（>6% 偏贵）
    up_s = _clip01(upside / 0.08)
    down_s = _clip01(1.0 - downside / 0.06)
    return _clip01(0.6 * up_s + 0.4 * down_s), detail


def technical_score(bars, weights=None):
    """技术面综合分。bars: DataFrame[close, high, low, volume]（按时间升序）。"""
    weights = weights or TECH_WEIGHTS
    detail = {}
    if bars is None or len(bars) == 0:
        return 0.5, {"note": "无K线数据"}
    closes = bars["close"]
    highs = bars.get("high", closes)
    lows = bars.get("low", closes)
    vols = bars.get("volume", pd.Series([0] * len(bars)))
    close = float(pd.Series(closes, dtype="float64").dropna().iloc[-1])

    s_ma, d_ma = ma_score(closes)
    s_vp, d_vp = volprice_score(closes, vols)
    s_macd, d_macd = macd_score(closes)
    levels = key_levels(highs, lows, closes, vols)
    s_lvl, d_lvl = level_score(close, levels)

    total = (weights.get("ma", 0) * s_ma + weights.get("volprice", 0) * s_vp
             + weights.get("macd", 0) * s_macd + weights.get("level", 0) * s_lvl)
    wsum = sum(weights.get(k, 0) for k in ("ma", "volprice", "macd", "level")) or 1.0
    detail = {"ma": d_ma, "volprice": d_vp, "macd": d_macd, "level": d_lvl,
              "levels": levels, "close": round(close, 3),
              "subscores": {"ma": round(s_ma, 3), "volprice": round(s_vp, 3),
                            "macd": round(s_macd, 3), "level": round(s_lvl, 3)}}
    return _clip01(total / wsum), detail


# ═════════════════════════════════════════════════════════════════
# 2. 基本面
# ═════════════════════════════════════════════════════════════════

def revenue_accel_score(rev_yoy_list):
    """营收增速：最好连续 3 季加速。rev_yoy_list 为按时间升序的同比增速(%)。"""
    s = [x for x in (rev_yoy_list or []) if x is not None and not _isnan(x)]
    if len(s) == 0:
        return 0.5
    last = s[-1]
    if len(s) >= 3:
        a, b, c = s[-3], s[-2], s[-1]
        if c > b > a:
            return 1.0                       # 连续 3 季加速
        if c > b or b > a:
            return 0.7                       # 局部加速
    if last > 0:
        return 0.55
    return 0.2 if last <= 0 else 0.4


def margin_score(margin_list):
    """毛利率变化：转正(由负转正)或持续改善加分；恶化降分。margin_list 升序(%)。"""
    s = [x for x in (margin_list or []) if x is not None and not _isnan(x)]
    if len(s) == 0:
        return 0.5
    last = s[-1]
    prev = s[-2] if len(s) >= 2 else None
    if prev is not None:
        if prev <= 0 < last:
            return 1.0                       # 毛利率转正（如西安奕材）
        if last > prev and last > 0:
            return 0.85                      # 持续改善
        if last < prev:
            return max(0.2, 0.5 - (prev - last) / 100.0)
    return 0.6 if last > 0 else 0.25


def quality_score(net_profit, op_cash_flow):
    """净利润 + 经营性现金流：盈利且现金流为正最抗跌；亏损降分。"""
    np_pos = net_profit is not None and not _isnan(net_profit) and net_profit > 0
    cf_pos = op_cash_flow is not None and not _isnan(op_cash_flow) and op_cash_flow > 0
    if np_pos and cf_pos:
        return 1.0
    if np_pos and not cf_pos:
        return 0.6                           # 盈利但现金流为负（含应收/扩张）
    if (not np_pos) and cf_pos:
        return 0.45
    return 0.2                               # 亏损（东芯式反弹乏力风险）


def fundamental_score(rev_yoy_list, margin_list, net_profit, op_cash_flow,
                      weights=None):
    weights = weights or FUND_WEIGHTS
    s_rev = revenue_accel_score(rev_yoy_list)
    s_mgn = margin_score(margin_list)
    s_q = quality_score(net_profit, op_cash_flow)
    wsum = sum(weights.get(k, 0) for k in ("rev_accel", "margin", "quality")) or 1.0
    total = (weights.get("rev_accel", 0) * s_rev + weights.get("margin", 0) * s_mgn
             + weights.get("quality", 0) * s_q)
    detail = {"rev_accel": round(s_rev, 3), "margin": round(s_mgn, 3),
              "quality": round(s_q, 3), "rev_yoy": rev_yoy_list,
              "margins": margin_list}
    return _clip01(total / wsum), detail


def _isnan(x):
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


# ═════════════════════════════════════════════════════════════════
# 3. 筹码结构（近似）
# ═════════════════════════════════════════════════════════════════

def chip_distribution(highs, lows, closes, turnovers, decay=None):
    """用“换手衰减法”近似筹码成本分布。

    每个交易日把当日成交筹码记在当日均价(高低收均值)上，并按 (1-换手率) 衰减更早筹码。
    turnovers 为各日换手率(%)。返回 {price: weight} 的 numpy 数组对 (prices, weights)。
    """
    decay = decay if decay is not None else CHIP_DECAY
    h = pd.Series(highs, dtype="float64").reset_index(drop=True)
    low = pd.Series(lows, dtype="float64").reset_index(drop=True)
    c = pd.Series(closes, dtype="float64").reset_index(drop=True)
    tr = pd.Series(turnovers, dtype="float64").reset_index(drop=True).fillna(0.0) / 100.0
    n = len(c)
    if n == 0:
        return np.array([]), np.array([])
    typ = ((h + low + c) / 3.0).to_numpy()
    weights = np.zeros(n)
    for i in range(n):
        t = min(max(float(tr.iloc[i]) if i < len(tr) else 0.0, 0.0), 1.0)
        if t <= 0:
            t = 0.02                          # 无换手数据时给一个小默认，避免全 0
        weights[:i] *= (1.0 - t) * decay      # 旧筹码衰减
        weights[i] += t
    return typ, weights


def chip_score(highs, lows, closes, turnovers, decay=None):
    """筹码结构打分：现价处于主力成本区附近、上方无重套牢、筹码集中 → 支撑强。"""
    prices, weights = chip_distribution(highs, lows, closes, turnovers, decay)
    detail = {"winner_ratio": None, "avg_cost": None, "concentration": None}
    if len(prices) == 0 or weights.sum() <= 0:
        return 0.5, detail
    w = weights / weights.sum()
    close = float(pd.Series(closes, dtype="float64").dropna().iloc[-1])
    avg_cost = float(np.sum(prices * w))
    winner = float(np.sum(w[prices <= close]))           # 获利盘比例
    # 集中度：90% 筹码价格区间宽度 / 现价，越窄越集中
    order = np.argsort(prices)
    cum = np.cumsum(w[order])
    p_sorted = prices[order]
    lo = p_sorted[np.searchsorted(cum, 0.05)] if len(p_sorted) else close
    hi_idx = min(np.searchsorted(cum, 0.95), len(p_sorted) - 1)
    hi = p_sorted[hi_idx] if len(p_sorted) else close
    width = (hi - lo) / close if close > 0 else 1.0
    detail.update(winner_ratio=round(winner, 3), avg_cost=round(avg_cost, 3),
                  concentration=round(width, 3))

    # 获利盘适中(0.5~0.8)最佳：太低=深套抛压重，太高(>0.9)=普遍获利易兑现
    if winner < 0.5:
        win_s = winner / 0.5 * 0.6
    elif winner <= 0.85:
        win_s = 1.0
    else:
        win_s = max(0.3, 1.0 - (winner - 0.85) / 0.15 * 0.5)
    # 现价贴近平均成本（主力成本区）→ 支撑强
    near = abs(close - avg_cost) / close if close > 0 else 1.0
    cost_s = _clip01(1.0 - near / 0.12)
    # 集中度：宽度 <15% 记满，>40% 记 0
    conc_s = _clip01(1.0 - (width - 0.15) / 0.25) if width > 0.15 else 1.0
    score = 0.45 * win_s + 0.30 * cost_s + 0.25 * conc_s
    return _clip01(score), detail


# ═════════════════════════════════════════════════════════════════
# 4. 大盘与板块
# ═════════════════════════════════════════════════════════════════

def index_trend_score(index_closes, lookback=None):
    """指数趋势：站上 MA20 且 MA20 向上 → 做多窗口开启。"""
    lookback = lookback or MARKET_TREND_LOOKBACK
    s = pd.Series(index_closes, dtype="float64").dropna()
    if len(s) < lookback + 1:
        return 0.5
    ma = s.rolling(lookback).mean()
    close = float(s.iloc[-1])
    ma_now = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-2])
    above = close >= ma_now
    rising = ma_now >= ma_prev
    if above and rising:
        return 1.0
    if above and not rising:
        return 0.65
    if (not above) and rising:
        return 0.45
    return 0.2


def relative_strength_score(stock_closes, index_closes, lookback=None):
    """个股相对强弱：近 lookback 日跑赢指数 = 被资金带动/板块强。"""
    lookback = lookback or MARKET_TREND_LOOKBACK
    sc = pd.Series(stock_closes, dtype="float64").dropna()
    ic = pd.Series(index_closes, dtype="float64").dropna()
    if len(sc) < lookback + 1 or len(ic) < lookback + 1:
        return 0.5
    sret = sc.iloc[-1] / sc.iloc[-lookback - 1] - 1.0
    iret = ic.iloc[-1] / ic.iloc[-lookback - 1] - 1.0
    diff = sret - iret
    # 跑赢 +10% 记满，跑输 -10% 记 0，线性
    return _clip01(0.5 + diff / 0.20)


def northbound_score(net_series):
    """北向资金近 N 日净流入：累计净流入为正→市场信心回暖。取不到时上层给中性。"""
    s = [x for x in (net_series or []) if x is not None and not _isnan(x)]
    if not s:
        return 0.5
    total = sum(float(x) for x in s)
    pos_days = sum(1 for x in s if float(x) > 0)
    base = 0.5 + 0.5 * (pos_days / len(s) - 0.5) * 2.0   # 全为净流入→1.0
    if total < 0:
        base *= 0.7
    return _clip01(base)


def market_score(index_closes, stock_closes, northbound_net=None, weights=None):
    weights = weights or MARKET_WEIGHTS
    s_idx = index_trend_score(index_closes)
    s_rs = relative_strength_score(stock_closes, index_closes)
    if USE_NORTHBOUND and northbound_net:
        s_nb = northbound_score(northbound_net)
    else:
        s_nb = 0.5
    wsum = sum(weights.get(k, 0) for k in ("index", "rs", "northbound")) or 1.0
    total = (weights.get("index", 0) * s_idx + weights.get("rs", 0) * s_rs
             + weights.get("northbound", 0) * s_nb)
    detail = {"index": round(s_idx, 3), "rs": round(s_rs, 3),
              "northbound": round(s_nb, 3)}
    return _clip01(total / wsum), detail


# ═════════════════════════════════════════════════════════════════
# 5. 消息面与催化剂
# ═════════════════════════════════════════════════════════════════

def catalyst_score(unlock_rate=0.0, days_to_unlock=None, override=0.0,
                   base=None, warn_days=None, veto_rate=None):
    """消息面催化：默认基准分；近期解禁按占比降分（重大解禁近乎否决）；叠加自定义催化。

    :param unlock_rate: 未来 warn_days 内解禁占总股本比例（0~1）。
    :param days_to_unlock: 距最近一次解禁的自然日数（用于提示）。
    :param override: 自定义催化调整（正=利好，负=利空），叠加到基准分。
    """
    base = base if base is not None else CATALYST_BASE
    warn_days = warn_days if warn_days is not None else UNLOCK_WARN_DAYS
    veto_rate = veto_rate if veto_rate is not None else UNLOCK_RATE_VETO
    score = float(base)
    detail = {"unlock_rate": round(float(unlock_rate or 0.0), 4),
              "days_to_unlock": days_to_unlock, "override": override}

    r = float(unlock_rate or 0.0)
    if r > 0:
        if r >= veto_rate:
            score = min(score, 0.1)          # 重大解禁，强烈规避
        else:
            score *= max(0.2, 1.0 - r / veto_rate)
    score += float(override or 0.0)
    return _clip01(score), detail


# ═════════════════════════════════════════════════════════════════
# 综合（加权五维）
# ═════════════════════════════════════════════════════════════════

def aggregate(dim_scores, weights=None):
    """五维加权综合分（自动归一化权重）。dim_scores: {dim: score}。"""
    weights = weights or DIM_WEIGHTS
    keys = [k for k in weights if k in dim_scores and dim_scores[k] is not None]
    wsum = sum(weights[k] for k in keys) or 1.0
    total = sum(weights[k] * _clip01(dim_scores[k]) for k in keys)
    return _clip01(total / wsum)


# ═════════════════════════════════════════════════════════════════
# 数据获取 + 评分（依赖 jqdatasdk，惰性导入 jq_data）
# ═════════════════════════════════════════════════════════════════

def _jd():
    import jq_data as jd
    return jd


def fetch_daily_bars(code, end_date=None, count=None):
    """取日线 K 线（close/high/low/volume），按时间升序。"""
    jd = _jd()
    count = count or (max(LEVEL_LOOKBACK, CHIP_LOOKBACK, 60) + 35)
    return jd.get_daily_bars(code, end_date=end_date, count=count)


def quarterly_statdates(ref_date=None, n=None):
    """生成最近 n 个财报季的 statDate（如 '2025q1'），按时间升序。"""
    n = n or FUND_QUARTERS
    if ref_date is None:
        ref = datetime.now().date()
    elif isinstance(ref_date, str):
        ref = datetime.strptime(ref_date[:10], "%Y-%m-%d").date()
    else:
        ref = ref_date
    y, m = ref.year, ref.month
    q = (m - 1) // 3 + 1
    # 当前季报通常未出，从上一季开始往前推
    q -= 1
    if q == 0:
        q = 4
        y -= 1
    out = []
    for _ in range(n):
        out.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    return list(reversed(out))


def compute_market_context(board, ref_date=None):
    """计算某板块对应指数的收盘序列与北向净流入序列（用于 market_score）。"""
    jd = _jd()
    idx_code = BOARD_INDEX.get(board, BOARD_INDEX.get("其他", "000001.XSHG"))
    count = MARKET_TREND_LOOKBACK + 5
    try:
        idx_closes = jd.get_index_closes(idx_code, end_date=ref_date, count=count)
    except Exception:
        idx_closes = []
    northbound = []
    if USE_NORTHBOUND:
        try:
            northbound = jd.get_northbound_netflow(end_date=ref_date,
                                                   count=NORTHBOUND_LOOKBACK)
        except Exception:
            northbound = []
    return {"index_code": idx_code, "index_closes": idx_closes,
            "northbound": northbound}
