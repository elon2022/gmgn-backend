"""
储物袋（关注池）：增删查 + 入场基准存储。

收藏一个币时立即拉一次 token info 拿当前价格/市值，存为 entry_*。
这是后续判定的基准，不能丢。
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any

import gmgn_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(chain: str, address: str) -> tuple[str, str]:
    chain_norm = chain.strip().lower()
    addr = address.strip()
    if chain_norm in {"eth", "bsc", "base"}:
        addr = addr.lower()
    return chain_norm, addr


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_entry_price(chain: str, address: str) -> tuple[float | None, float | None]:
    """
    收藏时调一次 token info，返回 (price, market_cap)。
    任一缺失返回 None，不阻塞收藏。
    """
    try:
        info = gmgn_client.token_info(chain=chain, address=address) or {}
        price = _to_float(info.get("price"))
        mc = _to_float(info.get("market_cap") or info.get("usd_market_cap"))
        # 如果 mc 缺但有 price 和 supply，估算一个
        if mc is None and price:
            supply = _to_float(info.get("total_supply"))
            if supply:
                mc = price * supply
        return price, mc
    except Exception as e:
        print(f"[watchlist] fetch entry price failed for {chain}:{address}: {e}")
        return None, None


def add(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    notes: str | None = None,
) -> dict:
    """
    收藏一个币。立即拉 token info 拿入场价存下来。
    重复收藏（已存在）不更新 entry_* 字段（保留首次收藏时的基准）。
    """
    chain_n, addr_n = _normalize(chain, address)

    # 检查是否已存在
    existing = conn.execute(
        "SELECT 1 FROM watched_tokens WHERE chain = ? AND address = ?",
        (chain_n, addr_n),
    ).fetchone()

    if existing:
        # 已存在：仅更新 notes（可选），不动 entry_*
        if notes is not None:
            conn.execute(
                "UPDATE watched_tokens SET notes = ? WHERE chain = ? AND address = ?",
                (notes, chain_n, addr_n),
            )
            conn.commit()
        return {"chain": chain_n, "address": addr_n, "already_exists": True}

    # 新收藏：拉入场价
    entry_price, entry_mc = _fetch_entry_price(chain_n, addr_n)
    now = _now_iso()

    conn.execute(
        """
        INSERT INTO watched_tokens (
            chain, address, added_at, notes,
            entry_price, entry_mc, entry_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chain_n, addr_n, now, notes, entry_price, entry_mc, now),
    )
    conn.commit()
    return {
        "chain": chain_n,
        "address": addr_n,
        "entry_price": entry_price,
        "entry_mc": entry_mc,
        "entry_at": now,
    }


def remove(conn: sqlite3.Connection, chain: str, address: str) -> bool:
    chain_n, addr_n = _normalize(chain, address)
    cur = conn.execute(
        "DELETE FROM watched_tokens WHERE chain = ? AND address = ?",
        (chain_n, addr_n),
    )
    # 同时清理这个币的所有储物袋信号
    conn.execute(
        "DELETE FROM storage_bag_signals WHERE chain = ? AND address = ?",
        (chain_n, addr_n),
    )
    conn.commit()
    return cur.rowcount > 0


def list_all(conn: sqlite3.Connection, chain: str | None = None) -> list[dict]:
    """
    列出所有收藏的币，附带最新一条 storage_bag_signals。
    iOS 拿到这个就能直接渲染列表 + 后缀彩色信号。
    """
    if chain:
        chain_n = chain.strip().lower()
        where = "WHERE w.chain = ?"
        params: list[Any] = [chain_n]
    else:
        where = ""
        params = []

    sql = f"""
        SELECT
            w.chain, w.address, w.added_at, w.notes,
            w.entry_price, w.entry_mc, w.entry_at,
            t.symbol, t.name, t.logo_url,

            -- 最新一条信号
            -- 优先级：STABILIZED > UP_200 > UP_50 > DOWN_50
            -- 同优先级里取最新触发时间
            (SELECT signal_kind FROM storage_bag_signals s
              WHERE s.chain = w.chain AND s.address = w.address
              ORDER BY
                CASE s.signal_kind
                  WHEN 'STABILIZED' THEN 4
                  WHEN 'UP_200'     THEN 3
                  WHEN 'UP_50'      THEN 2
                  WHEN 'DOWN_50'    THEN 1
                  ELSE 0
                END DESC,
                s.triggered_at DESC
              LIMIT 1) AS latest_kind,
            (SELECT triggered_at FROM storage_bag_signals s
              WHERE s.chain = w.chain AND s.address = w.address
              ORDER BY
                CASE s.signal_kind
                  WHEN 'STABILIZED' THEN 4 WHEN 'UP_200' THEN 3
                  WHEN 'UP_50'      THEN 2 WHEN 'DOWN_50' THEN 1 ELSE 0 END DESC,
                s.triggered_at DESC
              LIMIT 1) AS latest_at,
            (SELECT pct_change FROM storage_bag_signals s
              WHERE s.chain = w.chain AND s.address = w.address
              ORDER BY
                CASE s.signal_kind
                  WHEN 'STABILIZED' THEN 4 WHEN 'UP_200' THEN 3
                  WHEN 'UP_50'      THEN 2 WHEN 'DOWN_50' THEN 1 ELSE 0 END DESC,
                s.triggered_at DESC
              LIMIT 1) AS latest_pct,
            (SELECT peak_pct FROM storage_bag_signals s
              WHERE s.chain = w.chain AND s.address = w.address
              ORDER BY
                CASE s.signal_kind
                  WHEN 'STABILIZED' THEN 4 WHEN 'UP_200' THEN 3
                  WHEN 'UP_50'      THEN 2 WHEN 'DOWN_50' THEN 1 ELSE 0 END DESC,
                s.triggered_at DESC
              LIMIT 1) AS latest_peak_pct,
            (SELECT min_pct FROM storage_bag_signals s
              WHERE s.chain = w.chain AND s.address = w.address
              ORDER BY
                CASE s.signal_kind
                  WHEN 'STABILIZED' THEN 4 WHEN 'UP_200' THEN 3
                  WHEN 'UP_50'      THEN 2 WHEN 'DOWN_50' THEN 1 ELSE 0 END DESC,
                s.triggered_at DESC
              LIMIT 1) AS latest_min_pct
        FROM watched_tokens w
        LEFT JOIN tokens t ON t.chain = w.chain AND t.address = w.address
        {where}
        ORDER BY w.added_at DESC
    """

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "chain": r["chain"],
            "address": r["address"],
            "added_at": r["added_at"],
            "notes": r["notes"],
            "entry_price": r["entry_price"],
            "entry_mc": r["entry_mc"],
            "entry_at": r["entry_at"],
            "symbol": r["symbol"],
            "name": r["name"],
            "logo_url": r["logo_url"],
            # 最新信号（可能为 None）
            "latest_signal_kind": r["latest_kind"],
            "latest_signal_at": r["latest_at"],
            "latest_pct_change": r["latest_pct"],
            "latest_peak_pct": r["latest_peak_pct"],
            "latest_min_pct": r["latest_min_pct"],
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
