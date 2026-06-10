"""
盘中交易层
==========
⚠️ 重要：jqdatasdk 只能取数，**不能下单**。真正的实盘下单有两条合规路径：
   1) 在聚宽策略平台上运行策略（order/order_target_value 等 API，见
      jq_strategy_joinquant.py）——代码跑在聚宽服务器，由其对接券商；
   2) 本地接入券商交易接口（QMT/Ptrade、easytrader 等）。

本模块给出统一的下单抽象 `Broker`：
   - `PaperBroker`：本地模拟盘，用聚宽实时行情撮合，安全，便于验证策略；
   - `LiveBroker`：实盘占位，需你接入券商 API 后实现（默认抛出明确错误）。

并实现一个带风控的日内交易引擎 `IntradayTrader`：建仓 → 轮询 →
止盈/止损/移动止盈 → （可选）收盘清仓。

────────────────────────────────────────────────────────────
免责声明：本代码仅供学习研究。任何策略都**无法保证盈利**，历史表现不代表
未来收益。实盘前请充分回测并自担风险，严格控制仓位。
────────────────────────────────────────────────────────────
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

# 逐项读取配置：config.py 缺少某些键时不影响其它已设置项（避免整体回退默认值）。
try:
    import config as _cfg
except ImportError:
    _cfg = None


def _cfg_get(name, default):
    return getattr(_cfg, name, default) if _cfg is not None else default


TRADE_MODE = _cfg_get("TRADE_MODE", "paper")
TRADE_CAPITAL = _cfg_get("TRADE_CAPITAL", 100000.0)
MAX_POSITIONS = _cfg_get("MAX_POSITIONS", 3)
PER_POSITION_PCT = _cfg_get("PER_POSITION_PCT", 0.3)
TAKE_PROFIT_PCT = _cfg_get("TAKE_PROFIT_PCT", 0.08)
STOP_LOSS_PCT = _cfg_get("STOP_LOSS_PCT", 0.04)
TRAIL_STOP_PCT = _cfg_get("TRAIL_STOP_PCT", 0.03)
INTRADAY_POLL_SECONDS = _cfg_get("INTRADAY_POLL_SECONDS", 30)
FORCE_CLOSE_BEFORE_END = _cfg_get("FORCE_CLOSE_BEFORE_END", True)


# ═════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════

@dataclass
class Position:
    code: str
    name: str
    shares: int
    entry_price: float
    highest_price: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.highest_price <= 0:
            self.highest_price = self.entry_price

    def market_value(self, price: float) -> float:
        return self.shares * price

    def pnl_pct(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price


@dataclass
class Trade:
    time: datetime
    code: str
    name: str
    side: str          # "buy" | "sell"
    price: float
    shares: int
    reason: str = ""


# ═════════════════════════════════════════
# 纯函数：仓位与风控（便于单元测试）
# ═════════════════════════════════════════

def position_size(cash: float, price: float, pct: float, lot: int = 100) -> int:
    """
    计算可买股数：按目标仓位金额，向下取整到 100 股（A 股一手=100股）。
    现金不足以买一手则返回 0。
    """
    if price <= 0 or pct <= 0 or cash <= 0:
        return 0
    # pct<=1 视为占现金比例，>1 视为绝对金额（不超过现金）
    budget = cash * pct if pct <= 1 else min(cash, pct)
    shares = int(budget // (price * lot)) * lot
    if shares * price > cash:          # 防止超买
        shares = int(cash // (price * lot)) * lot
    return max(0, shares)


def decide_exit(position: Position, price: float, *,
                take_profit=TAKE_PROFIT_PCT, stop_loss=STOP_LOSS_PCT,
                trail_stop=TRAIL_STOP_PCT) -> str:
    """
    根据当前价决定是否离场，返回原因字符串，None 表示继续持有。
    优先级：止损 > 止盈 > 移动止盈。
    """
    if price <= 0:
        return None
    pnl = position.pnl_pct(price)

    if stop_loss is not None and pnl <= -abs(stop_loss):
        return f"止损(浮亏{pnl*100:.1f}%)"
    if take_profit is not None and pnl >= abs(take_profit):
        return f"止盈(浮盈{pnl*100:.1f}%)"
    if trail_stop is not None and position.highest_price > 0:
        drawdown = (position.highest_price - price) / position.highest_price
        # 仅在已有浮盈时启用移动止盈，避免刚建仓就被甩出
        if pnl > 0 and drawdown >= abs(trail_stop):
            return f"移动止盈(回撤{drawdown*100:.1f}%)"
    return None


def in_trading_hours(now: datetime = None) -> bool:
    """A 股交易时段：09:30-11:30, 13:00-15:00"""
    now = now or datetime.now()
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def near_market_close(now: datetime = None, minutes: int = 5) -> bool:
    """是否临近收盘（默认收盘前 minutes 分钟内，即 [15:00-minutes, 15:00]）"""
    now = now or datetime.now()
    minutes = max(0, min(minutes, 90))
    start_minute = 15 * 60 - minutes      # 距 0 点的分钟数
    start = dtime(start_minute // 60, start_minute % 60)
    return start <= now.time() <= dtime(15, 0)


# ═════════════════════════════════════════
# Broker 抽象与实现
# ═════════════════════════════════════════

class Broker(ABC):
    @abstractmethod
    def buy(self, code: str, name: str, shares: int, price: float, reason: str = "") -> bool: ...

    @abstractmethod
    def sell(self, code: str, shares: int, price: float, reason: str = "") -> bool: ...

    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_positions(self) -> dict: ...


class PaperBroker(Broker):
    """
    本地模拟盘：按传入价格即时成交，计入手续费（默认万分之2.5，最低5元，
    卖出加千分之1印花税）。仅用于策略验证，不产生真实交易。
    """

    def __init__(self, cash: float = TRADE_CAPITAL,
                 commission_rate: float = 0.00025, min_commission: float = 5.0,
                 stamp_tax: float = 0.001):
        self.cash = float(cash)
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax = stamp_tax
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []

    def _commission(self, amount: float) -> float:
        return max(amount * self.commission_rate, self.min_commission)

    def buy(self, code, name, shares, price, reason="") -> bool:
        if shares <= 0 or price <= 0:
            return False
        amount = shares * price
        cost = amount + self._commission(amount)
        if cost > self.cash:
            print(f"  ⚠️  现金不足，无法买入 {code}（需{cost:.0f}，余{self.cash:.0f}）")
            return False
        self.cash -= cost
        if code in self.positions:
            pos = self.positions[code]
            total = pos.shares + shares
            pos.entry_price = (pos.entry_price * pos.shares + price * shares) / total
            pos.shares = total
        else:
            self.positions[code] = Position(code=code, name=name,
                                            shares=shares, entry_price=price)
        self.trades.append(Trade(datetime.now(), code, name, "buy", price, shares, reason))
        print(f"  🟢 买入 {code} {name} {shares}股 @ {price:.2f}  {reason}")
        return True

    def sell(self, code, shares, price, reason="") -> bool:
        pos = self.positions.get(code)
        if pos is None or shares <= 0 or price <= 0:
            return False
        shares = min(shares, pos.shares)
        amount = shares * price
        proceeds = amount - self._commission(amount) - amount * self.stamp_tax
        self.cash += proceeds
        pnl = pos.pnl_pct(price) * 100
        self.trades.append(Trade(datetime.now(), code, pos.name, "sell", price, shares, reason))
        print(f"  🔴 卖出 {code} {pos.name} {shares}股 @ {price:.2f}  "
              f"盈亏{pnl:+.1f}%  {reason}")
        pos.shares -= shares
        if pos.shares <= 0:
            del self.positions[code]
        return True

    def get_cash(self) -> float:
        return self.cash

    def get_positions(self) -> dict:
        return self.positions

    def total_equity(self, price_func) -> float:
        """总资产 = 现金 + 持仓市值（price_func: code->price）"""
        mv = 0.0
        for code, pos in self.positions.items():
            p = price_func(code) or pos.entry_price
            mv += pos.market_value(p)
        return self.cash + mv


class LiveBroker(Broker):
    """
    实盘下单占位。jqdatasdk 无下单能力，请在此接入券商交易接口，例如：
      - QMT / Ptrade（券商提供的本地交易终端 + Python API）
      - easytrader（模拟券商网页/客户端，稳定性有限，自担风险）
      - 券商官方交易 API
    实现 buy/sell/get_cash/get_positions 后即可把 TRADE_MODE 设为 "live"。
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "实盘下单需自行接入券商 API（QMT/Ptrade/easytrader 等）。\n"
            "jqdatasdk 仅提供行情数据，不能下单。请先用 TRADE_MODE='paper' 验证策略，"
            "或在聚宽策略平台用 jq_strategy_joinquant.py 跑模拟/实盘。"
        )

    def buy(self, *a, **k): ...
    def sell(self, *a, **k): ...
    def get_cash(self): ...
    def get_positions(self): ...


def make_broker() -> Broker:
    """按配置创建 Broker"""
    if TRADE_MODE == "live":
        return LiveBroker()
    return PaperBroker(cash=TRADE_CAPITAL)


# ═════════════════════════════════════════
# 日内交易引擎
# ═════════════════════════════════════════

class IntradayTrader:
    """
    日内交易引擎。依赖一个 price_func(code)->float 提供实时价（默认用聚宽）。

    流程：
      open_positions(candidates) 按候选股建仓（最多 MAX_POSITIONS 只）
      run() 循环轮询，对每只持仓做止盈/止损/移动止盈，必要时收盘清仓。
    """

    def __init__(self, broker: Broker = None, price_func=None,
                 max_positions=MAX_POSITIONS, per_position_pct=PER_POSITION_PCT,
                 poll_seconds=INTRADAY_POLL_SECONDS,
                 force_close_before_end=FORCE_CLOSE_BEFORE_END):
        self.broker = broker or make_broker()
        self.max_positions = max_positions
        self.per_position_pct = per_position_pct
        self.poll_seconds = poll_seconds
        self.force_close_before_end = force_close_before_end

        if price_func is None:
            import jq_data as jd
            price_func = jd.get_last_price
        self.price_func = price_func

    def open_positions(self, candidates: list) -> None:
        """对候选股建仓（按主力净占比优先，受最大持仓数与仓位比例约束）"""
        slots = self.max_positions - len(self.broker.get_positions())
        if slots <= 0:
            print("已满仓，跳过建仓")
            return
        print(f"\n📥 建仓（最多 {slots} 只，每只目标仓位 {self.per_position_pct*100:.0f}%）...")
        for c in candidates[:slots]:
            code = c.get("jq代码") or c.get("代码")
            name = c.get("名称", "")
            price = self.price_func(code)
            if not price or price <= 0:
                print(f"  ⚠️  {code} 无有效报价，跳过")
                continue
            shares = position_size(self.broker.get_cash(), price, self.per_position_pct)
            if shares <= 0:
                print(f"  ⚠️  {code} 资金不足一手，跳过")
                continue
            self.broker.buy(code, name, shares, price,
                            reason=f"建仓 主力占比{c.get('今日主力净占比','?')}%")

    def check_exits(self) -> None:
        """轮询一次：对每只持仓检查是否触发离场"""
        for code, pos in list(self.broker.get_positions().items()):
            price = self.price_func(code)
            if not price or price <= 0:
                continue
            if price > pos.highest_price:
                pos.highest_price = price
            reason = decide_exit(pos, price)
            if reason:
                self.broker.sell(code, pos.shares, price, reason=reason)

    def close_all(self, reason="收盘清仓") -> None:
        for code, pos in list(self.broker.get_positions().items()):
            price = self.price_func(code) or pos.entry_price
            self.broker.sell(code, pos.shares, price, reason=reason)

    def run(self, candidates: list, max_loops: int = None,
            respect_hours: bool = True) -> None:
        """
        主循环。
        :param max_loops: 限制轮询次数（测试/演示用）；None=一直到收盘。
        :param respect_hours: True 时仅在交易时段交易（实盘）；回测/演示可设 False。
        """
        print("\n" + "=" * 50)
        print("  🤖 日内交易引擎启动")
        print(f"  模式：{TRADE_MODE}  初始资金：{self.broker.get_cash():.0f}")
        print("=" * 50)

        if respect_hours and not in_trading_hours():
            print("⏰ 当前非交易时段（09:30-11:30 / 13:00-15:00），仅做建仓演示。")

        self.open_positions(candidates)

        loops = 0
        while True:
            if respect_hours:
                now = datetime.now()
                if not in_trading_hours(now):
                    if now.time() > dtime(15, 0):
                        break
                    time.sleep(self.poll_seconds)
                    continue
                if self.force_close_before_end and near_market_close(now):
                    self.close_all("收盘前清仓")
                    break

            self.check_exits()

            loops += 1
            if max_loops is not None and loops >= max_loops:
                break
            if not self.broker.get_positions():
                # 已全部离场，日内策略可结束
                if max_loops is not None:
                    break
            time.sleep(self.poll_seconds if respect_hours else 0)

        self.report()

    def report(self) -> None:
        print("\n" + "=" * 50)
        print("  📊 交易汇总")
        print("=" * 50)
        eq = self.broker.total_equity(self.price_func) if isinstance(self.broker, PaperBroker) else self.broker.get_cash()
        print(f"  现金：{self.broker.get_cash():.2f}")
        print(f"  持仓：{len(self.broker.get_positions())} 只")
        for code, pos in self.broker.get_positions().items():
            p = self.price_func(code) or pos.entry_price
            print(f"    {code} {pos.name} {pos.shares}股 成本{pos.entry_price:.2f} "
                  f"现价{p:.2f} 浮动{pos.pnl_pct(p)*100:+.1f}%")
        if isinstance(self.broker, PaperBroker):
            ret = (eq - TRADE_CAPITAL) / TRADE_CAPITAL * 100 if TRADE_CAPITAL else 0
            print(f"  总资产：{eq:.2f}  累计收益：{ret:+.2f}%")
            print(f"  成交笔数：{len(self.broker.trades)}")
        print("=" * 50)
        print("⚠️  以上为模拟结果，不构成投资建议；策略无法保证盈利，实盘需自担风险。")
