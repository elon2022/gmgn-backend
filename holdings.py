"""
聪明钱共识持仓聚合。

输入：N 个钱包地址
输出：哪些代币被 ≥ min_holders 个钱包同时持有

性能策略：
- 单地址持仓缓存 5 分钟（同一地址多次查询直接走缓存）
- 多地址查询用线程池并发拉取（cli 调用是 IO 密集）

线程安全：cache 用 dict + threading.Lock，FastAPI 是单进程多线程模型够用。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import gmgn_client

# (chain, address) -> (timestamp, holdings_list)
_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_CACHE_TTL_SECONDS = 300  # 5 分钟
_CACHE_LOCK = threading.Lock()
_MAX_WORKERS = 8  # 同时最多 8 个 cli 进程，避免 CPU 爆炸


def _get_cached_holdings(chain: str, address: str) -> list[dict] | None:
    key = (chain, address)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL_SECONDS:
        return None
    return data


def _set_cached_holdings(chain: str, address: str, data: list[dict]) -> None:
    with _CACHE_LOCK:
        _CACHE[(chain, address)] = (time.time(), data)


def _fetch_one(chain: str, address: str) -> tuple[str, list[dict] | None, str | None]:
    """拉取单个地址持仓。返回 (address, holdings, error)。"""
    cached = _get_cached_holdings(chain, address)
    if cached is not None:
        return address, cached, None
    try:
        data = gmgn_client.wallet_holdings(chain, address)
        _set_cached_holdings(chain, address, data)
        return address, data, None
    except gmgn_client.GMGNCliError as e:
        return address, None, str(e)


def aggregate(
    chain: str, addresses: list[str], min_holders: int = 2
) -> dict[str, Any]:
    """
    聚合多个地址的持仓。
    返回结构见 main.py 路由文档。
    """
    if not addresses:
        return {"chain": chain, "min_holders": min_holders, "tokens": [], "errors": []}

    # 1. 并发拉持仓
    results: dict[str, list[dict]] = {}
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(addresses))) as pool:
        futures = {pool.submit(_fetch_one, chain, a): a for a in addresses}
        for fut in as_completed(futures):
            address, holdings, err = fut.result()
            if err:
                errors.append({"address": address, "error": err})
            else:
                results[address] = holdings or []

    # 2. 聚合：按 token_address 分组
    # token_key -> {token_meta, holders: set, total_usd: float}
    grouped: dict[str, dict] = {}
    for holder_addr, holdings in results.items():
        for h in holdings:
            token = _extract_token(h)
            if not token or not token.get("address"):
                continue
            token_addr = token["address"].lower()
            entry = grouped.setdefault(
                token_addr,
                {
                    "token": token,
                    "holders": set(),
                    "total_usd": 0.0,
                },
            )
            entry["holders"].add(holder_addr)
            entry["total_usd"] += _to_float(h.get("usd_value")) or 0.0

    # 3. 过滤 + 排序
    tokens_out = []
    for token_addr, entry in grouped.items():
        if len(entry["holders"]) < min_holders:
            continue
        tokens_out.append({
            "chain": entry["token"].get("chain") or chain,
            "address": entry["token"].get("address"),
            "symbol": entry["token"].get("symbol"),
            "name": entry["token"].get("name"),
            "logo_url": entry["token"].get("logo") or entry["token"].get("logo_url"),
            "holders_count": len(entry["holders"]),
            "total_value_usd": round(entry["total_usd"], 2),
            "holders": sorted(entry["holders"]),
        })

    # 排序：先按持有人数降序，相同人数按总价值降序
    tokens_out.sort(key=lambda x: (-x["holders_count"], -x["total_value_usd"]))

    return {
        "chain": chain,
        "min_holders": min_holders,
        "queried_addresses": len(addresses),
        "succeeded_addresses": len(results),
        "tokens": tokens_out,
        "errors": errors,
    }


def _extract_token(holding: dict) -> dict | None:
    """
    不同 cli 版本返回的持仓项结构不一致。
    可能是 {token: {...}, balance, usd_value} 也可能是平铺的。
    这里做最小兼容。
    """
    if "token" in holding and isinstance(holding["token"], dict):
        return holding["token"]
    # 平铺结构
    if "address" in holding or "token_address" in holding:
        return {
            "chain": holding.get("chain"),
            "address": holding.get("token_address") or holding.get("address"),
            "symbol": holding.get("symbol") or holding.get("token_symbol"),
            "name": holding.get("name") or holding.get("token_name"),
            "logo": holding.get("logo") or holding.get("logo_url"),
        }
    return None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clear_cache() -> int:
    """测试/手动用。返回清掉的条目数。"""
    with _CACHE_LOCK:
        n = len(_CACHE)
        _CACHE.clear()
    return n