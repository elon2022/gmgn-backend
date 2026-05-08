"""
破灭法目（雷达 v2）主入口。

每 10 分钟由 refresh.py 调用一次，对每条链：
  1. 构建候选池（trending + trenches + watchlist）
  2. 对每个候选拉一次 K 线
  3. 按 B/C/E1/E2 四档规则判定
  4. 过滤 cooldown
  5. 入 radar_v2_signals 表
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any

import radar_v2_candidates as cands
import radar_v2_kline as kline


# 链顺序（单线程跑）
DEFAULT_CHAINS = ["sol", "bsc", "base", "eth"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_in_cooldown(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    signal_kind: str,
    cooldown_hours: int,
) -> bool:
    """检查这个 (chain, address, signal_kind) 是否在 cooldown 内已触发过。"""
    row = conn.execute(
        """
        SELECT triggered_at FROM radar_v2_signals
         WHERE chain = ? AND address = ? AND signal_kind = ?
         ORDER BY triggered_at DESC LIMIT 1
        """,
        (chain, address, signal_kind),
    ).fetchone()
    if not row:
        return False

    last_iso = row["triggered_at"]
    try:
        last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    except Exception:
        return False

    delta_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return delta_hours < cooldown_hours


def _save_signal(
    conn: sqlite3.Connection,
    candidate: dict,
    metrics: dict,
    cur_mc: float | None,
    peak_mc: float | None,
    signal_kind: str,
) -> None:
    """写一条信号到 radar_v2_signals。"""
    conn.execute(
        """
        INSERT INTO radar_v2_signals (
            chain, address, triggered_at, signal_kind,
            current_price, current_mc, liquidity_usd,
            multiplier_24h, price_24h_low, price_24h_low_at,
            peak_mc_7d, peak_price_7d, peak_at, drawdown_pct,
            symbol, name, logo_url, is_honeypot, holder_count,
            source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate["chain"],
            candidate["address"],
            _now_iso(),
            signal_kind,
            metrics.get("current_price"),
            cur_mc,
            candidate.get("liquidity_usd"),
            metrics.get("multiplier_24h"),
            metrics.get("price_24h_low"),
            metrics.get("price_24h_low_at"),
            peak_mc,
            metrics.get("peak_price_7d"),
            metrics.get("peak_at"),
            metrics.get("drawdown_pct"),
            candidate.get("symbol"),
            candidate.get("name"),
            candidate.get("logo_url"),
            int(bool(candidate["is_honeypot"])) if candidate.get("is_honeypot") is not None else None,
            candidate.get("holder_count"),
            candidate.get("source"),
        ),
    )


def scan_chain(conn: sqlite3.Connection, chain: str) -> dict[str, int]:
    """
    扫描某条链。返回各档触发数量统计：{'B':n, 'C':n, 'E1':n, 'E2':n}
    """
    stats = {"B": 0, "C": 0, "E1": 0, "E2": 0, "candidates": 0, "kline_ok": 0, "kline_fail": 0}

    candidates = cands.build_candidates(conn, chain)
    stats["candidates"] = len(candidates)
    if not candidates:
        return stats

    for cand in candidates:
        # 关注池里的币如果缺少基础信息，先补一次 token info
        if cand.get("needs_info_fetch"):
            info = kline.fetch_token_info_safe(chain, cand["address"])
            if info:
                cand["symbol"] = info.get("symbol")
                cand["name"] = info.get("name")
                cand["logo_url"] = info.get("logo")
                cand["current_mc"] = kline._to_float(info.get("market_cap") or info.get("usd_market_cap"))
                cand["current_price"] = kline._to_float(info.get("price"))
                cand["liquidity_usd"] = kline._to_float(info.get("liquidity"))
                cand["holder_count"] = info.get("holder_count")
                cand["total_supply"] = kline._to_float(info.get("total_supply"))

        # 拉 K 线
        candles = kline.fetch_kline_safe(chain, cand["address"], hours=168)
        if not candles:
            stats["kline_fail"] += 1
            continue

        metrics = kline.analyze_kline(candles, current_price=cand.get("current_price"))
        if not metrics:
            stats["kline_fail"] += 1
            continue
        stats["kline_ok"] += 1

        # 计算市值（kline 没 supply，要从 candidate 拿）
        total_supply = cand.get("total_supply")
        cur_mc = cand.get("current_mc")
        if (cur_mc is None or cur_mc <= 0) and total_supply and metrics.get("current_price"):
            cur_mc = metrics["current_price"] * total_supply

        peak_price = metrics.get("peak_price_7d")
        peak_mc = (peak_price * total_supply) if (peak_price and total_supply) else None

        triggered = kline.evaluate_signals(metrics, cur_mc, total_supply)
        if not triggered:
            continue

        for signal_kind in triggered:
            cooldown_h = kline.SIGNAL_RULES[signal_kind]["cooldown_hours"]
            if _is_in_cooldown(conn, chain, cand["address"], signal_kind, cooldown_h):
                continue
            _save_signal(conn, cand, metrics, cur_mc, peak_mc, signal_kind)
            stats[signal_kind] += 1

    conn.commit()
    return stats


def scan_all_chains_v2(conn: sqlite3.Connection, chains: list[str] | None = None) -> dict[str, dict]:
    """
    给 refresh.py 调用的主入口。
    """
    if chains is None:
        chains = DEFAULT_CHAINS

    all_stats: dict[str, dict] = {}
    for chain in chains:
        try:
            stats = scan_chain(conn, chain)
            all_stats[chain] = stats
            n = stats["B"] + stats["C"] + stats["E1"] + stats["E2"]
            print(
                f"[radar_v2] [{chain}] candidates={stats['candidates']} "
                f"kline_ok={stats['kline_ok']} fail={stats['kline_fail']} "
                f"signals: B={stats['B']} C={stats['C']} E1={stats['E1']} E2={stats['E2']} (total {n})"
            )
        except Exception as e:
            print(f"[radar_v2] [{chain}] FAILED: {type(e).__name__}: {e}")
            all_stats[chain] = {"error": str(e)}

    return all_stats
