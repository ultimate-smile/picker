"""连续七跌反弹策略回测脚本。

策略规则（按日线交易日逐根处理）：
1. 空仓时寻找“连续 7 个交易日收盘价低于前一交易日收盘价”的区间；
2. 在该区间之后的第 8 个交易日，用开盘价与第 7 个下跌日收盘价比较：
   - 开盘价 >= 第 7 个下跌日收盘价：按第 8 日开盘价全仓买入（A 股按 100 股一手）；
   - 开盘价 <  第 7 个下跌日收盘价：不买入，继续向后寻找下一段连续 7 跌；
3. 持仓后逐日判断：
   - 若当日开盘价 < 前一交易日收盘价：按当日开盘价卖出；
   - 否则继续持有；
   - 若持仓期间出现连续 7 个交易日收盘价高于前一交易日收盘价，按第 7 个上涨日收盘价卖出；
4. 卖出后继续从卖出日之后寻找下一段连续 7 跌。

本脚本默认使用 jq_data / jqdatasdk 获取交易日与日线行情，可用于指定股票代码、日期区间和本金的回测。
仅供学习研究，不构成投资建议。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import pandas as pd

import jq_data as jd


@dataclass
class BacktestTrade:
    date: date
    code: str
    side: str
    price: float
    shares: int
    cash: float
    reason: str


@dataclass
class BacktestResult:
    code: str
    start_date: date
    end_date: date
    initial_cash: float
    final_equity: float
    cash: float
    shares: int
    last_price: float
    trades: list[BacktestTrade]

    @property
    def profit(self) -> float:
        return self.final_equity - self.initial_cash

    @property
    def return_pct(self) -> float:
        return self.profit / self.initial_cash * 100 if self.initial_cash else 0.0


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()


def _normalise_bars(bars: pd.DataFrame, code: str) -> pd.DataFrame:
    """统一 jqdatasdk 日线返回格式，保留 date/open/close 并按交易日升序。"""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["date", "open", "close"])
    df = bars.copy().reset_index()
    if "time" not in df.columns:
        # get_price 单标的常把日期放在 index；reset_index 后通常叫 index。
        time_col = "index" if "index" in df.columns else df.columns[0]
        df = df.rename(columns={time_col: "time"})
    if "code" in df.columns:
        jq_code = jd.to_jq_code(code)
        df = df[df["code"].astype(str) == jq_code]
    df["date"] = pd.to_datetime(df["time"]).dt.date
    for col in ("open", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["date", "open", "close"]].dropna().sort_values("date").reset_index(drop=True)


def fetch_daily_bars(code: str, start_date, end_date, *, lookback_days: int = 30) -> pd.DataFrame:
    """使用聚宽接口获取回测所需日线行情。

    为了能判断 start_date 开始后的首个交易日是否已处于连续下跌/上涨序列，
    实际取数会向前扩展 lookback_days 个自然日；交易信号仍只会在 start_date
    至 end_date 内执行。
    """
    jd.ensure_auth()
    jq_code = jd.to_jq_code(code)
    start = _as_date(start_date) - timedelta(days=max(0, int(lookback_days)))
    end = _as_date(end_date)
    bars = jd.jq.get_price(
        jq_code,
        start_date=start,
        end_date=end,
        frequency="daily",
        fields=["open", "close"],
        skip_paused=False,
        fq="pre",
        panel=False,
    )
    return _normalise_bars(bars, code)


def _buy_shares(cash: float, price: float, lot: int) -> int:
    if cash <= 0 or price <= 0 or lot <= 0:
        return 0
    return int(cash // (price * lot)) * lot


def run_strategy(
    bars: pd.DataFrame,
    *,
    code: str,
    start_date,
    end_date,
    initial_cash: float = 100000.0,
    lot: int = 100,
) -> BacktestResult:
    """在已给定的日线数据上运行连续七跌反弹策略。"""
    start = _as_date(start_date)
    end = _as_date(end_date)
    df = _normalise_bars(bars, code) if "date" not in bars.columns else bars.copy()
    df["date"] = df["date"].apply(_as_date)
    df = df[(df["date"] <= end)].sort_values("date").reset_index(drop=True)

    cash = float(initial_cash)
    shares = 0
    trades: list[BacktestTrade] = []
    down_streak = 0
    up_streak = 0
    armed_after_down7: Optional[int] = None

    for i in range(1, len(df)):
        day = df.loc[i, "date"]
        if day < start:
            prev_close = float(df.loc[i - 1, "close"])
            close = float(df.loc[i, "close"])
            down_streak = down_streak + 1 if close < prev_close else 0
            up_streak = up_streak + 1 if close > prev_close else 0
            continue

        prev_close = float(df.loc[i - 1, "close"])
        open_price = float(df.loc[i, "open"])
        close = float(df.loc[i, "close"])

        if shares > 0:
            if open_price < prev_close:
                cash += shares * open_price
                trades.append(BacktestTrade(day, code, "sell", open_price, shares, cash, "开盘价低于前收盘价"))
                shares = 0
                down_streak = 0
                up_streak = 0
                armed_after_down7 = None
                continue

            up_streak = up_streak + 1 if close > prev_close else 0
            down_streak = down_streak + 1 if close < prev_close else 0
            if up_streak >= 7:
                cash += shares * close
                trades.append(BacktestTrade(day, code, "sell", close, shares, cash, "连续上涨7个交易日"))
                shares = 0
                down_streak = 0
                up_streak = 0
                armed_after_down7 = None
            continue

        if armed_after_down7 is not None and i == armed_after_down7 + 1:
            if open_price >= prev_close:
                shares = _buy_shares(cash, open_price, lot)
                if shares > 0:
                    cash -= shares * open_price
                    trades.append(BacktestTrade(day, code, "buy", open_price, shares, cash, "七连跌后第8日开盘不低于第7日收盘"))
                    up_streak = 1 if close > prev_close else 0
                    down_streak = 1 if close < prev_close else 0
                    armed_after_down7 = None
                    continue
            armed_after_down7 = None
            down_streak = 0
            up_streak = 0

        down_streak = down_streak + 1 if close < prev_close else 0
        up_streak = up_streak + 1 if close > prev_close else 0
        if down_streak >= 7:
            armed_after_down7 = i

    last_price = float(df[df["date"] <= end]["close"].iloc[-1]) if not df.empty else 0.0
    final_equity = cash + shares * last_price
    return BacktestResult(code, start, end, float(initial_cash), final_equity, cash, shares, last_price, trades)


def format_result(result: BacktestResult) -> str:
    lines = [
        "════════════════════════════════════",
        "📊 连续七跌反弹策略回测报告",
        f"标的：{result.code}",
        f"区间：{result.start_date} ~ {result.end_date}",
        f"初始本金：{result.initial_cash:,.2f}",
        f"最终权益：{result.final_equity:,.2f}",
        f"收益：{result.profit:+,.2f}（{result.return_pct:+.2f}%）",
        f"期末持仓：{result.shares} 股，期末参考价：{result.last_price:.2f}",
        "────────────────────────────────────",
    ]
    if not result.trades:
        lines.append("回测区间内没有触发交易。")
    else:
        for t in result.trades:
            side = "买入" if t.side == "buy" else "卖出"
            lines.append(f"{t.date} {side} {t.shares}股 @ {t.price:.2f}｜现金 {t.cash:,.2f}｜{t.reason}")
    lines.append("════════════════════════════════════")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="连续七跌反弹策略回测")
    parser.add_argument("--code", required=True, help="股票代码，如 600000 或 600000.XSHG")
    parser.add_argument("--start", required=True, help="回测开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="回测结束日期 YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=100000.0, help="初始本金，默认 100000")
    parser.add_argument("--lot", type=int, default=100, help="每手股数，A 股默认 100")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    bars = fetch_daily_bars(args.code, args.start, args.end)
    result = run_strategy(bars, code=args.code, start_date=args.start, end_date=args.end,
                          initial_cash=args.cash, lot=args.lot)
    print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
