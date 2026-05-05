# GMGN 后端服务

iPhone App 的后端，调 `gmgn-cli` 拉行情数据并提供 HTTP 接口。

## 前置条件

- macOS / Linux
- Python 3.10+
- 已安装 `gmgn-cli`（`npm install -g gmgn-cli`）
- 已配置 GMGN API Key（`~/.config/gmgn/.env`）

验证 `gmgn-cli` 能正常工作：

```bash
gmgn-cli market trending --chain eth --interval 5m --limit 3 --raw
# 应该看到一个大 JSON 输出
```

## 安装

```bash
cd ~/gmgn-backend

# 装 Python 依赖
python3 -m pip install -r requirements.txt

# 配置 API_TOKEN
cp .env.example .env
# 编辑 .env，把 API_TOKEN 改成你自己的随机字符串
```

## 第一次跑

```bash
# 1. 手动拉一次数据，验证链路通了
python3 refresh.py eth
# 应该看到：[2026-05-03T10:35:00+00:00] saved 50 tokens for chain=eth interval=5m

# 2. 启动 API 服务
uvicorn main:app --host 0.0.0.0 --port 8000

# 3. 健康检查
curl http://localhost:8000/healthz
```

## 让 iPhone 访问

1. 找 Mac 的局域网 IP：
   ```bash
   ipconfig getifaddr en0
   # 比如 192.168.1.100
   ```
2. iPhone App 的设置页：
   - 地址：`http://192.168.1.100:8000`
   - Token：你 `.env` 里的 `API_TOKEN`
   - 关掉「使用 Mock 数据」
3. iPhone 和 Mac 必须连同一个 Wi-Fi。

## 自动每 5 分钟刷新

### macOS：用 launchd

创建 `~/Library/LaunchAgents/com.gmgn.refresh.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gmgn.refresh</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>/Users/你的用户名/gmgn-backend/refresh.py</string>
        <string>eth</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/你的用户名/gmgn-backend/refresh.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/你的用户名/gmgn-backend/refresh.err</string>
    <key>WorkingDirectory</key>
    <string>/Users/你的用户名/gmgn-backend</string>
</dict>
</plist>
```

加载：

```bash
launchctl load ~/Library/LaunchAgents/com.gmgn.refresh.plist
# 查日志
tail -f ~/gmgn-backend/refresh.log
```

> ⚠️ Mac 睡眠时 launchd 不跑。MacMini 不常开机的话，最终建议把后端部署到 Linux 服务器上（用 systemd timer 替代 launchd）。

## 接口

| 路径 | 方法 | 认证 | 说明 |
|---|---|---|---|
| `/healthz` | GET | 否 | 健康检查 + DB 状态 |
| `/api/v1/trending?chain=eth&limit=50` | GET | 是 | 最新热门榜（读本地 DB） |
| `/api/v1/token/{chain}/{address}` | GET | 是 | 代币详情 + 吸筹分 |
| `/api/v1/token/{chain}/{address}/kline?resolution=1h&limit=200` | GET | 是 | K 线，resolution 支持 1m/5m/15m/1h/4h/1d |
| `/api/v1/refresh?chain=eth` | POST | 是 | 手动触发一次抓取 |

### 详情接口返回示例

```jsonc
{
  "chain": "eth",
  "address": "0x...",
  "symbol": "MOCK", "name": "Mock Token",
  "price_usd": 0.000123, "price_change_pct": 5.2,
  "volume_usd": 850000, "liquidity_usd": 1200000,
  "holder_count": 1820, "smart_degen_count": 8,
  "top10_holder_rate": 0.42,
  "is_honeypot": false, "is_renounced": true,
  "snapshot_ts": "2026-...", "previous_ts": "2026-...",
  "holder_growth_pct": 8.3,    // 跟上一次快照对比，第一次跑会是 null
  "volume_ratio": 1.05,         // 同上
  "accumulation_score": {
    "total": 84,
    "components": {
      "smart_money":          {"score": 24, "max": 30, "reason": "..."},
      "stealth_accumulation": {"score": 30, "max": 30, "reason": "..."},
      "holder_concentration": {"score": 25, "max": 25, "reason": "..."},
      "liquidity":            {"score": 15, "max": 15, "reason": "..."}
    },
    "penalties": [
      {"score": -10, "reason": "合约未 renounce"}
    ]
  }
}
```

### 吸筹分公式

满分 100，由四个维度加和后扣风险项：

| 维度 | 满分 | 思路 |
|---|---|---|
| 聪明钱信号 | 30 | 持有的聪明钱地址数 × 3，上限 30 |
| 温和吸筹 | 30 | 持币人数在涨 + 成交量没爆 → 庄家在静收筹码 |
| 筹码结构 | 25 | 前 10 占比落在 30%-60% 甜蜜区给满分 |
| 流动性 | 15 | 大于 $500K 给满分 |
| 风险扣分 | — | 蜜罐归零；高税 -20；未 renounce -10 |

**注**：温和吸筹维度需要历史快照对比（持币人数 / 成交量增速），第一次跑后端时这个维度会显示「数据不足」。等 launchd 跑过几轮、积累了多个快照后自动启用。

公式实现在 `accumulation.py`，调权重只改这一个文件。

## 文件说明

| 文件 | 说明 |
|---|---|
| `main.py` | FastAPI 入口，路由定义 |
| `refresh.py` | 拉热门榜入库，独立可执行（给 launchd 调用） |
| `gmgn_client.py` | 封装所有 `gmgn-cli` 子进程调用 |
| `accumulation.py` | 吸筹分计算逻辑 |
| `schema.sql` | SQLite 表结构 |
| `gmgn.db` | 数据库文件（运行后自动生成） |
| `refresh.log` / `refresh.err` | launchd 日志（运行后自动生成） |

## 排错

**`gmgn-cli: command not found`（在 launchd 日志里）**
launchd 的 PATH 跟终端不同。`gmgn_client.py` 里的 `GMGN_CLI` 应该是绝对路径。如果你换了 node 版本（fnm install 新版），要更新这个路径，或者用环境变量覆盖：
```bash
export GMGN_CLI=/Users/.../installation/bin/gmgn-cli
```

**iPhone 连不上后端**
- iPhone 和 Mac 同 Wi-Fi
- Mac 系统设置 → 网络 → 防火墙：允许入站 8000 端口（或暂时关闭防火墙测试）
- `uvicorn` 必须用 `--host 0.0.0.0`，不能是 `127.0.0.1`

**详情接口报 502 `gmgn-cli error`**
本地 DB 里没有这个代币，后端会调 `gmgn-cli token info` 兜底。如果 cli 也拿不到（地址错、链错、API key 失效），就 502。检查：
- 地址和链的组合是否合法
- 直接命令行跑 `gmgn-cli token info --chain eth --address 0x...` 看看

**详情页吸筹分里「温和吸筹」一直显示「数据不足」**
你只跑过一次 `refresh.py`，没有历史快照。多跑几次（或等 launchd 跑几轮）就有了。

**`POST /api/v1/refresh` 太慢**
正常——要等 `gmgn-cli` 完成（1-2 秒）。

## 后续

- [ ] 部署到阿里云 ECS（Linux + systemd timer 替代 launchd）
- [ ] 阶段 D：聪明钱交易历史（接 `/v1/user/wallet_activity`）
- [ ] 阶段 E：资金流向
- [ ] 阶段 F：通知与多链