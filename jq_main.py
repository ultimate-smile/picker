"""
聚宽版 自动化选股 + 盘中交易 主入口
====================================
用法：
    python3 jq_main.py --selftest     # 登录聚宽并检查额度/接口连通性
    python3 jq_main.py --select       # 仅选股并打印候选
    python3 jq_main.py --analyze      # 选股 + Claude 深度分析（需配置 ANTHROPIC_API_KEY）
    python3 jq_main.py --paper        # 选股 + 本地模拟盘日内交易（安全，推荐）
    python3 jq_main.py --paper --demo # 同上，但用历史价做一次性演示（非交易时段也可跑）

数据来源：聚宽 JQData（jqdatasdk）
交易：默认本地模拟盘（PaperBroker）。实盘需自行接券商，或用聚宽策略平台
      （见 jq_strategy_joinquant.py）。

────────────────────────────────────────────────────────────
免责声明：仅供学习研究，任何策略都无法保证盈利，实盘自担风险。
────────────────────────────────────────────────────────────
"""

import sys
from datetime import datetime

import jq_data as jd
import jq_selector as sel
import jq_trader as trd


def cmd_selftest() -> bool:
    """登录聚宽并逐个探测各数据接口的可用性（部分数据需付费档位）"""
    print("=" * 50)
    print("  🧪 聚宽连通性 & 数据权限自检")
    print("=" * 50)
    try:
        jd.ensure_auth()
    except Exception as e:
        print(f"❌ 登录失败：{e}")
        return False

    import jqdatasdk as jq
    try:
        print("剩余额度：", jq.get_query_count())
    except Exception as e:
        print(f"⚠️  额度查询失败：{e}")

    sample = "000001.XSHE"
    # 逐个接口探测：name, 调用, 是否关键
    probes = [
        ("证券列表 get_all_securities",
         lambda: jq.get_all_securities(["stock"]), False),
        ("估值表 get_valuation（市值/换手）",
         lambda: jq.get_valuation(sample, count=1,
                                  fields=["code", "market_cap", "turnover_ratio"]), True),
        ("资金流向 get_money_flow（主力净占比，部分账号需付费）",
         lambda: jq.get_money_flow_pro(sample, count=1,
                                   fields=["sec_code", "net_amount_main", "net_pct_main"]), True),
        ("日线 get_price(daily)",
         lambda: jq.get_price(sample, count=1, frequency="daily", fields=["close"]), False),
        ("分钟线 get_price(1m)",
         lambda: jq.get_price(sample, count=1, frequency="1m", fields=["close"]), False),
        ("实时tick get_current_tick（需实时行情权限）",
         lambda: jq.get_current_tick(sample), False),
    ]

    print("\n[接口可用性探测]")
    critical_ok = True
    for name, fn, critical in probes:
        try:
            r = fn()
            n = len(r) if hasattr(r, "__len__") else 1
            print(f"  ✅ {name}  → {n} 条")
        except Exception as e:
            msg = str(e)
            hint = ""
            if any(k in msg for k in ("权限", "permission", "付费", "未订阅", "无权")):
                hint = "（该数据需开通对应 JQData 付费档位）"
            print(f"  ❌ {name}  → {type(e).__name__}: {msg[:80]} {hint}")
            if critical:
                critical_ok = False

    print("\n" + "=" * 50)
    if critical_ok:
        print("  ✅ 关键接口（估值 + 资金流向）可用，可正常选股。")
    else:
        print("  ⚠️  关键接口不可用：可能是账号未开通对应数据权限（见付费档位），")
        print("      或当日额度耗尽 / 非交易时段。实时tick不可用不影响模拟盘（会自动用分钟/日线兜底）。")
    print("=" * 50)
    return critical_ok


def cmd_select() -> list:
    candidates = sel.select_candidates()
    sel.print_candidates(candidates)
    return candidates


def cmd_analyze() -> None:
    candidates = cmd_select()
    if not candidates:
        print("无候选股，跳过分析")
        return
    try:
        from stock_picker import analyze_with_claude
    except Exception as e:
        print(f"⚠️  无法加载 Claude 分析模块：{e}")
        return

    # 适配 analyze_with_claude 期望的字段
    adapted = [{
        "代码": c["代码"], "名称": c["名称"], "板块": c["板块"],
        "今日主力净占比": c["今日主力净占比"],
        "今日主力净流入(万)": c["今日主力净流入(万)"],
        "连续净流入天数": c["连续净流入天数"],
        "近5日主力流向": c["近N日主力流向"],
    } for c in candidates]

    hot = "（聚宽资金流向选股）"
    result = analyze_with_claude(adapted, hot)
    print("\n" + "=" * 50)
    print(result)
    print("=" * 50)


def cmd_paper(demo: bool = False) -> None:
    candidates = cmd_select()
    if not candidates:
        print("无候选股，结束")
        return

    if demo:
        # 演示模式：用最新价做一次性撮合（非交易时段也能跑通流程）
        trader = trd.IntradayTrader(price_func=jd.get_last_price)
        trader.run(candidates, max_loops=1, respect_hours=False)
    else:
        trader = trd.IntradayTrader(price_func=jd.get_last_price)
        trader.run(candidates, respect_hours=True)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    args = set(argv)

    print("=" * 50)
    print("  🚀 聚宽版 选股 + 盘中交易系统")
    print(f"  ⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    try:
        if "--selftest" in args or "--diagnose" in args:
            ok = cmd_selftest()
            return 0 if ok else 1
        if "--analyze" in args:
            cmd_analyze()
            return 0
        if "--paper" in args:
            cmd_paper(demo="--demo" in args)
            return 0
        if "--select" in args or not args:
            cmd_select()
            return 0
    except RuntimeError as e:
        print(f"\n❌ {e}")
        return 1
    except KeyboardInterrupt:
        print("\n已手动中断。")
        return 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
