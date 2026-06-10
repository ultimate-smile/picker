"""
聚宽选股层
==========
基于聚宽数据，复刻并增强原 AKShare 版选股逻辑：
  1. 构建股票池（全 A 股或指定指数成分股）→ 过滤 ST/次新/板块
  2. 按市值、换手率初筛
  3. 拉取当日主力资金流向，按“主力净占比”筛选并排序
  4. 取前 N 只，补充“连续主力净流入天数”等特征
  5. 输出候选股列表（可交给 jq_trader 建仓，或交给 Claude 深度分析）
"""

from datetime import datetime

import pandas as pd

import jq_data as jd

try:
    from config import (
        JQ_UNIVERSE_INDEX, JQ_MIN_NET_PCT_MAIN, JQ_MIN_MARKET_CAP,
        JQ_MAX_MARKET_CAP, JQ_MAX_TURNOVER, JQ_HIST_LOOKBACK_DAYS,
        JQ_TOP_N, JQ_FINAL_PICKS, JQ_EXCLUDE_ST, JQ_EXCLUDE_KCB,
        JQ_EXCLUDE_BJ, JQ_EXCLUDE_NEW_DAYS,
    )
except ImportError:  # 合理默认值
    JQ_UNIVERSE_INDEX = None
    JQ_MIN_NET_PCT_MAIN = 5.0
    JQ_MIN_MARKET_CAP = 50.0
    JQ_MAX_MARKET_CAP = 1000.0
    JQ_MAX_TURNOVER = 30.0
    JQ_HIST_LOOKBACK_DAYS = 5
    JQ_TOP_N = 20
    JQ_FINAL_PICKS = 3
    JQ_EXCLUDE_ST = True
    JQ_EXCLUDE_KCB = False
    JQ_EXCLUDE_BJ = True
    JQ_EXCLUDE_NEW_DAYS = 60


def _passes_valuation(row) -> bool:
    cap = row.get("market_cap")
    if cap is None or pd.isna(cap):
        return False
    if JQ_MIN_MARKET_CAP is not None and cap < JQ_MIN_MARKET_CAP:
        return False
    if JQ_MAX_MARKET_CAP is not None and cap > JQ_MAX_MARKET_CAP:
        return False
    if JQ_MAX_TURNOVER is not None:
        tr = row.get("turnover_ratio")
        if tr is not None and not pd.isna(tr) and tr > JQ_MAX_TURNOVER:
            return False
    return True


def select_candidates(date=None, top_n=None) -> list:
    """
    主选股流程，返回候选股列表（按主力净占比降序）。

    每个元素为 dict：
        代码 / 名称 / 板块 / 总市值(亿) / 换手率(%) /
        今日主力净占比(%) / 今日主力净流入(万) / 连续净流入天数 / 近N日主力流向
    """
    top_n = top_n or JQ_TOP_N
    d = date or datetime.now().date()

    print("📡 [聚宽] 构建股票池...")
    uni = jd.get_universe(date=d, index_code=JQ_UNIVERSE_INDEX)
    uni = jd.filter_universe(
        uni, exclude_st=JQ_EXCLUDE_ST, exclude_kcb=JQ_EXCLUDE_KCB,
        exclude_bj=JQ_EXCLUDE_BJ, exclude_new_days=JQ_EXCLUDE_NEW_DAYS, ref_date=d,
    )
    codes = list(uni.index)
    print(f"  股票池规模：{len(codes)} 只")
    if not codes:
        return []

    print("📡 [聚宽] 拉取估值（市值/换手率）并初筛...")
    val = jd.get_valuation_oneday(codes, date=d)
    if not val.empty:
        keep = [c for c in val.index if _passes_valuation(val.loc[c])]
        codes = keep
    print(f"  市值/换手初筛后：{len(codes)} 只")
    if not codes:
        return []

    print("📡 [聚宽] 拉取当日主力资金流向...")
    mf = jd.get_money_flow_oneday(codes, date=d)
    if mf.empty:
        print("  ⚠️  资金流向为空。可能原因：非交易日 / 当日额度耗尽 / "
              "账号未开通『资金流向(get_money_flow)』数据权限（部分为付费档位）。")
        print("      可先运行 `python3 jq_main.py --selftest` 查看各接口可用性。")
        return []

    mf = mf.copy()
    mf["net_pct_main"] = pd.to_numeric(mf["net_pct_main"], errors="coerce")
    mf["net_amount_main"] = pd.to_numeric(mf["net_amount_main"], errors="coerce")
    mf = mf[mf["net_pct_main"] >= JQ_MIN_NET_PCT_MAIN]
    mf = mf.sort_values("net_pct_main", ascending=False).head(top_n)
    print(f"  主力净占比 ≥ {JQ_MIN_NET_PCT_MAIN}% 的候选：{len(mf)} 只")
    if mf.empty:
        return []

    top_codes = list(mf.index)
    print("📡 [聚宽] 补充历史资金流向（连续净流入天数）...")
    hist = jd.get_money_flow_history(
        top_codes, end_date=d, count=JQ_HIST_LOOKBACK_DAYS,
    )

    candidates = []
    for code in top_codes:
        flows = hist.get(code, [])
        cons = jd.consecutive_inflow_days(flows)
        cap = val.loc[code]["market_cap"] if (not val.empty and code in val.index) else None
        turн = val.loc[code]["turnover_ratio"] if (not val.empty and code in val.index) else None
        candidates.append({
            "代码": jd.from_jq_code(code),
            "jq代码": code,
            "名称": jd.get_security_name(code),
            "板块": jd.get_board(code),
            "总市值(亿)": round(float(cap), 1) if cap is not None and not pd.isna(cap) else None,
            "换手率(%)": round(float(turн), 2) if turн is not None and not pd.isna(turн) else None,
            "今日主力净占比": round(float(mf.loc[code]["net_pct_main"]), 2),
            "今日主力净流入(万)": round(float(mf.loc[code]["net_amount_main"]), 1),
            "连续净流入天数": cons,
            "近N日主力流向": "、".join(
                f"{'+' if float(v) > 0 else ''}{round(float(v), 1)}" for v in flows
            ),
        })

    print(f"✅ 选股完成，得到 {len(candidates)} 只候选股")
    return candidates


def print_candidates(candidates: list) -> None:
    """美观打印候选股"""
    if not candidates:
        print("（无候选股）")
        return
    print("\n" + "─" * 70)
    print(f"{'代码':<8}{'名称':<10}{'板块':<10}{'主力占比%':>9}{'连续流入':>8}{'市值亿':>9}")
    print("─" * 70)
    for c in candidates:
        print(f"{c['代码']:<8}{str(c['名称'])[:8]:<10}{c['板块']:<10}"
              f"{c['今日主力净占比']:>9.2f}{c['连续净流入天数']:>8}"
              f"{(c['总市值(亿)'] or 0):>9.1f}")
    print("─" * 70)
