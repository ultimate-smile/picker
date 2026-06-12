"""
聚宽版 自动化选股 + 盘中交易 主入口
====================================
用法：
    python3 jq_main.py --selftest     # 登录聚宽并检查额度/接口连通性
    python3 jq_main.py --select       # 仅选股并打印候选
    python3 jq_main.py --deep         # 多维度综合评估选股 + 买卖价位 + 持有建议（推荐）
    python3 jq_main.py --analyze      # 选股 + Claude 深度分析（需配置 ANTHROPIC_API_KEY）
    python3 jq_main.py --paper        # 选股 + 本地模拟盘日内交易（安全，推荐）
    python3 jq_main.py --paper --demo # 同上，但用历史价做一次性演示（非交易时段也可跑）

自定义股票池（可与上面任一动作组合）：
    python3 jq_main.py --select --codes 600000,000001,300750
    python3 jq_main.py --select --watchlist my_list.txt
        # watchlist 文件：每行/逗号/空格分隔的代码，# 开头为注释

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


def _extract_opt(argv, name):
    """从 argv 中取 --name value 或 --name=value 的值，没有则返回 None。"""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _read_watchlist(path):
    """读取自选股文件：支持换行/逗号/空格分隔，# 开头为注释。"""
    codes = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                for tok in line.replace("，", ",").replace("\t", " ").replace(",", " ").split():
                    if tok:
                        codes.append(tok)
    except OSError as e:
        print(f"⚠️  读取 watchlist 失败：{e}")
    return codes


def _parse_codes(argv):
    """解析 --codes / --watchlist，返回代码列表或 None（表示用默认股票池）。"""
    codes = []
    cv = _extract_opt(argv, "--codes")
    if cv:
        codes += [x.strip() for x in cv.replace("，", ",").split(",") if x.strip()]
    wf = _extract_opt(argv, "--watchlist")
    if wf:
        codes += _read_watchlist(wf)
    # 去重保序
    seen, uniq = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq or None


def _probe_fundamentals():
    """探测 get_fundamentals（基本面维度依赖）。"""
    import jqdatasdk as jq
    from jqdatasdk import query, indicator
    q = (query(indicator.code, indicator.inc_revenue_year_on_year)
         .filter(indicator.code.in_(["000001.XSHE"])))
    return jq.get_fundamentals(q, statDate="2024q4")


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

    # 资金流向探测：按 config.JQ_MONEY_FLOW_API 选择实际使用的接口
    if jd.JQ_MONEY_FLOW_API == "basic":
        mf_probe = ("资金流向 get_money_flow（主力净占比，部分账号需付费）",
                    lambda: jq.get_money_flow(
                        sample, count=1,
                        fields=["sec_code", "net_amount_main", "net_pct_main"]), True)
    else:
        mf_probe = ("资金流向 get_money_flow_pro（按单量分档，推导主力净额）",
                    lambda: jq.get_money_flow_pro(
                        sample, count=1,
                        fields=["netflow_xl", "netflow_l"], data_type="money"), True)

    # 逐个接口探测：name, 调用, 是否关键
    probes = [
        ("证券列表 get_all_securities",
         lambda: jq.get_all_securities(["stock"]), False),
        ("估值表 get_valuation（市值/换手）",
         lambda: jq.get_valuation(sample, count=1,
                                  fields=["code", "market_cap", "turnover_ratio"]), True),
        mf_probe,
        ("日线 get_price(daily)",
         lambda: jq.get_price(sample, count=1, frequency="daily", fields=["close"]), False),
        ("分钟线 get_price(1m)",
         lambda: jq.get_price(sample, count=1, frequency="1m", fields=["close"]), False),
        ("实时tick get_current_tick（需实时行情权限）",
         lambda: jq.get_current_tick(sample), False),
        # ── 多维度评估（--deep）所需接口 ──
        ("基本面 get_fundamentals（营收/毛利/净利/现金流）",
         _probe_fundamentals, False),
        ("解禁 get_locked_shares（消息面利空规避）",
         lambda: jq.get_locked_shares([sample],
                                      start_date=datetime.now().strftime("%Y-%m-%d"),
                                      end_date=datetime.now().strftime("%Y-%m-%d")), False),
        ("行业 get_industry（板块归属）", lambda: jq.get_industry(sample), False),
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


def cmd_select(codes=None) -> list:
    candidates = sel.select_candidates(codes=codes)
    sel.print_candidates(candidates)
    return candidates


def cmd_deep(codes=None) -> None:
    """多维度综合评估：技术/基本面/筹码/大盘板块/消息催化 加权打分 + 操作持有建议。"""
    import jq_deep
    picks = jq_deep.select_deep(codes=codes)
    if not picks:
        print("无符合条件的标的，建议观望。")
        return
    print("\n" + "=" * 50)
    print(jq_deep.format_report(picks))
    print("=" * 50)


def cmd_analyze(codes=None) -> None:
    # 给 Claude/本地分析器更宽的候选池（JQ_TOP_N），再由其精选到 JQ_FINAL_PICKS
    pool = sel.select_candidates(codes=codes, top_n=sel.JQ_TOP_N)
    sel.print_candidates(pool)
    if not pool:
        print("无候选股，跳过分析")
        return

    import jq_analyst
    report = jq_analyst.generate_report(pool, final_picks=sel.JQ_FINAL_PICKS)
    print("\n" + "=" * 50)
    print(report)
    print("=" * 50)


def cmd_paper(demo: bool = False, codes=None) -> None:
    candidates = cmd_select(codes=codes)
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
    codes = _parse_codes(argv)

    print("=" * 50)
    print("  🚀 聚宽版 选股 + 盘中交易系统")
    print(f"  ⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if codes:
        print(f"  🎯 自定义股票池：{len(codes)} 只")
    print("=" * 50)

    # 仅含自定义池参数（无显式动作）时，默认执行选股
    action_flags = {"--selftest", "--diagnose", "--analyze", "--paper",
                    "--select", "--deep"}
    has_action = bool(args & action_flags)

    try:
        if "--selftest" in args or "--diagnose" in args:
            ok = cmd_selftest()
            return 0 if ok else 1
        if "--deep" in args:
            cmd_deep(codes=codes)
            return 0
        if "--analyze" in args:
            cmd_analyze(codes=codes)
            return 0
        if "--paper" in args:
            cmd_paper(demo="--demo" in args, codes=codes)
            return 0
        if "--select" in args or not has_action:
            cmd_select(codes=codes)
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
