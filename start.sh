#!/bin/bash
#
# start.sh —— 启动 GMGN 后端所有服务
#

set -e

echo "============================================"
echo " GMGN Backend 启动"
echo "============================================"

# === 1. 启动 API 服务 ===
echo ""
echo "[1/3] 启动 gmgn-api..."
sudo systemctl restart gmgn-api
sleep 2

if systemctl is-active gmgn-api > /dev/null; then
    echo "    ✅ gmgn-api 已启动"
else
    echo "    ❌ gmgn-api 启动失败"
    echo "    看错误："
    sudo journalctl -u gmgn-api -n 20 --no-pager
    exit 1
fi

# === 2. 启动 refresh timer ===
echo ""
echo "[2/3] 启动 gmgn-refresh.timer..."
sudo systemctl start gmgn-refresh.timer

if systemctl is-active gmgn-refresh.timer > /dev/null; then
    echo "    ✅ gmgn-refresh.timer 已启动"
    NEXT_RUN=$(systemctl list-timers gmgn-refresh.timer --no-pager | awk 'NR==2 {print $1, $2, $3}')
    echo "    下次触发: $NEXT_RUN"
else
    echo "    ❌ gmgn-refresh.timer 启动失败"
    exit 1
fi

# === 3. 验证 ===
echo ""
echo "[3/3] 健康检查..."

# 等 API 就绪
for i in 1 2 3 4 5; do
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/healthz | grep -q 200; then
        echo "    ✅ /healthz 返回 200"
        break
    fi
    sleep 1
done

# 看数据库最新状态
LATEST_TS=$(sqlite3 ~/gmgn-backend/gmgn.db "SELECT MAX(ts) FROM trending_snapshots" 2>/dev/null)
if [ -n "$LATEST_TS" ]; then
    echo "    最新 trending 快照: $LATEST_TS"
fi

echo ""
echo "============================================"
echo " ✅ 启动完成"
echo ""
echo " 服务状态："
echo "   sudo systemctl status gmgn-api"
echo "   sudo systemctl status gmgn-refresh.timer"
echo ""
echo " 看日志："
echo "   sudo journalctl -u gmgn-api -f          # API 实时日志"
echo "   sudo journalctl -u gmgn-refresh -n 50   # 最近 refresh 日志"
echo "============================================"