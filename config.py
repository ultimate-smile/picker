"""
配置文件 - 根据自己需求修改
"""

# ─────────────────────────────────────────
# Claude API Key
# 获取地址：https://console.anthropic.com/
# ─────────────────────────────────────────
ANTHROPIC_API_KEY = "crsr_1fca8f96e8d851b516a313f5dfb0753c07837d3437d86d3f103aa55164a04190"   # ← 替换成你的API Key
# Claude 模型名（--analyze 生成操作报告时使用）。若 Key 无效/网络不可达，
# 程序会自动降级为本地规则化操作报告（买入区间/止损/目标价/仓位）。
CLAUDE_MODEL = "claude-sonnet-4-6"


# ─────────────────────────────────────────
# 选股过滤参数
# ─────────────────────────────────────────

# 主力净占比最低阈值（%），低于此值不纳入候选
# 建议范围：3–8，数值越高候选股越少但信号越强
MIN_MAIN_FORCE_RATIO = 5.0

# 市值范围（亿元）：过滤太小（流动性差）和太大（弹性不足）的股票
MIN_MARKET_CAP = 50     # 最小市值
MAX_MARKET_CAP = 1000   # 最大市值（设为 None 则不限制）

# 候选股数量：从主力净占比排行中取前N只进行深度分析
# 数量越多Claude分析越全面，但耗时更长
TOP_N_CANDIDATES = 20

# 最终推荐数量
FINAL_PICKS = 3


# ─────────────────────────────────────────
# 板块范围（留空列表则覆盖全部板块）
# ─────────────────────────────────────────
# 可选值："主板(沪)"、"主板(深)"、"创业板"、"科创板"、"北交所"
# 示例：只看科创板+创业板 → ["科创板", "创业板"]
# 全部板块 → []
INCLUDE_BOARDS = []   # 默认全部板块


# ─────────────────────────────────────────
# 网络设置
# ─────────────────────────────────────────
# 东方财富（AKShare 数据源）是国内服务。如果你的电脑开启了科学上网/全局代理
# （VPN、Clash、Shadowsocks 等），所有请求都会被强行转发到海外代理，
# 反而连不上国内的行情服务器，从而出现：
#   ProxyError('Unable to connect to proxy', ...)
# 把下面这一项设为 True，程序在请求行情数据时会自动绕过系统代理（直连），
# 这是大多数代理报错的根本解决办法。如果你必须通过代理才能上网，可设为 False。
BYPASS_SYSTEM_PROXY = True

# 数据请求失败时的自动重试设置（应对偶发的网络抖动 / 限流）
DATA_FETCH_RETRIES = 3        # 最大重试次数
DATA_FETCH_RETRY_DELAY = 3.0  # 首次重试前的等待秒数（之后按指数退避：3s、6s、12s…）


# ─────────────────────────────────────────
# 运行时间设置（用于定时任务参考）
# ─────────────────────────────────────────
# 建议运行时间：
#   - 收盘后（15:30–16:00）：获取当日完整数据，分析明日机会
#   - 早盘前（09:00–09:15）：运行前一交易日收盘数据，辅助今日决策
RUN_AFTER_CLOSE = True   # True=收盘后运行，False=开盘前运行


# ═════════════════════════════════════════════════════════════════
# 聚宽（JoinQuant / JQData）配置  —— 见 jq_main.py / jq_data.py
# ═════════════════════════════════════════════════════════════════
# 在 https://www.joinquant.com/ 注册后获得账号（手机号）与密码。
# jqdatasdk 免费额度有限（每日若干万~千万条），请合理使用。
# 也可用环境变量 JQ_USERNAME / JQ_PASSWORD 覆盖下面的值（更安全）。
JQ_USERNAME = ""   # 聚宽账号（注册手机号）
JQ_PASSWORD = ""   # 聚宽密码

# ── 选股参数（基于聚宽数据）──
# 选股票池：None=全 A 股；或填指数代码只在成分股里选，如沪深300 "000300.XSHG"
JQ_UNIVERSE_INDEX = None
JQ_MIN_NET_PCT_MAIN = 5.0    # 主力资金净占比下限（%），低于此值不入选
# 主力净占比上限（%）：过滤异常爆量（多为涨停/拉升后的极端值）；None=不限
JQ_MAX_NET_PCT_MAIN = 25.0
JQ_MIN_MARKET_CAP = 50.0     # 最小总市值（亿元）
JQ_MAX_MARKET_CAP = 1000.0   # 最大总市值（亿元）；None=不限
JQ_MIN_TURNOVER = 2.0        # 最小换手率（%），过滤流动性差的票；None=不限
JQ_MAX_TURNOVER = 30.0       # 最大换手率（%），过滤过热炒作；None=不限

# ── 可操作性过滤（避免选出买不进/追高的涨停板）──
# 剔除涨停及“接近涨停”的股票（封板买不进、追高风险大）
JQ_EXCLUDE_NEAR_LIMIT = True
# 收盘价距涨停价 ≤ 该比例视为“接近涨停”，剔除（0.015 = 1.5%）
JQ_NEAR_LIMIT_BUFFER = 0.015
# 当日涨跌幅可接受区间（%）：默认 -3% ~ +7%，既不追高也不抄弱势；None=不限
JQ_MIN_CHANGE_PCT = -3.0
JQ_MAX_CHANGE_PCT = 7.0

# ── 综合评分权重（候选股按综合分排序，而非单看主力净占比）──
# inflow=主力净占比, consec=连续净流入, change=涨跌幅健康度, turnover=换手健康度
JQ_SCORE_WEIGHTS = {"inflow": 0.4, "consec": 0.2, "change": 0.2, "turnover": 0.2}

# ── 自定义股票池 ──
# 填 6 位或聚宽代码列表则只在该集合内选股（如自选股/行业池）；留空 [] 用全 A 股/指数。
# 命令行 --codes 600000,000001 或 --watchlist file.txt 会覆盖此项。
JQ_CUSTOM_UNIVERSE = []
# 资金流向接口选择：
#   "pro"   = 只用 get_money_flow_pro（默认；多数账号未开通 get_money_flow 数据权限，
#             该接口按单量分档返回，主力净额=超大单+大单，净占比=主力净额/当日总成交额，为推导值）
#   "basic" = 只用 get_money_flow（账号已开通该数据时最精确）
#   "auto"  = 先用 get_money_flow，不可用时自动降级到 get_money_flow_pro 并推导
JQ_MONEY_FLOW_API = "pro"
JQ_HIST_LOOKBACK_DAYS = 5    # 统计“连续主力净流入天数”回看的交易日数
JQ_TOP_N = 20                # 参与综合评分的候选池大小（评分后只取 JQ_FINAL_PICKS 只）
JQ_FINAL_PICKS = 3           # 最终选出的股票数量（select_candidates 默认返回的只数）
JQ_EXCLUDE_ST = True         # 排除 ST/*ST
JQ_EXCLUDE_KCB = False       # 排除科创板（68 开头）
JQ_EXCLUDE_BJ = True         # 排除北交所（4/8 开头）
JQ_EXCLUDE_NEW_DAYS = 60     # 排除上市不足 N 个自然日的次新股

# ── 盘中交易参数 ──
# 交易模式：
#   "paper" = 本地模拟盘（用聚宽实时行情撮合，安全，强烈推荐先用它）
#   "live"  = 实盘（jqdatasdk 不能下单，需自行接入券商 API，见 jq_trader.py）
TRADE_MODE = "paper"
TRADE_CAPITAL = 100000.0     # 模拟盘初始资金（元）
MAX_POSITIONS = 3            # 最大同时持仓只数
PER_POSITION_PCT = 0.3       # 单只目标仓位占总资金比例（0.3=30%）
TAKE_PROFIT_PCT = 0.08       # 止盈：浮盈达到 +8% 卖出
STOP_LOSS_PCT = 0.04         # 止损：浮亏达到 -4% 卖出
TRAIL_STOP_PCT = 0.03        # 移动止盈：从最高点回撤 3% 卖出（锁定利润）
INTRADAY_POLL_SECONDS = 30   # 盘中轮询间隔（秒）
FORCE_CLOSE_BEFORE_END = True  # 收盘前是否清仓（做 T/日内策略时建议 True）
