"""
吸筹分计算。

满分 100 = 维度1 (聪明钱信号 30) + 维度2 (温和吸筹 30)
        + 维度3 (筹码结构 25) + 维度4 (流动性 15)
        - 风险扣分

返回结构：
    {
        "total": int 0-100,
        "components": {
            "smart_money": {"score": ..., "reason": "..."},
            "stealth_accumulation": {...},
            "holder_concentration": {...},
            "liquidity": {...},
        },
        "penalties": [{"score": -X, "reason": "..."}, ...],
    }
"""
from typing import Any


def _round(v: Any) -> int:
    return int(round(v)) if v is not None else 0


def _smart_money(smart_degen_count: int | None) -> dict:
    """聪明钱信号 (0-30)。每个聪明钱地址 +3，上限 30。"""
    n = smart_degen_count or 0
    score = min(n * 3, 30)
    if n == 0:
        reason = "暂无聪明钱地址持仓"
    elif n < 3:
        reason = f"{n} 个聪明钱地址持仓"
    else:
        reason = f"{n} 个聪明钱地址在持仓 ✓"
    return {"score": score, "max": 30, "reason": reason}


def _stealth_accumulation(
    holder_growth_pct: float | None,
    volume_ratio: float | None,
) -> dict:
    """
    温和吸筹模式 (0-30)。
    特征：持币人数在涨，但成交量没爆 → 庄家在静静收筹码。

    holder_growth_pct: 持币人数变化百分比（正数表示涨）
    volume_ratio:      当前 volume / 前一周期 volume（1.0 = 持平）
    """
    if holder_growth_pct is None or volume_ratio is None:
        return {
            "score": 0,
            "max": 30,
            "reason": "数据不足（需要至少 2 次快照对比）",
        }

    g = holder_growth_pct
    v = volume_ratio

    if g > 5 and 0.7 <= v <= 1.3:
        return {
            "score": 30,
            "max": 30,
            "reason": f"持币人数 +{g:.1f}%，量价平稳（×{v:.2f}）✓",
        }
    if g > 2 and 0.5 <= v <= 1.5:
        return {
            "score": 15,
            "max": 30,
            "reason": f"持币人数 +{g:.1f}%，量价较稳（×{v:.2f}）",
        }
    if g > 0:
        return {
            "score": 5,
            "max": 30,
            "reason": f"持币人数 +{g:.1f}%，但量价异常（×{v:.2f}）",
        }
    return {
        "score": 0,
        "max": 30,
        "reason": f"持币人数 {g:.1f}%，无吸筹迹象",
    }


def _holder_concentration(top10_rate: float | None) -> dict:
    """
    筹码结构 (0-25)。
    甜蜜区 30%-60%（有庄但不会一人砸盘）。
    """
    if top10_rate is None:
        return {"score": 0, "max": 25, "reason": "缺少持仓数据"}

    pct = top10_rate * 100 if top10_rate <= 1 else top10_rate

    if 30 <= pct <= 60:
        return {"score": 25, "max": 25, "reason": f"前 10 占比 {pct:.0f}%，健康区间 ✓"}
    if 20 <= pct < 30 or 60 < pct <= 75:
        return {"score": 10, "max": 25, "reason": f"前 10 占比 {pct:.0f}%，偏离最佳"}
    if pct < 20:
        return {"score": 0, "max": 25, "reason": f"前 10 占比仅 {pct:.0f}%，筹码过散"}
    return {"score": 0, "max": 25, "reason": f"前 10 占比 {pct:.0f}%，过度集中"}


def _liquidity(liquidity_usd: float | None) -> dict:
    """流动性 (0-15)。"""
    v = liquidity_usd or 0
    if v >= 500_000:
        return {"score": 15, "max": 15, "reason": f"流动性 ${v/1000:.0f}K ✓"}
    if v >= 100_000:
        return {"score": 8, "max": 15, "reason": f"流动性 ${v/1000:.0f}K"}
    return {"score": 0, "max": 15, "reason": f"流动性仅 ${v/1000:.1f}K，过低"}


def _penalties(
    is_honeypot: bool | None,
    buy_tax: float | None,
    sell_tax: float | None,
    is_renounced: bool | None,
) -> tuple[list[dict], bool]:
    """返回 (惩罚项列表, 是否归零)。"""
    items: list[dict] = []
    zero_out = False

    if is_honeypot:
        items.append({"score": -100, "reason": "蜜罐合约 ⚠️"})
        zero_out = True

    bt = (buy_tax or 0) * (100 if buy_tax and buy_tax <= 1 else 1)
    st = (sell_tax or 0) * (100 if sell_tax and sell_tax <= 1 else 1)
    if bt > 10 or st > 10:
        items.append({"score": -20, "reason": f"高税费（买 {bt:.0f}%/卖 {st:.0f}%）"})

    if is_renounced is False:
        items.append({"score": -10, "reason": "合约未 renounce"})

    return items, zero_out


def calculate(
    *,
    smart_degen_count: int | None,
    holder_growth_pct: float | None,
    volume_ratio: float | None,
    top10_holder_rate: float | None,
    liquidity_usd: float | None,
    is_honeypot: bool | None,
    buy_tax: float | None,
    sell_tax: float | None,
    is_renounced: bool | None,
) -> dict:
    components = {
        "smart_money": _smart_money(smart_degen_count),
        "stealth_accumulation": _stealth_accumulation(holder_growth_pct, volume_ratio),
        "holder_concentration": _holder_concentration(top10_holder_rate),
        "liquidity": _liquidity(liquidity_usd),
    }
    penalties, zero_out = _penalties(is_honeypot, buy_tax, sell_tax, is_renounced)

    if zero_out:
        total = 0
    else:
        total = sum(c["score"] for c in components.values())
        total += sum(p["score"] for p in penalties)
        total = max(0, min(100, total))

    return {
        "total": _round(total),
        "components": components,
        "penalties": penalties,
    }