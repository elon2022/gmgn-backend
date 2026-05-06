-- GMGN 后端 SQLite 数据库 schema

CREATE TABLE IF NOT EXISTS tokens (
    chain                TEXT NOT NULL,
    address              TEXT NOT NULL,
    symbol               TEXT,
    name                 TEXT,
    logo_url             TEXT,
    twitter_url          TEXT,
    website_url          TEXT,
    telegram_url         TEXT,
    is_honeypot          INTEGER,
    is_renounced         INTEGER,
    buy_tax              REAL,
    sell_tax             REAL,
    total_supply         REAL,
    creation_timestamp   INTEGER,
    updated_at           TEXT,
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS trending_snapshots (
    chain                TEXT NOT NULL,
    ts                   TEXT NOT NULL,
    rank                 INTEGER NOT NULL,
    address              TEXT NOT NULL,
    -- 价格
    price_usd            REAL,
    price_change_5m      REAL,
    price_change_1h      REAL,
    -- 市场
    volume_usd           REAL,
    liquidity_usd        REAL,
    market_cap           REAL,
    -- 持币
    holder_count         INTEGER,
    top10_holder_rate    REAL,
    -- 智能信号（先存不显示）
    smart_degen_count    INTEGER,
    renowned_count       INTEGER,
    hot_level            INTEGER,
    PRIMARY KEY (chain, ts, rank)
);

CREATE INDEX IF NOT EXISTS idx_trending_latest
    ON trending_snapshots (chain, ts DESC);

CREATE INDEX IF NOT EXISTS idx_trending_address
    ON trending_snapshots (chain, address, ts DESC);


-- 雷达信号表（阶段 E 新增）
CREATE TABLE IF NOT EXISTS radar_signals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chain                TEXT NOT NULL,
    address              TEXT NOT NULL,
    triggered_at         TEXT NOT NULL,        -- ISO 时间，触发时刻
    -- 触发原因
    trigger_window       TEXT NOT NULL,        -- '10m' / '30m' (暴涨) / 'rebound_major' / 'rebound_minor' (回溯)
    trigger_pct          REAL NOT NULL,        -- 暴涨：正数涨幅；回溯：负数回撤（如 -65 表示从高点跌了 65%）
    -- 触发时刻的快照
    price_usd            REAL,
    market_cap           REAL,
    liquidity_usd        REAL,
    volume_usd           REAL,
    -- 触发时刻的智能信号（让 iOS 不用再查详情就能看个大概）
    smart_degen_count    INTEGER,
    holder_count         INTEGER,
    top10_holder_rate    REAL,
    is_honeypot          INTEGER,
    -- 代币基本信息（冗余存一份，避免 join）
    symbol               TEXT,
    name                 TEXT,
    logo_url             TEXT,
    -- 回溯型信号专用：触发时该币的历史最高市值（30 天内）
    -- 暴涨型信号此字段为 NULL
    peak_market_cap      REAL
);

-- 同一代币在 cooldown 时间内不重复触发，需要按 (chain, address) 倒序查最新
CREATE INDEX IF NOT EXISTS idx_radar_token_recent
    ON radar_signals (chain, address, triggered_at DESC);

-- iOS 拉取信号列表按时间倒序
CREATE INDEX IF NOT EXISTS idx_radar_recent
    ON radar_signals (triggered_at DESC);