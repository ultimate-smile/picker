"""
聚宽策略平台 —— 实盘/模拟盘/回测 策略模板
=========================================
⚠️ 本文件 **不是** 在本地用 jqdatasdk 运行的脚本，而是直接粘贴到
   聚宽（joinquant.com）「策略研究 / 策略交易」里运行的策略代码。

为什么需要它？
  jqdatasdk 只能取数、不能下单。要在 A 股做真正的（合规）自动交易，最简单的
  途径就是把策略跑在聚宽平台：平台提供 order_target_value 等下单 API，
  并对接券商进行模拟盘/实盘。

策略逻辑（与本地 jq_selector / jq_trader 保持一致的思路）：
  - 每日开盘后选股：主力资金净占比高 + 连续净流入 + 市值/换手率达标；
  - 等权重建仓，最多持有 g.max_positions 只；
  - 盘中定时风控：止盈 / 止损 / 移动止盈；
  - 尾盘可选清仓（日内策略）。

────────────────────────────────────────────────────────────
免责声明：策略仅供学习研究，无法保证盈利。实盘前请充分回测，自担风险。
────────────────────────────────────────────────────────────
"""

# 注意：以下 import 与函数（order_target_value、run_daily、get_money_flow、
# get_valuation、attribute_history、get_current_data 等）均由 **聚宽平台** 提供，
# 本地不可运行。
from jqdata import *  # noqa: F401,F403  （聚宽平台环境提供）


# ─────────────────────────────────────────
# 1. 初始化
# ─────────────────────────────────────────

def initialize(context):
    # 基准与撮合设置
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    # 交易成本：佣金万2.5（最低5元）、印花税千1
    set_order_cost(OrderCost(
        open_commission=0.00025, close_commission=0.00025,
        close_tax=0.001, min_commission=5), type='stock')

    # ── 策略参数 ──
    g.min_net_pct_main = 5.0      # 主力净占比阈值(%)
    g.min_market_cap = 50.0       # 最小市值(亿)
    g.max_market_cap = 1000.0     # 最大市值(亿)
    g.max_turnover = 30.0         # 最大换手率(%)
    g.hist_lookback = 5           # 连续净流入回看交易日
    g.top_n = 20                  # 候选数
    g.max_positions = 3           # 最大持仓
    g.take_profit = 0.08          # 止盈
    g.stop_loss = 0.04            # 止损
    g.trail_stop = 0.03           # 移动止盈
    g.force_close = True          # 尾盘清仓（日内）
    g.highest = {}                # code -> 持仓期间最高价

    # ── 定时任务 ──
    run_daily(select_and_open, time='09:35')      # 开盘后选股建仓
    run_daily(risk_control, time='every_bar')     # 盘中风控
    if g.force_close:
        run_daily(close_all, time='14:55')        # 尾盘清仓


# ─────────────────────────────────────────
# 2. 选股
# ─────────────────────────────────────────

def pick_candidates(context):
    """与本地 jq_selector.select_candidates 等价的平台版实现"""
    date = context.previous_date  # 用上一交易日资金流向，避免未来函数

    # 股票池：全 A 股，剔除 ST/停牌/次新
    universe = get_all_securities(types=['stock'], date=date).index.tolist()
    cur = get_current_data()
    universe = [s for s in universe
                if not cur[s].is_st
                and not cur[s].paused
                and (context.current_dt.date() - get_security_info(s).start_date).days > 60]

    # 估值初筛（市值、换手率）
    q = query(valuation.code, valuation.market_cap, valuation.turnover_ratio
              ).filter(valuation.code.in_(universe))
    val = get_fundamentals(q, date=date)
    val = val[(val.market_cap >= g.min_market_cap) &
              (val.market_cap <= g.max_market_cap) &
              (val.turnover_ratio <= g.max_turnover)]
    codes = val.code.tolist()
    if not codes:
        return []

    # 当日主力资金流向
    mf = get_money_flow(codes, end_date=date, count=1,
                        fields=['sec_code', 'net_amount_main', 'net_pct_main'])
    if mf is None or mf.empty:
        return []
    mf = mf[mf.net_pct_main >= g.min_net_pct_main]
    mf = mf.sort_values('net_pct_main', ascending=False).head(g.top_n)
    top = mf.sec_code.tolist()

    # 连续净流入天数
    hist = get_money_flow(top, end_date=date, count=g.hist_lookback,
                          fields=['date', 'sec_code', 'net_amount_main'])
    cons = {}
    if hist is not None and not hist.empty:
        for code, gdf in hist.sort_values('date').groupby('sec_code'):
            days = 0
            for v in reversed(gdf.net_amount_main.tolist()):
                if v and v > 0:
                    days += 1
                else:
                    break
            cons[code] = days

    # 综合排序：主力净占比 + 连续净流入加权
    ranked = sorted(top, key=lambda c: (
        mf.set_index('sec_code').loc[c, 'net_pct_main'] + cons.get(c, 0) * 2
    ), reverse=True)
    return ranked


def select_and_open(context):
    candidates = pick_candidates(context)
    if not candidates:
        log.info("今日无符合条件的候选股")
        return

    slots = g.max_positions - len(context.portfolio.positions)
    if slots <= 0:
        return
    targets = candidates[:slots]
    # 等权重分配可用资金
    cash_per = context.portfolio.available_cash / max(1, len(targets))
    for code in targets:
        if code in context.portfolio.positions:
            continue
        order_target_value(code, cash_per)
        g.highest[code] = get_current_data()[code].last_price
        log.info("建仓 %s 目标金额 %.0f" % (code, cash_per))


# ─────────────────────────────────────────
# 3. 盘中风控
# ─────────────────────────────────────────

def risk_control(context):
    cur = get_current_data()
    for code in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[code]
        price = cur[code].last_price
        if not price:
            continue
        g.highest[code] = max(g.highest.get(code, pos.avg_cost), price)
        cost = pos.avg_cost
        if cost <= 0:
            continue
        pnl = (price - cost) / cost
        drawdown = (g.highest[code] - price) / g.highest[code] if g.highest[code] else 0

        reason = None
        if pnl <= -g.stop_loss:
            reason = "止损 %.1f%%" % (pnl * 100)
        elif pnl >= g.take_profit:
            reason = "止盈 %.1f%%" % (pnl * 100)
        elif pnl > 0 and drawdown >= g.trail_stop:
            reason = "移动止盈 回撤%.1f%%" % (drawdown * 100)

        if reason and pos.closeable_amount > 0:
            order_target_value(code, 0)
            g.highest.pop(code, None)
            log.info("卖出 %s %s" % (code, reason))


def close_all(context):
    for code in list(context.portfolio.positions.keys()):
        if context.portfolio.positions[code].closeable_amount > 0:
            order_target_value(code, 0)
            g.highest.pop(code, None)
    log.info("尾盘清仓完成")
