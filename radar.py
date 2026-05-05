"""
信号雷达：扫描最新两次快照，找出"小市值短期暴涨"的代币入库。

触发规则（预设 A：保守严苛，匹配 10 分钟刷新频率）：
- 市值 $200K - $1M
- 10 分钟内 +50% 或 30 分钟内 +100%
- 流动性 ≥ $50K
- 不是蜜罐
- 同一代币 24h 内已触发过则跳过（cooldown）

由 refresh.py 在每次刷新所有链之后调用一次。
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

# 配置（将来想做用户可调，先写死在这）
CONFIG = {
    "market_cap_min":     200_000,
    "market_cap_max":   1_000_000,
    "liquidity_min":       50_000,
    # 两档时间窗口 + 对应阈值
    "trigger_10m_pct":         50.0,
    "trigger_30m_pct":        100.0,
    "cooldown_hours":          24,
    "skip_honeypot":         True,
}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_in_cooldown(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    now: datetime,
    cooldown_hours: int,
) -> bool:
    """判断这个 token 在 cooldown 时间内是否已经触发过。"""
    cutoff = (now - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT 1 FROM radar_signals
         WHERE chain = ? AND address = ? AND triggered_at >= ?
         LIMIT 1
        """,
        (chain, address, cutoff),
    ).fetchone()
    return row is not None


def _find_past_snapshot(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    latest_ts: str,
    target_minutes_ago: int,
    tolerance_minutes: int = 4,
) -> sqlite3.Row | None:
    """
    找一个"约 N 分钟前"的快照（容差 ±tolerance）。
    10 分钟刷新频率下，容差设 ±4min 能稳定匹配上一次/上两次快照。
    """
    latest_dt = _parse_iso(latest_ts)
    if not latest_dt:
        return None
    target_dt = latest_dt - timedelta(minutes=target_minutes_ago)
    lo_ts = (target_dt - timedelta(minutes=tolerance_minutes)).isoformat(timespec="seconds")
    hi_ts = (target_dt + timedelta(minutes=tolerance_minutes)).isoformat(timespec="seconds")

    return conn.execute(
        """
        SELECT * FROM trending_snapshots
         WHERE chain = ? AND address = ?
           AND ts >= ? AND ts <= ?
         ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) ASC
         LIMIT 1
        """,
        (chain, address, lo_ts, hi_ts, target_dt.isoformat(timespec="seconds")),
    ).fetchone()


def _get_token_meta(conn: sqlite3.Connection, chain: str, address: str) -> tuple[str | None, str | None, str | None, bool | None]:
    """从 tokens 表拿 symbol/name/logo/honeypot。"""
    row = conn.execute(
        "SELECT symbol, name, logo_url, is_honeypot FROM tokens WHERE chain = ? AND address = ?",
        (chain, address),
    ).fetchone()
    if not row:
        return None, None, None, None
    return row["symbol"], row["name"], row["logo_url"], (bool(row["is_honeypot"]) if row["is_honeypot"] is not None else None)


def scan_and_save(conn: sqlite3.Connection, chain: str) -> list[dict]:
    """
    扫描某条链最新的快照，找出符合触发条件的代币写入 radar_signals。
    返回触发的信号列表（用于日志输出）。
    """
    # 1. 拿这条链最新的一次快照时间
    latest_ts_row = conn.execute(
        "SELECT MAX(ts) FROM trending_snapshots WHERE chain = ?", (chain,)
    ).fetchone()
    if not latest_ts_row or not latest_ts_row[0]:
        return []
    latest_ts = latest_ts_row[0]
    now = _parse_iso(latest_ts) or datetime.now(timezone.utc)

    # 2. 拿这次快照里所有代币
    current_rows = conn.execute(
        """
        SELECT * FROM trending_snapshots
         WHERE chain = ? AND ts = ?
        """,
        (chain, latest_ts),
    ).fetchall()

    triggered = []
    for curr in current_rows:
        # 3. 市值过滤
        mc = curr["market_cap"]
        if mc is None or not (CONFIG["market_cap_min"] <= mc <= CONFIG["market_cap_max"]):
            continue

        # 4. 流动性过滤
        liq = curr["liquidity_usd"] or 0
        if liq < CONFIG["liquidity_min"]:
            continue

        # 5. 蜜罐过滤
        symbol, name, logo, is_honeypot = _get_token_meta(conn, chain, curr["address"])
        if CONFIG["skip_honeypot"] and is_honeypot:
            continue

        # 6. cooldown 过滤
        if _is_in_cooldown(conn, chain, curr["address"], now, CONFIG["cooldown_hours"]):
            continue

        # 7. 涨幅检测：10min 和 30min 各检查一次
        curr_price = curr["price_usd"]
        if not curr_price or curr_price <= 0:
            continue

        trigger_window = None
        trigger_pct = None

        # 10 分钟窗口（实际找 10±4 分钟前的快照）
        prev_10m = _find_past_snapshot(conn, chain, curr["address"], latest_ts, 10)
        if prev_10m and prev_10m["price_usd"] and prev_10m["price_usd"] > 0:
            pct = (curr_price - prev_10m["price_usd"]) / prev_10m["price_usd"] * 100
            if pct >= CONFIG["trigger_10m_pct"]:
                trigger_window = "10m"
                trigger_pct = pct

        # 30 分钟窗口（如果 10m 没触发或者 30m 涨幅更显著）
        prev_30m = _find_past_snapshot(conn, chain, curr["address"], latest_ts, 30)
        if prev_30m and prev_30m["price_usd"] and prev_30m["price_usd"] > 0:
            pct_30m = (curr_price - prev_30m["price_usd"]) / prev_30m["price_usd"] * 100
            if pct_30m >= CONFIG["trigger_30m_pct"]:
                if trigger_window is None or pct_30m > (trigger_pct or 0):
                    trigger_window = "30m"
                    trigger_pct = pct_30m

        if trigger_window is None:
            continue

        # 8. 触发！写入 radar_signals
        conn.execute(
            """
            INSERT INTO radar_signals (
                chain, address, triggered_at,
                trigger_window, trigger_pct,
                price_usd, market_cap, liquidity_usd, volume_usd,
                smart_degen_count, holder_count, top10_holder_rate, is_honeypot,
                symbol, name, logo_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain, curr["address"], latest_ts,
                trigger_window, round(trigger_pct, 2),
                curr_price, mc, liq, curr["volume_usd"],
                curr["smart_degen_count"], curr["holder_count"], curr["top10_holder_rate"],
                1 if is_honeypot else 0 if is_honeypot is not None else None,
                symbol, name, logo,
            ),
        )
        triggered.append({
            "chain": chain,
            "address": curr["address"],
            "symbol": symbol,
            "trigger": f"{trigger_window} +{trigger_pct:.1f}%",
            "market_cap": mc,
        })

    conn.commit()
    return triggered


def scan_all_chains(conn: sqlite3.Connection, chains: list[str]) -> dict[str, list[dict]]:
    """对多条链各扫一次。"""
    results = {}
    for chain in chains:
        try:
            results[chain] = scan_and_save(conn, chain)
        except Exception as e:
            print(f"[radar] {chain} scan failed: {e}")
            results[chain] = []
    return results