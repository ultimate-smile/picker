"""历史推荐复盘与规则优化建议。

读取历史报告文本，解析推荐日期、代码、买入区间、止损、目标价，
再用日线 OHLC 做保守撮合，输出盈亏、胜率、触发/未触发统计。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, List, Optional

import pandas as pd

import jq_data as jd

_DATE_RE = re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*(?:号|日)?")
_HEAD_RE = re.compile(r"推荐[:：]\s*(?P<code>\d{6})\s+(?P<name>[^（\s]+)")
_BUY_RE = re.compile(r"建议买入区间\s*(?P<low>\d+(?:\.\d+)?)\s*[~～-]\s*(?P<high>\d+(?:\.\d+)?)")
_STOP_RE = re.compile(r"止损位\s*(?P<stop>\d+(?:\.\d+)?)")
_T1_RE = re.compile(r"目标价1\s*(?P<t1>\d+(?:\.\d+)?)")
_T2_RE = re.compile(r"目标价2\s*(?P<t2>\d+(?:\.\d+)?)")
_SCORE_RE = re.compile(r"综合评分\s*(?P<score>\d+(?:\.\d+)?)")


@dataclass
class Recommendation:
    rec_date: date
    code: str
    name: str
    buy_low: float
    buy_high: float
    stop: float
    target1: float
    target2: Optional[float] = None
    score: Optional[float] = None


@dataclass
class ReviewResult:
    rec: Recommendation
    status: str
    entry_date: Optional[date] = None
    entry_price: Optional[float] = None
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl_pct: float = 0.0
    max_gain_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    holding_days: int = 0


def parse_recommendations(text: str, year: Optional[int] = None) -> List[Recommendation]:
    """从中文推荐报告中解析可复盘的推荐记录。"""
    year = year or datetime.now().year
    current_date: Optional[date] = None
    pending = None
    recs: List[Recommendation] = []

    def flush():
        nonlocal pending
        if pending and current_date and {"buy_low", "buy_high", "stop", "target1"} <= pending.keys():
            recs.append(Recommendation(rec_date=current_date, **pending))
        pending = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        dm = _DATE_RE.search(line)
        hm = _HEAD_RE.search(line)
        if dm and not hm:
            flush()
            current_date = date(year, int(dm.group("m")), int(dm.group("d")))
            continue
        if hm:
            flush()
            pending = {"code": hm.group("code"), "name": hm.group("name")}
            sm = _SCORE_RE.search(line)
            if sm:
                pending["score"] = float(sm.group("score"))
            continue
        if not pending:
            continue
        bm = _BUY_RE.search(line)
        if bm:
            pending["buy_low"] = float(bm.group("low"))
            pending["buy_high"] = float(bm.group("high"))
        stm = _STOP_RE.search(line)
        if stm:
            pending["stop"] = float(stm.group("stop"))
        t1m = _T1_RE.search(line)
        if t1m:
            pending["target1"] = float(t1m.group("t1"))
        t2m = _T2_RE.search(line)
        if t2m:
            pending["target2"] = float(t2m.group("t2"))
    flush()
    return recs


def _fetch_bars(code: str, start: date, end: date) -> pd.DataFrame:
    jd.ensure_auth()
    jq_code = jd.to_jq_code(code)
    return jd.jq.get_price(jq_code, start_date=start, end_date=end, frequency="daily",
                           fields=["open", "high", "low", "close"], panel=False)


def review_one(rec: Recommendation, end_date: date) -> ReviewResult:
    bars = _fetch_bars(rec.code, rec.rec_date, end_date)
    if bars is None or len(bars) == 0:
        return ReviewResult(rec=rec, status="无行情")
    bars = bars.reset_index()
    entry_price = None
    entry_date = None
    max_high = None
    min_low = None
    for _, row in bars.iterrows():
        day = pd.to_datetime(row.get("time") or row.get("index")).date()
        o, h, l, c = map(float, [row["open"], row["high"], row["low"], row["close"]])
        if entry_price is None:
            if l <= rec.buy_high and h >= rec.buy_low:
                entry_price = o if rec.buy_low <= o <= rec.buy_high else rec.buy_high
                entry_date = day
                max_high = h
                min_low = l
            else:
                continue
        max_high = max(max_high, h)
        min_low = min(min_low, l)
        # 保守：同一天同时碰止损/目标，按先止损处理。
        if l <= rec.stop:
            pnl = (rec.stop - entry_price) / entry_price * 100
            return ReviewResult(rec, "已止损", entry_date, entry_price, day, rec.stop, "stop", pnl,
                                (max_high-entry_price)/entry_price*100, (min_low-entry_price)/entry_price*100,
                                (day-entry_date).days + 1)
        if h >= rec.target1:
            pnl = (rec.target1 - entry_price) / entry_price * 100
            return ReviewResult(rec, "达目标1", entry_date, entry_price, day, rec.target1, "target1", pnl,
                                (max_high-entry_price)/entry_price*100, (min_low-entry_price)/entry_price*100,
                                (day-entry_date).days + 1)
    if entry_price is None:
        return ReviewResult(rec=rec, status="未触发买入")
    last = bars.iloc[-1]
    last_day = pd.to_datetime(last.get("time") or bars.index[-1]).date()
    close = float(last["close"])
    pnl = (close - entry_price) / entry_price * 100
    return ReviewResult(rec, "持有中", entry_date, entry_price, last_day, close, "close", pnl,
                        (max_high-entry_price)/entry_price*100, (min_low-entry_price)/entry_price*100,
                        (last_day-entry_date).days + 1)


def summarize(results: Iterable[ReviewResult]) -> str:
    rows = list(results)
    traded = [r for r in rows if r.entry_price]
    wins = [r for r in traded if r.pnl_pct > 0]
    losses = [r for r in traded if r.pnl_pct < 0]
    avg = sum(r.pnl_pct for r in traded) / len(traded) if traded else 0.0
    total = sum(r.pnl_pct for r in traded)
    win_rate = len(wins) / len(traded) * 100 if traded else 0.0
    lines = ["════════════════════════════════════",
             "📊 历史推荐复盘报告（保守日线撮合）",
             f"样本 {len(rows)} 条｜触发买入 {len(traded)} 条｜盈利 {len(wins)} 条｜亏损 {len(losses)} 条",
             f"胜率 {win_rate:.1f}%｜单票平均收益 {avg:.2f}%｜等权累计收益 {total:.2f}%",
             "────────────────────────────────────"]
    for r in rows:
        rec = r.rec
        if not r.entry_price:
            lines.append(f"{rec.rec_date} {rec.code} {rec.name}｜{r.status}")
            continue
        lines.append(f"{rec.rec_date} {rec.code} {rec.name}｜{r.status}｜买入 {r.entry_date} @{r.entry_price:.2f} "
                     f"→ {r.exit_date} @{r.exit_price:.2f}｜收益 {r.pnl_pct:+.2f}%｜"
                     f"最大浮盈 {r.max_gain_pct:+.2f}%｜最大回撤 {r.max_drawdown_pct:+.2f}%｜{r.holding_days}天")
    lines += ["────────────────────────────────────",
              "规则优化：1) 目标1盈亏比低于阈值的票降仓或剔除；2) 同一交易日同一标的重复推荐只保留最新计划；"
              "3) 出现 MACD 死叉却给出偏多/持有时降低技术分并提示观望；4) 买入区间高于现价时等待回踩/突破确认，避免倒挂追价。",
              "⚠️  复盘按日线 OHLC 保守估算，无法还原分钟级先后顺序；仅供策略检验，不构成投资建议。",
              "════════════════════════════════════"]
    return "\n".join(lines)


def review_text(text: str, *, year: Optional[int] = None, end_date: Optional[date] = None) -> str:
    end_date = end_date or datetime.now().date()
    recs = parse_recommendations(text, year=year or end_date.year)
    return summarize(review_one(r, end_date) for r in recs)
