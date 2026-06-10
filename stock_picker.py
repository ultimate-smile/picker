"""
自动化选股系统 - 每日筛选3只强势股
数据来源：AKShare（东方财富）
分析引擎：Claude API
"""

import os
import socket
import time
from datetime import datetime

import akshare as ak
import anthropic
import pandas as pd

from config import (
    ANTHROPIC_API_KEY,
    MIN_MAIN_FORCE_RATIO,    # 主力净占比最低阈值（%）
    MIN_MARKET_CAP,          # 最小市值（亿元）
    MAX_MARKET_CAP,          # 最大市值（亿元）
    TOP_N_CANDIDATES,        # 候选股数量
    FINAL_PICKS,             # 最终推荐数量
    INCLUDE_BOARDS,          # 包含的板块
)

# 网络相关配置（旧版 config.py 可能没有这些项，提供默认值兜底）
try:
    from config import BYPASS_SYSTEM_PROXY
except ImportError:
    BYPASS_SYSTEM_PROXY = True
try:
    from config import DATA_FETCH_RETRIES
except ImportError:
    DATA_FETCH_RETRIES = 3
try:
    from config import DATA_FETCH_RETRY_DELAY
except ImportError:
    DATA_FETCH_RETRY_DELAY = 3.0


# ─────────────────────────────────────────
# 0. 网络层（代理与重试）
# ─────────────────────────────────────────

def configure_network() -> None:
    """
    根据配置处理系统代理。

    东方财富是国内服务，若用户开启了全局代理（VPN/Clash 等），网络库会自动走代理，
    反而连不上国内行情服务器，导致：
        ProxyError('Unable to connect to proxy', ...)
        ('Connection aborted.', RemoteDisconnected(...))

    仅清除环境变量在 macOS 上不够用：requests 还会通过 getproxies() 读取
    “系统偏好设置-网络-代理”里的本地代理（如 127.0.0.1），所以连接仍会被劫持。
    因此当 BYPASS_SYSTEM_PROXY 为 True 时，这里同时：
      1) 清空代理相关环境变量并设置 NO_PROXY；
      2) 在进程内禁用 requests / curl_cffi 的代理自动探测（含 macOS 系统代理），
         强制直连。显式传入的 proxies 不受影响。
    """
    if not BYPASS_SYSTEM_PROXY:
        return

    proxy_vars = [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ]
    removed = [v for v in proxy_vars if os.environ.pop(v, None) not in (None, "")]

    # 显式告诉底层库这些域名不要走代理
    no_proxy = "eastmoney.com,push2.eastmoney.com,datacenter-web.eastmoney.com"
    existing = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    merged = ",".join(filter(None, [existing, no_proxy]))
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged

    # 关键：禁用 requests 的代理自动探测（覆盖 macOS 系统代理 + 环境变量）。
    # get_environ_proxies() 内部调用模块级的 getproxies()，将其替换为返回空字典，
    # 即可让所有未显式指定 proxies 的请求直连。
    patched_libs = []
    try:
        import requests.utils as _rqu

        _rqu.getproxies = lambda: {}
        patched_libs.append("requests")
    except Exception:
        pass

    # curl_cffi（部分 AKShare 接口使用）默认读取环境变量；上面已清空环境变量即可。
    try:
        import curl_cffi  # noqa: F401

        patched_libs.append("curl_cffi(env)")
    except Exception:
        pass

    detail = []
    if removed:
        detail.append(f"清除环境变量：{', '.join(removed)}")
    if patched_libs:
        detail.append(f"禁用代理探测：{', '.join(patched_libs)}")
    print("🌐 已绕过系统代理直连行情服务器" + ("（" + "；".join(detail) + "）" if detail else ""))


def fetch_with_retry(label: str, func, *args, **kwargs):
    """
    带指数退避重试的数据请求包装器。

    :param label: 用于日志显示的中文名称
    :param func:  实际执行的取数函数（通常是 akshare 接口）
    :return:      func 的返回值；若全部重试失败则向上抛出最后一次异常
    """
    attempts = max(1, int(DATA_FETCH_RETRIES))
    delay = max(0.0, float(DATA_FETCH_RETRY_DELAY))
    last_err = None

    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < attempts:
                wait = delay * (2 ** (attempt - 1))
                print(f"  ⚠️  {label}失败（第{attempt}/{attempts}次）：{e}")
                print(f"      {wait:.0f}秒后重试...")
                time.sleep(wait)
            else:
                print(f"  ❌ {label}失败（已重试{attempts}次）：{e}")

    raise last_err


def _is_loopback(host: str) -> bool:
    """判断地址是否为本机回环地址（代理通常监听在这里）"""
    return host.startswith("127.") or host in ("::1", "localhost", "0.0.0.0")


def diagnose_connectivity(host: str = "push2.eastmoney.com", port: int = 443,
                          timeout: float = 6.0) -> dict:
    """
    诊断到行情服务器的真实连接路径。

    用原生 socket 直接连接，并读取 getpeername()。如果连上的对端是本机回环地址
    （127.0.0.1 等），说明存在“系统级/透明代理”（Clash TUN、增强模式、全局模式，
    或注入到进程的 socket 钩子），它在 HTTP 层之下劫持了所有连接——这种情况下任何
    Python 代理设置都绕不过去，必须在代理软件侧处理。
    """
    result = {"host": host, "port": port}
    try:
        resolved = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        result["resolved_ip"] = resolved[0][4][0]
    except Exception as e:
        result["resolved_ip"] = None
        result["dns_error"] = str(e)

    try:
        s = socket.create_connection((host, port), timeout=timeout)
        try:
            peer = s.getpeername()
            result["peer_ip"] = peer[0]
            result["hijacked"] = _is_loopback(peer[0])
            result["ok"] = True
        finally:
            s.close()
    except Exception as e:
        result["ok"] = False
        result["connect_error"] = str(e)
    return result


def print_proxy_help() -> None:
    """数据获取失败时，诊断网络并打印针对性的解决指引"""
    print("\n" + "─" * 50)
    print("🩺 正在诊断网络连接...")
    diag = diagnose_connectivity()

    resolved = diag.get("resolved_ip")
    peer = diag.get("peer_ip")
    if resolved:
        print(f"   行情服务器解析到：{resolved}")
    if peer:
        print(f"   实际连接到的对端：{peer}")

    hijacked = diag.get("hijacked")
    if hijacked or (peer and _is_loopback(peer)):
        print("\n❗ 检测到连接被本机代理劫持（对端是 127.0.0.1 等回环地址）。")
        print("   这是 **系统级/透明代理**（如 Clash 的 TUN/增强模式、Surge 增强模式、")
        print("   或“全局模式”）在网络层拦截了所有连接——它位于 Python 之下，")
        print("   程序内的任何代理设置都无法绕过。东方财富是国内服务器，被转发到")
        print("   （连不上海外的）代理后即出现 Connection aborted / RemoteDisconnected。")
        print("\n✅ 解决办法（任选其一，推荐前两个）：")
        print("   1. 关闭代理软件的 TUN / 增强模式 / 全局模式（改回“规则/Rule 模式”），")
        print("      或运行本程序时临时退出代理软件。")
        print("   2. 在代理软件里给以下域名添加“直连(DIRECT)”规则：")
        print("        *.eastmoney.com")
        print("        push2.eastmoney.com")
        print("        quote.eastmoney.com")
        print("        datacenter-web.eastmoney.com")
        print("   3. 验证：在终端执行")
        print("        curl -v https://push2.eastmoney.com/api/qt/clist/get")
        print("      若 curl 也连到 127.0.0.1，则确认是系统级代理，需按上面处理。")
    else:
        err = diag.get("connect_error") or diag.get("dns_error")
        if diag.get("ok"):
            print("\n直连测试本身成功，可能是行情接口偶发抖动或非交易时段返回异常，")
            print("请稍后重试；若持续失败，可适当调大 config.py 中的 DATA_FETCH_RETRIES。")
        else:
            print(f"\n直连失败：{err}")
            print("可能是本机网络不通、DNS 异常或防火墙拦截。请检查网络连接后重试。")
    print("─" * 50)


# ─────────────────────────────────────────
# 1. 数据采集层
# ─────────────────────────────────────────

def fetch_main_force_flow() -> pd.DataFrame:
    """获取今日全市场主力资金流向排行"""
    print("📡 正在获取主力资金流向数据...")
    try:
        return fetch_with_retry(
            "资金流向获取",
            ak.stock_individual_fund_flow_rank,
            indicator="今日",
        )
    except Exception:
        return pd.DataFrame()


def fetch_sector_flow() -> pd.DataFrame:
    """获取今日板块资金流向，找出最热板块"""
    print("📡 正在获取板块资金流向...")
    try:
        # 注意：AKShare 的 sector_type 合法取值为
        # "行业资金流" / "概念资金流" / "地域资金流"（不是 "行业资金流向"）
        return fetch_with_retry(
            "板块数据获取",
            ak.stock_sector_fund_flow_rank,
            indicator="今日",
            sector_type="行业资金流",
        )
    except Exception:
        return pd.DataFrame()


def fetch_stock_info(symbol: str) -> dict:
    """获取单只股票基本信息（市值等）"""
    try:
        df = fetch_with_retry(
            f"{symbol} 基本信息获取",
            ak.stock_individual_info_em,
            symbol=symbol,
        )
        return dict(zip(df["item"], df["value"]))
    except Exception:
        return {}


def fetch_hist_flow(symbol: str, days: int = 5) -> pd.DataFrame:
    """获取个股近N日历史资金流向"""
    try:
        df = fetch_with_retry(
            f"{symbol} 历史资金流向获取",
            ak.stock_individual_fund_flow,
            stock=symbol,
            market="sh" if symbol.startswith("6") else "sz",
        )
        return df.tail(days)
    except Exception:
        return pd.DataFrame()


# ─────────────────────────────────────────
# 2. 初步过滤层
# ─────────────────────────────────────────

def parse_ratio(val) -> float:
    """解析百分比字符串为浮点数"""
    try:
        return float(str(val).replace("%", "").replace(",", ""))
    except:
        return 0.0


def parse_amount(val) -> float:
    """解析资金金额（万元）为浮点数"""
    try:
        s = str(val).replace(",", "").replace("万", "")
        return float(s)
    except:
        return 0.0


def get_board(symbol: str) -> str:
    """根据股票代码判断所属板块"""
    if symbol.startswith("60"):
        return "主板(沪)"
    elif symbol.startswith("00"):
        return "主板(深)"
    elif symbol.startswith("30"):
        return "创业板"
    elif symbol.startswith("68"):
        return "科创板"
    elif symbol.startswith("8") or symbol.startswith("4"):
        return "北交所"
    return "其他"


def filter_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    初步过滤规则：
    1. 主力净占比 > MIN_MAIN_FORCE_RATIO
    2. 非ST股
    3. 板块在 INCLUDE_BOARDS 范围内
    4. 取前 TOP_N_CANDIDATES 只
    """
    if df.empty:
        return df

    print(f"\n🔍 初步过滤（原始数据 {len(df)} 条）...")

    # 统一列名（AKShare不同版本列名可能略有差异）
    col_map = {}
    for col in df.columns:
        if "主力" in col and "净占比" in col:
            col_map["主力净占比"] = col
        elif "主力" in col and "净流" in col and "额" in col:
            col_map["主力净流入额"] = col
        elif "名称" in col or col == "股票名称":
            col_map["名称"] = col
        elif "代码" in col or col == "股票代码":
            col_map["代码"] = col

    df = df.rename(columns={v: k for k, v in col_map.items()})

    # 确保关键列存在
    required = ["代码", "名称", "主力净占比", "主力净流入额"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"  ⚠️  缺少列: {missing}，请检查AKShare版本")
        print(f"  当前列名: {list(df.columns)}")
        return pd.DataFrame()

    # 转换数值
    df["主力净占比_num"] = df["主力净占比"].apply(parse_ratio)
    df["主力净流入额_num"] = df["主力净流入额"].apply(parse_amount)

    # 过滤条件
    mask = (
        (df["主力净占比_num"] >= MIN_MAIN_FORCE_RATIO) &
        (~df["名称"].str.contains("ST|退|B股", na=False))
    )
    df = df[mask].copy()

    # 板块过滤
    df["板块"] = df["代码"].apply(get_board)
    if INCLUDE_BOARDS:
        df = df[df["板块"].isin(INCLUDE_BOARDS)]

    # 按主力净占比降序排列
    df = df.sort_values("主力净占比_num", ascending=False)

    # 取前N只
    df = df.head(TOP_N_CANDIDATES).reset_index(drop=True)

    print(f"  ✅ 过滤后剩余 {len(df)} 只候选股")
    return df


# ─────────────────────────────────────────
# 3. 深度数据丰富层
# ─────────────────────────────────────────

def enrich_candidates(df: pd.DataFrame) -> list[dict]:
    """为每只候选股补充近5日资金流向数据"""
    print(f"\n📊 补充历史资金数据（共 {len(df)} 只）...")
    enriched = []

    for i, row in df.iterrows():
        symbol = str(row["代码"]).zfill(6)
        name = row["名称"]
        board = row["板块"]
        today_ratio = row["主力净占比_num"]
        today_amount = row["主力净流入额_num"]

        print(f"  [{i+1}/{len(df)}] {symbol} {name} 主力占比{today_ratio:.2f}%", end="")

        # 获取近5日历史流向
        hist = fetch_hist_flow(symbol)
        hist_summary = ""
        consecutive_days = 0

        if not hist.empty:
            # 尝试找主力净流入列
            flow_col = None
            for col in hist.columns:
                if "主力" in col and "净流入" in col:
                    flow_col = col
                    break

            if flow_col:
                recent_flows = hist[flow_col].tolist()
                hist_summary = "、".join([
                    f"{'+' if float(str(v).replace(',',''))>0 else ''}{str(v).replace(',','')}"
                    for v in recent_flows[-5:]
                ])
                # 统计连续净流入天数
                for v in reversed(recent_flows):
                    try:
                        if float(str(v).replace(",", "")) > 0:
                            consecutive_days += 1
                        else:
                            break
                    except:
                        break

        print(f" | 连续净流入{consecutive_days}日")
        time.sleep(0.3)  # 避免频繁请求被限流

        enriched.append({
            "代码": symbol,
            "名称": name,
            "板块": board,
            "今日主力净占比": today_ratio,
            "今日主力净流入(万)": today_amount,
            "连续净流入天数": consecutive_days,
            "近5日主力流向": hist_summary,
        })

    return enriched


# ─────────────────────────────────────────
# 4. Claude AI 分析层
# ─────────────────────────────────────────

def analyze_with_claude(candidates: list[dict], hot_sectors: str) -> str:
    """调用Claude API对候选股进行深度分析并输出推荐"""
    print(f"\n🤖 正在调用 Claude 进行深度分析...")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 构建候选股数据文本
    stocks_text = ""
    for i, s in enumerate(candidates, 1):
        stocks_text += f"""
【候选股{i}】{s['代码']} {s['名称']}（{s['板块']}）
  - 今日主力净占比：{s['今日主力净占比']:.2f}%
  - 今日主力净流入：{s['今日主力净流入(万)']:.0f}万元
  - 连续主力净流入天数：{s['连续净流入天数']}天
  - 近5日主力流向：{s['近5日主力流向'] or '数据获取中'}
"""

    prompt = f"""你是一位专业的A股量化选股分析师。今天是{datetime.now().strftime('%Y年%m月%d日')}。

请根据以下数据，从候选股中选出今日最值得关注的3只股票，给出具体操作建议。

【今日最热板块TOP3】
{hot_sectors}

【候选股数据（已过滤主力净占比>5%）】
{stocks_text}

请按以下框架分析并输出：

评分维度（总分100分）：
- 主力净占比量级（30分）：越高越好，20%以上满分
- 连续净流入天数（25分）：连续3天以上加分，5天以上满分
- 净流入绝对金额（20分）：金额越大机构参与度越高
- 板块景气度（15分）：是否在今日热门板块中
- 资金持续性（10分）：近5日是否多次净流入

输出格式（严格按此格式）：

════════════════════════════════════
📅 {datetime.now().strftime('%Y-%m-%d')} 每日精选3股
════════════════════════════════════

🥇 第一推荐：[代码] [名称]（[板块]）
综合评分：XX/100
今日信号：[一句话描述今日资金特征]
核心逻辑：[2-3句话说明为何推荐]
建议买入价：XX元附近（基于今日收盘价±幅度）
止损位：XX元（跌破此价位止损）
目标价1：XX元 | 目标价2：XX元
风险提示：[主要风险点]

🥈 第二推荐：[代码] [名称]（[板块]）
[同上格式]

🥉 第三推荐：[代码] [名称]（[板块]）
[同上格式]

════════════════════════════════════
⚠️  综合风险提示
[今日整体市场判断+操作纪律提醒]
════════════════════════════════════

注意：
1. 只输出上述格式，不要额外解释
2. 买入价、止损位、目标价必须给出具体数字
3. 如果候选股质量普遍偏低，要直接说明"今日信号偏弱，建议观望"
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text


# ─────────────────────────────────────────
# 5. 主流程
# ─────────────────────────────────────────

def get_hot_sectors() -> str:
    """获取今日最热板块"""
    df = fetch_sector_flow()
    if df.empty:
        return "数据获取失败"
    try:
        # 找净流入最多的前3个板块
        for col in df.columns:
            if "净流入" in col or "净额" in col:
                df["_val"] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")
                df = df.sort_values("_val", ascending=False)
                break
        name_col = [c for c in df.columns if "名称" in c or "板块" in c][0]
        top3 = df.head(3)[name_col].tolist()
        return "、".join(top3)
    except:
        return "数据解析中"


def run():
    """主入口：每日选股全流程"""
    print("=" * 50)
    print(f"  🚀 自动化选股系统启动")
    print(f"  ⏰ 运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # Step 0: 配置网络（按需绕过系统代理，避免国内行情服务器连不上）
    configure_network()

    # Step 1: 获取热门板块
    hot_sectors = get_hot_sectors()
    print(f"\n🔥 今日最热板块：{hot_sectors}")

    # Step 2: 获取主力资金流向
    raw_df = fetch_main_force_flow()
    if raw_df.empty:
        print("\n❌ 主力资金流向数据获取失败。")
        print_proxy_help()
        return

    # Step 3: 初步过滤
    candidates_df = filter_candidates(raw_df)
    if candidates_df.empty:
        print("❌ 过滤后无候选股，请降低筛选阈值")
        return

    # Step 4: 丰富历史数据
    candidates = enrich_candidates(candidates_df)

    # Step 5: Claude 深度分析
    result = analyze_with_claude(candidates, hot_sectors)

    # Step 6: 输出结果
    print("\n" + "=" * 50)
    print(result)
    print("=" * 50)
    print("\n⚠️  以上内容仅供参考，不构成投资建议。股市有风险，请严格控制仓位。")


if __name__ == "__main__":
    run()
