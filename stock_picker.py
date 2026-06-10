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
                          timeout: float = 8.0) -> dict:
    """
    分层诊断到行情服务器的连通性：DNS → TCP → 真实 HTTPS API。

    仅测 TCP 不够：TUN 模式代理 / TLS 审查防火墙会让 TCP 握手成功（且对端显示真实
    服务器 IP），但在 TLS / HTTP 阶段把连接重置或关闭，表现为
    RemoteDisconnected / Connection reset。因此这里必须真正发一次 HTTPS 请求。

    返回字典含分层结果：
      dns_ok / resolved_ip
      tcp_ok / peer_ip / hijacked(对端是否回环地址=本地代理)
      http_ok / http_status / http_error / http_body_head
    """
    result = {"host": host, "port": port}

    # ── 1) DNS 解析 ──
    try:
        resolved = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        result["resolved_ip"] = resolved[0][4][0]
        result["dns_ok"] = True
    except Exception as e:
        result["dns_ok"] = False
        result["dns_error"] = str(e)
        return result

    # ── 2) 原始 TCP 连接（检测是否被本地代理劫持到 127.0.0.1）──
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        try:
            result["peer_ip"] = s.getpeername()[0]
            result["hijacked"] = _is_loopback(result["peer_ip"])
            result["tcp_ok"] = True
        finally:
            s.close()
    except Exception as e:
        result["tcp_ok"] = False
        result["tcp_error"] = str(e)
        return result

    # ── 3) 真实 HTTPS API 请求（这才是 akshare 真正会失败的那一层）──
    try:
        import requests
        url = f"https://{host}/api/qt/clist/get"
        params = {
            "fid": "f62", "po": "1", "pz": "1", "pn": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fs": "m:0+t:6+f:!2", "fields": "f12,f14,f2,f3,f62",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        result["http_status"] = r.status_code
        try:
            j = r.json()
            result["http_ok"] = r.status_code == 200 and isinstance(j, dict) and "data" in j
        except Exception:
            result["http_ok"] = False
            result["http_body_head"] = r.text[:120]
    except Exception as e:
        result["http_ok"] = False
        result["http_error"] = f"{type(e).__name__}: {e}"

    return result


def print_proxy_help() -> None:
    """数据获取失败时，分层诊断网络并打印针对性的解决指引"""
    print("\n" + "─" * 50)
    print("🩺 正在诊断网络连接（DNS → TCP → HTTPS）...")
    d = diagnose_connectivity()

    if d.get("resolved_ip"):
        print(f"   ① DNS 解析：{d['resolved_ip']}  ✅")
    if d.get("peer_ip"):
        tag = "（本地代理！）" if d.get("hijacked") else ""
        print(f"   ② TCP 连接对端：{d['peer_ip']} {tag} {'✅' if d.get('tcp_ok') else '❌'}")
    if "http_status" in d or "http_error" in d or "http_ok" in d:
        if d.get("http_ok"):
            print(f"   ③ HTTPS 接口：HTTP {d.get('http_status')}  ✅")
        else:
            extra = d.get("http_error") or f"HTTP {d.get('http_status')} {d.get('http_body_head','')}"
            print(f"   ③ HTTPS 接口：失败  ❌  {extra}")

    print()
    # ── 分支判断 ──
    if not d.get("dns_ok"):
        print(f"❗ DNS 解析失败：{d.get('dns_error')}")
        print("   本机无法解析域名，多为网络断开或 DNS 配置异常。请检查网络/DNS 后重试。")

    elif d.get("hijacked"):
        print("❗ 连接被本机代理劫持（对端是 127.0.0.1 等回环地址）。")
        print("   这是系统级/透明代理在 HTTP 层之下拦截了连接，程序内无法绕过。")
        _print_proxy_actions()

    elif not d.get("tcp_ok"):
        print(f"❗ TCP 无法连接行情服务器：{d.get('tcp_error')}")
        print("   多为本机网络不通、防火墙拦截或服务器临时不可达。请检查网络后重试。")

    elif d.get("http_ok"):
        print("✅ 诊断显示行情接口此刻可正常访问（HTTPS 返回正常）。")
        print("   刚才的失败很可能是偶发抖动 / 限流 / 非交易时段返回异常，请直接重跑。")
        print("   若稳定复现，可能是请求过于频繁被限流，稍等片刻再运行即可。")

    elif "http_status" in d:
        # 拿到了 HTTP 响应但不是正常数据（如 502/限流/异常返回）→ 服务端问题，非代理
        print(f"❗ 服务器返回了异常响应（HTTP {d.get('http_status')}），但连接本身是通的。")
        print("   这属于行情服务器端的临时故障 / 限流（如 502、网关错误），与代理无关。")
        print("   稍等片刻直接重跑即可；若持续，多为非交易时段或对方接口维护。")

    else:
        # 连接级异常（无 HTTP 状态码）。区分“被重置/断开”与一般网络错误。
        err = (d.get("http_error") or "").lower()
        conn_broken = any(k in err for k in
                          ("remotedisconnected", "connection reset", "connection aborted",
                           "aborted", "reset", "eof", "broken pipe"))
        if conn_broken:
            print("❗ TCP 能连通，但 HTTPS 请求被中断（Connection reset / RemoteDisconnected）。")
            print("   这通常不是 AKShare 或接口本身的问题，而是连接在 **TLS/加密层** 被干扰：")
            print("   • 代理处于 **TUN/增强模式**：TCP 握手能成（对端显示真实 IP），但流量被")
            print("     路由到（连不上国内站点的）代理核心，导致 TLS 阶段连接被关闭；")
            print("   • 或有 **TLS 审查的防火墙 / 杀毒软件 HTTPS 扫描** 重置了加密连接。")
            _print_proxy_actions()
            print("   4. 若装有杀毒/安全软件的 HTTPS/SSL 扫描，临时关闭它再试。")
            print("   5. 换一个网络环境（如手机热点）验证是否为当前网络/代理所致。")
        else:
            print(f"❗ HTTPS 请求失败：{d.get('http_error')}")
            print("   连接在加密/传输阶段出错。可能是代理(TUN/增强模式)、TLS 审查、")
            print("   或网络不稳定。可先按下列方式排查：")
            _print_proxy_actions()
            print("   4. 换一个网络环境（如手机热点）验证是否为当前网络/代理所致。")

    print("─" * 50)


def _print_proxy_actions() -> None:
    """打印代理类问题的通用处理步骤"""
    print("\n✅ 解决办法（任选其一，推荐前两个）：")
    print("   1. 关闭代理软件的 TUN / 增强模式 / 全局模式（改回“规则/Rule 模式”），")
    print("      或运行本程序时临时退出代理软件。")
    print("   2. 在代理软件里给以下域名添加“直连(DIRECT)”规则：")
    print("        *.eastmoney.com  push2.eastmoney.com")
    print("        quote.eastmoney.com  datacenter-web.eastmoney.com")
    print("   3. 终端验证：curl -v https://push2.eastmoney.com/api/qt/clist/get")


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


def selftest() -> bool:
    """
    连通性自检：在跑完整选股流程前，逐项验证 AKShare 各接口是否可用。

    运行：python3 stock_picker.py --selftest
    返回 True 表示关键接口（主力资金流向）可用。
    """
    print("=" * 50)
    print("  🧪 AKShare 连通性自检")
    print("=" * 50)

    configure_network()

    # 先做网络分层诊断
    print("\n[0] 网络分层诊断（DNS → TCP → HTTPS）")
    d = diagnose_connectivity()
    print(f"    DNS : {'✅ ' + str(d.get('resolved_ip')) if d.get('dns_ok') else '❌ ' + str(d.get('dns_error'))}")
    if "tcp_ok" in d:
        tag = "（本地代理!）" if d.get("hijacked") else ""
        print(f"    TCP : {'✅ ' + str(d.get('peer_ip')) + tag if d.get('tcp_ok') else '❌ ' + str(d.get('tcp_error'))}")
    if "http_ok" in d:
        print(f"    HTTPS: {'✅ HTTP ' + str(d.get('http_status')) if d.get('http_ok') else '❌ ' + str(d.get('http_error') or d.get('http_status'))}")

    # 逐个接口探测（每个接口都很轻量）
    checks = [
        ("主力资金流向排行 stock_individual_fund_flow_rank",
         lambda: ak.stock_individual_fund_flow_rank(indicator="今日"), True),
        ("板块资金流向 stock_sector_fund_flow_rank",
         lambda: ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流"), False),
        ("个股资金流向 stock_individual_fund_flow(sh:600519)",
         lambda: ak.stock_individual_fund_flow(stock="600519", market="sh"), False),
        ("个股信息 stock_individual_info_em(600519)",
         lambda: ak.stock_individual_info_em(symbol="600519"), False),
    ]

    print("\n[1] AKShare 接口探测")
    critical_ok = True
    for name, fn, critical in checks:
        try:
            df = fn()
            n = len(df) if hasattr(df, "__len__") else "?"
            print(f"    ✅ {name}  → {n} 行")
        except Exception as e:
            print(f"    ❌ {name}  → {type(e).__name__}: {e}")
            if critical:
                critical_ok = False

    print("\n" + "=" * 50)
    if critical_ok:
        print("  ✅ 自检通过：关键行情接口可用，可正常运行 `python3 stock_picker.py`")
    else:
        print("  ❌ 自检失败：关键行情接口不可用。")
        print_proxy_help()
    print("=" * 50)
    return critical_ok


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
    import sys

    if any(a in ("--selftest", "--diagnose", "--test", "-t") for a in sys.argv[1:]):
        ok = selftest()
        sys.exit(0 if ok else 1)
    else:
        run()
