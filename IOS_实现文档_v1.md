# GMGN App — 四个 Tab 当前实现文档

> **维护说明**：当代码改动时，对应章节也要同步更新，让本文档始终是"当前在线版本的权威说明"。
>
> 文档版本：v1（2026-05-09）
> 当前线上版本：iOS（5 Tab 布局）+ 后端（惊神刺 + 破灭法目 + 储物袋 三套并行雷达）

---

## 目录

- [一、Tab 总览](#一tab-总览)
- [二、山河榜（热门）](#二山河榜热门)
- [三、惊神刺（雷达）](#三惊神刺雷达)
- [四、破灭法目（红外线）](#四破灭法目红外线)
- [五、储物袋（自选币）](#五储物袋自选币)
- [六、共享基础设施](#六共享基础设施)
- [七、开发与运维](#七开发与运维)

---

## 一、Tab 总览

App 底部 5 个 Tab：

| Tab | 名称 | 核心功能 | iOS 文件 | 后端模块 |
|---|---|---|---|---|
| 1 | 山河榜（热门） | GMGN 热门榜呈现 + 详情 + 搜索 | `TrendingView.swift` / `TokenDetailView.swift` | trending 接口 |
| 2 | 精神刺（雷达） | 基于热门榜的暴涨/回溯检测 | `RadarView.swift` | `radar.py` |
| 3 | 破灭法目（红外线） | 多源候选池 + K 线驱动的四档雷达 | `RadarV2View.swift` | `radar_v2.py` 系列 |
| 4 | 储物袋（自选币） | 用户收藏代币 + 个性化信号判定 | `StorageBagView.swift` | `storage_bag.py` |
| 5 | 元婴期（聪明钱） | 聪明钱地址簿 + 共识表 | `SmartMoneyView.swift` | 与本文档无关 |

**注**：调息（设置）已并入元婴期 Tab 的左上角齿轮入口，不占独立 Tab。

---

## 二、山河榜（热门）

### 2.1 数据来源

- **GMGN 接口**：`/v1/market/rank`
- **CLI 命令**：`gmgn-cli market trending --chain <chain> --interval 5m --limit 50`
- **存储表**：`trending_snapshots`（每次刷新一份快照）+ `tokens`（基础元信息去重）

### 2.2 业务逻辑

每 10 分钟由 `refresh.py` 跑一次，依次刷新 4 条链（eth / sol / bsc / base），每条链拉 50 条记录入库。

**实测覆盖**：
- ETH 实际每次返回 25-39 条（GMGN 该链热门榜本身条数不足 50）
- SOL / BSC / Base 通常能拿满 50 条

### 2.3 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/trending?chain=<chain>&hours=<n>` | GET | 拉 trending 列表 |
| `/api/v1/token/{chain}/{address}` | GET | 详情页（含吸筹分） |
| `/api/v1/token/{chain}/{address}/kline?resolution=<r>` | GET | K 线 |

### 2.4 详情页核心：吸筹分

满分 100 = 4 维加分 - 风险扣分。

| 维度 | 满分 | 数据源 | 评分逻辑 |
|---|---|---|---|
| **聪明钱信号** | 30 | `smart_degen_count` | 每个聪明钱地址 +3，上限 30 |
| **温和吸筹** | 30 | `holder_growth_pct` | 持币人数增长率，按时间窗口自适应阈值（1h / 4h / 24h 三档不同门槛）|
| **筹码结构** | 25 | `top10_holder_rate` | 甜蜜区 30%-60%（有庄但不过度集中）满分 |
| **流动性** | 15 | `liquidity_usd` | ≥ $500K 满分；≥ $100K 半分；< $100K 0 分 |
| **风险扣分** | -130 | 蜜罐 / 高税费 / 未 renounce | 蜜罐归零；高税 -20；未 renounce -10 |

代码位置：`accumulation.py`

### 2.5 已知特性 / 局限

- 蜜罐合约直接归零，其他扣分叠加
- "数据收集中"：窗口 < 1h 时温和吸筹给 0 分但理由文案明确说明
- 待优化项（不在当前迭代）：分项理由"强吸筹"和总分标签存在脱节问题

---

## 三、精神刺（雷达）

### 3.1 数据来源

- **唯一数据源**：`trending_snapshots` 表（即山河榜的快照历史）
- **不直接调 GMGN cli**，复用山河榜已经入库的数据

### 3.2 业务逻辑

每次 `refresh.py` 跑完所有链的 trending 后，依次调用：

1. `radar.scan_all_chains()` — 暴涨检测
2. `radar.scan_rebounds_all_chains()` — 回溯检测

两个函数都是**只看 Top 50 候选**，对快照表做对比判定。

### 3.3 暴涨检测（CONFIG）

代码：`radar.py:36`

```python
CONFIG = {
    "market_cap_min":     200_000,    # 当前市值 $200K
    "market_cap_max":   1_000_000,    # 当前市值 $1M
    "liquidity_min":       50_000,    # 流动性 ≥ $50K
    "trigger_10m_pct":      50.0,     # 10 分钟内涨 50% 触发
    "trigger_30m_pct":     100.0,     # 30 分钟内涨 100% 触发
    "cooldown_hours":         24,
    "skip_honeypot":        True,
}
```

**判定**：当前快照对比 10 分钟前 / 30 分钟前的快照，价格涨幅超阈值即触发。

### 3.4 回溯检测（REBOUND_CONFIG）

代码：`radar.py:48`

```python
REBOUND_CONFIG = {
    "peak_lookback_days":      30,    # 30 天历史高点
    "peak_major_min":   5_000_000,    # ≥ $5M  → "大饱饱"
    "peak_minor_min":   1_000_000,    # $1M-$5M → "潜伏"
    "current_mc_min":     250_000,    # 当前市值 $250K
    "current_mc_max":   1_000_000,    # 当前市值 $1M
    "drop_threshold":       0.50,     # 当前 ≤ 历史高点 50%
    "liquidity_min":       20_000,
    "cooldown_hours":          72,
}
```

**判定**：
1. 当前在 Top 50 内
2. 30 天内**也曾**在 Top 50 内（即 `trending_snapshots` 里有它的高点数据）
3. 当前市值跌到机会区（$250K-$1M）
4. 跨越触发：上次扫描时市值 > 阈值，本次 ≤ 阈值（防徘徊刷屏）

### 3.5 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/radar/signals?hours=<n>&kind=<k>` | GET | 拉信号列表 |

### 3.6 已确认的局限

**漏检盲点**（已通过破灭法目 v2 解决，本档保留作为对比参考）：

1. **候选池仅 Top 50**：榜外的币 100% 看不到
2. **回溯依赖快照表**：30 天内必须进过榜，否则算不出高点
3. **市值范围窄**：$250K-$1M 才覆盖；中盘币（如 $15M 跌到 $2.7M 这种）完全不覆盖

实战漏检案例：
- `0x829f4b62...`（ETH）：$15M → $2.7M → $30M，从未进过 ETH Top 50
- `CB9dDufT...`（USDUC，SOL）：$2M → $25M（48h 涨 12 倍），从未进过 SOL Top 50

### 3.7 状态

**保留运行，与破灭法目并行。** 用于对比新雷达漏检率/误报率。建议跑满 1 个月再决定是否下线。

---

## 四、破灭法目（红外线）

### 4.1 设计意图

替代精神刺架构，解决"只看 Top 50 = 看不见榜外好币"的根本问题。

**核心改造**：
- 候选池从单一 trending 扩展为多源
- 判定从"对比快照表"改为"拉 K 线现场算"
- 与精神刺**完全隔离**，独立的表 / 接口 / 页面

### 4.2 数据来源（多源候选池）

| 来源 | CLI 命令 | 限制 | 用途 |
|---|---|---|---|
| trending Top 80 | `gmgn-cli market trending --chain X --interval 5m --limit 80` | 全链支持 | 主候选源 |
| trenches 已毕业 | `gmgn-cli market trenches --chain X --type completed --limit 80` | 仅 sol/bsc/base | 补充榜外活跃币 |
| 用户关注池 | （查 `watched_tokens` 表）| 全链支持 | 兜底 ETH 漏检 |

**重要**：4 条链中 ETH 不支持 trenches，所以 ETH 的候选池较窄，**主要依赖 trending + 用户手动关注**。

### 4.3 候选池过滤（轻量过滤）

代码：`radar_v2_candidates.py`

```python
CANDIDATE_MC_MIN = 200_000
CANDIDATE_MC_MAX = 50_000_000
CANDIDATE_LIQ_MIN = 20_000
```

合并去重后，先按市值 / 流动性 / 蜜罐过滤，去掉明显垃圾。
**关注池里的币例外**：永远纳入，不受过滤限制。

### 4.4 K 线判定（核心）

对每个候选拉一次 `gmgn-cli market kline --resolution 1h`，拿 7 天（168 根）K 线。

**算 3 个关键指标**：
- 24h 价格涨幅倍数 = current_price / 24h 最低价
- 7d 最高价（即 peak_price_7d）
- 当前距 7d 高点的回撤百分比 = (current - peak) / peak × 100

**市值口径**：用 close 价 × total_supply（不依赖快照表）。

### 4.5 四档信号

代码：`radar_v2_kline.py:SIGNAL_RULES`

| 档 | 名称 | 当前市值范围 | 触发条件 | Cooldown |
|---|---|---|---|---|
| **B** | 🐤 早鸟 | $200K – $2M | 24h 涨幅 ≥ 2x | 12h |
| **C** | 🦅 飞鹰 | $2M – $15M | 24h 涨幅 ≥ 4x | 24h |
| **E1** | 👀 小回溯 | $200K – $1M | 7d 高点 ≥ $2M + 跌 ≥ 50% | 48h |
| **E2** | 🐳 大回溯 | $1M – $5M | 7d 高点 ≥ $15M + 跌 ≥ 50% | 72h |

四档**互不排斥**，一个币可同时触发多档（比如 B + E1）。

### 4.6 流程

每 10 分钟由 `refresh.py` 末尾调用 `radar_v2.scan_all_chains_v2()`：

```
对每条链（顺序：sol → bsc → base → eth）：
  1. 构建候选池（trending ∪ trenches ∪ watchlist）→ 去重 + 过滤
  2. 对每个候选币拉一次 7d 1h K 线
  3. 算 24h 涨幅 + 7d 高点 + 回撤
  4. 按 B/C/E1/E2 四档判定
  5. cooldown 过滤
  6. 入 radar_v2_signals 表
```

**调用量估算**（每 10 分钟）：
- trending：4 链 × 1 = 4 次
- trenches：3 链 × 1 = 3 次
- K 线扫描：每条链 50-150 个候选 × 1 = 200-500 次
- 合计 ~210-510 次 / 10 分钟 ≈ 30,000-73,000 次 / 天

### 4.7 数据库

```sql
-- radar_v2_signals: 信号表
-- 字段：chain, address, triggered_at, signal_kind (B/C/E1/E2),
--      current_price/mc, multiplier_24h, peak_mc_7d, drawdown_pct,
--      symbol, name, logo_url, source (来源标记)

-- watched_tokens: 用户关注池（与储物袋共享）
-- 字段：chain, address, added_at, notes,
--      entry_price, entry_mc, entry_at（储物袋判定基准）
```

### 4.8 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/radar_v2/signals?hours=<n>&chain=<c>&kind=<k>` | GET | 信号列表，支持档位/链筛选 |

### 4.9 iOS 页面

`RadarV2View.swift`：

- 顶部双层 chip 筛选：档位（全部/🐤/🦅/👀/🐳）+ 链（全部/ETH/SOL/BSC/Base）
- 右上角省略号菜单：时间窗口（6h/24h/72h/7d，Picker 自动有对号）+ 刷新
- 信号卡片：
  - **暴涨档（B/C）**：显示 `24h 涨 X.X×` + 24h 低点价格
  - **回溯档（E1/E2）**：显示 `高点市值 → 当前市值（-XX%）`

---

## 五、储物袋（自选币）

### 5.1 设计意图

"破灭法目是市场视角，储物袋是用户视角。"

破灭法目用绝对市值范围扫全市场；储物袋是用户主动收藏的特定币，**判定基准是收藏那一刻**，跟市值大小无关。

### 5.2 与破灭法目的关键区别

| 维度 | 破灭法目 | 储物袋 |
|---|---|---|
| 候选来源 | trending + trenches + 关注池 | 仅 watched_tokens |
| 判定基准 | 绝对市值范围（B/C/E1/E2） | 相对收藏点的涨跌幅 |
| 适用范围 | 小盘 ~ 中盘币（$200K-$15M） | 任意大小，包括大币 |
| 信号性质 | 市场机会发现 | 个人持仓监控 |

### 5.3 入场基准

用户收藏一个币的瞬间，后端立即调一次 `gmgn-cli token info` 拿当前价，存为：

```sql
watched_tokens.entry_price   -- 收藏时价格
watched_tokens.entry_mc      -- 收藏时市值
watched_tokens.entry_at      -- 收藏时刻
```

**这是后续判定的基准，绝对不能丢**。重复收藏（已存在）不更新 entry_*，保留首次基准。

### 5.4 四档信号

代码：`storage_bag.py:SIGNAL_RULES`

| 档 | 名称 | 触发条件 | Cooldown |
|---|---|---|---|
| **UP_50** | 🚀 涨 50% | 收藏后涨幅 ≥ 50% | 48h |
| **UP_200** | 🚀🚀 涨 200% | 收藏后涨幅 ≥ 200% | 72h |
| **DOWN_50** | 📉 跌 50% | 收藏后跌幅 ≥ 50% | 48h |
| **STABILIZED** | 🪨 止跌企稳 | 复合判定（见下） | 24h |

### 5.5 止跌企稳的复合判定（重点）

5 个条件**同时满足**才触发：

```python
STABILIZE_WINDOW_HOURS = 24      # 最近 24h 不再创新低
STABILIZE_NOISE_TOL = 0.95       # 5% 噪声容忍
STABILIZE_REBOUND = 1.05         # 已反弹 5%
STABILIZE_LIQ_MIN = 20_000       # 流动性 ≥ $20K（防 rug）
STABILIZE_VOLUME_RATIO = 0.5     # 成交量回升至下跌期 50%
```

1. **跌过 50%**：收藏后历史最低 ≤ entry × 0.5
2. **已反弹 5%**：当前价 ≥ 历史最低 × 1.05
3. **24h 没创新低**：最近 24h 最低 ≥ 历史最低 × 0.95
4. **流动性达标**：当前流动性 ≥ $20K
5. **成交量回升**：最近 24h 平均成交量 ≥ 下跌期平均 × 50%

> 第 5 条若 K 线 volume 字段缺失，跳过该规则（不阻塞触发）。这是保守选择。

### 5.6 信号优先级（iOS 列表显示）

`watchlist.py:list_all` 用 SQL CASE 排序，让最高优先级的信号显示在列表后缀：

```
STABILIZED (4) > UP_200 (3) > UP_50 (2) > DOWN_50 (1)
```

设计意图：**🪨 止跌企稳是行动信号**——用户看到这个标识后会去看吸筹分判断是否抄底。所以即使 STABILIZED 和 DOWN_50 同时触发，列表里也优先显示 STABILIZED。

### 5.7 流程

每 10 分钟由 `refresh.py` 末尾调用 `storage_bag.scan_all()`：

```
对每个 watched_tokens 记录：
  1. 跳过没有 entry_price 的（旧数据）
  2. 跳过收藏不到 1 小时的（K 线还没出几根）
  3. 拉收藏后的 K 线（默认 30 天 1h）
  4. 拉一次 token info 拿当前价 + 流动性
  5. 算 4 档信号
  6. cooldown 过滤
  7. 入 storage_bag_signals 表
```

### 5.8 数据库

```sql
-- storage_bag_signals: 储物袋信号
-- 字段：chain, address, triggered_at, signal_kind (UP_50/UP_200/DOWN_50/STABILIZED),
--      entry_price/mc/at, current_price/mc, pct_change,
--      peak_price/pct, min_price/pct, symbol, name, logo_url
```

### 5.9 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/v1/watchlist?chain=<c>` | GET | 列表（含每个币的最新信号字段）|
| `/api/v1/watchlist/add` | POST | 收藏（自动拉 entry 价格） |
| `/api/v1/watchlist/remove` | POST | 移除（同时清理该币的所有信号） |
| `/api/v1/watchlist/check?chain=&address=` | GET | 详情页星标按钮显示状态用 |

### 5.10 iOS 页面

`StorageBagView.swift`：

- 顶部链筛选：全部 / ETH / SOL / BSC / Base
- 列表行：symbol + 链 badge + **彩色信号小字**（如 `🚀 +120%` 绿色 / `🪨 -45%` 橙色）
- 左滑：移出储物袋
- 右上角 "+" 加新代币（输入合约地址 + 选链 + 备注）

详情页右上角有 🛍️ 按钮，点击即收藏/移出（图标会切换到 `bag.fill` 紫色态）。

### 5.11 信号显示字段对应关系

| 信号 | iOS 显示 | 数据来源 |
|---|---|---|
| 🚀 UP_50 | `🚀 +XX%`（绿） | latest_pct_change（当前涨幅）|
| 🚀🚀 UP_200 | `🚀🚀 +XX%`（深绿） | latest_pct_change |
| 📉 DOWN_50 | `📉 -XX%`（红） | latest_pct_change（负数）|
| 🪨 STABILIZED | `🪨 -XX%`（橙） | latest_pct_change（已反弹但仍负）|

---

## 六、共享基础设施

### 6.1 数据库表全景

```
tokens                  ← 代币元信息（symbol/name/logo），所有模块共享
trending_snapshots      ← trending 快照（山河榜 + 精神刺）
radar_signals           ← 精神刺信号
radar_v2_signals        ← 破灭法目信号
watched_tokens          ← 用户关注池（破灭法目 + 储物袋共享）
storage_bag_signals     ← 储物袋信号
smart_money             ← 聪明钱地址簿（元婴期）
```

### 6.2 调度

systemd timer 每 10 分钟跑一次 `refresh.py`，串行执行：

```
refresh.py:
  1. 4 条链拉 trending → trending_snapshots
  2. radar.scan_all_chains（精神刺暴涨）
  3. radar.scan_rebounds_all_chains（精神刺回溯）
  4. radar_v2.scan_all_chains_v2（破灭法目，含 trenches + K 线扫描）
  5. storage_bag.scan_all（储物袋）
```

**任何一步失败都不影响后续**——每步独立 try/except。

### 6.3 GMGN CLI 调用量估算（每天）

| 来源 | 频次 | 总量/天 |
|---|---|---|
| 山河榜 trending（4 链） | 4 × 144 = 576 | 576 |
| 精神刺 | 不调 cli（用快照表）| 0 |
| 破灭法目 trending | 已包含在山河榜 trending | 0 |
| 破灭法目 trenches（3 链） | 3 × 144 = 432 | 432 |
| 破灭法目 K 线扫描 | 200-500 × 144 ≈ 30K-73K | 30K-73K |
| 储物袋 K 线 + token info | 关注池数 × 2 × 144 | 视收藏数 |
| **合计** | | **30K-75K** |

如果跑下来发现限流，按以下顺序降级：
1. 收紧候选池过滤（破灭法目）
2. 用户关注池仅每小时扫一次（不参与每 10 分钟）
3. trenches 改为每 30 分钟拉一次

---

## 七、开发与运维

### 7.1 后端文件结构

```
gmgn-backend/
├── main.py                    ← FastAPI 路由（所有 HTTP 接口）
├── refresh.py                 ← 定时调度入口（systemd timer 触发）
├── gmgn_client.py             ← cli 封装（trending / trenches / kline / token_info）
├── accumulation.py            ← 吸筹分计算（详情页用）
├── holdings.py                ← 钱包持仓聚合（聪明钱共识用）
├── radar.py                   ← 精神刺
├── radar_v2.py                ← 破灭法目主入口
├── radar_v2_candidates.py     ← 破灭法目候选池
├── radar_v2_kline.py          ← 破灭法目 K 线判定
├── storage_bag.py             ← 储物袋
├── watchlist.py               ← 关注池操作
├── schema.sql                 ← 主 schema
├── schema_v2.sql              ← 破灭法目 schema 增量
└── schema_v2_patch.sql        ← 储物袋 schema 增量
```

### 7.2 iOS 文件结构

```
gmgnai/
├── ContentView.swift          ← 5 Tab 入口
├── API.swift                  ← 网络层（所有接口）
├── Models.swift               ← 山河榜 / 精神刺 / 详情数据模型
├── RadarV2Models.swift        ← 破灭法目 / 储物袋数据模型
├── ServerEnvironment.swift    ← 双环境切换
├── Theme.swift                ← 颜色 / 风格
│
├── TrendingView.swift         ← 山河榜
├── TokenDetailView.swift      ← 详情页（含吸筹分 + K 线 + 储物袋星标）
├── KlineChartView.swift       ← K 线组件
├── RadarView.swift            ← 精神刺
├── RadarV2View.swift          ← 破灭法目
├── StorageBagView.swift       ← 储物袋（含 AddWatchedTokenView）
├── SmartMoneyView.swift       ← 元婴期（左上角齿轮 → SettingsView）
├── SmartMoneyCSV.swift        ← 聪明钱 CSV 导入导出
└── SettingsView.swift         ← 设置（push 进入，不再独立 Tab）
```

### 7.3 改动排查指南

**遇到雷达漏检 / 误报，去对应模块改阈值**：

| 问题 | 模块 | 改哪里 |
|---|---|---|
| 精神刺暴涨/回溯漏报 | `radar.py` | 顶部 `CONFIG` / `REBOUND_CONFIG` |
| 破灭法目某档信号噪音多 | `radar_v2_kline.py` | 顶部 `SIGNAL_RULES` |
| 破灭法目候选池太杂 | `radar_v2_candidates.py` | `CANDIDATE_MC_*` / `CANDIDATE_LIQ_MIN` |
| 储物袋止跌企稳触发不准 | `storage_bag.py` | `STABILIZE_*` 常量 |
| 储物袋四档阈值要调 | `storage_bag.py` | `analyze_storage_bag` 内的判定 |
| 吸筹分各维度权重 | `accumulation.py` | 各 `_smart_money` / `_stealth_*` 函数 |

**改完都要重启 systemd**：

```bash
sudo systemctl restart gmgn-backend
sudo systemctl status gmgn-backend
sudo journalctl -u gmgn-refresh -n 100
```

### 7.4 数据库检查命令速查

```bash
# 看 4 条链最近一次刷新各拿了多少条
sqlite3 gmgn.db "SELECT chain, ts, COUNT(*) FROM trending_snapshots
                  WHERE ts = (SELECT MAX(ts) FROM trending_snapshots WHERE chain = ts.chain)
                  GROUP BY chain"

# 看精神刺最近 24h 信号
sqlite3 gmgn.db "SELECT triggered_at, kind, symbol, trigger_pct
                 FROM radar_signals
                 WHERE triggered_at >= datetime('now', '-1 day')
                 ORDER BY triggered_at DESC"

# 看破灭法目最近 24h 信号
sqlite3 gmgn.db "SELECT triggered_at, signal_kind, symbol, multiplier_24h, drawdown_pct
                 FROM radar_v2_signals
                 WHERE triggered_at >= datetime('now', '-1 day')
                 ORDER BY triggered_at DESC"

# 看储物袋当前状态
sqlite3 gmgn.db "SELECT w.chain, w.address, w.entry_price, w.entry_at,
                        s.signal_kind, s.pct_change, s.triggered_at
                 FROM watched_tokens w
                 LEFT JOIN storage_bag_signals s
                   ON s.chain = w.chain AND s.address = w.address
                 ORDER BY w.added_at DESC"

# 检查某个币的所有数据
ADDR="0x..."
sqlite3 gmgn.db "SELECT 'trending' AS source, ts, market_cap, price_usd
                 FROM trending_snapshots WHERE address='$ADDR'
                 UNION ALL
                 SELECT 'radar', triggered_at, market_cap, NULL
                 FROM radar_signals WHERE address='$ADDR'
                 UNION ALL
                 SELECT 'radar_v2', triggered_at, current_mc, NULL
                 FROM radar_v2_signals WHERE address='$ADDR'
                 UNION ALL
                 SELECT 'storage_bag', triggered_at, current_mc, NULL
                 FROM storage_bag_signals WHERE address='$ADDR'
                 ORDER BY 2 DESC"
```

### 7.5 常见故障排查

**症状：破灭法目长期没有任何信号**
- 看 `journalctl -u gmgn-refresh` 是否有 `[radar_v2]` 日志
- 检查 `gmgn-cli market trenches --chain sol` 是否能正常返回
- 看候选池是否被过滤光（`CANDIDATE_LIQ_MIN` 是否过严）

**症状：储物袋触发了但 iOS 不显示**
- 检查 `watched_tokens.entry_price` 是否为 NULL（旧数据迁移问题）
- 用户重新收藏一次（remove + add）即可拉到 entry_price

**症状：某个币加进储物袋后没拿到 entry_price**
- 看 `watchlist.add` 日志：`[watchlist] fetch entry price failed for ...`
- 大概率是 cli 拉 token info 失败，用户重新收藏一次

**症状：iOS 编译报 `WatchedToken init` 缺参数**
- 升级 `WatchedToken` 后，`API.swift` 的 mock 数据和 `AddWatchedTokenView.submit` 的手动 init 都要补 8 个新字段
- 顺序：chain, address, addedAt, notes, entryPrice, entryMc, entryAt, symbol, name, logoUrl, latestSignalKind, latestSignalAt, latestPctChange, latestPeakPct, latestMinPct

---

## 附录：术语表

| 术语 | 含义 |
|---|---|
| 山河榜 | 热门榜（GMGN trending） |
| 精神刺 | 旧雷达（trending Top 50 内的暴涨/回溯检测） |
| 破灭法目 | 新雷达（多源候选 + K 线驱动的四档判定） |
| 储物袋 | 用户自选币 + 个性化信号 |
| 元婴期 | 聪明钱地址簿 + 共识表 |
| 调息 | 设置（已并入元婴期左上角） |
| 大饱饱 | 精神刺回溯档：曾 ≥ $5M 现在跌到 $250K-$1M |
| 潜伏 | 精神刺回溯档：曾 $1M-$5M 现在跌到 $250K-$1M |
| 早鸟 / 飞鹰 / 小回溯 / 大回溯 | 破灭法目四档：B / C / E1 / E2 |
| 涨 50% / 涨 200% / 跌 50% / 止跌企稳 | 储物袋四档：UP_50 / UP_200 / DOWN_50 / STABILIZED |
| 入场基准（entry_*） | 储物袋判定基准：用户收藏那一刻的价格/市值/时间 |
| Cooldown | 同一币同档信号触发后，多久内不再重复触发 |
| 跨越触发 | 上次扫描时不在阈值内、本次进入阈值内才触发（防徘徊刷屏） |

---

## 文档维护说明

每次代码改动后，请同步更新本文档对应章节：

- **改了任何 `CONFIG` / `SIGNAL_RULES` 阈值** → 更新 §3.3-3.4 / §4.5 / §5.4-5.5
- **加了新的数据源** → 更新 §4.2 / §5.2
- **改了接口字段** → 更新对应章节的"接口"表
- **改了 Tab 结构** → 更新 §一总览
- **加了新的 systemd 服务** → 更新 §6.2

文档与线上版本不一致时，**以线上版本为准**，但要在下次开会/迭代时同步回文档。

---

**文档版本：v1（2026-05-09）**
