"""
吸筹分计算。

满分 100 = 维度1 (聪明钱信号 30) + 维度2 (温和吸筹 30)
        + 维度3 (筹码结构 25) + 维度4 (流动性 15)
        - 风险扣分

关键改动（v0.4）：
- 温和吸筹改成只看 holder_growth_pct，按时间窗口自适应阈值
- 去掉 volume_ratio（GMGN volume 字段语义不明确，5min 采样下噪声太大）
- 新增 comparison_window_hours 入参：让公式知道是基于多长时间的数据
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
    window_hours: float | None,
) -> dict:
    """
    温和吸筹模式 (0-30)。

    阈值随时间窗口自适应：
    - 窗口 < 1h        → 数据不足，0 分（不算扣分，等数据攒够）
    - 1h <= 窗口 < 4h  → > 0.5% 满分；> 0.2% 半分；> 0  少量；其他 0
    - 4h <= 窗口 < 24h → > 2%   满分；> 0.8% 半分；> 0  少量；其他 0
    - 窗口 >= 24h      → > 5%   满分；> 2%   半分；> 0  少量；其他 0

    成交量维度被移除（5 分钟采样下 GMGN 的 volume 字段噪声过大，
    且字段含义模糊，比例不可靠）。
    """
    if holder_growth_pct is None or window_hours is None:
        return {
            "score": 0,
            "max": 30,
            "reason": "数据收集中（需要至少 1 小时历史）",
        }

    if window_hours < 1.0:
        return {
            "score": 0,
            "max": 30,
            "reason": f"数据收集中（已收集 {_format_hours(window_hours)}）",
        }

    g = holder_growth_pct

    # 按窗口大小分档
    if window_hours >= 24:
        full_thresh, half_thresh = 5.0, 2.0
        window_label = "24h"
    elif window_hours >= 4:
        full_thresh, half_thresh = 2.0, 0.8
        window_label = f"{int(window_hours)}h"
    else:
        full_thresh, half_thresh = 0.5, 0.2
        window_label = _format_hours(window_hours)

    if g >= full_thresh:
        return {
            "score": 30, "max": 30,
            "reason": f"持币人数 {window_label} 内 +{g:.2f}% ✓ 强吸筹",
        }
    if g >= half_thresh:
        return {
            "score": 15, "max": 30,
            "reason": f"持币人数 {window_label} 内 +{g:.2f}%，温和吸筹",
        }
    if g > 0:
        return {
            "score": 5, "max": 30,
            "reason": f"持币人数 {window_label} 内 +{g:.2f}%，弱信号",
        }
    return {
        "score": 0, "max": 30,
        "reason": f"持币人数 {window_label} 内 {g:+.2f}%，无吸筹迹象",
    }


def _format_hours(h: float) -> str:
    """把小数小时格式化成易读字符串。"""
    if h < 1:
        return f"{int(h * 60)}min"
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:.1f}h"


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
    window_hours: float | None,
    top10_holder_rate: float | None,
    liquidity_usd: float | None,
    is_honeypot: bool | None,
    buy_tax: float | None,
    sell_tax: float | None,
    is_renounced: bool | None,
) -> dict:
    components = {
        "smart_money": _smart_money(smart_degen_count),
        "stealth_accumulation": _stealth_accumulation(holder_growth_pct, window_hours),
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
        "window_hours": window_hours,
    }