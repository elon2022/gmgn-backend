"""
拉取 GMGN 热门榜并写入 SQLite。

用法：
    python3 refresh.py            # 默认 eth
    python3 refresh.py eth        # 指定链
    python3 refresh.py eth 5m 50  # 链 / 时段 / 数量
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import gmgn_client

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gmgn.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def upsert_token(conn: sqlite3.Connection, item: dict, ts: str) -> None:
    conn.execute(
        """
        INSERT INTO tokens (
            chain, address, symbol, name, logo_url,
            twitter_url, website_url, telegram_url,
            is_honeypot, is_renounced, buy_tax, sell_tax,
            total_supply, creation_timestamp, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chain, address) DO UPDATE SET
            symbol             = excluded.symbol,
            name               = excluded.name,
            logo_url           = excluded.logo_url,
            twitter_url        = excluded.twitter_url,
            website_url        = excluded.website_url,
            telegram_url       = excluded.telegram_url,
            is_honeypot        = excluded.is_honeypot,
            is_renounced       = excluded.is_renounced,
            buy_tax            = excluded.buy_tax,
            sell_tax           = excluded.sell_tax,
            total_supply       = excluded.total_supply,
            creation_timestamp = excluded.creation_timestamp,
            updated_at         = excluded.updated_at
        """,
        (
            item["chain"],
            item["address"],
            item.get("symbol"),
            item.get("name"),
            item.get("logo"),
            item.get("twitter_username"),
            item.get("website"),
            item.get("telegram"),
            item.get("is_honeypot"),
            item.get("is_renounced"),
            _to_float(item.get("buy_tax")),
            _to_float(item.get("sell_tax")),
            item.get("total_supply"),
            item.get("creation_timestamp"),
            ts,
        ),
    )


def insert_snapshot(conn: sqlite3.Connection, item: dict, ts: str, rank: int) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO trending_snapshots (
            chain, ts, rank, address,
            price_usd, price_change_5m, price_change_1h,
            volume_usd, liquidity_usd, market_cap,
            holder_count, top10_holder_rate,
            smart_degen_count, renowned_count, hot_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["chain"],
            ts,
            rank,
            item["address"],
            item.get("price"),
            item.get("price_change_percent5m"),
            item.get("price_change_percent1h"),
            item.get("volume"),
            item.get("liquidity"),
            item.get("market_cap"),
            item.get("holder_count"),
            item.get("top_10_holder_rate"),
            item.get("smart_degen_count"),
            item.get("renowned_count"),
            item.get("hot_level"),
        ),
    )


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def refresh(chain: str = "eth", interval: str = "5m", limit: int = 50) -> int:
    items = gmgn_client.trending(chain=chain, interval=interval, limit=limit)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = init_db()
    try:
        for rank, item in enumerate(items, start=1):
            upsert_token(conn, item, ts)
            insert_snapshot(conn, item, ts, rank)
        conn.commit()
    finally:
        conn.close()

    print(f"[{ts}] saved {len(items)} tokens for chain={chain} interval={interval}")
    return len(items)


if __name__ == "__main__":
    chain = sys.argv[1] if len(sys.argv) > 1 else "eth"
    interval = sys.argv[2] if len(sys.argv) > 2 else "5m"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    refresh(chain, interval, limit)