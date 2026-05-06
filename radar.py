"""
信号雷达：扫描最新快照，找出值得关注的代币入库。

两类信号：

1. 暴涨型（scan_and_save）：小市值短期暴涨
   - 市值 $200K - $1M
   - 10 分钟内 +50% 或 30 分钟内 +100%
   - 流动性 ≥ $50K
   - 不是蜜罐
   - 24h cooldown
   - trigger_window 写 "10m" / "30m"
   - trigger_pct 是涨幅正数

2. 回溯型（scan_rebounds）：高位币跌回小市值机会区
   双轨：
   - "rebound_major"（大饱饱）：历史高点 ≥ $5M
   - "rebound_minor"（潜伏）：历史高点 $1M-$5M
   共同条件：
   - 当前市值 $250K-$1M
   - 当前市值 ≤ 历史高点 50%
   - 流动性 ≥ $20K
   - 不是蜜罐
   - 跨越触发：上次扫描时该币市值 > 阈值，本次 ≤ 阈值（防徘徊刷屏）
   - 72h cooldown（兜底，防跨越逻辑万一失效）
   - trigger_pct 存"距高点的回撤百分比"，负数（如 -65 表示跌了 65%）
   - peak_market_cap 字段存历史高点（iOS 显示「高点 $X → 现 $Y」用）

由 refresh.py 在每次刷新所有链之后调用一次。
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

# 暴涨型配置
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

# 回溯型配置
REBOUND_CONFIG = {
    # 历史高点查询窗口
    "peak_lookback_days":          30,
    # 双轨高点门槛
    "peak_major_min":       5_000_000,   # ≥ $5M  → "rebound_major"（大饱饱）
    "peak_minor_min":       1_000_000,   # $1M-$5M → "rebound_minor"（潜伏）
    # 当前市值范围
    "current_mc_min":         250_000,
    "current_mc_max":       1_000_000,
    # 当前市值必须 ≤ 历史高点的多少
    "drop_threshold":            0.50,    # 50%
    "liquidity_min":           20_000,
    "cooldown_hours":              72,
    "skip_honeypot":            True,
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
                symbol, name, logo_url, peak_market_cap
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
    """对多条链各扫一次（暴涨型）。"""
    results = {}
    for chain in chains:
        try:
            results[chain] = scan_and_save(conn, chain)
        except Exception as e:
            print(f"[radar] {chain} scan failed: {e}")
            results[chain] = []
    return results


# ============================================================
# 回溯型扫描（rebound）：高位币跌回小市值机会区
# ============================================================

def _get_peak_market_cap(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    lookback_days: int,
) -> float | None:
    """
    在 lookback_days 内，从 trending_snapshots 找该币的历史最高市值。
    没数据返回 None。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT MAX(market_cap) AS peak
          FROM trending_snapshots
         WHERE chain = ? AND address = ? AND ts >= ?
           AND market_cap IS NOT NULL
        """,
        (chain, address, cutoff),
    ).fetchone()
    if not row or row["peak"] is None:
        return None
    return float(row["peak"])


def _get_previous_market_cap(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    latest_ts: str,
) -> float | None:
    """
    取该币上一次（latest_ts 之前最近的一次）扫描时的市值。
    用于跨越触发判断。没有上次快照返回 None。
    """
    row = conn.execute(
        """
        SELECT market_cap
          FROM trending_snapshots
         WHERE chain = ? AND address = ? AND ts < ?
         ORDER BY ts DESC
         LIMIT 1
        """,
        (chain, address, latest_ts),
    ).fetchone()
    if not row or row["market_cap"] is None:
        return None
    return float(row["market_cap"])


def _is_rebound_in_cooldown(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    now: datetime,
    cooldown_hours: int,
) -> bool:
    """检查该币是否在 cooldown 内已经触发过任何 rebound 类信号。"""
    cutoff = (now - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT 1 FROM radar_signals
         WHERE chain = ? AND address = ?
           AND trigger_window IN ('rebound_major', 'rebound_minor')
           AND triggered_at >= ?
         LIMIT 1
        """,
        (chain, address, cutoff),
    ).fetchone()
    return row is not None


def scan_rebounds(conn: sqlite3.Connection, chain: str) -> list[dict]:
    """
    扫描某条链最新快照，找出"高位回调到机会区"的代币写入 radar_signals。
    返回触发的信号列表。
    """
    cfg = REBOUND_CONFIG

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
        "SELECT * FROM trending_snapshots WHERE chain = ? AND ts = ?",
        (chain, latest_ts),
    ).fetchall()

    triggered = []
    for curr in current_rows:
        addr = curr["address"]
        mc = curr["market_cap"]

        # 3. 当前市值范围过滤
        if mc is None or not (cfg["current_mc_min"] <= mc <= cfg["current_mc_max"]):
            continue

        # 4. 流动性
        liq = curr["liquidity_usd"] or 0
        if liq < cfg["liquidity_min"]:
            continue

        # 5. 蜜罐
        symbol, name, logo, is_honeypot = _get_token_meta(conn, chain, addr)
        if cfg["skip_honeypot"] and is_honeypot:
            continue

        # 6. 拿历史高点（30 天内）
        peak = _get_peak_market_cap(conn, chain, addr, cfg["peak_lookback_days"])
        if peak is None or peak < cfg["peak_minor_min"]:
            continue  # 没历史数据，或最高也才几十万——不够格

        # 7. 当前市值必须 ≤ 高点的 50%
        threshold = peak * cfg["drop_threshold"]
        if mc > threshold:
            continue

        # 8. 跨越触发：上次扫描时市值必须 > 阈值（即"刚跌破"）
        prev_mc = _get_previous_market_cap(conn, chain, addr, latest_ts)
        if prev_mc is None:
            # 没历史快照（这是该币第一次进 trending），保守起见跳过
            continue
        if prev_mc <= threshold:
            continue  # 上次就已经在阈值下方了，不是"刚跌破"

        # 9. cooldown 兜底
        if _is_rebound_in_cooldown(conn, chain, addr, now, cfg["cooldown_hours"]):
            continue

        # 10. 分轨：major (≥$5M) vs minor ($1M-$5M)
        if peak >= cfg["peak_major_min"]:
            window = "rebound_major"
        else:
            window = "rebound_minor"

        # 11. 计算回撤百分比（负数）
        drawdown_pct = (mc - peak) / peak * 100  # 比如 mc=400K, peak=1M → -60.0

        # 12. 写入
        conn.execute(
            """
            INSERT INTO radar_signals (
                chain, address, triggered_at,
                trigger_window, trigger_pct,
                price_usd, market_cap, liquidity_usd, volume_usd,
                smart_degen_count, holder_count, top10_holder_rate, is_honeypot,
                symbol, name, logo_url, peak_market_cap
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain, addr, latest_ts,
                window, round(drawdown_pct, 2),
                curr["price_usd"], mc, liq, curr["volume_usd"],
                curr["smart_degen_count"], curr["holder_count"], curr["top10_holder_rate"],
                1 if is_honeypot else 0 if is_honeypot is not None else None,
                symbol, name, logo, peak,
            ),
        )
        triggered.append({
            "chain": chain,
            "address": addr,
            "symbol": symbol,
            "trigger": f"{window} {drawdown_pct:.1f}%",
            "market_cap": mc,
            "peak": peak,
        })

    conn.commit()
    return triggered


def scan_rebounds_all_chains(conn: sqlite3.Connection, chains: list[str]) -> dict[str, list[dict]]:
    """对多条链各扫一次（回溯型）。"""
    results = {}
    for chain in chains:
        try:
            results[chain] = scan_rebounds(conn, chain)
        except Exception as e:
            print(f"[radar:rebound] {chain} scan failed: {e}")
            results[chain] = []
    return results