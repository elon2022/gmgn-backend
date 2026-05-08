"""
破灭法目 K 线判定。

核心算法：对一个候选币，拉 7 天 1h K 线，算出：
  - 24h 价格涨幅倍数（current / 24h_low）
  - 7d 价格高点 + 高点时刻
  - 当前距 7d 高点的回撤百分比

然后按 B/C/E1/E2 四档规则判定。
"""
import time
from typing import Any

import gmgn_client


# 四档信号阈值
SIGNAL_RULES = {
    "B": {
        "name": "🐤 早鸟",
        "mc_min": 200_000,
        "mc_max": 2_000_000,
        "multiplier_min": 2.0,           # 24h 涨 ≥ 2x
        "cooldown_hours": 12,
        "kind": "breakout",              # B/C 是暴涨型
    },
    "C": {
        "name": "🦅 飞鹰",
        "mc_min": 2_000_000,
        "mc_max": 15_000_000,
        "multiplier_min": 4.0,           # 24h 涨 ≥ 4x
        "cooldown_hours": 24,
        "kind": "breakout",
    },
    "E1": {
        "name": "👀 小回溯",
        "mc_min": 200_000,
        "mc_max": 1_000_000,
        "peak_min": 2_000_000,           # 7d 内有过 ≥ $2M
        "drawdown_max": -50,             # 跌 ≥ 50%（drawdown_pct 是负数，-50 表示跌 50%）
        "cooldown_hours": 48,
        "kind": "rebound",
    },
    "E2": {
        "name": "🐳 大回溯",
        "mc_min": 1_000_000,
        "mc_max": 5_000_000,
        "peak_min": 15_000_000,          # 7d 内有过 ≥ $15M
        "drawdown_max": -50,
        "cooldown_hours": 72,
        "kind": "rebound",
    },
}


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_kline_safe(chain: str, address: str, hours: int = 168) -> list[dict]:
    """
    安全拉 K 线。失败返回空列表，不抛异常。
    hours=168 即 7 天，resolution=1h 共 168 根。
    """
    try:
        candles = gmgn_client.token_kline(
            chain=chain, address=address, resolution="1h", hours=hours
        )
        return candles or []
    except Exception as e:
        print(f"[v2/kline] {chain} {address[:10]} kline failed: {e}")
        return []


def fetch_token_info_safe(chain: str, address: str) -> dict:
    """安全拉 token info。失败返回空字典。"""
    try:
        return gmgn_client.token_info(chain=chain, address=address) or {}
    except Exception as e:
        print(f"[v2/kline] {chain} {address[:10]} token_info failed: {e}")
        return {}


def _normalize_candle(c: dict) -> dict | None:
    """
    把 cli 返回的 K 线统一成 {ts, open, high, low, close, volume} 格式（数值都是 float）。
    cli 返回 time 字段可能是毫秒或秒，统一转秒。
    """
    ts_raw = c.get("time") or c.get("timestamp") or c.get("ts")
    if ts_raw is None:
        return None
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return None
    if ts > 10**12:    # 毫秒
        ts = ts // 1000

    close = _to_float(c.get("close") or c.get("c"))
    if close is None:
        return None

    return {
        "ts": ts,
        "open": _to_float(c.get("open") or c.get("o")),
        "high": _to_float(c.get("high") or c.get("h")),
        "low": _to_float(c.get("low") or c.get("l")),
        "close": close,
        "volume": _to_float(c.get("volume") or c.get("v")),
    }


def analyze_kline(candles: list[dict], current_price: float | None = None) -> dict | None:
    """
    分析 K 线，返回判定要用的指标：
      - current_price：以传入参数优先，否则用最后一根 close
      - price_24h_low：过去 24h 最低价
      - price_24h_low_at：低点时刻 ISO
      - peak_price_7d：7d 最高价
      - peak_at：高点时刻 ISO
      - multiplier_24h：current / 24h_low
      - drawdown_pct：(current - peak) / peak * 100（负数）

    candles 不足时返回 None。
    """
    norm = [n for c in candles if (n := _normalize_candle(c)) is not None]
    if len(norm) < 2:
        return None

    # 按时间排序
    norm.sort(key=lambda x: x["ts"])
    now_ts = int(time.time())
    cutoff_24h = now_ts - 24 * 3600

    closes = [c["close"] for c in norm]
    last_close = closes[-1]
    cur_price = current_price if (current_price and current_price > 0) else last_close

    # 24h 窗口
    window_24h = [c for c in norm if c["ts"] >= cutoff_24h]
    if not window_24h:
        # 数据太老，用全量最后 24 根
        window_24h = norm[-24:] if len(norm) >= 24 else norm

    low_point_24h = min(window_24h, key=lambda c: c["close"])
    price_24h_low = low_point_24h["close"]

    # 7d 高点
    peak_point = max(norm, key=lambda c: c["close"])
    peak_price = peak_point["close"]

    # 计算指标
    multiplier_24h = (cur_price / price_24h_low) if price_24h_low > 0 else None
    drawdown_pct = ((cur_price - peak_price) / peak_price * 100) if peak_price > 0 else None

    return {
        "current_price": cur_price,
        "price_24h_low": price_24h_low,
        "price_24h_low_at": _ts_to_iso(low_point_24h["ts"]),
        "peak_price_7d": peak_price,
        "peak_at": _ts_to_iso(peak_point["ts"]),
        "multiplier_24h": multiplier_24h,
        "drawdown_pct": drawdown_pct,
    }


def _ts_to_iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def evaluate_signals(
    metrics: dict,
    current_mc: float | None,
    total_supply: float | None,
) -> list[str]:
    """
    根据 K 线指标 + 当前市值 + supply，判定命中哪些档（B/C/E1/E2）。
    返回命中的 kind 列表，如 ['B'] / ['E2'] / ['B', 'E1'] / []。
    """
    triggered: list[str] = []

    if not metrics:
        return triggered

    cur_price = metrics.get("current_price")
    multiplier_24h = metrics.get("multiplier_24h")
    peak_price = metrics.get("peak_price_7d")
    drawdown = metrics.get("drawdown_pct")

    # 当前市值：优先用传入，否则用 price * supply
    cur_mc = current_mc
    if (cur_mc is None or cur_mc <= 0) and cur_price and total_supply:
        cur_mc = cur_price * total_supply
    if cur_mc is None or cur_mc <= 0:
        return triggered

    # 7d 峰值市值：peak_price * supply（supply 当前漂移忽略不计）
    peak_mc = None
    if peak_price and total_supply:
        peak_mc = peak_price * total_supply

    # ---- B 档 ----
    rule = SIGNAL_RULES["B"]
    if (rule["mc_min"] <= cur_mc <= rule["mc_max"]
            and multiplier_24h is not None and multiplier_24h >= rule["multiplier_min"]):
        triggered.append("B")

    # ---- C 档 ----
    rule = SIGNAL_RULES["C"]
    if (rule["mc_min"] <= cur_mc <= rule["mc_max"]
            and multiplier_24h is not None and multiplier_24h >= rule["multiplier_min"]):
        triggered.append("C")

    # ---- E1 档 ----
    rule = SIGNAL_RULES["E1"]
    if (rule["mc_min"] <= cur_mc <= rule["mc_max"]
            and peak_mc is not None and peak_mc >= rule["peak_min"]
            and drawdown is not None and drawdown <= rule["drawdown_max"]):
        triggered.append("E1")

    # ---- E2 档 ----
    rule = SIGNAL_RULES["E2"]
    if (rule["mc_min"] <= cur_mc <= rule["mc_max"]
            and peak_mc is not None and peak_mc >= rule["peak_min"]
            and drawdown is not None and drawdown <= rule["drawdown_max"]):
        triggered.append("E2")

    return triggered
