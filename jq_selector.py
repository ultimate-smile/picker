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
                            {"inflow": 0.4, "consec": 0.2, "change": 0.2, "turnover": 0.2})
JQ_CUSTOM_UNIVERSE = _cfg_get("JQ_CUSTOM_UNIVERSE", [])


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
    """涨跌幅健康度：温和上涨(1%~6%)最佳；过热/下跌降分。"""
    if change_pct is None or pd.isna(change_pct):
        return 0.3
    c = float(change_pct)
    if 1.0 <= c <= 6.0:
        return 1.0
    if 0.0 <= c < 1.0:
        return 0.6 + 0.4 * c                      # 0→0.6, 1→1.0
    if 6.0 < c <= 9.0:
        return max(0.0, 1.0 - (c - 6.0) / 3.0)    # 过热衰减
    if -3.0 <= c < 0.0:
        return max(0.0, 0.4 * (1.0 + c / 3.0))    # 小幅回调尚可
    return 0.0


def _turnover_score(turnover) -> float:
    """换手率健康度：3%~15% 最佳；过低(流动性差)或过高(过热)降分。"""
    if turnover is None or pd.isna(turnover):
        return 0.3
    t = float(turnover)
    if 3.0 <= t <= 15.0:
        return 1.0
    if t < 3.0:
        return max(0.0, t / 3.0)
    return max(0.0, 1.0 - (t - 15.0) / 15.0)      # 15→1.0, 30→0.0


def composite_score(net_pct_main, consec_days, change_pct, turnover,
                    weights=None) -> float:
    """多因子综合分（0~1），权重见 JQ_SCORE_WEIGHTS。"""
    w = weights or JQ_SCORE_WEIGHTS
    return (w.get("inflow", 0) * _inflow_score(net_pct_main)
            + w.get("consec", 0) * _consec_score(consec_days)
            + w.get("change", 0) * _change_score(change_pct)
            + w.get("turnover", 0) * _turnover_score(turnover))


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


def select_candidates(date=None, top_n=None, codes=None) -> list:
    """
    主选股流程，返回候选股列表（按综合评分降序）。

    :param codes: 可选，自定义股票集合（6 位或聚宽代码）。传入则只在该集合内选股。
    :param top_n: 最终返回的候选数量；默认取 config.JQ_FINAL_PICKS（如 3 只）。
    :return: 每个元素为 dict，含 代码/名称/板块/市值/换手率/涨跌幅/主力净占比/
             主力净流入/连续净流入天数/综合评分/近N日主力流向。
    """
    top_n = top_n or JQ_FINAL_PICKS
    d = date or datetime.now().date()

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
        code_list = [c for c in val.index if _passes_valuation(val.loc[c])]
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
    mf = mf[mf["net_pct_main"] >= JQ_MIN_NET_PCT_MAIN]
    if JQ_MAX_NET_PCT_MAIN is not None:
        mf = mf[mf["net_pct_main"] <= JQ_MAX_NET_PCT_MAIN]
    hi = JQ_MAX_NET_PCT_MAIN if JQ_MAX_NET_PCT_MAIN is not None else "∞"
    print(f"  主力净占比 ∈ [{JQ_MIN_NET_PCT_MAIN}, {hi}]%：{len(mf)} 只")
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

    candidates = []
    for code in prelim_codes:
        flows = hist.get(code, [])
        consec = jd.consecutive_inflow_days(flows)
        net_pct = float(prelim.loc[code, "net_pct_main"])
        net_amt = float(prelim.loc[code, "net_amount_main"])
        turnover = _safe(val, code, "turnover_ratio")
        cap = _safe(val, code, "market_cap")
        change_pct = _safe(px, code, "change_pct")
        score = composite_score(net_pct, consec, change_pct, turnover)
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
            "综合评分": round(score, 3),
            "近N日主力流向": "、".join(
                f"{'+' if float(v) > 0 else ''}{round(float(v), 1)}" for v in flows
            ),
        })

    candidates.sort(key=lambda c: c["综合评分"], reverse=True)
    candidates = candidates[:top_n]
    print(f"✅ 选股完成，得到 {len(candidates)} 只候选股（按综合评分排序）")
    return candidates


def print_candidates(candidates: list) -> None:
    """美观打印候选股"""
    if not candidates:
        print("（无候选股）")
        return
    print("\n" + "─" * 84)
    print(f"{'代码':<8}{'名称':<10}{'板块':<9}{'涨跌幅%':>8}{'主力占比%':>9}"
          f"{'连续流入':>8}{'换手%':>7}{'综合分':>8}")
    print("─" * 84)
    for c in candidates:
        print(f"{c['代码']:<8}{str(c['名称'])[:8]:<10}{c['板块']:<9}"
              f"{(c.get('今日涨跌幅(%)') or 0):>8.2f}"
              f"{c['今日主力净占比']:>9.2f}{c['连续净流入天数']:>8}"
              f"{(c.get('换手率(%)') or 0):>7.2f}{c['综合评分']:>8.3f}")
    print("─" * 84)
    print("⚠️  已剔除涨停/接近涨停/停牌；候选按综合评分排序，仅供参考，非投资建议。")
