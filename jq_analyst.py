"""
聚宽版 操作报告生成
====================
把选股结果（主力资金净占比/净流入、连续净流入天数、涨跌幅、换手率、市值、综合评分、
近 N 日主力流向）推送给 Claude，生成可执行的“操作报告”。

若 Claude 不可用（未配置 / 未安装 SDK / Key 无效 / 网络或额度问题），自动降级为
**基于综合评分 + 风控参数**的规则化操作报告（买入区间 / 止损 / 目标价 / 仓位），
确保任何情况下都能给出可执行建议（即“不能调用 Claude 时按最好的设置选股和操作”）。
"""

from datetime import datetime

import jq_data as jd

try:
    import config as _cfg
except ImportError:
    _cfg = None


def _cfg_get(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


CLAUDE_MODEL = _cfg_get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = _cfg_get("ANTHROPIC_API_KEY", "")

TAKE_PROFIT_PCT = _cfg_get("TAKE_PROFIT_PCT", 0.08)
STOP_LOSS_PCT = _cfg_get("STOP_LOSS_PCT", 0.04)
PER_POSITION_PCT = _cfg_get("PER_POSITION_PCT", 0.3)
MAX_POSITIONS = _cfg_get("MAX_POSITIONS", 3)


def _fmt(v, suffix="", nd=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _candidate_lines(candidates) -> str:
    text = ""
    for i, s in enumerate(candidates, 1):
        text += (
            f"\n【候选股{i}】{s.get('代码')} {s.get('名称')}（{s.get('板块')}）\n"
            f"  - 今日涨跌幅：{_fmt(s.get('今日涨跌幅(%)'), '%')}\n"
            f"  - 主力净占比：{_fmt(s.get('今日主力净占比'), '%')}\n"
            f"  - 主力净流入：{_fmt(s.get('今日主力净流入(万)'), '万', 0)}\n"
            f"  - 连续净流入天数：{s.get('连续净流入天数')}天\n"
            f"  - 换手率：{_fmt(s.get('换手率(%)'), '%')}\n"
            f"  - 总市值：{_fmt(s.get('总市值(亿)'), '亿', 1)}\n"
            f"  - 综合评分：{_fmt(s.get('综合评分'), '', 3)}\n"
            f"  - 近N日主力流向：{s.get('近N日主力流向') or '—'}\n"
        )
    return text


def build_prompt(candidates, final_picks=3) -> str:
    return f"""你是专业的 A 股交易分析师。今天是 {datetime.now().strftime('%Y年%m月%d日')}。
以下候选股已经过严格的“可操作性过滤”：已剔除涨停/接近涨停（封板买不进、追高风险）、
跌停、停牌，且当日涨跌幅控制在合理区间。请据此从中选出最值得操作的 {final_picks} 只，
并给出**可执行的操作报告**（不要追涨停、不要抄弱势）。

【候选股数据（含主力资金净占比/净流入等）】
{_candidate_lines(candidates)}

评分参考：主力净占比与连续净流入体现资金持续性；温和上涨(1%~6%)、健康换手(3%~15%)优先；
异常爆量/过热应降权。请输出（严格、简洁、给出具体数字）：

════════════════════════════════════
📅 {datetime.now().strftime('%Y-%m-%d')} 操作报告（精选{final_picks}股）
════════════════════════════════════
对每只股票：
- 代码 名称（板块）｜综合评分
- 资金信号：用主力净占比 / 连续净流入 / 近N日流向 一句话点评
- 操作建议：买入区间、止损位、目标价1 / 目标价2、建议仓位（单只不超过 {PER_POSITION_PCT*100:.0f}%）
- 风险提示
最后给出“整体市场判断 + 操作纪律”。
若候选普遍偏弱，请直接说明“信号偏弱，建议观望”。
"""


def analyze_with_claude(candidates, final_picks=3):
    """调用 Claude 生成操作报告；不可用时返回 None（由上层降级到本地报告）。"""
    if not ANTHROPIC_API_KEY:
        print("⚠️  未配置 ANTHROPIC_API_KEY，跳过 Claude，改用本地规则化报告。")
        return None
    try:
        import anthropic
    except ImportError:
        print("⚠️  未安装 anthropic（pip install anthropic），改用本地规则化报告。")
        return None

    print("🤖 正在调用 Claude 生成操作报告...")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": build_prompt(candidates, final_picks)}],
        )
        return msg.content[0].text
    except Exception as e:
        print(f"⚠️  Claude 调用失败（{e}），改用本地规则化报告。")
        return None


def local_report(candidates, final_picks=3, price_func=None) -> str:
    """不依赖 Claude 的规则化操作报告：综合评分排序 + 风控参数给出买卖价位。"""
    price_func = price_func or jd.get_last_price
    picks = candidates[:final_picks]
    out = ["════════════════════════════════════",
           f"📅 {datetime.now().strftime('%Y-%m-%d')} 操作报告"
           f"（本地规则化，精选{len(picks)}股）",
           "════════════════════════════════════"]
    medals = ["🥇 第一", "🥈 第二", "🥉 第三"]
    for i, s in enumerate(picks):
        head = medals[i] if i < len(medals) else f"第{i + 1}"
        code = s.get("jq代码") or s.get("代码")
        try:
            price = float(price_func(code) or 0)
        except Exception:
            price = 0.0
        out.append("")
        out.append(f"{head}推荐：{s.get('代码')} {s.get('名称')}（{s.get('板块')}）"
                   f"｜综合评分 {_fmt(s.get('综合评分'), '', 3)}")
        out.append(f"  资金信号：主力净占比 {_fmt(s.get('今日主力净占比'), '%')}，"
                   f"连续净流入 {s.get('连续净流入天数')} 天，"
                   f"今日涨跌 {_fmt(s.get('今日涨跌幅(%)'), '%')}，"
                   f"换手 {_fmt(s.get('换手率(%)'), '%')}")
        if price > 0:
            buy_lo, buy_hi = price * 0.99, price * 1.01
            stop = price * (1 - STOP_LOSS_PCT)
            t1 = price * (1 + TAKE_PROFIT_PCT)
            t2 = price * (1 + 2 * TAKE_PROFIT_PCT)
            out.append(f"  现价≈{price:.2f}　建议买入区间 {buy_lo:.2f}~{buy_hi:.2f}")
            out.append(f"  止损位 {stop:.2f}（-{STOP_LOSS_PCT * 100:.0f}%）｜"
                       f"目标价1 {t1:.2f}（+{TAKE_PROFIT_PCT * 100:.0f}%）｜"
                       f"目标价2 {t2:.2f}（+{2 * TAKE_PROFIT_PCT * 100:.0f}%）")
            out.append(f"  建议仓位：单只不超过 {PER_POSITION_PCT * 100:.0f}%"
                       f"（最多持仓 {MAX_POSITIONS} 只）")
        else:
            out.append("  ⚠️  暂无法获取现价，买卖价位以盘中实时为准。")
        out.append(f"  近N日主力流向：{s.get('近N日主力流向') or '—'}")
    out += ["",
            "════════════════════════════════════",
            "⚠️  纪律：已剔除涨停/不可买入标的；严格止损、不追高、仓位分散。",
            "    本报告由规则生成，仅供参考，非投资建议，实盘自担风险。",
            "════════════════════════════════════"]
    return "\n".join(out)


def generate_report(candidates, final_picks=3, price_func=None) -> str:
    """优先用 Claude；不可用时自动降级到本地规则化操作报告。"""
    if not candidates:
        return "（无候选股，无法生成操作报告）"
    report = analyze_with_claude(candidates, final_picks=final_picks)
    if report:
        return report
    return local_report(candidates, final_picks=final_picks, price_func=price_func)
