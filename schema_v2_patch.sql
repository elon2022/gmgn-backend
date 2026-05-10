-- ============================================
-- 储物袋（雷达 v2 后续）schema 升级
-- 扩展 watched_tokens 表 + 新建 storage_bag_signals 表
-- ============================================

-- 1. 老库迁移：watched_tokens 加 entry 字段
-- SQLite 不支持 IF NOT EXISTS for ADD COLUMN，refresh.py 会做检测式迁移

-- 新建：储物袋信号表（独立于 radar_v2_signals）
-- 用 watched_tokens 联合主键 (chain, address) 查最新信号
CREATE TABLE IF NOT EXISTS storage_bag_signals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chain                TEXT NOT NULL,
    address              TEXT NOT NULL,
    triggered_at         TEXT NOT NULL,
    signal_kind          TEXT NOT NULL,   -- 'UP_50' / 'UP_200' / 'DOWN_50' / 'STABILIZED'

    -- 入场基准（冗余存，便于查询）
    entry_price          REAL,
    entry_mc             REAL,
    entry_at             TEXT,

    -- 触发时刻状态
    current_price        REAL,
    current_mc           REAL,
    pct_change           REAL,            -- 当前相对入场点的涨跌百分比（正负数）

    -- 历史极值（自收藏以来）
    peak_price           REAL,            -- 收藏后的最高价
    peak_pct             REAL,            -- 高点相对入场点的涨跌幅
    min_price            REAL,            -- 收藏后的最低价
    min_pct              REAL,            -- 低点相对入场点的涨跌幅（负数）

    -- 元信息冗余
    symbol               TEXT,
    name                 TEXT,
    logo_url             TEXT
);

CREATE INDEX IF NOT EXISTS idx_sb_signals_recent
    ON storage_bag_signals (triggered_at DESC);

CREATE INDEX IF NOT EXISTS idx_sb_signals_token
    ON storage_bag_signals (chain, address, signal_kind, triggered_at DESC);
