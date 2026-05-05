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
