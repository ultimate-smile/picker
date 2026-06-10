#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 设置每日定时任务（Mac crontab）
# 使用方法：bash setup_cron.sh
# 默认每个交易日 15:35 自动运行（收盘后5分钟）
# ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_SCRIPT="$SCRIPT_DIR/run.sh"
LOG_FILE="$SCRIPT_DIR/logs/stock_picker.log"

# 创建日志目录
mkdir -p "$SCRIPT_DIR/logs"

# 定时任务：周一至周五 15:35 运行
CRON_JOB="35 15 * * 1-5 bash $RUN_SCRIPT >> $LOG_FILE 2>&1"

echo ""
echo "================================================"
echo "  ⏰ 设置每日定时任务"
echo "================================================"
echo ""
echo "  运行时间：每周一至周五 15:35（收盘后）"
echo "  日志路径：$LOG_FILE"
echo ""

# 检查是否已存在
if crontab -l 2>/dev/null | grep -q "$RUN_SCRIPT"; then
    echo "✅ 定时任务已存在，无需重复添加"
else
    # 添加到 crontab
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "✅ 定时任务已添加成功"
fi

echo ""
echo "  查看定时任务：crontab -l"
echo "  删除定时任务：crontab -e（删除对应行）"
echo "  查看运行日志：tail -f $LOG_FILE"
echo ""
echo "================================================"
