"""
拉取 GMGN 热门榜并写入 SQLite，刷新完后跑一次雷达扫描。

用法：
    python3 refresh.py                  # 跑所有默认链 (eth, sol, bsc, base)
    python3 refresh.py eth              # 只跑 eth
    python3 refresh.py eth sol          # 只跑 eth 和 sol
    python3 refresh.py all 1m 100       # 全部链 + 自定义 interval/limit

systemd timer 调用：python3 refresh.py
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import gmgn_client
import radar

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gmgn.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

DEFAULT_CHAINS = ["eth", "sol", "bsc", "base"]


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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


def refresh_chain(
    conn: sqlite3.Connection,
    chain: str,
    interval: str = "5m",
    limit: int = 50,
) -> int:
    items = gmgn_client.trending(chain=chain, interval=interval, limit=limit)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for rank, item in enumerate(items, start=1):
        upsert_token(conn, item, ts)
        insert_snapshot(conn, item, ts, rank)
    conn.commit()

    print(f"[{ts}] [{chain}] saved {len(items)} tokens")
    return len(items)


def refresh_all(
    chains: list[str],
    interval: str = "5m",
    limit: int = 50,
) -> dict[str, int | str]:
    conn = init_db()
    results: dict[str, int | str] = {}
    success_chains: list[str] = []
    try:
        for chain in chains:
            try:
                results[chain] = refresh_chain(conn, chain, interval, limit)
                success_chains.append(chain)
            except Exception as e:
                err = f"FAILED: {type(e).__name__}: {e}"
                results[chain] = err
                print(f"[{chain}] {err}", file=sys.stderr)

        # 所有链刷完之后跑雷达扫描（只扫成功刷新的链）
        if success_chains:
            scan_results = radar.scan_all_chains(conn, success_chains)
            total_signals = sum(len(v) for v in scan_results.values())
            if total_signals > 0:
                print(f"---- radar: {total_signals} new signals ----")
                for chain, signals in scan_results.items():
                    for sig in signals:
                        print(f"  [{chain}] {sig.get('symbol') or sig['address'][:10]}: {sig['trigger']}, MC=${sig['market_cap']:,.0f}")
            else:
                print("---- radar: no new signals ----")
    finally:
        conn.close()
    return results


def refresh(chain: str = "eth", interval: str = "5m", limit: int = 50) -> int:
    """单链刷新（保留给 main.py 的 /api/v1/refresh 接口用）。"""
    conn = init_db()
    try:
        n = refresh_chain(conn, chain, interval, limit)
        # 单链刷新也跑一次雷达
        radar.scan_and_save(conn, chain)
        return n
    finally:
        conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        chains = DEFAULT_CHAINS
        interval = "5m"
        limit = 50
    elif args[0] == "all":
        chains = DEFAULT_CHAINS
        interval = args[1] if len(args) > 1 else "5m"
        limit = int(args[2]) if len(args) > 2 else 50
    else:
        chains = []
        rest = []
        for a in args:
            if a in {"1m", "5m", "15m", "1h", "4h", "1d"} or a.isdigit():
                rest.append(a)
            else:
                chains.append(a)
        if not chains:
            chains = DEFAULT_CHAINS
        interval = rest[0] if len(rest) >= 1 else "5m"
        limit = int(rest[1]) if len(rest) >= 2 else 50

    results = refresh_all(chains, interval=interval, limit=limit)

    print("---- summary ----")
    for chain, r in results.items():
        print(f"  {chain}: {r}")

    if any(isinstance(v, str) for v in results.values()):
        sys.exit(1)