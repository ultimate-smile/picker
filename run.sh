#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 每日运行脚本
# 使用方法：bash run.sh
# ─────────────────────────────────────────────────────────────

# 进入脚本所在目录
cd "$(dirname "$0")"

# 激活虚拟环境
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "❌ 虚拟环境未创建，请先运行 bash setup.sh"
    exit 1
fi

# 检查 API Key
if grep -q "your_api_key_here" config.py; then
    echo "❌ 请先在 config.py 中配置 Claude API Key"
    exit 1
fi

# 运行选股系统
python3 stock_picker.py

# 输出运行时间
echo ""
echo "⏰ 完成时间：$(date '+%Y-%m-%d %H:%M:%S')"
