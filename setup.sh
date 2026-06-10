#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 自动化选股系统 - 一键安装 & 运行脚本（Mac 版）
# 使用方法：在终端运行  bash setup.sh
# ─────────────────────────────────────────────────────────────

set -e  # 遇到错误立即停止

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "================================================"
echo "  📈 自动化选股系统 - 安装程序"
echo "================================================"
echo ""

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ 未检测到 Python3，请先安装：https://www.python.org/downloads/${NC}"
    exit 1
fi

PYTHON_VER=$(python3 --version 2>&1)
echo -e "${GREEN}✅ 检测到 $PYTHON_VER${NC}"

# 检查 pip
if ! command -v pip3 &>/dev/null; then
    echo -e "${YELLOW}⚠️  pip3 未找到，尝试安装...${NC}"
    python3 -m ensurepip --upgrade
fi

# 创建虚拟环境
if [ ! -d "venv" ]; then
    echo ""
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
    echo -e "${GREEN}✅ 虚拟环境创建完成${NC}"
else
    echo -e "${GREEN}✅ 虚拟环境已存在，跳过创建${NC}"
fi

# 激活虚拟环境并安装依赖
echo ""
echo "📥 安装依赖包..."
source venv/bin/activate
pip install --upgrade pip -q
pip install akshare anthropic pandas jqdatasdk -q
echo -e "${GREEN}✅ 依赖安装完成${NC}"

# 检查 API Key 配置
echo ""
if grep -q "your_api_key_here" config.py; then
    echo -e "${YELLOW}⚠️  请先配置 Claude API Key！${NC}"
    echo ""
    echo "  步骤："
    echo "  1. 访问 https://console.anthropic.com/"
    echo "  2. 登录后点击左侧 'API Keys' → 'Create Key'"
    echo "  3. 复制 Key，格式为 sk-ant-xxxxxx"
    echo "  4. 用文本编辑器打开 config.py"
    echo "  5. 将 'your_api_key_here' 替换为你的 Key"
    echo ""
    echo "  修改完成后，运行以下命令启动系统："
    echo -e "  ${GREEN}bash run.sh${NC}"
    echo ""
else
    echo -e "${GREEN}✅ API Key 已配置${NC}"
    echo ""
    echo -e "  运行系统：${GREEN}bash run.sh${NC}"
    echo ""
fi

echo "================================================"
echo "  安装完成！"
echo "================================================"
