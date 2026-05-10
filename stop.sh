#!/bin/bash
#
# stop.sh —— 停止 GMGN 后端所有服务和进程
# 顺序：timer → service → 残余进程
#

echo "============================================"
echo " GMGN Backend 停止"
echo "============================================"

# === 1. 停 timer（先停 timer 才不会再触发新的 refresh）===
echo ""
echo "[1/4] 停止 gmgn-refresh.timer..."
if systemctl is-active gmgn-refresh.timer > /dev/null 2>&1; then
    sudo systemctl stop gmgn-refresh.timer
    echo "    ✅ timer 已停"
else
    echo "    timer 当前没在跑，跳过"
fi

# === 2. 停正在跑的 refresh.service（如果刚好在跑）===
echo ""
echo "[2/4] 停止正在跑的 refresh.service（如有）..."
if systemctl is-active gmgn-refresh.service > /dev/null 2>&1; then
    sudo systemctl stop gmgn-refresh.service
    echo "    ✅ refresh.service 已停"
else
    echo "    refresh.service 当前没在跑，跳过"
fi

# === 3. 停 API ===
echo ""
echo "[3/4] 停止 gmgn-api..."
if systemctl is-active gmgn-api > /dev/null 2>&1; then
    sudo systemctl stop gmgn-api
    echo "    ✅ gmgn-api 已停"
else
    echo "    gmgn-api 当前没在跑，跳过"
fi

# === 4. 杀掉残余进程（兜底）===
# 比如手动 nohup python3 refresh.py 跑的进程，systemd 不知道
echo ""
echo "[4/4] 检查残余进程..."

# 找跟 gmgn 相关的进程
RESIDUAL=$(pgrep -af "refresh.py|gmgn-backend|uvicorn main:app" | grep -v "stop.sh" | grep -v grep || true)

if [ -n "$RESIDUAL" ]; then
    echo "    发现残余进程："
    echo "$RESIDUAL" | sed 's/^/      /'
    echo ""
    read -p "    要杀掉这些进程吗？[y/N]: " confirm
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        # 提取 PID 并 kill
        echo "$RESIDUAL" | awk '{print $1}' | xargs -r sudo kill -TERM
        sleep 2
        # 仍在的强制 kill -9
        STILL=$(pgrep -af "refresh.py|gmgn-backend|uvicorn main:app" | grep -v "stop.sh" | grep -v grep | awk '{print $1}' || true)
        if [ -n "$STILL" ]; then
            echo "$STILL" | xargs -r sudo kill -9
            echo "    强制 kill -9 完成"
        fi
        echo "    ✅ 残余进程已清理"
    else
        echo "    跳过（残余进程仍在运行）"
    fi
else
    echo "    ✅ 无残余进程"
fi

# === 总结 ===
echo ""
echo "============================================"
echo " ✅ 停止完成"
echo ""
echo " 最终状态："
for svc in gmgn-api gmgn-refresh.timer gmgn-refresh.service; do
    STATUS=$(systemctl is-active $svc 2>&1)
    printf "   %-25s %s\n" "$svc" "$STATUS"
done
echo ""
echo " 重新启动: bash start.sh"
echo "============================================"