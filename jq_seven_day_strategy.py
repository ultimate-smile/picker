"""连续七跌反弹策略回测脚本。

策略规则（按接口返回的日线交易日逐根处理）：
1. 空仓时寻找“连续 7 个交易日收盘价低于前一交易日收盘价”的区间；
2. 在该区间之后的第 8 个交易日，用开盘价与第 7 个下跌日收盘价比较：
   - 开盘价 >= 第 7 个下跌日收盘价：按第 8 日开盘价全仓买入（默认 A 股 100 股一手）；
   - 开盘价 <  第 7 个下跌日收盘价：不买入，继续向后寻找下一段连续 7 跌；
3. 持仓后，从买入后的后续交易日开始逐日判断：
   - 当日开盘价 >= 前一交易日收盘价：继续持有；
   - 当日开盘价 <  前一交易日收盘价：按当日开盘价卖出；
   - 若连续 7 个后续交易日均满足“开盘价 >= 前一交易日收盘价”，按第 7 个满足日开盘价卖出；
4. 卖出后继续从卖出日之后寻找下一段连续 7 跌。

行情数据使用示例中的十字星接口：
https://api.shizixi.com/api/v3/data/kline/batch
仅供学习研究，不构成投资建议。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener, urlopen

import pandas as pd

DEFAULT_API_URL = "https://api.shizixi.com/api/v3/data/kline/batch"


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


def _normalise_code(code: str) -> str:
    """接口使用 6 位 A 股代码；兼容传入 688188.XSHG 等格式。"""
    return str(code).strip().split(".")[0].zfill(6)


def _bars_from_api_item(item: dict) -> list[dict]:
    """接口样例同时提供 bars/data；优先 bars，缺失时回退 data。"""
    bars = item.get("bars")
    if bars is None:
        bars = item.get("data")
    return bars or []


def normalise_bars(payload_or_bars, code: str = "") -> pd.DataFrame:
    """统一十字星接口响应或 bars 列表，保留 date/open/close 并按交易日升序。"""
    if payload_or_bars is None:
        return pd.DataFrame(columns=["date", "open", "close"])

    if isinstance(payload_or_bars, pd.DataFrame):
        df = payload_or_bars.copy()
    else:
        bars = payload_or_bars
        if isinstance(payload_or_bars, dict):
            items = payload_or_bars.get("items") or []
            target = _normalise_code(code) if code else None
            item = None
            for candidate in items:
                if target is None or str(candidate.get("code", "")).zfill(6) == target:
                    item = candidate
                    break
            if item is None:
                return pd.DataFrame(columns=["date", "open", "close"])
            bars = _bars_from_api_item(item)
        df = pd.DataFrame(bars)

    if df.empty:
        return pd.DataFrame(columns=["date", "open", "close"])
    if "date" not in df.columns:
        raise ValueError("行情数据缺少 date 字段")
    missing = {"open", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"行情数据缺少字段：{', '.join(sorted(missing))}")

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    for col in ("open", "close"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[["date", "open", "close"]].dropna().sort_values("date").reset_index(drop=True)


def fetch_daily_bars(
    code: str,
    start_date,
    end_date,
    *,
    api_url: str = DEFAULT_API_URL,
    limit: int = 10000,
    period: str = "daily",
    adjust: str = "qfq",
    timeout: int = 30,
) -> pd.DataFrame:
    """使用十字星 K 线接口获取回测所需交易日行情。"""
    stock_code = _normalise_code(code)
    params = {
        "codes": stock_code,
        "period": period,
        "adjust": adjust,
        "limit": int(limit),
        "since": _as_date(start_date).isoformat(),
        "to": _as_date(end_date).isoformat(),
    }
    url = f"{api_url}?{urlencode(params)}"
    opener = build_opener(ProxyHandler({}))
    try:
        response_cm = opener.open(url, timeout=timeout)
    except Exception:
        # 单元测试可 mock 模块级 urlopen；真实环境优先绕过代理，失败后再按系统配置兜底。
        response_cm = urlopen(url, timeout=timeout)
    with response_cm as response:
        payload = json.loads(response.read().decode("utf-8"))

    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError(f"行情接口返回错误：{errors}")
    return normalise_bars(payload, stock_code)


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
    df = normalise_bars(bars, code)
    df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)

    cash = float(initial_cash)
    shares = 0
    trades: list[BacktestTrade] = []
    down_streak = 0
    hold_open_streak = 0
    armed_after_down7: Optional[int] = None

    for i in range(1, len(df)):
        day = df.loc[i, "date"]
        prev_close = float(df.loc[i - 1, "close"])
        open_price = float(df.loc[i, "open"])
        close = float(df.loc[i, "close"])

        if shares > 0:
            if open_price < prev_close:
                cash += shares * open_price
                trades.append(BacktestTrade(day, code, "sell", open_price, shares, cash, "开盘价低于前一交易日收盘价"))
                shares = 0
                down_streak = 0
                hold_open_streak = 0
                armed_after_down7 = None
                continue

            hold_open_streak += 1
            if hold_open_streak >= 7:
                cash += shares * open_price
                trades.append(BacktestTrade(day, code, "sell", open_price, shares, cash, "连续7个交易日开盘价不低于前收盘价"))
                shares = 0
                down_streak = 0
                hold_open_streak = 0
                armed_after_down7 = None
            continue

        if armed_after_down7 is not None and i == armed_after_down7 + 1:
            if open_price >= prev_close:
                shares = _buy_shares(cash, open_price, lot)
                if shares > 0:
                    cash -= shares * open_price
                    trades.append(BacktestTrade(day, code, "buy", open_price, shares, cash, "七连跌后第8日开盘不低于第7日收盘"))
                    hold_open_streak = 0
                    armed_after_down7 = None
                    continue
            armed_after_down7 = None
            down_streak = 0

        down_streak = down_streak + 1 if close < prev_close else 0
        if down_streak >= 7:
            armed_after_down7 = i

    last_price = float(df["close"].iloc[-1]) if not df.empty else 0.0
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
    parser.add_argument("--code", required=True, help="股票代码，如 688188")
    parser.add_argument("--start", required=True, help="回测开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="回测结束日期 YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=100000.0, help="初始本金，默认 100000")
    parser.add_argument("--lot", type=int, default=100, help="每手股数，A 股默认 100")
    parser.add_argument("--adjust", default="qfq", help="复权方式，默认 qfq；可按接口支持传 none/hfq 等")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="K 线接口地址")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    bars = fetch_daily_bars(args.code, args.start, args.end, api_url=args.api_url, adjust=args.adjust)
    result = run_strategy(bars, code=args.code, start_date=args.start, end_date=args.end,
                          initial_cash=args.cash, lot=args.lot)
    print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
