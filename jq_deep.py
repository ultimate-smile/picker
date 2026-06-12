"""
多维度综合选股 + 操作/持有建议（--deep）
=========================================
把“技术面 / 基本面 / 筹码结构 / 大盘与板块 / 消息面催化”五大维度按重要等级（权重）
综合打分，从资金面初筛出的候选池里精选个股，并基于**关键价位**给出可执行的
买入区间 / 止损位 / 目标价，以及结合趋势与基本面的**持有建议**。

流程：
  1. 复用 jq_selector.select_candidates 用资金面+可操作性初筛出候选池（JQ_DEEP_POOL_SIZE）；
  2. 逐只补齐五维数据（K线/财务/筹码/指数/解禁），用 jq_factors 打分并加权综合；
  3. 按综合分排序取前 N，依据技术面关键价位生成买卖价位与持有建议；
  4. 打印结构化报告（含各维度分数，便于复盘“为什么选它”）。

所有权重/阈值/价位规则均在 config.py 可配置（评估规则可配置）。
"""

from datetime import datetime

import pandas as pd

import jq_data as jd
import jq_factors as jf
import jq_selector as sel

try:
    import config as _cfg
except ImportError:
    _cfg = None


def _cfg_get(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


DEEP_POOL_SIZE = _cfg_get("JQ_DEEP_POOL_SIZE", 30)
DEEP_FINAL_PICKS = _cfg_get("JQ_DEEP_FINAL_PICKS", 3)
DEEP_MIN_SCORE = _cfg_get("JQ_DEEP_MIN_SCORE", 0.0)
BUY_PULLBACK_PCT = _cfg_get("JQ_BUY_PULLBACK_PCT", 0.02)
STOP_BUFFER_PCT = _cfg_get("JQ_STOP_BUFFER_PCT", 0.02)
STOP_MAX_PCT = _cfg_get("JQ_STOP_MAX_PCT", 0.08)
PER_POSITION_PCT = _cfg_get("PER_POSITION_PCT", 0.3)
MAX_POSITIONS = _cfg_get("MAX_POSITIONS", 3)
UNLOCK_WARN_DAYS = _cfg_get("JQ_UNLOCK_WARN_DAYS", 30)
CATALYST_OVERRIDES = _cfg_get("JQ_CATALYST_OVERRIDES", {})

DIM_LABELS = {
    "technical": "技术面", "market": "大盘板块", "fundamental": "基本面",
    "chips": "筹码", "catalyst": "消息催化",
}


# ═════════════════════════════════════════════════════════════════
# 单只股票五维评分
# ═════════════════════════════════════════════════════════════════

def score_one(code, *, board, ref_date=None, market_ctx=None,
              fundamentals=None, unlock=None, override=0.0):
    """对单只股票做五维评分，返回 (综合分, dims, detail)。各依赖可预取后传入以减少请求。"""
    dims = {}
    detail = {}

    # 1) 技术面（K线）
    try:
        bars = jf.fetch_daily_bars(code, end_date=ref_date)
    except Exception as e:
        bars = pd.DataFrame()
        detail["tech_err"] = str(e)
    s_tech, d_tech = jf.technical_score(bars)
    dims["technical"] = s_tech
    detail["technical"] = d_tech

    # 2) 基本面
    fd = (fundamentals or {}).get(jd.from_jq_code(code), {})
    s_fund, d_fund = jf.fundamental_score(
        fd.get("rev_yoy"), fd.get("margin"),
        fd.get("net_profit"), fd.get("op_cash_flow"))
    dims["fundamental"] = s_fund
    detail["fundamental"] = d_fund

    # 3) 筹码结构（用同一份 K 线）
    if bars is not None and len(bars) > 0:
        s_chip, d_chip = jf.chip_score(
            bars.get("high", bars["close"]), bars.get("low", bars["close"]),
            bars["close"], bars.get("turnover", pd.Series([float("nan")] * len(bars))))
    else:
        s_chip, d_chip = 0.5, {}
    dims["chips"] = s_chip
    detail["chips"] = d_chip

    # 4) 大盘与板块
    ctx = market_ctx or jf.compute_market_context(board, ref_date)
    stock_closes = bars["close"] if (bars is not None and len(bars) > 0) else []
    s_mkt, d_mkt = jf.market_score(ctx.get("index_closes", []), stock_closes,
                                   northbound_net=ctx.get("northbound"))
    dims["market"] = s_mkt
    detail["market"] = d_mkt
    detail["market"]["index_code"] = ctx.get("index_code")

    # 5) 消息面催化（解禁 + 自定义）
    u = (unlock or {}).get(jd.from_jq_code(code), {})
    s_cat, d_cat = jf.catalyst_score(
        unlock_rate=u.get("rate", 0.0), days_to_unlock=u.get("days"),
        override=override)
    if u:
        d_cat["unlock_date"] = u.get("date")
    dims["catalyst"] = s_cat
    detail["catalyst"] = d_cat

    total = jf.aggregate(dims)
    return total, dims, detail


# ═════════════════════════════════════════════════════════════════
# 买卖价位 + 持有建议（基于关键价位与趋势/基本面）
# ═════════════════════════════════════════════════════════════════

def trade_plan(close, levels, dims, detail):
    """根据关键价位生成买入区间/止损/目标价，并结合趋势+基本面给持有建议。"""
    plan = {}
    if not close or close <= 0:
        return {"note": "无现价/K线，价位以盘中实时为准"}
    sup = (levels or {}).get("support") or close * 0.95
    res = (levels or {}).get("resistance") or close * 1.08
    prior_high = (levels or {}).get("prior_high")

    # 买入区间：现价下方小幅回踩到支撑之间（不追高、不抄弱势）
    buy_high = min(close * 1.01, res * 0.99)
    buy_low = max(sup * 1.005, close * (1 - BUY_PULLBACK_PCT))
    if buy_low > buy_high:
        buy_low, buy_high = min(sup, close), close

    # 止损：关键支撑下方一个缓冲；但不超过最大风险（取更高/更紧者）
    stop_by_level = sup * (1 - STOP_BUFFER_PCT)
    stop_by_cap = close * (1 - STOP_MAX_PCT)
    stop = max(stop_by_level, stop_by_cap)

    # 目标价：T1=压力位，T2=前高(若更高)或测幅
    t1 = res
    measured = res + (res - sup)
    t2 = max(prior_high or 0, measured)
    if t2 <= t1:
        t2 = t1 * 1.05

    plan.update(buy_low=round(buy_low, 2), buy_high=round(buy_high, 2),
                stop=round(stop, 2), target1=round(t1, 2), target2=round(t2, 2),
                support=round(sup, 2), resistance=round(res, 2),
                risk_pct=round((close - stop) / close * 100, 1),
                reward_pct=round((t1 - close) / close * 100, 1))
    plan["hold"] = _holding_advice(dims, detail)
    plan["position_pct"] = _position_pct(dims)
    return plan


def _holding_advice(dims, detail):
    """趋势决定何时走，基本面决定能不能拿得住。"""
    tech = detail.get("technical", {})
    below20 = (tech.get("ma") or {}).get("below_ma20")
    below60 = (tech.get("ma") or {}).get("below_ma60")
    fund = dims.get("fundamental", 0.5)

    if below60:
        return "趋势走坏（已跌破 60 日线），不建议持有，反弹减仓为主。"
    if below20:
        base = "已跌破 20 日线，初步警戒：先减仓，站回均线再考虑。"
    else:
        base = "站上 20 日均线，趋势健康：持有，回踩不破支撑可逢低加仓。"
    if fund >= 0.7:
        base += "基本面扎实（营收/毛利/现金流良好），中线可耐心持有、容忍正常回调。"
    elif fund <= 0.35:
        base += "基本面偏弱，以短线对待：严格止损、破位即走，不宜重仓久持。"
    else:
        base += "基本面中性，按技术信号操作为主。"
    return base


def _position_pct(dims):
    """综合分越高、风险维度越好，建议仓位越高（上限 PER_POSITION_PCT）。"""
    total = jf.aggregate(dims)
    cat = dims.get("catalyst", 0.5)
    factor = total
    if cat < 0.3:                 # 有重大解禁等利空，压缩仓位
        factor *= 0.5
    pct = PER_POSITION_PCT * max(0.3, min(1.0, factor / 0.7))
    return round(min(pct, PER_POSITION_PCT), 3)


# ═════════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════════

def select_deep(date=None, codes=None, pool_size=None, final_picks=None):
    """多维度综合选股主流程，返回精选列表（按综合分降序）。"""
    pool_size = pool_size or DEEP_POOL_SIZE
    final_picks = final_picks or DEEP_FINAL_PICKS
    ref = date or datetime.now().date()

    print("📡 [深度] 资金面+可操作性初筛候选池...")
    pool = sel.select_candidates(date=date, top_n=pool_size, codes=codes)
    if not pool:
        print("  无候选池（资金面初筛为空）。")
        return []
    print(f"  候选池 {len(pool)} 只，开始五维评估...")

    pool_codes = [c["jq代码"] for c in pool]

    # 预取批量数据，减少请求
    print("📡 [深度] 批量拉取基本面（营收/毛利/净利/现金流）...")
    try:
        statdates = jf.quarterly_statdates(ref)
        fundamentals = jd.get_fundamentals_history(pool_codes, statdates)
    except Exception as e:
        print(f"  ⚠️  基本面获取失败（{e}），基本面维度按中性处理。")
        fundamentals = {}

    print("📡 [深度] 批量查询未来解禁（消息面利空规避）...")
    try:
        unlock = jd.get_locked_shares_window(pool_codes, start_date=ref,
                                             forward_days=UNLOCK_WARN_DAYS)
    except Exception as e:
        print(f"  ⚠️  解禁数据获取失败（{e}），消息面按中性处理。")
        unlock = {}

    # 市场上下文按板块缓存（同板块共享指数序列）；北向全局只取一次
    market_cache = {}

    results = []
    for i, c in enumerate(pool, 1):
        code = c["jq代码"]
        board = c.get("板块") or jd.get_board(code)
        if board not in market_cache:
            market_cache[board] = jf.compute_market_context(board, ref)
        override = float(CATALYST_OVERRIDES.get(jd.from_jq_code(code), 0.0))
        try:
            total, dims, detail = score_one(
                code, board=board, ref_date=ref,
                market_ctx=market_cache[board], fundamentals=fundamentals,
                unlock=unlock, override=override)
        except Exception as e:
            print(f"  ⚠️  [{i}/{len(pool)}] {code} 评分失败：{e}")
            continue

        tech = detail.get("technical", {})
        levels = tech.get("levels", {})
        close = tech.get("close") or 0.0
        plan = trade_plan(close, levels, dims, detail)

        merged = dict(c)
        merged.update({
            "综合评分": round(total, 3),
            "维度分": {k: round(v, 3) for k, v in dims.items()},
            "维度明细": detail,
            "现价": round(close, 2) if close else None,
            "操作计划": plan,
        })
        results.append(merged)
        print(f"  [{i}/{len(pool)}] {c.get('代码')} {str(c.get('名称'))[:6]}"
              f"  综合 {total:.3f} ｜技 {dims['technical']:.2f}"
              f" 基 {dims['fundamental']:.2f} 筹 {dims['chips']:.2f}"
              f" 市 {dims['market']:.2f} 息 {dims['catalyst']:.2f}")

    results.sort(key=lambda r: r["综合评分"], reverse=True)
    if DEEP_MIN_SCORE > 0:
        results = [r for r in results if r["综合评分"] >= DEEP_MIN_SCORE]
    picks = results[:final_picks]
    print(f"✅ 五维评估完成，精选 {len(picks)} 只。")
    return picks


def format_report(picks, final_picks=None):
    """把精选结果格式化为可执行的操作/持有报告。"""
    final_picks = final_picks or DEEP_FINAL_PICKS
    if not picks:
        return "（无符合条件的标的，建议观望。）"
    today = datetime.now().strftime("%Y-%m-%d")
    out = ["════════════════════════════════════",
           f"📅 {today} 多维度综合选股报告（精选 {len(picks)} 股）",
           "   权重："
           + "、".join(f"{DIM_LABELS.get(k, k)} {int(v * 100)}%"
                       for k, v in jf.DIM_WEIGHTS.items()),
           "════════════════════════════════════"]
    medals = ["🥇 第一", "🥈 第二", "🥉 第三"]
    for i, s in enumerate(picks):
        head = medals[i] if i < len(medals) else f"第{i + 1}"
        dims = s.get("维度分", {})
        plan = s.get("操作计划", {})
        out.append("")
        out.append(f"{head}推荐：{s.get('代码')} {s.get('名称')}（{s.get('板块')}）"
                   f"｜综合评分 {s.get('综合评分')}")
        out.append("  维度分：" + " ".join(
            f"{DIM_LABELS.get(k, k)} {dims.get(k, 0):.2f}"
            for k in ("technical", "market", "fundamental", "chips", "catalyst")))
        out.append("  " + _signal_line(s))
        if plan.get("buy_low"):
            out.append(f"  现价≈{s.get('现价')}　建议买入区间 "
                       f"{plan['buy_low']}~{plan['buy_high']}（关键支撑 {plan['support']}）")
            out.append(f"  止损位 {plan['stop']}（-{plan['risk_pct']}%）｜"
                       f"目标价1 {plan['target1']}（+{plan['reward_pct']}%，压力位）｜"
                       f"目标价2 {plan['target2']}")
            out.append(f"  建议仓位：≤{plan['position_pct'] * 100:.0f}%"
                       f"（最多 {MAX_POSITIONS} 只）")
            out.append(f"  持有建议：{plan['hold']}")
        else:
            out.append(f"  ⚠️  {plan.get('note', '无价位数据')}")
    out += ["",
            "════════════════════════════════════",
            "⚠️  纪律：技术面定时机、基本面定持有、解禁等利空提前规避；",
            "    严格止损、不追高、仓位分散。本报告由规则生成，仅供参考，非投资建议。",
            "════════════════════════════════════"]
    return "\n".join(out)


def _signal_line(s):
    """一句话点评：资金 + 技术 + 风险。"""
    detail = s.get("维度明细", {})
    tech = detail.get("technical", {})
    macd = (tech.get("macd") or {})
    ma = (tech.get("ma") or {})
    cat = (detail.get("catalyst") or {})
    parts = [f"主力净占比 {s.get('今日主力净占比')}%、连续净流入 {s.get('连续净流入天数')} 天"]
    if ma.get("bull_align"):
        parts.append("均线多头排列")
    if macd.get("golden"):
        parts.append("MACD 金叉" + ("(零轴上)" if macd.get("above_zero") else ""))
    elif macd.get("death"):
        parts.append("MACD 死叉")
    if (tech.get("volprice") or {}).get("divergence"):
        parts.append("⚠️量价背离")
    rate = cat.get("unlock_rate") or 0
    if rate and rate > 0:
        parts.append(f"⚠️{UNLOCK_WARN_DAYS}日内解禁{rate * 100:.1f}%"
                     + (f"({cat.get('unlock_date')})" if cat.get("unlock_date") else ""))
    return "信号：" + "，".join(parts)
