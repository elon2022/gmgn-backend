"""
代币关注池：增删查。
"""
import sqlite3
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(chain: str, address: str) -> tuple[str, str]:
    """统一格式：链小写，EVM 地址小写。"""
    chain_norm = chain.strip().lower()
    addr = address.strip()
    if chain_norm in {"eth", "bsc", "base"}:
        addr = addr.lower()
    return chain_norm, addr


def add(conn: sqlite3.Connection, chain: str, address: str, notes: str | None = None) -> dict:
    chain_n, addr_n = _normalize(chain, address)
    conn.execute(
        """
        INSERT OR REPLACE INTO watched_tokens (chain, address, added_at, notes)
        VALUES (?, ?, ?, ?)
        """,
        (chain_n, addr_n, _now_iso(), notes),
    )
    conn.commit()
    return {"chain": chain_n, "address": addr_n, "notes": notes}


def remove(conn: sqlite3.Connection, chain: str, address: str) -> bool:
    chain_n, addr_n = _normalize(chain, address)
    cur = conn.execute(
        "DELETE FROM watched_tokens WHERE chain = ? AND address = ?",
        (chain_n, addr_n),
    )
    conn.commit()
    return cur.rowcount > 0


def list_all(conn: sqlite3.Connection, chain: str | None = None) -> list[dict]:
    if chain:
        chain_n = chain.strip().lower()
        rows = conn.execute(
            """
            SELECT w.chain, w.address, w.added_at, w.notes,
                   t.symbol, t.name, t.logo_url
              FROM watched_tokens w
              LEFT JOIN tokens t
                ON t.chain = w.chain AND t.address = w.address
             WHERE w.chain = ?
             ORDER BY w.added_at DESC
            """,
            (chain_n,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT w.chain, w.address, w.added_at, w.notes,
                   t.symbol, t.name, t.logo_url
              FROM watched_tokens w
              LEFT JOIN tokens t
                ON t.chain = w.chain AND t.address = w.address
             ORDER BY w.added_at DESC
            """
        ).fetchall()

    return [
        {
            "chain": r["chain"],
            "address": r["address"],
            "added_at": r["added_at"],
            "notes": r["notes"],
            "symbol": r["symbol"],
            "name": r["name"],
            "logo_url": r["logo_url"],
        }
        for r in rows
    ]


def is_watched(conn: sqlite3.Connection, chain: str, address: str) -> bool:
    chain_n, addr_n = _normalize(chain, address)
    row = conn.execute(
        "SELECT 1 FROM watched_tokens WHERE chain = ? AND address = ? LIMIT 1",
        (chain_n, addr_n),
    ).fetchone()
    return row is not None
