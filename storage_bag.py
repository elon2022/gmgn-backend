"""
储物袋信号判定。

四档信号（基于"收藏后的 K 线"算）：
  🚀 UP_50      涨 ≥ 50%
  🚀🚀 UP_200    涨 ≥ 200%
  📉 DOWN_50    跌 ≥ 50%
  🪨 STABILIZED 止跌企稳：曾跌 ≥ 50% + 24h 不再创新低 + 反弹 5% + 流动性 ≥ $20K + 成交量回升

每个币每次扫描可能触发多档（比如同时 UP_50 和 UP_200）。
触发后 cooldown 期内不写新信号，但每次扫描会更新现有信号的 pct_change / peak / min
等实时字段，让 iOS 列表始终显示"自入场以来的最新状态"。
"""
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import gmgn_client


# ---- 阈值集中配置 ----
SIGNAL_RULES = {
    "UP_50":      {"label": "🚀",        "name": "涨 50%",   "cooldown_hours": 48},
    "UP_200":     {"label": "🚀🚀",      "name": "涨 200%",  "cooldown_hours": 72},
    "DOWN_50":    {"label": "📉",        "name": "跌 50%",   "cooldown_hours": 48},
    "STABILIZED": {"label": "🪨",        "name": "止跌企稳", "cooldown_hours": 24},
}

# 止跌企稳的判定参数
STABILIZE_WINDOW_HOURS = 24      # 最近 24h 不再创新低
STABILIZE_NOISE_TOL = 0.95       # 容忍 5% 噪声（min(last24.low) ≥ min_low × 0.95）
STABILIZE_REBOUND = 1.05         # 当前价 ≥ 历史最低 × 1.05（已反弹）
STABILIZE_LIQ_MIN = 20_000       # 流动性 ≥ $20K（防 rug）
STABILIZE_VOLUME_RATIO = 0.5     # 最近 24h 平均成交量 ≥ 下跌期平均的 50%


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_candle(c: dict) -> dict | None:
    """统一 K 线字段。返回 {ts(秒), open, high, low, close, volume}。"""
    ts_raw = c.get("time") or c.get("timestamp") or c.get("ts")
    if ts_raw is None:
        return None
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return None
    if ts > 10**12:
        ts = ts // 1000

    close = _to_float(c.get("close") or c.get("c"))
    if close is None:
        return None

    return {
        "ts": ts,
        "open": _to_float(c.get("open") or c.get("o")),
        "high": _to_float(c.get("high") or c.get("h")),
        "low": _to_float(c.get("low") or c.get("l")) or close,
        "close": close,
        "volume": _to_float(c.get("volume") or c.get("v")) or 0.0,
    }


def fetch_kline_safe(chain: str, address: str, hours: int = 720) -> list[dict]:
    """
    储物袋默认拉 30 天 1h K 线，覆盖大部分收藏场景。
    如果收藏超过 30 天，超出的部分忽略（极端长持的可以用 4h K 线扩展，本期不做）。
    """
    try:
        candles = gmgn_client.token_kline(
            chain=chain, address=address, resolution="1h", hours=hours
        )
        return candles or []
    except Exception as e:
        print(f"[storage_bag] {chain} {address[:10]} kline fail: {e}")
        return []


def fetch_token_info_safe(chain: str, address: str) -> dict:
    try:
        return gmgn_client.token_info(chain=chain, address=address) or {}
    except Exception as e:
        print(f"[storage_bag] {chain} {address[:10]} token_info fail: {e}")
        return {}


def _entry_ts(entry_at_iso: str | None) -> int | None:
    if not entry_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(entry_at_iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def analyze_storage_bag(
    candles: list[dict],
    entry_price: float,
    entry_at_ts: int,
    current_price: float,
    current_liquidity: float | None,
) -> dict | None:
    """
    分析单个储物袋代币。返回判定指标 + 命中的信号列表。

    candles: 30 天 1h K 线
    entry_price: 收藏时价格
    entry_at_ts: 收藏时刻 unix 秒
    current_price: 当前价
    current_liquidity: 当前流动性
    """
    norm = [n for c in candles if (n := _normalize_candle(c)) is not None]
    if not norm:
        return None

    # 只用收藏后的 K 线
    after_entry = [c for c in norm if c["ts"] >= entry_at_ts]
    if len(after_entry) < 2:
        # 收藏时间太短，没几根 K 线，无法判定
        return None
    after_entry.sort(key=lambda x: x["ts"])

    if entry_price <= 0:
        return None

    # ---- 基本指标 ----
    cur_price = current_price if current_price > 0 else after_entry[-1]["close"]
    pct_change = (cur_price - entry_price) / entry_price * 100

    # 收藏后的极值（用 close 价，跟其他模块口径一致）
    closes_after = [c["close"] for c in after_entry]
    peak_close = max(closes_after)
    min_close = min(closes_after)
    peak_pct = (peak_close - entry_price) / entry_price * 100
    min_pct = (min_close - entry_price) / entry_price * 100

    metrics = {
        "current_price": cur_price,
        "pct_change": pct_change,
        "peak_price": peak_close,
        "peak_pct": peak_pct,
        "min_price": min_close,
        "min_pct": min_pct,
    }

    # ---- 判定信号 ----
    triggered: list[str] = []

    # 🚀 涨 50%
    if pct_change >= 50:
        triggered.append("UP_50")
    # 🚀🚀 涨 200%
    if pct_change >= 200:
        triggered.append("UP_200")
    # 📉 跌 50%
    if pct_change <= -50:
        triggered.append("DOWN_50")

    # 🪨 止跌企稳：复杂判定，单独算
    if _check_stabilized(after_entry, entry_price, cur_price, current_liquidity):
        triggered.append("STABILIZED")

    metrics["triggered"] = triggered
    return metrics


def _check_stabilized(
    after_entry_candles: list[dict],
    entry_price: float,
    cur_price: float,
    cur_liquidity: float | None,
) -> bool:
    """
    止跌企稳：必须同时满足
      1. 历史最低 ≤ entry × 0.5（曾跌 50%+）
      2. 当前价 ≥ 历史最低 × 1.05（反弹 5%+）
      3. 最近 24h 最低 ≥ 历史最低 × 0.95（24h 没创新低）
      4. 流动性 ≥ $20K
      5. 最近 24h 成交量 ≥ 下跌期成交量 × 0.5（成交量回升，没死透）
    """
    if not after_entry_candles:
        return False

    # 1. 跌过 50%
    lows_close = [c["close"] for c in after_entry_candles]
    min_close = min(lows_close)
    if min_close > entry_price * 0.5:
        return False

    # 2. 已反弹 5%
    if cur_price < min_close * STABILIZE_REBOUND:
        return False

    # 3. 24h 没创新低
    now_ts = int(time.time())
    cutoff_24h = now_ts - STABILIZE_WINDOW_HOURS * 3600
    last24 = [c for c in after_entry_candles if c["ts"] >= cutoff_24h]
    if not last24:
        return False     # 24h 内没数据，说明 cli 可能漏数据，保守不触发
    last24_min = min(c["close"] for c in last24)
    if last24_min < min_close * STABILIZE_NOISE_TOL:
        return False

    # 4. 流动性
    if cur_liquidity is None or cur_liquidity < STABILIZE_LIQ_MIN:
        return False

    # 5. 成交量回升
    # "下跌期" = 从最低点之前的所有 K 线（如果数据少就用全部）
    min_idx = lows_close.index(min_close)
    decline_period = after_entry_candles[: min_idx + 1] if min_idx > 0 else after_entry_candles
    decline_volumes = [c["volume"] for c in decline_period if c["volume"] > 0]
    last24_volumes = [c["volume"] for c in last24 if c["volume"] > 0]

    if not decline_volumes or not last24_volumes:
        # 成交量数据缺失，跳过这条规则（不能因为缺数据就拒绝触发）
        return True

    avg_decline_vol = sum(decline_volumes) / len(decline_volumes)
    avg_last24_vol = sum(last24_volumes) / len(last24_volumes)

    if avg_decline_vol > 0 and avg_last24_vol < avg_decline_vol * STABILIZE_VOLUME_RATIO:
        return False

    return True


def _is_in_cooldown(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    signal_kind: str,
    cooldown_hours: int,
) -> bool:
    row = conn.execute(
        """
        SELECT triggered_at FROM storage_bag_signals
         WHERE chain = ? AND address = ? AND signal_kind = ?
         ORDER BY triggered_at DESC LIMIT 1
        """,
        (chain, address, signal_kind),
    ).fetchone()
    if not row:
        return False
    try:
        last_dt = datetime.fromisoformat(row["triggered_at"].replace("Z", "+00:00"))
    except Exception:
        return False
    delta_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return delta_h < cooldown_hours


def _save_signal(
    conn: sqlite3.Connection,
    watched: dict,
    metrics: dict,
    current_mc: float | None,
    signal_kind: str,
) -> None:
    conn.execute(
        """
        INSERT INTO storage_bag_signals (
            chain, address, triggered_at, signal_kind,
            entry_price, entry_mc, entry_at,
            current_price, current_mc, pct_change,
            peak_price, peak_pct, min_price, min_pct,
            symbol, name, logo_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            watched["chain"],
            watched["address"],
            _now_iso(),
            signal_kind,
            watched.get("entry_price"),
            watched.get("entry_mc"),
            watched.get("entry_at"),
            metrics.get("current_price"),
            current_mc,
            metrics.get("pct_change"),
            metrics.get("peak_price"),
            metrics.get("peak_pct"),
            metrics.get("min_price"),
            metrics.get("min_pct"),
            watched.get("symbol"),
            watched.get("name"),
            watched.get("logo_url"),
        ),
    )


def _update_latest_signal(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    signal_kind: str,
    metrics: dict,
    current_mc: float | None,
) -> None:
    """
    Cooldown 期内不写新信号，但更新现有最新信号的实时数据
    （当前价、当前 pct、新的 peak、新的 min）。

    设计目的：让储物袋列表始终显示"自入场以来的最新状态"，
    而不是"触发那一刻的快照"。

    peak / min 取历史极值（不是"现在的"peak）：
    新算出的 peak 比库里的高就替换，否则保留旧值；min 同理。
    """
    row = conn.execute(
        """
        SELECT id, peak_price, peak_pct, min_price, min_pct
          FROM storage_bag_signals
         WHERE chain = ? AND address = ? AND signal_kind = ?
         ORDER BY triggered_at DESC LIMIT 1
        """,
        (chain, address, signal_kind),
    ).fetchone()
    if not row:
        return

    # 取已有 peak / min 和新算出的对比，保留极值
    new_peak_price = metrics.get("peak_price")
    new_peak_pct = metrics.get("peak_pct")
    new_min_price = metrics.get("min_price")
    new_min_pct = metrics.get("min_pct")

    keep_peak_price = row["peak_price"]
    keep_peak_pct = row["peak_pct"]
    if new_peak_price is not None and (keep_peak_price is None or new_peak_price > keep_peak_price):
        keep_peak_price = new_peak_price
        keep_peak_pct = new_peak_pct

    keep_min_price = row["min_price"]
    keep_min_pct = row["min_pct"]
    if new_min_price is not None and (keep_min_price is None or new_min_price < keep_min_price):
        keep_min_price = new_min_price
        keep_min_pct = new_min_pct

    conn.execute(
        """
        UPDATE storage_bag_signals
           SET current_price = ?,
               current_mc = ?,
               pct_change = ?,
               peak_price = ?,
               peak_pct = ?,
               min_price = ?,
               min_pct = ?
         WHERE id = ?
        """,
        (
            metrics.get("current_price"),
            current_mc,
            metrics.get("pct_change"),
            keep_peak_price,
            keep_peak_pct,
            keep_min_price,
            keep_min_pct,
            row["id"],
        ),
    )


def scan_all(conn: sqlite3.Connection) -> dict[str, int]:
    """
    扫描所有储物袋代币。
    每 10 分钟由 refresh.py 调用一次。
    """
    stats = {"UP_50": 0, "UP_200": 0, "DOWN_50": 0, "STABILIZED": 0,
             "updated": 0,    # cooldown 内被刷新的信号数
             "watched_total": 0, "kline_ok": 0, "kline_fail": 0,
             "skipped_no_entry": 0, "skipped_too_new": 0}

    rows = conn.execute("""
        SELECT
            w.chain, w.address, w.entry_price, w.entry_mc, w.entry_at,
            t.symbol, t.name, t.logo_url
          FROM watched_tokens w
          LEFT JOIN tokens t ON t.chain = w.chain AND t.address = w.address
    """).fetchall()
    stats["watched_total"] = len(rows)

    for r in rows:
        watched = dict(r)
        chain = watched["chain"]
        address = watched["address"]

        # 缺 entry 的（旧库迁移上来的）跳过——下次 watchlist.add 不会跳过，
        # 但旧数据没有入场价没法判
        entry_price = watched.get("entry_price")
        entry_at_ts = _entry_ts(watched.get("entry_at"))
        if not entry_price or entry_price <= 0 or not entry_at_ts:
            stats["skipped_no_entry"] += 1
            continue

        # 收藏不到 1 小时，K 线还没出几根，跳过
        if int(time.time()) - entry_at_ts < 3600:
            stats["skipped_too_new"] += 1
            continue

        # 拉 K 线
        candles = fetch_kline_safe(chain, address, hours=720)
        if not candles:
            stats["kline_fail"] += 1
            continue

        # 拉 token info 拿当前价 + 流动性
        info = fetch_token_info_safe(chain, address)
        cur_price = _to_float(info.get("price"))
        cur_liquidity = _to_float(info.get("liquidity"))
        cur_mc = _to_float(info.get("market_cap") or info.get("usd_market_cap"))
        if (cur_mc is None or cur_mc <= 0) and cur_price:
            supply = _to_float(info.get("total_supply"))
            if supply:
                cur_mc = cur_price * supply

        if not cur_price:
            # 当前价拿不到，用 K 线最后一根兜底
            last_candle = _normalize_candle(candles[-1])
            cur_price = last_candle["close"] if last_candle else None
            if not cur_price:
                stats["kline_fail"] += 1
                continue

        # 顺便补一下元信息（如果 watched 里没有）
        if not watched.get("symbol"):
            watched["symbol"] = info.get("symbol")
        if not watched.get("name"):
            watched["name"] = info.get("name")
        if not watched.get("logo_url"):
            watched["logo_url"] = info.get("logo")

        metrics = analyze_storage_bag(
            candles, entry_price, entry_at_ts, cur_price, cur_liquidity
        )
        if not metrics:
            stats["kline_fail"] += 1
            continue
        stats["kline_ok"] += 1

        for kind in metrics["triggered"]:
            cooldown_h = SIGNAL_RULES[kind]["cooldown_hours"]
            if _is_in_cooldown(conn, chain, address, kind, cooldown_h):
                # cooldown 内：不写新信号，但更新现有信号的实时数据
                _update_latest_signal(conn, chain, address, kind, metrics, cur_mc)
                stats["updated"] += 1
                continue
            _save_signal(conn, watched, metrics, cur_mc, kind)
            stats[kind] += 1

    conn.commit()
    n_total = stats["UP_50"] + stats["UP_200"] + stats["DOWN_50"] + stats["STABILIZED"]
    print(
        f"[storage_bag] watched={stats['watched_total']} "
        f"kline_ok={stats['kline_ok']} fail={stats['kline_fail']} "
        f"skip_no_entry={stats['skipped_no_entry']} skip_new={stats['skipped_too_new']} | "
        f"new: 🚀={stats['UP_50']} 🚀🚀={stats['UP_200']} 📉={stats['DOWN_50']} 🪨={stats['STABILIZED']} (total {n_total}) "
        f"updated={stats['updated']}"
    )
    return stats