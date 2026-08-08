"""
基于 shizixi K 线批量接口的历史规律回测与下一交易日建议。

示例：
    python3 shizixi_strategy.py --codes 688188 --since 2020-12-29 --to 2026-08-07
    python3 shizixi_strategy.py --codes 688188,688981 --period daily --adjust qfq

说明：策略只使用 OHLCV 历史数据，不依赖未来函数；同一参数可用于多只股票横向比较。
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import pandas as pd
import requests


API_URL = "https://api.shizixi.com/api/v3/data/kline/batch"


@dataclass
class StrategyParams:
    ma_fast: int = 5
    ma_mid: int = 20
    ma_slow: int = 60
    breakout_window: int = 20
    volume_window: int = 20
    atr_window: int = 14
    min_bars: int = 80
    fee_rate: float = 0.0003
    stamp_tax: float = 0.001
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 3.0
    max_holding_days: int = 20


@dataclass
class BacktestResult:
    code: str
    trades: int
    win_rate: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    profit_factor: float
    last_signal: str
    suggestion: str
    entry_price: float | None
    stop_loss: float | None
    take_profit: float | None


def normalize_code(code: str) -> str:
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) != 6:
        raise ValueError(f"股票代码必须为 6 位数字：{code}")
    return digits


def fetch_kline_batch(
    codes: Iterable[str],
    since: str,
    to: str,
    period: str = "daily",
    adjust: str = "qfq",
    limit: int = 10000,
    api_url: str = API_URL,
    timeout: int = 30,
) -> dict[str, pd.DataFrame]:
    """调用 shizixi 批量 K 线接口并转为按代码索引的 DataFrame。"""
    code_list = [normalize_code(c) for c in codes]
    resp = requests.get(
        api_url,
        params={
            "codes": ",".join(code_list),
            "period": period,
            "adjust": adjust,
            "limit": limit,
            "since": since,
            "to": to,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"接口返回错误：{payload['errors']}")

    out: dict[str, pd.DataFrame] = {}
    for item in payload.get("items", []):
        code = normalize_code(item.get("code", ""))
        # 兼容示例中的 bars/data 双字段；优先 bars。
        rows = item.get("bars") or item.get("data") or []
        df = pd.DataFrame(rows)
        if df.empty:
            out[code] = df
            continue
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        out[code] = df.dropna(subset=["open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
    return out


def add_factors(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """计算趋势、突破、量能和波动率因子。"""
    x = df.copy()
    x["ret"] = x["close"].pct_change()
    for n in [p.ma_fast, p.ma_mid, p.ma_slow]:
        x[f"ma{n}"] = x["close"].rolling(n).mean()
    x["vol_ma"] = x["volume"].rolling(p.volume_window).mean()
    x["break_high"] = x["high"].rolling(p.breakout_window).max().shift(1)
    x["prev_close"] = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - x["prev_close"]).abs(),
        (x["low"] - x["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    x["atr"] = tr.rolling(p.atr_window).mean()
    x["atr_pct"] = x["atr"] / x["close"]
    x["momentum20"] = x["close"] / x["close"].shift(p.ma_mid) - 1
    return x


def signal_for_row(row: pd.Series, p: StrategyParams) -> str:
    """历史规律：多头均线 + 放量突破 + 中期动量为买入；跌破中期均线或趋势转弱卖出。"""
    if row.isna().any():
        return "HOLD"
    bullish = row[f"ma{p.ma_fast}"] > row[f"ma{p.ma_mid}"] > row[f"ma{p.ma_slow}"]
    breakout = row["close"] > row["break_high"]
    volume_ok = row["volume"] > row["vol_ma"] * 1.2
    momentum_ok = 0 < row["momentum20"] < 0.35
    if bullish and breakout and volume_ok and momentum_ok:
        return "BUY"
    if row["close"] < row[f"ma{p.ma_mid}"] or row[f"ma{p.ma_fast}"] < row[f"ma{p.ma_mid}"]:
        return "SELL"
    return "HOLD"


def backtest(code: str, df: pd.DataFrame, p: StrategyParams = StrategyParams()) -> BacktestResult:
    x = add_factors(df, p)
    if len(x) < p.min_bars:
        raise ValueError(f"{code} 有效 K 线不足：{len(x)} < {p.min_bars}")
    x["signal"] = x.apply(lambda r: signal_for_row(r, p), axis=1)

    cash, shares, entry, highest, days = 1.0, 0.0, None, None, 0
    equity_curve, trade_returns = [], []
    for _, row in x.iterrows():
        close, atr = float(row["close"]), float(row.get("atr") or 0)
        if shares:
            days += 1
            highest = max(highest or close, close)
            stop = max((entry or close) - p.stop_loss_atr * atr, (highest or close) - p.stop_loss_atr * atr)
            target = (entry or close) + p.take_profit_atr * atr
            should_sell = row["signal"] == "SELL" or close <= stop or close >= target or days >= p.max_holding_days
            if should_sell:
                proceeds = shares * close * (1 - p.fee_rate - p.stamp_tax)
                trade_returns.append(proceeds / (shares * (entry or close)) - 1)
                cash, shares, entry, highest, days = proceeds, 0.0, None, None, 0
        elif row["signal"] == "BUY" and math.isfinite(close) and close > 0:
            shares = cash * (1 - p.fee_rate) / close
            cash, entry, highest, days = 0.0, close, close, 0
        equity_curve.append(cash + shares * close)

    total_return = equity_curve[-1] - 1 if equity_curve else 0.0
    curve = pd.Series(equity_curve, index=x["date"])
    daily = curve.pct_change().dropna()
    max_dd = float((curve / curve.cummax() - 1).min()) if not curve.empty else 0.0
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 2 and daily.std() else 0.0
    years = max((x["date"].iloc[-1] - x["date"].iloc[0]).days / 365.25, 1 / 252)
    annual = (1 + total_return) ** (1 / years) - 1
    wins = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else (float("inf") if wins else 0.0)

    last = x.iloc[-1]
    last_signal = signal_for_row(last, p)
    entry_price = stop_loss = take_profit = None
    if last_signal == "BUY":
        entry_price = float(last["close"])
        stop_loss = max(float(last[f"ma{p.ma_mid}"]), entry_price - p.stop_loss_atr * float(last["atr"]))
        take_profit = entry_price + p.take_profit_atr * float(last["atr"])
        suggestion = f"下一交易日可关注回踩/突破买入，参考买入价 {entry_price:.2f}，止损 {stop_loss:.2f}，目标 {take_profit:.2f}。"
    elif shares:
        suggestion = "已有持仓信号：趋势未破可继续持有；若跌破 MA20 或触发 ATR 移动止损则卖出。"
    else:
        suggestion = "暂不买入：等待多头均线、放量突破与正动量同时出现。"

    return BacktestResult(
        code=code, trades=len(trade_returns), win_rate=len(wins) / len(trade_returns) if trade_returns else 0.0,
        total_return=float(total_return), annual_return=float(annual), max_drawdown=max_dd, sharpe=sharpe,
        profit_factor=float(pf), last_signal=last_signal, suggestion=suggestion,
        entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
    )


def rank_results(results: list[BacktestResult]) -> list[BacktestResult]:
    """按收益、胜率、回撤和夏普综合排序，用于多股票比较。"""
    return sorted(results, key=lambda r: (r.total_return, r.win_rate, -abs(r.max_drawdown), r.sharpe), reverse=True)


def format_pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def print_report(results: list[BacktestResult]) -> None:
    ranked = rank_results(results)
    print("\n📊 回测对比（按综合表现排序）")
    print("代码     交易数  胜率     总收益   年化收益  最大回撤  夏普   盈亏比  最新信号")
    for r in ranked:
        pf = "∞" if math.isinf(r.profit_factor) else f"{r.profit_factor:.2f}"
        print(f"{r.code:8s} {r.trades:4d}  {format_pct(r.win_rate):>7s} {format_pct(r.total_return):>8s} "
              f"{format_pct(r.annual_return):>8s} {format_pct(r.max_drawdown):>8s} {r.sharpe:6.2f} {pf:>6s}  {r.last_signal}")
    if ranked:
        best = ranked[0]
        print(f"\n🏆 最优：{best.code}，胜率 {format_pct(best.win_rate)}，总收益 {format_pct(best.total_return)}。")
        print("\n🧭 下一交易日建议")
        for r in ranked:
            print(f"- {r.code}: {r.suggestion}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="shizixi K线量化策略回测与下一交易日建议")
    ap.add_argument("--codes", required=True, help="股票代码，逗号分隔，如 688188,688981")
    ap.add_argument("--since", default="2020-12-29")
    ap.add_argument("--to", default=datetime.utcnow().strftime("%Y-%m-%d"))
    ap.add_argument("--period", default="daily")
    ap.add_argument("--adjust", default="qfq")
    ap.add_argument("--limit", type=int, default=10000)
    ap.add_argument("--api-url", default=API_URL)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    data = fetch_kline_batch(codes, args.since, args.to, args.period, args.adjust, args.limit, args.api_url)
    results = [backtest(code, df) for code, df in data.items() if not df.empty]
    print_report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
