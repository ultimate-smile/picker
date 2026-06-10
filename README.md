# 📈 自动化系统

每日自动从市场筛选3只主力重仓强势股，结合 Claude AI 给出具体买入价、止损位和目标价。

---

## 🗂 文件结构

```
stock_picker/
├── stock_picker.py   # 主程序
├── config.py         # 配置文件（你需要修改这个）
├── setup.sh          # 一键安装脚本
├── run.sh            # 每日手动运行
├── setup_cron.sh     # 设置每日自动运行
├── logs/             # 运行日志（自动生成）
└── README.md         # 本文件
```

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
