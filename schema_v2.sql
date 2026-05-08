-- 破灭法目（雷达 v2）独立 schema
-- 跟现有 radar_signals 完全隔离，互不影响

-- 信号表
CREATE TABLE IF NOT EXISTS radar_v2_signals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chain                TEXT NOT NULL,
    address              TEXT NOT NULL,
    triggered_at         TEXT NOT NULL,           -- ISO 时间，触发时刻
    signal_kind          TEXT NOT NULL,           -- 'B' / 'C' / 'E1' / 'E2'

    -- 触发时刻数据
    current_price        REAL,
    current_mc           REAL,
    liquidity_usd        REAL,

    -- B/C 档：24h 涨幅
    multiplier_24h       REAL,
    price_24h_low        REAL,
    price_24h_low_at     TEXT,                    -- 24h 低点出现的时刻

    -- E1/E2 档：7d 高点 + 回撤
    peak_mc_7d           REAL,
    peak_price_7d        REAL,
    peak_at              TEXT,                    -- 7d 高点出现的时刻
    drawdown_pct         REAL,                    -- 当前距高点回撤百分比（负数，如 -65 = 跌 65%）

    -- 代币元信息（冗余存，避免 join）
    symbol               TEXT,
    name                 TEXT,
    logo_url             TEXT,
    is_honeypot          INTEGER,
    holder_count         INTEGER,

    -- 候选来源（多个用 ';' 分隔，便于分析）
    source               TEXT                     -- 'trending' / 'trenches' / 'watchlist'
);

-- iOS 拉信号列表按时间倒序
CREATE INDEX IF NOT EXISTS idx_radar_v2_recent
    ON radar_v2_signals (triggered_at DESC);

-- cooldown 检查需要按 (chain, address, signal_kind) 倒序找最新一条
CREATE INDEX IF NOT EXISTS idx_radar_v2_cooldown
    ON radar_v2_signals (chain, address, signal_kind, triggered_at DESC);


-- 用户代币关注池（iOS 端"代币收藏"）
CREATE TABLE IF NOT EXISTS watched_tokens (
    chain                TEXT NOT NULL,
    address              TEXT NOT NULL,
    added_at             TEXT NOT NULL,
    notes                TEXT,
    PRIMARY KEY (chain, address)
);
