# 📈 自动化系统

每日自动从市场筛选3只主力重仓强势股，结合 Claude AI 给出具体买入价、止损位和目标价。

---

## 🗂 文件结构

```
stock_picker/
├── stock_picker.py            # AKShare 版主程序（东方财富数据源）
├── config.py                  # 配置文件（你需要修改这个）
│
├── jq_main.py                 # 🆕 聚宽版主入口（选股 + 盘中交易）
├── jq_data.py                 # 🆕 聚宽数据层（jqdatasdk 封装）
├── jq_selector.py             # 🆕 聚宽选股逻辑
├── jq_trader.py               # 🆕 盘中交易引擎（模拟盘/风控/券商抽象）
├── jq_strategy_joinquant.py   # 🆕 聚宽“策略平台”实盘/回测策略模板
├── test_stock_picker.py       # AKShare 版单元测试
├── test_jq.py                 # 🆕 聚宽版单元测试
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
python3 jq_main.py --selftest   # 登录聚宽 + 检查额度/连通性
python3 jq_main.py --select     # 仅选股并打印候选
python3 jq_main.py --analyze    # 选股 + Claude 深度分析
python3 jq_main.py --paper      # 选股 + 本地模拟盘日内交易（推荐，安全）
python3 jq_main.py --paper --demo  # 用最新价做一次性演示（非交易时段也能跑）
```

### 选股逻辑（`jq_selector.py`）

1. 构建股票池（全 A 股或指定指数成分股）→ 剔除 ST / 次新 / 指定板块；
2. 按 **市值、换手率** 初筛（`get_valuation`）；
3. 拉取当日 **主力资金流向**（`get_money_flow`），按 **主力净占比** 筛选排序；
4. 取前 N 只，补充 **连续主力净流入天数**；
5. 输出候选股（可建仓或交给 Claude 分析）。

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
| `get_current_tick` | 实时逐笔 | **需实时行情权限**（较高档位）|

为此程序做了两点稳健处理：
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
