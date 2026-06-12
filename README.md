# 📈 自动化系统

每日自动从市场筛选3只主力重仓强势股，结合 Claude AI 给出具体买入价、止损位和目标价。

---

## 🗂 文件结构

```
stock_picker/
├── stock_picker.py            # AKShare 版主程序（东方财富数据源）
├── config.py                  # 配置文件（你需要修改这个）
│
├── jq_main.py                 # 🆕 聚宽版主入口（选股 + 盘中交易 + 多维度评估）
├── jq_data.py                 # 🆕 聚宽数据层（jqdatasdk 封装）
├── jq_selector.py             # 🆕 聚宽选股逻辑（资金面+可操作性）
├── jq_factors.py              # 🆕 多维度因子评分（技术/基本面/筹码/大盘板块/消息催化）
├── jq_deep.py                 # 🆕 多维度综合选股 + 买卖价位 + 持有建议（--deep）
├── jq_trader.py               # 🆕 盘中交易引擎（模拟盘/风控/券商抽象）
├── jq_strategy_joinquant.py   # 🆕 聚宽“策略平台”实盘/回测策略模板
├── test_stock_picker.py       # AKShare 版单元测试
├── test_jq.py                 # 🆕 聚宽版单元测试
├── test_jq_factors.py         # 🆕 多维度因子评分单元测试（离线）
├── test_jq_deep.py            # 🆕 综合选股编排层单元测试（离线）
│
├── setup.sh / run.sh / setup_cron.sh
└── README.md
```

---

## 🆕 聚宽（JoinQuant / jqdatasdk）版本

用聚宽 JQData 重新实现了选股与盘中交易逻辑，作为 AKShare 版的替代/增强。
数据更规范、字段统一，且自带实时行情与分钟 K 线，适合做日内交易。

### 安装与配置

```bash
pip install -U jqdatasdk
```

在 [joinquant.com](https://www.joinquant.com/) 注册账号后，编辑 `config.py`：

```python
JQ_USERNAME = "你的聚宽账号(手机号)"
JQ_PASSWORD = "你的聚宽密码"
```
（也可用环境变量 `JQ_USERNAME` / `JQ_PASSWORD`，更安全。）

### 使用

```bash
python3 jq_main.py              # ⭐默认=多维度综合评估，直接给出买卖价位 + 持有建议
python3 jq_main.py --selftest   # 登录聚宽 + 检查额度/连通性/各数据接口权限
python3 jq_main.py --select     # 候选表 + 对这些候选给出买卖价位/持有建议（不做全市场五维重排）
python3 jq_main.py --deep       # 同默认：多维度综合评估选股 + 买卖价位 + 持有建议
python3 jq_main.py --analyze    # 选股 + Claude 深度分析
python3 jq_main.py --paper      # 选股 + 本地模拟盘日内交易（推荐，安全）
python3 jq_main.py --paper --demo  # 用最新价做一次性演示（非交易时段也能跑）

# 自定义股票池（可与上面任一动作组合）
python3 jq_main.py --select --codes 600000,000001,300750   # 候选表 + 买卖价位/持有建议
python3 jq_main.py --select --watchlist my_list.txt   # 文件内换行/逗号/空格分隔，# 为注释
```

### 选股逻辑（`jq_selector.py`）—— 多因子 + 可操作性优先

> 早期版本只按“主力净占比”降序，排在最前的几乎都是**当日涨停/拉升**的票——封死涨停
> 根本买不进、追高次日易回落，**不适合实际交易**。新策略显式剔除涨停并改用多因子综合分。

1. **股票池**：全 A 股 / 指定指数成分股 / **自定义集合**（`--codes` / `--watchlist` / `JQ_CUSTOM_UNIVERSE`）→ 剔除 ST / 次新 / 指定板块；
2. **估值初筛**（`get_valuation`）：市值区间 + 换手率**上下限**（过滤流动性差与过热）；
3. **主力资金流向**：主力净占比落在 `[下限, 上限]` 区间（上限剔除涨停式异常爆量）；
4. **可操作性过滤**（`get_price`）：剔除 **涨停 / 接近涨停（封板买不进）/ 跌停 / 停牌**，并限制当日涨跌幅在合理区间（默认 -3%~+7%，不追高、不抄弱势）；
5. **综合评分**：`主力净占比 + 连续净流入 + 涨跌幅健康度 + 换手健康度` 加权（`JQ_SCORE_WEIGHTS`），按综合分排序取前 N；
6. 输出候选股（可建仓或交给 Claude 分析）。

> 相关参数全部在 `config.py` 可调：`JQ_MIN/MAX_NET_PCT_MAIN`、`JQ_MIN/MAX_TURNOVER`、
> `JQ_EXCLUDE_NEAR_LIMIT`、`JQ_NEAR_LIMIT_BUFFER`、`JQ_MIN/MAX_CHANGE_PCT`、`JQ_SCORE_WEIGHTS`、`JQ_CUSTOM_UNIVERSE`。
> 默认返回 `JQ_FINAL_PICKS`（如 3 只）；`JQ_TOP_N` 为参与评分的候选池大小。

### 🆕 多维度综合评估选股（`jq_deep.py` / `jq_factors.py`，`--deep`）—— 推荐

把用户的五维投资框架**量化为可计算、可配置的评分**，按重要等级（权重）综合打分，
选出合适的股票，并直接给出**买入区间 / 止损位 / 目标价 / 持有建议**。
所有权重与阈值都在 `config.py` 可调——这就是“评估规则可配置”。

```bash
python3 jq_main.py                              # 默认即为多维度综合选股（等同 --deep）
python3 jq_main.py --deep                       # 全市场多维度综合选股（显式）
python3 jq_main.py --deep --codes 600498,688981 # 只评估指定自选股
```

**五大维度（重要等级 = 权重，`config.JQ_DIM_WEIGHTS` 可改）：**

| 维度 | 默认权重 | 量化内容（数据来自 jqdatasdk） |
|------|---------|------------------------------|
| **技术面** technical | 35% | 均线系统(5/10/20/60 站上+多头排列)、量价配合(涨放量/跌缩量/量价背离)、MACD(金叉死叉/零轴上下)、关键价位(前高前低/整数关口/密集成交区) |
| **大盘与板块** market | 25% | 相关指数趋势(科创板→科创50、创业板→创业板指、否则上证；站上 MA20 且向上)、个股相对强弱(跑赢指数=被资金带动)、北向资金净流入 |
| **基本面** fundamental | 20% | 营收增速(连续 3 季加速)、毛利率变化(改善/转正)、净利润+经营性现金流(盈利且现金流为正更抗跌) |
| **筹码结构** chips | 10% | 换手衰减法近似筹码成本分布 → 获利盘比例 / 主力成本区 / 集中度 |
| **消息面催化** catalyst | 10% | **解禁利空规避**(未来 N 日解禁占总股本比例，重大解禁近乎否决)+ 自定义催化(`JQ_CATALYST_OVERRIDES`) |

**买卖价位与持有建议如何来的：**
- **买入区间**：现价小幅回踩到**关键支撑**之间（不追高、不抄弱势）；
- **止损位**：关键支撑下方一个缓冲，且不超过最大风险上限 `JQ_STOP_MAX_PCT`（控制单笔亏损）；
- **目标价 1/2**：压力位 / 前高或测幅（关键价位是止盈止损的核心依据）；
- **持有建议**：*技术面定时机、基本面定持有*——站上 20 日线则持有、跌破 20 日线警戒、跌破 60 日线趋势走坏离场；基本面扎实可中线持有并容忍回调，基本面弱则按短线严格止损；
- **仓位**：综合分越高仓位越高（上限 `PER_POSITION_PCT`），有重大解禁等利空自动压缩。

**输出示例：**

```
🥇 第一推荐：688981 中芯国际（科创板）｜综合评分 0.839
  维度分：技术面 0.78 大盘板块 0.94 基本面 1.00 筹码 0.71 消息催化 0.60
  信号：主力净占比 12.0%、连续净流入 3 天，均线多头排列
  现价≈63.8　建议买入区间 62.52~64.44（关键支撑 60.0）
  止损位 58.8（-7.8%）｜目标价1 70.0（+9.7%，压力位）｜目标价2 80.0
  建议仓位：≤30%（最多 3 只）
  持有建议：站上 20 日均线，趋势健康：持有，回踩不破支撑可逢低加仓。基本面扎实，中线可耐心持有。

🥈 第二推荐：600498 烽火通信（主板沪）｜综合评分 0.654
  维度分：技术面 0.70 大盘板块 0.99 基本面 0.41 筹码 0.68 消息催化 0.10
  信号：…，⚠️30日内解禁12.0%(2026-06-20)
  建议仓位：≤14%（最多 3 只）  ← 解禁利空自动压缩仓位
```

> **数据权限**：`--deep` 额外用到 `get_fundamentals`（财务）、`get_locked_shares`（解禁）、
> 指数 `get_price`、`finance.STK_ML_QUOTA`（北向）。任一不可用都会**自动降级为该维度中性分**，
> 不影响整体运行。先跑 `python3 jq_main.py --selftest` 可逐项探测这些接口是否可用。

### 操作报告（`jq_analyst.py`，`--analyze`）

`--analyze` 会把选股结果（含**主力资金净占比/净流入**、连续净流入、涨跌幅、换手、综合评分、
近 N 日主力流向）推送给 **Claude**，生成可执行的操作报告（买入区间 / 止损 / 目标价 / 仓位）。

- **Claude 可用时**：调用 `config.CLAUDE_MODEL`，由 Claude 从候选池精选并给出报告；
- **Claude 不可用时**（未配置 / Key 无效 / 网络或额度问题）：自动降级为**本地规则化操作报告**——
  按综合评分排序，并结合风控参数（`STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` / `PER_POSITION_PCT`）
  给出买入区间、止损位、目标价 1/2 与建议仓位。**无论 Claude 是否可用都能产出可执行建议。**

### 盘中交易（`jq_trader.py`）

- **`PaperBroker`（本地模拟盘）**：用聚宽实时行情撮合，计佣金/印花税，安全，强烈建议先用它验证策略；
- 风控：**止盈 / 止损 / 移动止盈**（参数见 `config.py`），可尾盘清仓；
- **`Broker` 抽象**：实盘下单需自行接入券商接口（`LiveBroker` 为占位）。

> ⚠️ **关于实盘交易的重要说明**：`jqdatasdk` 只能取数，**不能下单**。真正的自动交易有两条合规途径：
> 1. 在 **聚宽策略平台** 上运行 `jq_strategy_joinquant.py`（平台提供 `order_target_value` 等下单 API，对接券商做模拟/实盘）；
> 2. 本地接入 **券商交易接口**（QMT/Ptrade、easytrader 等），并实现 `jq_trader.LiveBroker`。

### ⚠️ 数据权限（重要）

聚宽 JQData 的接口按 **付费档位** 提供不同数据（见官方价目：股票基础/进阶方案等）。
本项目用到的接口及常见权限要求：

| 接口 | 用途 | 说明 |
|------|------|------|
| `get_all_securities` / `get_price` / `get_bars` | 证券列表、K线 | 基础，一般可用 |
| `get_valuation` | 市值、换手率 | 基础/基本面 |
| `get_money_flow` | 主力资金流向（选股核心） | **部分账号需付费档位** |
| `get_money_flow_pro` | 资金流向（按单量分档） | `get_money_flow` 不可用时的兜底 |
| `get_current_tick` | 实时逐笔 | **需实时行情权限**（较高档位）|

为此程序做了三点稳健处理：
- **资金流向接口可配置**（`config.JQ_MONEY_FLOW_API`）：多数账号未开通 `get_money_flow` 数据权限，
  故**默认 `"pro"`**，直接用 `get_money_flow_pro`，由 `netflow_xl + netflow_l`（超大单+大单=主力）
  推导主力净额，并按“主力净额 / 当日总成交额”推导主力净占比。若你的账号已开通 `get_money_flow`，
  可设为 `"basic"`（最精确）或 `"auto"`（先 basic、失败自动降级到 pro）。
- **`get_last_price` 自动降级**：实时 tick 不可用时，依次用 `get_price('1m')` → `get_price('daily')` 兜底，
  所以没有实时权限的账号也能跑模拟盘/盘后演示。
- **先自检再使用**：`python3 jq_main.py --selftest` 会逐个探测各接口可用性，
  明确告诉你哪些数据你的账号能取、哪些需要开通付费档位。

> 所有方法调用均已对照 jqdatasdk 实际签名核对（`get_money_flow` / `get_valuation` /
> `get_price` / `get_current_tick` / `get_bars` / `get_all_securities` / `get_index_stocks` 等）。

### 风险与收益的诚实说明

> 本系统（含“最大化获利”的目标）通过 **资金面动量选股 + 严格风控** 来 *争取* 收益，
> 但**没有任何策略能保证盈利**。历史表现不代表未来收益，市场有不可预测的风险。
> 请务必先在模拟盘充分验证、控制仓位，实盘后果自负。

---

## 🚀 快速开始（三步完成）

### 第一步：安装

打开 Mac 终端（Command + 空格，搜索"终端"），进入项目目录：

```bash
cd ~/stock_picker       # 根据你的实际路径修改
bash setup.sh
```

### 第二步：配置 API Key

1. 访问 https://console.anthropic.com/
2. 注册/登录后，点击左侧 **API Keys** → **Create Key**
3. 复制 Key（格式：`sk-ant-api03-xxxxxxxx`）
4. 用文本编辑器打开 `config.py`，找到这一行：
   ```python
   ANTHROPIC_API_KEY = "your_api_key_here"
   ```
   替换为你的真实 Key：
   ```python
   ANTHROPIC_API_KEY = "sk-ant-api03-你的key"
   ```

### 第三步：运行

```bash
bash run.sh
```

---

## 📋 输出示例

```
==================================================
  🚀 自动化选股系统启动
  ⏰ 运行时间：2026-06-05 15:35:02
==================================================

🔥 今日最热板块：光通信、半导体、AI算力

📡 正在获取主力资金流向数据...
🔍 初步过滤（原始数据 5000 条）...
  ✅ 过滤后剩余 18 只候选股
📊 补充历史资金数据（共 18 只）...
  [1/18] 600498 烽火通信 主力占比18.37% | 连续净流入4日
  [2/18] 688396 华润微  主力占比12.50% | 连续净流入7日
  ...
🤖 正在调用 Claude 进行深度分析...

════════════════════════════════════
📅 2026-06-05 每日精选3股
════════════════════════════════════

🥇 第一推荐：600498 烽火通信（主板沪）
综合评分：94/100
今日信号：主力单日净流入21.74亿，超大单25.71亿，机构级扫货
核心逻辑：连续4日主力净流入，今日量级骤升6倍，光纤涨价+算力双催化...
建议买入价：56元附近（回踩55–56元区间）
止损位：53元
目标价1：60元 | 目标价2：65元
风险提示：短期涨幅较大，注意高开低走风险

...
```

---

## ⚙️ 常用配置调整

打开 `config.py` 修改：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MIN_MAIN_FORCE_RATIO` | 5.0 | 主力净占比门槛，调高=候选更少但更强 |
| `TOP_N_CANDIDATES` | 20 | 发给Claude分析的候选数量 |
| `INCLUDE_BOARDS` | [] | 指定板块，如 `["科创板","创业板"]` |
| `BYPASS_SYSTEM_PROXY` | True | 取行情数据时绕过系统代理（直连国内服务器），解决代理报错 |
| `DATA_FETCH_RETRIES` | 3 | 数据请求失败时的最大重试次数 |
| `DATA_FETCH_RETRY_DELAY` | 3.0 | 首次重试前等待秒数（之后指数退避） |

---

## ⏰ 设置每日自动运行

```bash
bash setup_cron.sh
```

设置完成后，每个交易日 **15:35** 自动运行，结果保存到 `logs/stock_picker.log`。

查看日志：
```bash
tail -f logs/stock_picker.log
```

---

## 🧪 连通性自检（数据获取失败时先跑这个）

如果运行时报数据获取失败，先做一次连通性自检，它会**分层诊断**（DNS → TCP → 真实 HTTPS 接口）
并逐个探测 AKShare 接口，直接告诉你问题出在哪一层：

```bash
python3 stock_picker.py --selftest
```

输出示例（关键看 ③ HTTPS 这一层）：

```
[0] 网络分层诊断（DNS → TCP → HTTPS）
    DNS : ✅ 120.76.218.228
    TCP : ✅ 119.3.232.150
    HTTPS: ❌ ConnectionError: RemoteDisconnected
[1] AKShare 接口探测
    ❌ 主力资金流向排行 ...
```

判读：
- **③ HTTPS ✅** → 接口可用，刚才多半是偶发抖动/限流，直接重跑即可。
- **TCP ✅ 但 HTTPS ❌（RemoteDisconnected/reset）** → 连接在 TLS 层被干扰，
  通常是代理的 **TUN/增强模式** 或 **TLS 审查防火墙/杀毒 HTTPS 扫描**（见下方 FAQ）。
- **TCP 对端是 `127.0.0.1`** → 本地代理劫持（关闭全局/TUN 模式或加直连规则）。
- **拿到 HTTP 502 等状态码** → 服务器端临时故障/限流，与代理无关，稍后重跑。

运行单元测试（离线，验证解析、重试、代理绕过、诊断逻辑等）：

```bash
python3 -m unittest -v test_stock_picker.py
# 需要联网验证东方财富可达时：
RUN_LIVE_TESTS=1 python3 -m unittest -v test_stock_picker.TestLive
# 通过 akshare 真正拉取指定几支股票（茅台/平安/宁德/中芯）的资金流向并校验：
RUN_LIVE_TESTS=1 python3 -m unittest -v test_stock_picker.TestSpecificStocksFundFlowLive
```

> 联网测试若遇到当前网络/代理无法访问东方财富，会自动 **skip**（而非误报失败）。

---

## ❓ 常见问题

**Q：运行报错 `ModuleNotFoundError: akshare`**
```bash
source venv/bin/activate
pip install akshare --upgrade
```

**Q：报错 `authentication_error`**
检查 `config.py` 中的 API Key 是否正确，注意前后不要有空格。

**Q：数据获取失败或返回空**
AKShare 依赖东方财富接口，非交易时段或节假日可能返回空数据，属正常现象。

**Q：报错 `ProxyError('Unable to connect to proxy', ...)` / `Connection aborted` / `RemoteDisconnected` / `Max retries exceeded`**
这是因为你的电脑开启了科学上网/全局代理（VPN、Clash、Shadowsocks 等）。
东方财富是国内服务器，请求被强行转发到代理后反而连不上、或被代理中断连接。
本系统默认 `BYPASS_SYSTEM_PROXY = True`，会在取数时自动绕过代理直连——
**不仅清除代理环境变量，还会禁用 `requests` 对 macOS“系统偏好设置-网络-代理”
里本地代理（如 `127.0.0.1`）的自动探测**，所以即使开着代理软件也能直连。
如果仍然报错：
- 确认 `config.py` 中 `BYPASS_SYSTEM_PROXY = True`；
- 或临时退出代理软件 / 关闭“全局模式”后再运行；
- 偶发的网络抖动会自动重试（见 `DATA_FETCH_RETRIES`）。

**Q：已开启绕过代理，但仍报 `Connection aborted` / `RemoteDisconnected`，且连接到 `127.0.0.1`**
说明你的代理是 **系统级/透明代理**（Clash 的 **TUN/增强模式**、Surge 增强模式、
或“**全局模式**”），它在网络层拦截了所有连接，位于 Python 之下，程序内任何代理
设置都绕不过去。本程序会在取数失败时自动诊断并打印对端地址——若对端是 `127.0.0.1`
即属此情况。解决办法（任选其一）：
1. **关闭代理软件的 TUN / 增强模式 / 全局模式**（改回“规则/Rule 模式”），或运行时临时退出代理软件；
2. 在代理软件里给以下域名加“**直连(DIRECT)**”规则：`*.eastmoney.com`、`push2.eastmoney.com`、`quote.eastmoney.com`、`datacenter-web.eastmoney.com`；
3. 终端验证：`curl -v https://push2.eastmoney.com/api/qt/clist/get`，若 curl 也连到 `127.0.0.1` 即确认是系统级代理。

**Q：报错 `KeyError: '行业资金流向'`**
旧版本传入了错误的板块参数。新版已修正为 AKShare 要求的 `"行业资金流"`，
更新代码后即可正常获取板块资金流向。

**Q：想只看科创板和创业板怎么办**
在 `config.py` 中修改：
```python
INCLUDE_BOARDS = ["科创板", "创业板"]
```

---

## ⚠️ 免责声明

本系统仅供学习和参考，不构成任何投资建议。股市有风险，投资需谨慎。
所有操作建议请结合自身风险承受能力独立判断。
