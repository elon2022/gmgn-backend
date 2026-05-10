"""
破灭法目候选池构建。

候选池来源（每条链分别处理）：
  - trending（所有链都支持）
  - trenches（仅 sol/bsc/base，eth 不支持）
  - watched_tokens（用户关注池，所有链）

合并去重 + 轻量过滤后返回候选列表。
"""
import sqlite3
from typing import Any

import gmgn_client


# 哪些链支持 trenches
TRENCHES_SUPPORTED_CHAINS = {"sol", "bsc", "base"}

# 候选池轻量过滤范围（避免拉一堆死币 / 巨型币的 K 线）
CANDIDATE_MC_MIN = 200_000
CANDIDATE_MC_MAX = 50_000_000
CANDIDATE_LIQ_MIN = 20_000


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_address(chain: str, address: str) -> str:
    """EVM 链统一小写；Solana 大小写敏感，不能动。"""
    if chain in {"eth", "bsc", "base"}:
        return address.lower()
    return address


def _make_candidate(item: dict, chain: str, source: str) -> dict | None:
    """
    把 trending / trenches 返回的 item 标准化成候选格式。
    返回 None 表示这个 item 缺关键字段，跳过。
    """
    address = item.get("address") or item.get("token_address")
    if not address:
        return None

    mc = _to_float(item.get("market_cap") or item.get("usd_market_cap"))
    liquidity = _to_float(item.get("liquidity"))
    is_honeypot_raw = item.get("is_honeypot")
    # is_honeypot 在不同接口里可能是 bool / 0/1 / ""
    is_honeypot = None
    if isinstance(is_honeypot_raw, bool):
        is_honeypot = is_honeypot_raw
    elif isinstance(is_honeypot_raw, (int, float)):
        is_honeypot = bool(is_honeypot_raw)
    elif isinstance(is_honeypot_raw, str) and is_honeypot_raw not in ("", "false", "0"):
        is_honeypot = True

    return {
        "chain": chain,
        "address": _normalize_address(chain, str(address)),
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "logo_url": item.get("logo"),
        "current_mc": mc,
        "current_price": _to_float(item.get("price")),
        "liquidity_usd": liquidity,
        "holder_count": item.get("holder_count"),
        "total_supply": _to_float(item.get("total_supply")),
        "is_honeypot": is_honeypot,
        "source": source,
    }


def _passes_filter(c: dict) -> bool:
    """轻量过滤：明显不合格的直接砍，省 K 线调用。"""
    if c.get("is_honeypot"):
        return False
    mc = c.get("current_mc")
    if mc is not None and (mc < CANDIDATE_MC_MIN or mc > CANDIDATE_MC_MAX):
        return False
    liq = c.get("liquidity_usd")
    if liq is not None and liq < CANDIDATE_LIQ_MIN:
        return False
    # mc 缺失时不直接砍——可能是新币没数据，K 线阶段会再算
    return True


def _merge(existing: dict, new: dict) -> dict:
    """同一个 (chain,address) 在多个来源出现时，合并 source 字段，其他取非空更新。"""
    merged_source = existing.get("source", "")
    new_source = new.get("source", "")
    if new_source and new_source not in merged_source:
        merged_source = (merged_source + ";" + new_source).strip(";")
    out = dict(existing)
    for k, v in new.items():
        if v is not None and out.get(k) is None:
            out[k] = v
    out["source"] = merged_source
    return out


def fetch_watchlist(conn: sqlite3.Connection, chain: str) -> list[dict]:
    """从 watched_tokens 表拉某条链的关注池，返回最简候选（只有 chain+address）。"""
    rows = conn.execute(
        "SELECT chain, address FROM watched_tokens WHERE chain = ?",
        (chain,),
    ).fetchall()
    return [
        {
            "chain": r["chain"],
            "address": _normalize_address(r["chain"], r["address"]),
            "source": "watchlist",
        }
        for r in rows
    ]


def build_candidates(conn: sqlite3.Connection, chain: str) -> list[dict]:
    """
    构建某条链的候选池。
    返回去重 + 过滤后的候选列表。
    """
    pool: dict[str, dict] = {}   # key = address

    # ---- 来源 1：trending ----
    try:
        trending = gmgn_client.trending(chain=chain, interval="5m", limit=80)
        for item in trending:
            item.setdefault("chain", chain)
            cand = _make_candidate(item, chain, source="trending")
            if cand:
                addr = cand["address"]
                pool[addr] = _merge(pool[addr], cand) if addr in pool else cand
    except Exception as e:
        print(f"[v2/candidates] [{chain}] trending failed: {e}")

    # ---- 来源 2：trenches（仅部分链）----
    if chain in TRENCHES_SUPPORTED_CHAINS:
        try:
            trenches = gmgn_client.trenches(chain=chain, type_="completed", limit=80)
            for item in trenches:
                item.setdefault("chain", chain)
                cand = _make_candidate(item, chain, source="trenches")
                if cand:
                    addr = cand["address"]
                    pool[addr] = _merge(pool[addr], cand) if addr in pool else cand
        except Exception as e:
            print(f"[v2/candidates] [{chain}] trenches failed: {e}")

    # ---- 来源 3：用户关注池（永远纳入，不受过滤限制）----
    watchlist = fetch_watchlist(conn, chain)
    for w in watchlist:
        addr = w["address"]
        if addr in pool:
            # 已有：merge source
            existing = pool[addr]
            if "watchlist" not in (existing.get("source") or ""):
                existing["source"] = (existing.get("source", "") + ";watchlist").strip(";")
        else:
            # 新加：标记需要从 token info 补全
            pool[addr] = {
                "chain": chain,
                "address": addr,
                "source": "watchlist",
                "needs_info_fetch": True,    # 后续 K 线阶段要单独补一次 token info
            }

    # ---- 过滤 ----
    candidates = []
    for addr, c in pool.items():
        # 关注池的币不参与轻量过滤（必扫）
        if c.get("source", "").startswith("watchlist") or "watchlist" in c.get("source", ""):
            candidates.append(c)
        elif _passes_filter(c):
            candidates.append(c)
    candidates = [c for c in candidates if _passes_sol_suffix_filter(c)]
    return candidates




# radar_v2_candidates.py 末尾追加一个过滤函数

# SOL 上"安全后缀"白名单
SOL_SAFE_SUFFIXES = ("pump", "BAGS")

def _passes_sol_suffix_filter(c: dict) -> bool:
    """
    SOL 链：只保留 pump / BAGS 结尾的代币。
    用户关注池里的不过滤（永远纳入）。
    其他链不受影响。
    """
    if c["chain"] != "sol":
        return True

    # watchlist 来源的币豁免
    source = c.get("source", "")
    if "watchlist" in source:
        return True

    # 检查后缀（地址区分大小写，但后缀检查保险起见两种都试）
    addr = c.get("address", "")
    return any(addr.endswith(suf) for suf in SOL_SAFE_SUFFIXES)