"""
GMGN 后端 API 服务。

启动：
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import accumulation
import gmgn_client
import holdings as holdings_agg

API_TOKEN = os.environ.get("API_TOKEN", "dev-token-change-me")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gmgn.db"

app = FastAPI(title="GMGN Backend", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------- 工具 ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def require_auth(authorization: str | None) -> None:
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _normalize_pct(v: Any) -> float | None:
    if v is None:
        return None
    f = float(v)
    return f * 100 if abs(f) <= 1 else f


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------- 健康检查 ----------
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    db_exists = DB_PATH.exists()
    snapshot_count = 0
    latest_ts = None
    if db_exists:
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT COUNT(*), MAX(ts) FROM trending_snapshots"
                ).fetchone()
                snapshot_count, latest_ts = row[0], row[1]
        except sqlite3.OperationalError:
            pass
    return {
        "ok": True,
        "db_exists": db_exists,
        "snapshot_count": snapshot_count,
        "latest_snapshot_ts": latest_ts,
    }


# ---------- 热门榜 ----------
@app.get("/api/v1/trending")
def trending(
    chain: str = Query("eth"),
    limit: int = Query(50, ge=1, le=100),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    require_auth(authorization)

    with db() as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM trending_snapshots WHERE chain = ?", (chain,),
        ).fetchone()
        latest_ts = row[0]

        if not latest_ts:
            return {"chain": chain, "snapshot_ts": None, "items": []}

        rows = conn.execute(
            """
            SELECT s.rank, s.address,
                   s.price_usd, s.price_change_5m, s.price_change_1h,
                   s.volume_usd, s.liquidity_usd, s.market_cap,
                   s.holder_count, s.smart_degen_count, s.renowned_count,
                   t.symbol, t.name, t.logo_url,
                   t.is_honeypot, t.is_renounced, t.buy_tax, t.sell_tax
              FROM trending_snapshots s
              LEFT JOIN tokens t
                     ON t.chain = s.chain AND t.address = s.address
             WHERE s.chain = ? AND s.ts = ?
             ORDER BY s.rank
             LIMIT ?
            """,
            (chain, latest_ts, limit),
        ).fetchall()

    items = [
        {
            "rank": r["rank"],
            "token": {
                "chain": chain,
                "address": r["address"],
                "symbol": r["symbol"],
                "name": r["name"],
                "logo_url": r["logo_url"],
            },
            "price_usd": r["price_usd"],
            "price_change_pct": r["price_change_5m"],
            "volume_usd": r["volume_usd"],
            "liquidity_usd": r["liquidity_usd"],
            "market_cap": r["market_cap"],
            "price_change_1h": r["price_change_1h"],
            "holder_count": r["holder_count"],
            "smart_degen_count": r["smart_degen_count"],
            "renowned_count": r["renowned_count"],
            "is_honeypot": bool(r["is_honeypot"]) if r["is_honeypot"] is not None else None,
            "is_renounced": bool(r["is_renounced"]) if r["is_renounced"] is not None else None,
            "buy_tax": r["buy_tax"],
            "sell_tax": r["sell_tax"],
        }
        for r in rows
    ]

    return {"chain": chain, "snapshot_ts": latest_ts, "items": items}


# ---------- 详情页 ----------
def _find_comparison_snapshot(
    conn: sqlite3.Connection,
    chain: str,
    address: str,
    latest_ts: str,
) -> sqlite3.Row | None:
    """
    选取用于"温和吸筹"对比的历史快照。

    策略（按优先级）：
    1) 找一个 24h 前后 ±30 分钟的快照（理想情况）
    2) 找最早的快照（数据不足 24h 时用）
    """
    latest_dt = _parse_iso(latest_ts)
    if not latest_dt:
        return None

    # 1) 试图找 24h 前的快照
    target_dt = latest_dt.timestamp() - 24 * 3600
    target_iso_lo = datetime.fromtimestamp(target_dt - 30 * 60, tz=latest_dt.tzinfo).isoformat(timespec="seconds")
    target_iso_hi = datetime.fromtimestamp(target_dt + 30 * 60, tz=latest_dt.tzinfo).isoformat(timespec="seconds")

    row = conn.execute(
        """
        SELECT * FROM trending_snapshots
         WHERE chain = ? AND address = ?
           AND ts >= ? AND ts <= ?
         ORDER BY ts ASC
         LIMIT 1
        """,
        (chain, address, target_iso_lo, target_iso_hi),
    ).fetchone()
    if row:
        return row

    # 2) 退化：拿最早的快照（且要早于 latest_ts）
    return conn.execute(
        """
        SELECT * FROM trending_snapshots
         WHERE chain = ? AND address = ? AND ts < ?
         ORDER BY ts ASC
         LIMIT 1
        """,
        (chain, address, latest_ts),
    ).fetchone()


@app.get("/api/v1/token/{chain}/{address}")
def token_detail(
    chain: str,
    address: str,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    require_auth(authorization)

    with db() as conn:
        latest = conn.execute(
            """
            SELECT s.*, t.symbol, t.name, t.logo_url,
                   t.is_honeypot, t.is_renounced, t.buy_tax, t.sell_tax,
                   t.twitter_url, t.website_url, t.telegram_url,
                   t.creation_timestamp
              FROM trending_snapshots s
              LEFT JOIN tokens t
                     ON t.chain = s.chain AND t.address = s.address
             WHERE s.chain = ? AND s.address = ?
             ORDER BY s.ts DESC
             LIMIT 1
            """,
            (chain, address),
        ).fetchone()

        previous = _find_comparison_snapshot(conn, chain, address, latest["ts"]) if latest else None

    if latest is None:
        # 本地没有这个代币，调 cli 兜底
        try:
            info = gmgn_client.token_info(chain, address)
        except gmgn_client.GMGNCliError as e:
            raise HTTPException(status_code=404, detail=f"token not found: {e}")

        base = {
            "chain": chain,
            "address": address,
            "symbol": info.get("symbol"),
            "name": info.get("name"),
            "logo_url": info.get("logo"),
            "price_usd": info.get("price"),
            "price_change_pct": _normalize_pct(info.get("price_change_percent5m")),
            "price_change_1h": _normalize_pct(info.get("price_change_percent1h")),
            "volume_usd": info.get("volume"),
            "liquidity_usd": info.get("liquidity"),
            "market_cap": info.get("market_cap"),
            "holder_count": info.get("holder_count"),
            "smart_degen_count": info.get("smart_degen_count"),
            "top10_holder_rate": info.get("top_10_holder_rate"),
            "is_honeypot": info.get("is_honeypot"),
            "is_renounced": info.get("is_renounced"),
            "buy_tax": info.get("buy_tax"),
            "sell_tax": info.get("sell_tax"),
            "twitter_url": info.get("twitter_username"),
            "website_url": info.get("website"),
            "telegram_url": info.get("telegram"),
            "snapshot_ts": None,
            "previous_ts": None,
        }
        score_input = {**base, "holder_growth_pct": None, "window_hours": None}
    else:
        base = {
            "chain": chain,
            "address": address,
            "symbol": latest["symbol"],
            "name": latest["name"],
            "logo_url": latest["logo_url"],
            "price_usd": latest["price_usd"],
            "price_change_pct": latest["price_change_5m"],
            "price_change_1h": latest["price_change_1h"],
            "volume_usd": latest["volume_usd"],
            "liquidity_usd": latest["liquidity_usd"],
            "market_cap": latest["market_cap"],
            "holder_count": latest["holder_count"],
            "smart_degen_count": latest["smart_degen_count"],
            "top10_holder_rate": latest["top10_holder_rate"],
            "is_honeypot": bool(latest["is_honeypot"]) if latest["is_honeypot"] is not None else None,
            "is_renounced": bool(latest["is_renounced"]) if latest["is_renounced"] is not None else None,
            "buy_tax": latest["buy_tax"],
            "sell_tax": latest["sell_tax"],
            "twitter_url": latest["twitter_url"],
            "website_url": latest["website_url"],
            "telegram_url": latest["telegram_url"],
            "snapshot_ts": latest["ts"],
            "previous_ts": previous["ts"] if previous else None,
        }

        # 算 holder_growth + 时间窗口
        holder_growth_pct = None
        window_hours = None
        if previous:
            prev_dt = _parse_iso(previous["ts"])
            curr_dt = _parse_iso(latest["ts"])
            if prev_dt and curr_dt:
                window_hours = (curr_dt - prev_dt).total_seconds() / 3600
            if previous["holder_count"] and latest["holder_count"]:
                prev_h = previous["holder_count"]
                curr_h = latest["holder_count"]
                if prev_h > 0:
                    holder_growth_pct = (curr_h - prev_h) / prev_h * 100

        score_input = {
            **base,
            "holder_growth_pct": holder_growth_pct,
            "window_hours": window_hours,
        }

    score = accumulation.calculate(
        smart_degen_count=score_input.get("smart_degen_count"),
        holder_growth_pct=score_input.get("holder_growth_pct"),
        window_hours=score_input.get("window_hours"),
        top10_holder_rate=score_input.get("top10_holder_rate"),
        liquidity_usd=score_input.get("liquidity_usd"),
        is_honeypot=score_input.get("is_honeypot"),
        buy_tax=score_input.get("buy_tax"),
        sell_tax=score_input.get("sell_tax"),
        is_renounced=score_input.get("is_renounced"),
    )

    return {
        **base,
        "holder_growth_pct": score_input.get("holder_growth_pct"),
        "comparison_window_hours": score_input.get("window_hours"),
        # 兼容老字段：iOS Models 里有 volume_ratio，给 null 不会崩
        "volume_ratio": None,
        "accumulation_score": score,
    }


# ---------- K 线 ----------
_RESOLUTION_HOURS: dict[str, int] = {
    "1m": 6,
    "5m": 24,
    "15m": 72,
    "1h": 168,
    "4h": 720,
    "1d": 2160,
}


@app.get("/api/v1/token/{chain}/{address}/kline")
def token_kline(
    chain: str,
    address: str,
    resolution: str = Query("1h"),
    hours: int | None = Query(None, ge=1, le=24 * 365),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    require_auth(authorization)

    if hours is None:
        hours = _RESOLUTION_HOURS.get(resolution, 168)

    try:
        candles = gmgn_client.token_kline(chain, address, resolution=resolution, hours=hours)
    except gmgn_client.GMGNCliError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")

    normalized = []
    for c in candles:
        raw_ts = c.get("time") or c.get("timestamp") or c.get("ts") or 0
        try:
            raw_ts_int = int(raw_ts)
        except (TypeError, ValueError):
            continue
        ts_seconds = raw_ts_int // 1000 if raw_ts_int > 10_000_000_000 else raw_ts_int

        normalized.append({
            "ts": ts_seconds,
            "open":   _to_float(c.get("open")   or c.get("o")),
            "high":   _to_float(c.get("high")   or c.get("h")),
            "low":    _to_float(c.get("low")    or c.get("l")),
            "close":  _to_float(c.get("close")  or c.get("c")),
            "volume": _to_float(c.get("volume") or c.get("v")),
        })

    return {
        "chain": chain,
        "address": address,
        "resolution": resolution,
        "candles": normalized,
    }


# ---------- 共识持仓聚合 ----------
class HoldingsAggregateRequest(BaseModel):
    chain: str = Field(..., description="eth / sol / bsc / base")
    addresses: list[str] = Field(..., description="聪明钱地址列表")
    min_holders: int = Field(2, ge=1, le=20, description="至少被几个地址持有才返回")


@app.post("/api/v1/holdings/aggregate")
def holdings_aggregate(
    req: HoldingsAggregateRequest,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    require_auth(authorization)

    if len(req.addresses) > 100:
        raise HTTPException(status_code=400, detail="too many addresses (max 100)")

    return holdings_agg.aggregate(
        chain=req.chain,
        addresses=req.addresses,
        min_holders=req.min_holders,
    )


# ---------- 手动刷新 ----------
@app.post("/api/v1/refresh")
def manual_refresh(
    chain: str = Query("eth"),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    require_auth(authorization)
    from refresh import refresh as do_refresh
    try:
        n = do_refresh(chain=chain)
        return {"ok": True, "saved": n, "chain": chain}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))