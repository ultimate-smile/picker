"""
聚宽选股层
==========
多因子、可操作性优先的选股策略（替代原先“单看主力净占比”导致专挑涨停板的问题）：

  1. 构建股票池：全 A 股 / 指定指数成分股 / **自定义股票集合** → 过滤 ST/次新/板块
  2. 估值初筛：市值区间 + 换手率上下限（过滤流动性差与过热）
  3. 主力资金流向：主力净占比落在 [下限, 上限] 区间（上限剔除异常爆量）
  4. **可操作性过滤**：剔除涨停 / 接近涨停（封板买不进、追高风险）、跌停、停牌，
     并限制当日涨跌幅在合理区间（不追高、不抄弱势）
  5. **综合评分**：主力净占比 + 连续净流入 + 涨跌幅健康度 + 换手健康度 加权打分，
     按综合分排序取前 N（不再单看主力净占比）
  6. 输出候选股列表（可交给 jq_trader 建仓，或交给 Claude 深度分析）

为什么这样更合理？
  纯按“主力净占比”降序，排在最前的几乎都是当日涨停/拉升的票——这类票要么封死涨停
  根本买不进，要么次日高开回落，并不适合实际交易。本策略显式剔除涨停/接近涨停，
  并用多因子综合分挑选“资金流入温和、涨幅适中、流动性健康”的可操作标的。
"""

from datetime import datetime

import pandas as pd

import jq_data as jd

# 逐项读取配置：即使 config.py 缺少某些（较新）键，也不会让其它已设置的项失效。
# （之前用 `from config import (...)` 一次性导入，任一键缺失就会整体回退到默认值，
#   导致像 JQ_EXCLUDE_BJ=False 这样的用户设置被悄悄忽略。）
try:
    import config as _cfg
except ImportError:
    _cfg = None


def _cfg_get(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


JQ_UNIVERSE_INDEX = _cfg_get("JQ_UNIVERSE_INDEX", None)
JQ_MIN_NET_PCT_MAIN = _cfg_get("JQ_MIN_NET_PCT_MAIN", 5.0)
JQ_MAX_NET_PCT_MAIN = _cfg_get("JQ_MAX_NET_PCT_MAIN", 25.0)
JQ_MIN_MARKET_CAP = _cfg_get("JQ_MIN_MARKET_CAP", 50.0)
JQ_MAX_MARKET_CAP = _cfg_get("JQ_MAX_MARKET_CAP", 1000.0)
JQ_MIN_TURNOVER = _cfg_get("JQ_MIN_TURNOVER", 2.0)
JQ_MAX_TURNOVER = _cfg_get("JQ_MAX_TURNOVER", 30.0)
JQ_HIST_LOOKBACK_DAYS = _cfg_get("JQ_HIST_LOOKBACK_DAYS", 5)
JQ_TOP_N = _cfg_get("JQ_TOP_N", 20)
JQ_FINAL_PICKS = _cfg_get("JQ_FINAL_PICKS", 3)
JQ_EXCLUDE_ST = _cfg_get("JQ_EXCLUDE_ST", True)
JQ_EXCLUDE_KCB = _cfg_get("JQ_EXCLUDE_KCB", False)
JQ_EXCLUDE_BJ = _cfg_get("JQ_EXCLUDE_BJ", True)
JQ_EXCLUDE_NEW_DAYS = _cfg_get("JQ_EXCLUDE_NEW_DAYS", 60)
JQ_EXCLUDE_NEAR_LIMIT = _cfg_get("JQ_EXCLUDE_NEAR_LIMIT", True)
JQ_NEAR_LIMIT_BUFFER = _cfg_get("JQ_NEAR_LIMIT_BUFFER", 0.015)
JQ_MIN_CHANGE_PCT = _cfg_get("JQ_MIN_CHANGE_PCT", -3.0)
JQ_MAX_CHANGE_PCT = _cfg_get("JQ_MAX_CHANGE_PCT", 7.0)
JQ_SCORE_WEIGHTS = _cfg_get("JQ_SCORE_WEIGHTS",
                            {"inflow": 0.35, "consec": 0.15, "change": 0.15,
                             "turnover": 0.15, "trend": 0.20})
JQ_CUSTOM_UNIVERSE = _cfg_get("JQ_CUSTOM_UNIVERSE", [])
JQ_SNAPSHOT_USE_LAST_COMPLETE = _cfg_get("JQ_SNAPSHOT_USE_LAST_COMPLETE", True)
JQ_MARKET_CLOSE_HHMM = _cfg_get("JQ_MARKET_CLOSE_HHMM", (15, 5))

# 历史走势（个股趋势）纳入筛选
JQ_USE_TREND_IN_SELECT = _cfg_get("JQ_USE_TREND_IN_SELECT", True)
JQ_TREND_LOOKBACK = _cfg_get("JQ_TREND_LOOKBACK", 60)
JQ_TREND_VETO_BELOW_MA60 = _cfg_get("JQ_TREND_VETO_BELOW_MA60", True)

# 大盘走势（市场环境）纳入筛选
JQ_USE_MARKET_REGIME = _cfg_get("JQ_USE_MARKET_REGIME", True)
JQ_MARKET_REGIME_INDEX = _cfg_get("JQ_MARKET_REGIME_INDEX", "000001.XSHG")
JQ_MARKET_REGIME_LOOKBACK = _cfg_get("JQ_MARKET_REGIME_LOOKBACK", 20)
JQ_WEAK_MARKET_SCORE = _cfg_get("JQ_WEAK_MARKET_SCORE", 0.45)
JQ_WEAK_MARKET_MIN_NET_BOOST = _cfg_get("JQ_WEAK_MARKET_MIN_NET_BOOST", 2.0)
JQ_WEAK_MARKET_PICK_FACTOR = _cfg_get("JQ_WEAK_MARKET_PICK_FACTOR", 0.5)


# ─────────────────────────────────────────
# 过滤与评分（纯函数，便于单元测试）
# ─────────────────────────────────────────

def _passes_valuation(row) -> bool:
    """市值在区间内、换手率在 [下限, 上限] 内。"""
    cap = row.get("market_cap")
    if cap is None or pd.isna(cap):
        return False
    if JQ_MIN_MARKET_CAP is not None and cap < JQ_MIN_MARKET_CAP:
        return False
    if JQ_MAX_MARKET_CAP is not None and cap > JQ_MAX_MARKET_CAP:
        return False
    tr = row.get("turnover_ratio")
    if tr is not None and not pd.isna(tr):
        if JQ_MIN_TURNOVER is not None and tr < JQ_MIN_TURNOVER:
            return False
        if JQ_MAX_TURNOVER is not None and tr > JQ_MAX_TURNOVER:
            return False
    return True


def is_tradable(price_row, *, exclude_near_limit=None) -> bool:
    """判断某股票当日是否“可操作”：非停牌、非跌停、非涨停/接近涨停。"""
    if price_row is None:
        return True  # 无价格数据时不强制剔除（上层会提示）
    if exclude_near_limit is None:
        exclude_near_limit = JQ_EXCLUDE_NEAR_LIMIT
    if bool(price_row.get("is_paused")):
        return False
    if bool(price_row.get("is_limit_down")):
        return False
    if exclude_near_limit and (bool(price_row.get("is_limit_up"))
                               or bool(price_row.get("near_limit_up"))):
        return False
    return True


def _in_change_band(change_pct) -> bool:
    if change_pct is None or pd.isna(change_pct):
        return True  # 缺数据不剔除
    if JQ_MIN_CHANGE_PCT is not None and change_pct < JQ_MIN_CHANGE_PCT:
        return False
    if JQ_MAX_CHANGE_PCT is not None and change_pct > JQ_MAX_CHANGE_PCT:
        return False
    return True


def _inflow_score(net_pct_main) -> float:
    """主力净占比越高越好，但 20% 以上封顶（避免极端值主导）。"""
    if net_pct_main is None or pd.isna(net_pct_main):
        return 0.0
    return max(0.0, min(float(net_pct_main), 20.0) / 20.0)


def _consec_score(days) -> float:
    """连续净流入天数，5 天封顶。"""
    return min(max(int(days or 0), 0), 5) / 5.0


def _change_score(change_pct) -> float:
    """涨跌幅健康度：温和上涨(1%~6%)最佳；过热/深跌降分。

    与放宽后的涨跌幅过滤区间（-5% ~ +9%）保持一致：下沿到 -5% 才归零，
    让强势股的正常幅度回踩（洗盘）仍保留一定分数，不被一刀切。
    """
    if change_pct is None or pd.isna(change_pct):
        return 0.3
    c = float(change_pct)
    if 1.0 <= c <= 6.0:
        return 1.0
    if 0.0 <= c < 1.0:
        return 0.6 + 0.4 * c                      # 0→0.6, 1→1.0
    if 6.0 < c <= 9.0:
        return max(0.0, 1.0 - (c - 6.0) / 3.0)    # 过热衰减：9→0
    if -5.0 <= c < 0.0:
        return max(0.0, 0.4 * (1.0 + c / 5.0))    # 回调衰减：0→0.4, -5→0
    return 0.0


def _turnover_score(turnover) -> float:
    """换手率健康度：3%~15% 最佳；过低(流动性差)或过高(过热)降分。

    与放宽后的换手上限（40%）保持一致：衰减区间拉到 40%，
    避免“刚启动放量”的高换手票在评分阶段被直接打成 0。
    """
    if turnover is None or pd.isna(turnover):
        return 0.3
    t = float(turnover)
    if 3.0 <= t <= 15.0:
        return 1.0
    if t < 3.0:
        return max(0.0, t / 3.0)
    return max(0.0, 1.0 - (t - 15.0) / 25.0)      # 15→1.0, 40→0.0


def _trend_score(closes, lookback=None) -> float:
    """个股历史走势健康度（0~1）：综合均线结构 + 中期斜率 + 相对 MA20 位置。

    纯函数，输入按时间升序的收盘价序列；数据不足时返回中性 0.5（稳健降级）。
      - 均线结构：站上 5/10/20/60、多头排列加分（复用技术面 ma_score 思路）；
      - 中期斜率：MA20 上行/下行；
      - 位置：现价相对 MA20 的偏离（贴着均线上方最稳）。
    """
    lookback = lookback or JQ_TREND_LOOKBACK
    s = pd.Series(closes, dtype="float64").dropna()
    if len(s) < 20:
        return 0.5
    close = float(s.iloc[-1])

    def _ma(n):
        return float(s.iloc[-n:].mean()) if len(s) >= n else float("nan")

    ma5, ma10, ma20 = _ma(5), _ma(10), _ma(20)
    ma60 = _ma(min(60, lookback)) if len(s) >= 20 else float("nan")

    score = 0.0
    # 站上短中期均线（5/10/20 各 0.15）
    for m in (ma5, ma10, ma20):
        if not pd.isna(m) and close >= m:
            score += 0.15
    # 多头排列（MA5>MA10>MA20[>MA60]）+0.25
    arr = [m for m in (ma5, ma10, ma20, ma60) if not pd.isna(m)]
    if len(arr) >= 3 and all(arr[i] > arr[i + 1] for i in range(len(arr) - 1)):
        score += 0.25
    # MA20 斜率（上行 +0.15）
    if len(s) >= 25:
        ma20_prev = float(s.iloc[-25:-5].mean())
        if not pd.isna(ma20) and ma20 >= ma20_prev:
            score += 0.15
    # 跌破 60 日线视为趋势走坏，整体封顶很低
    if not pd.isna(ma60) and close < ma60:
        score = min(score, 0.2)
    return max(0.0, min(1.0, score))


def composite_score(net_pct_main, consec_days, change_pct, turnover,
                    trend=None, weights=None) -> float:
    """多因子综合分（0~1），权重见 JQ_SCORE_WEIGHTS。

    :param trend: 历史走势健康度（0~1）。None 时按中性 0.5 计入（无 K 线/未启用趋势）。
    """
    w = weights or JQ_SCORE_WEIGHTS
    trend_s = 0.5 if trend is None else max(0.0, min(1.0, float(trend)))
    return (w.get("inflow", 0) * _inflow_score(net_pct_main)
            + w.get("consec", 0) * _consec_score(consec_days)
            + w.get("change", 0) * _change_score(change_pct)
            + w.get("turnover", 0) * _turnover_score(turnover)
            + w.get("trend", 0) * trend_s)


def _safe(df, code, col):
    """从 DataFrame 安全取值，缺失返回 None。"""
    try:
        if df is not None and not df.empty and code in df.index and col in df.columns:
            v = df.loc[code, col]
            return None if pd.isna(v) else v
    except Exception:
        pass
    return None


# ─────────────────────────────────────────
# 大盘走势 / 个股历史走势（纳入筛选）
# ─────────────────────────────────────────

def compute_market_regime(date=None):
    """判断大盘强弱（基于基准指数趋势）。

    :return: dict{score, label, weak, index_code}。score 为 0~1 的趋势分
        （复用 jq_factors.index_trend_score）；取数失败时按中性 0.5、weak=False
        处理（不收紧也不放宽），保证稳健降级。
    """
    out = {"score": 0.5, "label": "中性", "weak": False,
           "index_code": JQ_MARKET_REGIME_INDEX}
    if not JQ_USE_MARKET_REGIME:
        return out
    try:
        import jq_factors as jf
        closes = jd.get_index_closes(
            JQ_MARKET_REGIME_INDEX, end_date=date,
            count=JQ_MARKET_REGIME_LOOKBACK + 6)
        if not closes:
            return out
        score = float(jf.index_trend_score(closes, lookback=JQ_MARKET_REGIME_LOOKBACK))
    except Exception as e:
        print(f"  ⚠️  大盘走势判断失败（{e}），按中性处理（不收紧阈值）。")
        return out
    weak = score <= JQ_WEAK_MARKET_SCORE
    label = "走强" if score >= 0.65 else ("走弱" if weak else "震荡")
    out.update(score=round(score, 3), label=label, weak=weak)
    return out


def _fetch_trend_scores(codes, date=None):
    """批量计算一批股票的历史走势健康度。

    :return: {聚宽代码: trend_score(0~1)}。任一环节失败时返回 {}（上层按中性处理）。
    """
    if not JQ_USE_TREND_IN_SELECT or not codes:
        return {}
    try:
        count = max(JQ_TREND_LOOKBACK, 60) + 10
        closes_map = jd.get_daily_closes_batch(codes, end_date=date, count=count)
    except Exception as e:
        print(f"  ⚠️  历史走势数据获取失败（{e}），历史走势维度按中性处理。")
        return {}
    return {code: _trend_score(closes) for code, closes in closes_map.items()}


# ─────────────────────────────────────────
# 主选股流程
# ─────────────────────────────────────────

def _resolve_universe(d, codes):
    """返回 (uni 数据帧, 是否自定义池)。codes 为 None 时用配置/全市场。"""
    custom = codes if codes is not None else (JQ_CUSTOM_UNIVERSE or None)
    if custom:
        print(f"📡 [聚宽] 使用自定义股票池（{len(custom)} 只）...")
        uni_all = jd.get_universe(date=d, index_code=None)
        jq_codes = [jd.to_jq_code(c) for c in custom]
        uni = uni_all.loc[uni_all.index.intersection(jq_codes)].copy()
        missing = [c for c in jq_codes if c not in uni.index]
        if missing:
            shown = "、".join(jd.from_jq_code(m) for m in missing[:10])
            more = "…" if len(missing) > 10 else ""
            print(f"  ⚠️  {len(missing)} 只不在可交易股票列表中，已忽略：{shown}{more}")
        return uni, True

    print("📡 [聚宽] 构建股票池...")
    uni = jd.get_universe(date=d, index_code=JQ_UNIVERSE_INDEX)
    return uni, False


def _resolve_snapshot_date(date):
    """显式传入日期则用之；否则按配置自动选用最近已收盘交易日（盘中用上一交易日完整数据）。"""
    if date is not None:
        return date
    if not JQ_SNAPSHOT_USE_LAST_COMPLETE:
        return datetime.now().date()
    try:
        tdays = jd.get_recent_trade_days(count=10)
        d = jd.resolve_snapshot_date(datetime.now(), tdays,
                                     close_hhmm=tuple(JQ_MARKET_CLOSE_HHMM))
        if d != datetime.now().date():
            print(f"  ℹ️  数据基准日自动选用最近已收盘交易日：{d}"
                  f"（盘中/盘前用完整数据；如需当日盘中数据请显式传入日期）")
        return d
    except Exception:
        return datetime.now().date()


def _diagnose_valuation(val, d):
    """估值初筛后为 0 时，分项诊断到底是市值还是换手把候选滤光，并打印数值分布。"""
    n = len(val)
    cap = pd.to_numeric(val.get("market_cap"), errors="coerce")
    tr = pd.to_numeric(val.get("turnover_ratio"), errors="coerce")
    lo_c = JQ_MIN_MARKET_CAP if JQ_MIN_MARKET_CAP is not None else float("-inf")
    hi_c = JQ_MAX_MARKET_CAP if JQ_MAX_MARKET_CAP is not None else float("inf")
    lo_t = JQ_MIN_TURNOVER if JQ_MIN_TURNOVER is not None else float("-inf")
    hi_t = JQ_MAX_TURNOVER if JQ_MAX_TURNOVER is not None else float("inf")
    in_cap = int(((cap >= lo_c) & (cap <= hi_c)).sum())
    in_tr = int(((tr >= lo_t) & (tr <= hi_t)).sum())

    def _q(s):
        s = s.dropna()
        if s.empty:
            return "—"
        return f"{s.min():.2f}/{s.median():.2f}/{s.max():.2f}"

    print(f"  ⚠️  估值初筛后为 0，分项诊断（数据日 {d}，共 {n} 只）：")
    print(f"      市值落在 [{JQ_MIN_MARKET_CAP}, {JQ_MAX_MARKET_CAP}] 亿：{in_cap} 只"
          f"｜市值(亿) 最小/中位/最大 = {_q(cap)}")
    print(f"      换手落在 [{JQ_MIN_TURNOVER}, {JQ_MAX_TURNOVER}] %：{in_tr} 只"
          f"｜换手(%) 最小/中位/最大 = {_q(tr)}")
    if in_tr == 0 and tr.dropna().median() < (JQ_MIN_TURNOVER or 0):
        print("      → 多数标的换手率低于下限。常见原因：在【早盘】运行，当日换手尚未累积；")
        print("        建议收盘后运行、或让程序自动用上一交易日数据"
              "（JQ_SNAPSHOT_USE_LAST_COMPLETE=True），或调低 JQ_MIN_TURNOVER。")
    if in_cap == 0:
        print("      → 没有标的市值落在区间，请检查 JQ_MIN/MAX_MARKET_CAP 是否过窄。")


def select_candidates(date=None, top_n=None, codes=None) -> list:
    """
    主选股流程，返回候选股列表（按综合评分降序）。

    :param codes: 可选，自定义股票集合（6 位或聚宽代码）。传入则只在该集合内选股。
    :param top_n: 最终返回的候选数量；默认取 config.JQ_FINAL_PICKS（如 3 只）。
    :return: 每个元素为 dict，含 代码/名称/板块/市值/换手率/涨跌幅/主力净占比/
             主力净流入/连续净流入天数/综合评分/近N日主力流向。
    """
    top_n = top_n or JQ_FINAL_PICKS
    d = _resolve_snapshot_date(date)

    # 0) 大盘走势：走弱时自动收紧（提高资金门槛、压缩最终选股数）
    regime = compute_market_regime(d)
    min_net_main = JQ_MIN_NET_PCT_MAIN
    if regime.get("weak"):
        min_net_main = JQ_MIN_NET_PCT_MAIN + JQ_WEAK_MARKET_MIN_NET_BOOST
        top_n = max(1, int(top_n * JQ_WEAK_MARKET_PICK_FACTOR))
        print(f"  🌧️  大盘{regime['label']}（趋势分 {regime['score']}）：逆势收紧 → "
              f"主力净占比下限 {JQ_MIN_NET_PCT_MAIN}%→{min_net_main}%，"
              f"最终精选缩减至 {top_n} 只。")
    elif JQ_USE_MARKET_REGIME:
        print(f"  ☀️  大盘{regime['label']}（趋势分 {regime['score']}），维持正常筛选阈值。")

    # 1) 股票池
    uni, _is_custom = _resolve_universe(d, codes)
    uni = jd.filter_universe(
        uni, exclude_st=JQ_EXCLUDE_ST, exclude_kcb=JQ_EXCLUDE_KCB,
        exclude_bj=JQ_EXCLUDE_BJ, exclude_new_days=JQ_EXCLUDE_NEW_DAYS, ref_date=d,
    )
    code_list = list(uni.index)
    print(f"  股票池规模：{len(code_list)} 只")
    if not code_list:
        return []

    name_col = ("display_name" if "display_name" in uni.columns
                else ("name" if "name" in uni.columns else None))

    def _name(code):
        if name_col and code in uni.index:
            return uni.loc[code, name_col]
        return jd.get_security_name(code)

    # 2) 估值初筛：市值 + 换手率上下限
    print("📡 [聚宽] 拉取估值（市值/换手率）并初筛...")
    val = jd.get_valuation_oneday(code_list, date=d)
    if not val.empty:
        kept = [c for c in val.index if _passes_valuation(val.loc[c])]
        if not kept:
            _diagnose_valuation(val, d)
        code_list = kept
    else:
        print("  ⚠️  估值数据为空（可能：非交易日 / 额度耗尽 / 数据权限）。")
        return []
    print(f"  市值/换手初筛后：{len(code_list)} 只")
    if not code_list:
        return []

    # 3) 主力资金流向：净占比落在 [下限, 上限]
    print("📡 [聚宽] 拉取主力资金流向...")
    mf = jd.get_money_flow_oneday(code_list, date=d)
    if mf.empty:
        print("  ⚠️  资金流向为空。可能原因：非交易日 / 当日额度耗尽 / 数据权限。")
        print("      可先运行 `python3 jq_main.py --selftest` 查看各接口可用性。")
        return []
    mf = mf.copy()
    mf["net_pct_main"] = pd.to_numeric(mf["net_pct_main"], errors="coerce")
    mf["net_amount_main"] = pd.to_numeric(mf["net_amount_main"], errors="coerce")
    mf = mf[mf["net_pct_main"] >= min_net_main]
    if JQ_MAX_NET_PCT_MAIN is not None:
        mf = mf[mf["net_pct_main"] <= JQ_MAX_NET_PCT_MAIN]
    hi = JQ_MAX_NET_PCT_MAIN if JQ_MAX_NET_PCT_MAIN is not None else "∞"
    print(f"  主力净占比 ∈ [{min_net_main}, {hi}]%：{len(mf)} 只")
    if mf.empty:
        return []

    # 4) 可操作性过滤：剔除涨停/接近涨停/跌停/停牌、涨跌幅越界
    print("📡 [聚宽] 拉取日线价格，过滤涨停/停牌等不可操作标的...")
    px = jd.get_price_oneday(list(mf.index), date=d,
                             near_limit_buffer=JQ_NEAR_LIMIT_BUFFER)
    if not px.empty:
        before = len(mf)
        keep = []
        for c in mf.index:
            row = px.loc[c] if c in px.index else None
            if row is not None:
                if not is_tradable(row):
                    continue
                if not _in_change_band(row.get("change_pct")):
                    continue
            keep.append(c)
        mf = mf.loc[keep]
        print(f"  剔除涨停/停牌/涨跌幅越界后：{before} → {len(mf)} 只")
    else:
        print("  ⚠️  价格数据为空，跳过可操作性过滤（结果可能含涨停板，请谨慎）。")
    if mf.empty:
        return []

    # 5) 预筛（评分用更宽的池子，最终只取 top_n）→ 连续净流入天数 → 综合评分
    prelim_n = max(JQ_TOP_N, top_n * 3, 30)
    prelim = mf.sort_values("net_pct_main", ascending=False).head(prelim_n)
    prelim_codes = list(prelim.index)
    print(f"📡 [聚宽] 计算连续净流入天数（{len(prelim_codes)} 只）...")
    hist = jd.get_money_flow_history(prelim_codes, end_date=d,
                                     count=JQ_HIST_LOOKBACK_DAYS)

    # 历史走势：批量拉日线算个股趋势健康度（计入综合分；明显下行趋势可否决）
    trend_map = {}
    if JQ_USE_TREND_IN_SELECT:
        print(f"📡 [聚宽] 计算个股历史走势（均线/斜率，{len(prelim_codes)} 只）...")
        trend_map = _fetch_trend_scores(prelim_codes, date=d)

    candidates = []
    vetoed = 0
    for code in prelim_codes:
        flows = hist.get(code, [])
        consec = jd.consecutive_inflow_days(flows)
        net_pct = float(prelim.loc[code, "net_pct_main"])
        net_amt = float(prelim.loc[code, "net_amount_main"])
        turnover = _safe(val, code, "turnover_ratio")
        cap = _safe(val, code, "market_cap")
        change_pct = _safe(px, code, "change_pct")
        trend = trend_map.get(code)
        # 趋势否决：明显跌破 60 日线（趋势分被封顶到 ≤0.2）的票直接剔除
        if (JQ_USE_TREND_IN_SELECT and JQ_TREND_VETO_BELOW_MA60
                and trend is not None and trend <= 0.2):
            vetoed += 1
            continue
        score = composite_score(net_pct, consec, change_pct, turnover, trend=trend)
        candidates.append({
            "代码": jd.from_jq_code(code),
            "jq代码": code,
            "名称": _name(code),
            "板块": jd.get_board(code),
            "总市值(亿)": round(float(cap), 1) if cap is not None else None,
            "换手率(%)": round(float(turnover), 2) if turnover is not None else None,
            "今日涨跌幅(%)": round(float(change_pct), 2) if change_pct is not None else None,
            "今日主力净占比": round(net_pct, 2),
            "今日主力净流入(万)": round(net_amt, 1),
            "连续净流入天数": consec,
            "历史走势分": round(float(trend), 3) if trend is not None else None,
            "综合评分": round(score, 3),
            "近N日主力流向": "、".join(
                f"{'+' if float(v) > 0 else ''}{round(float(v), 1)}" for v in flows
            ),
        })

    if vetoed:
        print(f"  趋势否决（已跌破 60 日线的下行趋势）剔除：{vetoed} 只")
    candidates.sort(key=lambda c: c["综合评分"], reverse=True)
    candidates = candidates[:top_n]
    print(f"✅ 选股完成，得到 {len(candidates)} 只候选股（按综合评分排序）")
    return candidates


def print_candidates(candidates: list) -> None:
    """美观打印候选股"""
    if not candidates:
        print("（无候选股）")
        return
    print("\n" + "─" * 92)
    print(f"{'代码':<8}{'名称':<10}{'板块':<9}{'涨跌幅%':>8}{'主力占比%':>9}"
          f"{'连续流入':>8}{'换手%':>7}{'走势分':>7}{'综合分':>8}")
    print("─" * 92)
    for c in candidates:
        trend = c.get("历史走势分")
        trend_s = f"{trend:>7.2f}" if trend is not None else f"{'—':>7}"
        print(f"{c['代码']:<8}{str(c['名称'])[:8]:<10}{c['板块']:<9}"
              f"{(c.get('今日涨跌幅(%)') or 0):>8.2f}"
              f"{c['今日主力净占比']:>9.2f}{c['连续净流入天数']:>8}"
              f"{(c.get('换手率(%)') or 0):>7.2f}{trend_s}{c['综合评分']:>8.3f}")
    print("─" * 92)
    print("⚠️  已剔除涨停/接近涨停/停牌；候选按综合评分排序，仅供参考，非投资建议。")
