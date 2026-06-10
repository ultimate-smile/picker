"""
聚宽（JoinQuant / JQData）数据层
================================
封装 jqdatasdk 的认证与常用数据接口，替代原先基于 AKShare（东方财富）的取数。

API 参考：https://www.joinquant.com/help/api/help#name:api

说明：
- jqdatasdk 是 **数据 SDK**，只负责取数，不能下单交易。
- 股票代码统一用聚宽格式：沪市 `XXXXXX.XSHG`，深市/创业板 `XXXXXX.XSHE`。
  本模块提供 to_jq_code / from_jq_code 在 6 位代码与聚宽代码之间转换。
"""

import os
from datetime import datetime, date as _date

import pandas as pd

# 复用主程序里的网络代理绕过与重试逻辑
from stock_picker import configure_network, fetch_with_retry

try:
    import jqdatasdk as jq
except ImportError:  # 友好提示
    jq = None


# ─────────────────────────────────────────
# 认证
# ─────────────────────────────────────────

_AUTHED = False


def _require_sdk():
    if jq is None:
        raise RuntimeError(
            "未安装 jqdatasdk，请先运行：pip install -U jqdatasdk"
        )


def _credentials():
    """优先用环境变量，其次用 config.py"""
    user = os.environ.get("JQ_USERNAME")
    pwd = os.environ.get("JQ_PASSWORD")
    if not user or not pwd:
        try:
            from config import JQ_USERNAME, JQ_PASSWORD
            user = user or JQ_USERNAME
            pwd = pwd or JQ_PASSWORD
        except ImportError:
            pass
    return user, pwd


def ensure_auth() -> None:
    """登录聚宽（幂等：已登录则直接返回）"""
    global _AUTHED
    _require_sdk()
    if _AUTHED and jq.is_auth():
        return

    configure_network()  # 按需绕过系统代理，避免连不上聚宽服务器

    user, pwd = _credentials()
    if not user or not pwd:
        raise RuntimeError(
            "缺少聚宽账号。请在 config.py 设置 JQ_USERNAME / JQ_PASSWORD，"
            "或设置同名环境变量。注册地址：https://www.joinquant.com/"
        )

    # 调整超时与重试（在 auth 之前设置）
    try:
        jq.set_params(request_timeout=120, request_attempt_count=5,
                      enable_auth_prompt=False)
    except Exception:
        pass

    fetch_with_retry("聚宽登录", jq.auth, user, pwd)
    _AUTHED = True

    try:
        quota = jq.get_query_count()
        print(f"✅ 聚宽登录成功，今日剩余额度：{quota}")
    except Exception:
        print("✅ 聚宽登录成功")


# ─────────────────────────────────────────
# 代码转换
# ─────────────────────────────────────────

def to_jq_code(symbol: str) -> str:
    """6 位代码 → 聚宽代码。已是聚宽格式则原样返回。"""
    s = str(symbol).strip().upper()
    if "." in s:
        return s
    s = s.zfill(6)
    if s.startswith("6"):
        return f"{s}.XSHG"          # 沪市主板/科创板
    if s[0] in ("0", "3"):
        return f"{s}.XSHE"          # 深市主板/创业板
    if s[0] in ("4", "8"):
        return f"{s}.BJSE"          # 北交所
    # 兜底
    return f"{s}.XSHG"


def from_jq_code(code: str) -> str:
    """聚宽代码 → 6 位代码"""
    return str(code).split(".")[0]


def get_board(code: str) -> str:
    """根据代码判断板块（接受 6 位或聚宽代码）"""
    s = from_jq_code(code).zfill(6)
    if s.startswith("60"):
        return "主板(沪)"
    if s.startswith("00"):
        return "主板(深)"
    if s.startswith("30"):
        return "创业板"
    if s.startswith("68"):
        return "科创板"
    if s[0] in ("4", "8"):
        return "北交所"
    return "其他"


# ─────────────────────────────────────────
# 股票池
# ─────────────────────────────────────────

def _to_date_str(d=None) -> str:
    if d is None:
        d = datetime.now().date()
    if isinstance(d, (datetime, _date)):
        return d.strftime("%Y-%m-%d")
    return str(d)


def get_universe(date=None, index_code=None) -> pd.DataFrame:
    """
    获取候选股票池（聚宽代码为索引），并附带名称、上市日期。

    :param index_code: 指定指数则只取其成分股；None 取全 A 股。
    :return: DataFrame[index=code], columns: display_name, name, start_date, end_date
    """
    ensure_auth()
    d = _to_date_str(date)

    if index_code:
        codes = fetch_with_retry("成分股", jq.get_index_stocks, index_code, date=d)
        info = fetch_with_retry("全证券列表", jq.get_all_securities, ["stock"], d)
        df = info.loc[info.index.intersection(codes)].copy()
    else:
        df = fetch_with_retry("全证券列表", jq.get_all_securities, ["stock"], d).copy()

    return df


def filter_universe(df: pd.DataFrame, *, exclude_st=True, exclude_kcb=False,
                    exclude_bj=True, exclude_new_days=60, ref_date=None) -> pd.DataFrame:
    """按板块/ST/次新等规则过滤股票池"""
    if df.empty:
        return df
    out = df.copy()

    if exclude_st:
        name_col = "display_name" if "display_name" in out.columns else "name"
        out = out[~out[name_col].astype(str).str.contains("ST|退", na=False)]

    boards = out.index.to_series().apply(get_board)
    if exclude_kcb:
        out = out[boards != "科创板"]
    if exclude_bj:
        out = out[boards != "北交所"]

    if exclude_new_days and "start_date" in out.columns:
        ref = pd.Timestamp(_to_date_str(ref_date))
        start = pd.to_datetime(out["start_date"], errors="coerce")
        out = out[(ref - start).dt.days >= exclude_new_days]

    return out


# ─────────────────────────────────────────
# 资金流向
# ─────────────────────────────────────────

# get_money_flow（标准接口）字段：单位“万元”，含主力(主力=超大单+大单)汇总字段。
MONEY_FLOW_FIELDS = [
    "date", "sec_code", "change_pct",
    "net_amount_main", "net_pct_main",       # 主力净额(万元) / 主力净占比(%)
    "net_amount_xl", "net_pct_xl",           # 超大单
    "net_amount_l", "net_pct_l",             # 大单
]

# get_money_flow_pro（兜底接口）仅支持以下字段（单位“元”，netflow = inflow - outflow）：
# 主力 = 超大单(xl) + 大单(l)，据此推导 net_amount_main / net_pct_main 以兼容下游逻辑。
MONEY_FLOW_PRO_FIELDS = [
    "inflow_xl", "inflow_l", "inflow_m", "inflow_s",
    "outflow_xl", "outflow_l", "outflow_m", "outflow_s",
    "netflow_xl", "netflow_l",
]


def _pro_main_amount(df: pd.DataFrame) -> pd.Series:
    """主力净额(万元) = (超大单净额 + 大单净额) / 1e4。pro 字段单位为元。"""
    xl = pd.to_numeric(df.get("netflow_xl"), errors="coerce").fillna(0.0)
    l = pd.to_numeric(df.get("netflow_l"), errors="coerce").fillna(0.0)
    return (xl + l) / 1e4


def _pro_to_main(df: pd.DataFrame) -> pd.DataFrame:
    """把 get_money_flow_pro 明细换算为 net_amount_main(万元)/net_pct_main(%)。
    净占比 = 主力净额 / 当日总成交额 * 100（成交额 = 各档买入额 + 卖出额）。
    """
    g = df.copy()
    flow_cols = ["inflow_xl", "inflow_l", "inflow_m", "inflow_s",
                 "outflow_xl", "outflow_l", "outflow_m", "outflow_s"]
    for col in flow_cols:
        g[col] = (pd.to_numeric(g[col], errors="coerce").fillna(0.0)
                  if col in g.columns else 0.0)
    net_main = _pro_main_amount(g) * 1e4                 # 元
    total = sum(g[c] for c in flow_cols)                 # 元，当日总成交额
    g["net_amount_main"] = net_main / 1e4               # 万元
    pct = pd.Series(0.0, index=g.index)
    nz = total != 0
    pct[nz] = net_main[nz] / total[nz] * 100
    g["net_pct_main"] = pct
    return g


def _set_money_flow_index(df: pd.DataFrame) -> pd.DataFrame:
    """get_money_flow_pro 多标的返回列名为 code，统一索引名为 sec_code。"""
    idx = "sec_code" if "sec_code" in df.columns else ("code" if "code" in df.columns else None)
    if idx:
        df = df.set_index(idx)
        df.index.name = "sec_code"
    return df


def get_money_flow_oneday(codes, date=None) -> pd.DataFrame:
    """
    获取一批股票某交易日的资金流向。
    优先用 get_money_flow；若该接口不可用（如账号未开通），自动降级到
    get_money_flow_pro 并推导主力净额/净占比。
    :return: DataFrame[index=聚宽代码], 含 net_amount_main / net_pct_main
    """
    ensure_auth()
    jq_codes = [to_jq_code(c) for c in codes]
    d = _to_date_str(date)
    try:
        df = fetch_with_retry(
            "资金流向", jq.get_money_flow,
            jq_codes, start_date=d, end_date=d, fields=MONEY_FLOW_FIELDS,
        )
        if df is not None and not df.empty:
            return df.set_index("sec_code")
        return pd.DataFrame()
    except Exception as e:
        print(f"  ⚠️  get_money_flow 不可用（{e}）；改用 get_money_flow_pro 兜底"
              f"（主力=超大单+大单）...")
        return _money_flow_oneday_via_pro(jq_codes, d)


def _money_flow_oneday_via_pro(jq_codes, d) -> pd.DataFrame:
    df = fetch_with_retry(
        "资金流向(pro)", jq.get_money_flow_pro,
        jq_codes, start_date=d, end_date=d,
        fields=MONEY_FLOW_PRO_FIELDS, data_type="money",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = _set_money_flow_index(_pro_to_main(df))
    return df[["net_amount_main", "net_pct_main"]]


def get_money_flow_history(codes, end_date=None, count=5) -> dict:
    """
    获取一批股票近 count 个交易日的主力净额序列，用于统计“连续净流入天数”。
    同样在 get_money_flow 不可用时自动降级到 get_money_flow_pro。
    :return: {聚宽代码: [按日期升序的 net_amount_main, ...]}
    """
    ensure_auth()
    jq_codes = [to_jq_code(c) for c in codes]
    d = _to_date_str(end_date)
    try:
        df = fetch_with_retry(
            "历史资金流向", jq.get_money_flow,
            jq_codes, end_date=d, count=count,
            fields=["date", "sec_code", "net_amount_main"],
        )
        if df is not None and not df.empty:
            return {code: g["net_amount_main"].tolist()
                    for code, g in df.sort_values("date").groupby("sec_code")}
        return {}
    except Exception as e:
        print(f"  ⚠️  历史 get_money_flow 不可用（{e}）；改用 get_money_flow_pro 兜底...")
        return _money_flow_history_via_pro(jq_codes, d, count)


def _money_flow_history_via_pro(jq_codes, d, count) -> dict:
    df = fetch_with_retry(
        "历史资金流向(pro)", jq.get_money_flow_pro,
        jq_codes, end_date=d, count=count,
        fields=["netflow_xl", "netflow_l"], data_type="money",
    )
    result = {}
    if df is None or df.empty:
        return result
    df = df.copy()
    df["net_amount_main"] = _pro_main_amount(df)
    code_col = "sec_code" if "sec_code" in df.columns else "code"
    time_col = "date" if "date" in df.columns else "time"
    for code, g in df.sort_values(time_col).groupby(code_col):
        result[code] = g["net_amount_main"].tolist()
    return result


def consecutive_inflow_days(flows: list) -> int:
    """从最近一天往前数，连续主力净流入（>0）的天数"""
    days = 0
    for v in reversed(flows or []):
        try:
            if float(v) > 0:
                days += 1
            else:
                break
        except (TypeError, ValueError):
            break
    return days


# ─────────────────────────────────────────
# 估值（市值、换手率）
# ─────────────────────────────────────────

VALUATION_FIELDS = ["code", "day", "market_cap", "circulating_market_cap",
                    "turnover_ratio", "pe_ratio", "pb_ratio"]


def get_valuation_oneday(codes, date=None) -> pd.DataFrame:
    """获取一批股票某日估值（market_cap 单位：亿元）"""
    ensure_auth()
    jq_codes = [to_jq_code(c) for c in codes]
    d = _to_date_str(date)
    df = fetch_with_retry(
        "估值数据", jq.get_valuation,
        jq_codes, end_date=d, count=1, fields=VALUATION_FIELDS,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    return df.set_index("code")


# ─────────────────────────────────────────
# 实时行情 / K线（盘中交易用）
# ─────────────────────────────────────────

def _price_from_get_price(jq_code, frequency) -> float:
    """用 get_price 取最近一根 bar 的收盘价"""
    df = jq.get_price(jq_code, end_date=datetime.now(), frequency=frequency,
                      count=1, fields=["close"], skip_paused=True, panel=False)
    if df is None or len(df) == 0:
        return 0.0
    val = df["close"].iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def get_last_price(code) -> float:
    """
    获取最新成交价，按权限/可用性自动降级（返回 0.0 表示失败）：
      1) get_current_tick —— 实时 tick（需实时行情权限，JQData 较高档位）
      2) get_price(frequency='1m') —— 最近一分钟收盘（盘中可用，覆盖更广）
      3) get_price(frequency='daily') —— 最近交易日收盘（盘后/兜底）
    这样在没有实时 tick 权限的账号上也能正常运行（如做模拟盘/盘后演示）。
    """
    ensure_auth()
    jq_code = to_jq_code(code)

    # 1) 实时 tick
    try:
        tick = jq.get_current_tick(jq_code)
        if isinstance(tick, pd.DataFrame) and not tick.empty:
            cur = tick.iloc[0]["current"]
            if pd.notna(cur) and float(cur) > 0:
                return float(cur)
        elif tick is not None and hasattr(tick, "current") and tick.current:
            return float(tick.current)
    except Exception:
        pass  # 无实时权限/非交易时段，降级到 K 线

    # 2) 最近一分钟收盘
    for freq in ("1m", "daily"):
        try:
            p = _price_from_get_price(jq_code, freq)
            if p > 0:
                return p
        except Exception:
            continue
    return 0.0


def get_intraday_bars(code, count=48, unit="5m") -> pd.DataFrame:
    """获取盘中分钟 K 线"""
    ensure_auth()
    jq_code = to_jq_code(code)
    return fetch_with_retry(
        "分钟K线", jq.get_bars, jq_code, count=count, unit=unit,
        fields=("date", "open", "high", "low", "close", "volume", "money"),
        include_now=True, df=True,
    )


def get_security_name(code) -> str:
    """获取股票名称"""
    ensure_auth()
    try:
        info = jq.get_security_info(to_jq_code(code))
        return getattr(info, "display_name", from_jq_code(code))
    except Exception:
        return from_jq_code(code)
