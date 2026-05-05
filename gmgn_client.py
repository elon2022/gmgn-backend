"""
封装 gmgn-cli 子进程调用，主程序通过这个模块调外部命令。
"""
import json
import os
import subprocess
import time
from typing import Any

GMGN_CLI = os.environ.get(
    "GMGN_CLI",
    "gmgn-cli",
)


class GMGNCliError(Exception):
    """gmgn-cli 调用或返回内容异常。"""


def _run(args: list[str]) -> dict[str, Any] | list:
    """
    跑 gmgn-cli 子命令，返回解析后的 data 部分。

    cli 返回有两种风格：
      A: {"code": 0, "data": {...}, "message": "success"}   ← trending / token info / wallet 用这种
      B: {"list": [...]}                                     ← kline 直接裸返回，没有 code 包装
    我们都要兼容。
    """
    cmd = [GMGN_CLI, *args, "--raw"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )
    except subprocess.CalledProcessError as e:
        raise GMGNCliError(f"gmgn-cli failed: {e.stderr or e.stdout}") from e
    except subprocess.TimeoutExpired as e:
        raise GMGNCliError("gmgn-cli timeout") from e

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise GMGNCliError(f"gmgn-cli bad JSON: {result.stdout[:200]}") from e

    # 兼容裸返回：没有 code 字段且不是字典 / 字典里没 code，直接当成数据返回
    if not isinstance(payload, dict):
        return payload
    if "code" not in payload:
        return payload   # 例如 kline 的 {"list":[...]}

    # 标准包装：检查 code
    if payload.get("code") != 0:
        raise GMGNCliError(f"gmgn-cli error: {payload.get('message')}")

    return payload.get("data", {})


def trending(chain: str, interval: str = "5m", limit: int = 50) -> list[dict]:
    data = _run(
        ["market", "trending", "--chain", chain, "--interval", interval, "--limit", str(limit)]
    )
    return data.get("rank", []) if isinstance(data, dict) else []


def token_info(chain: str, address: str) -> dict[str, Any]:
    data = _run(["token", "info", "--chain", chain, "--address", address])
    return data if isinstance(data, dict) else {}


def token_kline(
    chain: str,
    address: str,
    resolution: str = "1h",
    hours: int = 168,   # 默认拉 7 天历史
) -> list[dict]:
    """
    K 线。
    cli 用 --from / --to 时间戳范围（秒），不支持 --limit。
    返回：直接是 {"list":[{time(毫秒), open, high, low, close, volume(都是字符串)}, ...]}
    没有 code 字段包装。
    """
    now = int(time.time())
    from_ts = now - hours * 3600
    data = _run([
        "market", "kline",
        "--chain", chain,
        "--address", address,
        "--resolution", resolution,
        "--from", str(from_ts),
        "--to", str(now),
    ])
    if isinstance(data, list):
        return data
    return data.get("list") or data.get("klines") or []


def wallet_holdings(chain: str, address: str) -> list[dict]:
    """
    单个钱包的代币持仓列表。
    """
    data = _run(["wallet", "holdings", "--chain", chain, "--address", address])
    if isinstance(data, list):
        return data
    return data.get("holdings") or data.get("list") or []