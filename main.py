"""
GMGN 后端 API 服务。
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
import refresh as refresh_mod

API_TOKEN = os.environ.get("API_TOKEN", "dev-token-change-me")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gmgn.db"

app = FastAPI(title="GMGN Backend", version="0.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------- 启动时跑数据库迁移 ----------
@app.on_event("startup")
def _on_startup() -> None:
    try:
        conn = refresh_mod.init_db()
        conn.close()
        print("[startup] db init/migrate ok")
    except Exception as e:
        print(f"[startup] db init/migrate failed: {e}")


# ---------- 工具 ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _to_num(v: Any) -> float | None:
    """
    上游 cli 经常把数字以字符串形式返回（如 "923793.21"）。
    iOS 期望 Double，下游 accumulation.calculate 也会做大小比较——
    必须统一转成 float（或 None）。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    latest_dt = _parse_iso(latest_ts)
    if not latest_dt:
        return None

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

    try:
        return _build_token_detail(chain, address)
    except HTTPException:
        raise  # 401/404 直接透传
    except Exception as e:
        # 防御兜底：详情页对错误最敏感（iOS 直接白屏 500）。
        # 把真实错误打到 stderr 让我们能在 journalctl 里看到，但仍返回 200，
        # 让 iOS 至少展示链 + 地址 + 错误说明，不至于完全白板。
        import traceback
        print(f"[token_detail] {chain}/{address} failed:", flush=True)
        traceback.print_exc()
        return {
            "chain": chain,
            "address": address,
            "symbol": None,
            "name": None,
            "logo_url": None,
            "price_usd": None,
            "price_change_pct": None,
            "price_change_1h": None,
            "volume_usd": None,
            "liquidity_usd": None,
            "market_cap": None,
            "holder_count": None,
            "smart_degen_count": None,
            "top10_holder_rate": None,
            "is_honeypot": None,
            "is_renounced": None,
            "buy_tax": None,
            "sell_tax": None,
            "twitter_url": None,
            "website_url": None,
            "telegram_url": None,
            "snapshot_ts": None,
            "previous_ts": None,
            "holder_growth_pct": None,
            "comparison_window_hours": None,
            "volume_ratio": None,
            "accumulation_score": None,
            "_error": f"{type(e).__name__}: {e}",
        }


def _build_token_detail(chain: str, address: str) -> dict[str, Any]:
    """token_detail 主体逻辑——抽出来方便上层做 try/except 兜底。"""
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
        try:
            info = gmgn_client.token_info(chain, address)
        except gmgn_client.GMGNCliError as e:
            raise HTTPException(status_code=404, detail=f"token not found: {e}")

        # 关键：上游 cli 在 sol/bsc/base 上常把数字以字符串返回，必须统一转 float
        # 同时 sol cli 不返回 market_cap 字段，要从 price * total_supply 算出来
        price = _to_num(info.get("price"))
        total_supply = _to_num(info.get("total_supply")) or _to_num(info.get("circulating_supply"))
        market_cap = _to_num(info.get("market_cap"))
        if market_cap is None and price is not None and total_supply is not None:
            market_cap = price * total_supply

        base = {
            "chain": chain,
            "address": address,
            "symbol": info.get("symbol"),
            "name": info.get("name"),
            "logo_url": info.get("logo"),
            "price_usd": price,
            "price_change_pct": _normalize_pct(info.get("price_change_percent5m")),
            "price_change_1h": _normalize_pct(info.get("price_change_percent1h")),
            "volume_usd": _to_num(info.get("volume")),
            "liquidity_usd": _to_num(info.get("liquidity")),
            "market_cap": market_cap,
            "holder_count": info.get("holder_count"),
            "smart_degen_count": info.get("smart_degen_count"),
            "top10_holder_rate": _to_num(info.get("top_10_holder_rate")),
            "is_honeypot": info.get("is_honeypot"),
            "is_renounced": info.get("is_renounced"),
            "buy_tax": _to_num(info.get("buy_tax")),
            "sell_tax": _to_num(info.get("sell_tax")),
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
            "price_usd": _to_num(latest["price_usd"]),
            "price_change_pct": _to_num(latest["price_change_5m"]),
            "price_change_1h": _to_num(latest["price_change_1h"]),
            "volume_usd": _to_num(latest["volume_usd"]),
            "liquidity_usd": _to_num(latest["liquidity_usd"]),
            "market_cap": _to_num(latest["market_cap"]),
            "holder_count": latest["holder_count"],
            "smart_degen_count": latest["smart_degen_count"],
            "top10_holder_rate": _to_num(latest["top10_holder_rate"]),
            "is_honeypot": bool(latest["is_honeypot"]) if latest["is_honeypot"] is not None else None,
            "is_renounced": bool(latest["is_renounced"]) if latest["is_renounced"] is not None else None,
            "buy_tax": _to_num(latest["buy_tax"]),
            "sell_tax": _to_num(latest["sell_tax"]),
            "twitter_url": latest["twitter_url"],
            "website_url": latest["website_url"],
            "telegram_url": latest["telegram_url"],
            "snapshot_ts": latest["ts"],
            "previous_ts": previous["ts"] if previous else None,
        }

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
        "volume_ratio": None,
        "accumulation_score": score,
    }


# ---------- K 线 ----------
_RESOLUTION_HOURS: dict[str, int] = {
    "1m": 6, "5m": 24, "15m": 72, "1h": 168, "4h": 720, "1d": 2160,
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


# ---------- 雷达信号（阶段 E 新增）----------
@app.get("/api/v1/radar/signals")
def radar_signals(
    hours: int = Query(24, ge=1, le=168, description="回溯多少小时的信号"),
    chain: str | None = Query(None, description="可选：只返回某条链的信号"),
    limit: int = Query(100, ge=1, le=500),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """
    返回最近触发的雷达信号，按时间倒序。
    """
    require_auth(authorization)

    cutoff = datetime.utcnow().isoformat(timespec="seconds") + "+00:00"
    # 上一行只是占位，下面用 SQL 算 cutoff
    sql = """
        SELECT id, chain, address, triggered_at,
               trigger_window, trigger_pct,
               price_usd, market_cap, liquidity_usd, volume_usd,
               smart_degen_count, holder_count, top10_holder_rate, is_honeypot,
               symbol, name, logo_url
          FROM radar_signals
         WHERE triggered_at >= datetime('now', ?)
    """
    params: list[Any] = [f'-{hours} hours']
    if chain:
        sql += " AND chain = ?"
        params.append(chain)
    sql += " ORDER BY triggered_at DESC LIMIT ?"
    params.append(limit)

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()

    signals = [
        {
            "id": r["id"],
            "chain": r["chain"],
            "address": r["address"],
            "triggered_at": r["triggered_at"],
            "trigger_window": r["trigger_window"],
            "trigger_pct": r["trigger_pct"],
            "price_usd": r["price_usd"],
            "market_cap": r["market_cap"],
            "liquidity_usd": r["liquidity_usd"],
            "volume_usd": r["volume_usd"],
            "smart_degen_count": r["smart_degen_count"],
            "holder_count": r["holder_count"],
            "top10_holder_rate": r["top10_holder_rate"],
            "is_honeypot": bool(r["is_honeypot"]) if r["is_honeypot"] is not None else None,
            "symbol": r["symbol"],
            "name": r["name"],
            "logo_url": r["logo_url"],
        }
        for r in rows
    ]

    return {
        "hours": hours,
        "chain": chain,
        "count": len(signals),
        "signals": signals,
    }


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