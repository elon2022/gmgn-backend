"""
封装 gmgn-cli 子进程调用，主程序通过这个模块调外部命令。
"""
import json
import os
import subprocess
from typing import Any

# GMGN_CLI = os.environ.get(
#     "GMGN_CLI",
#     "/Users/liuyangyang/.local/share/fnm/node-versions/v24.14.0/installation/bin/gmgn-cli",
# )

GMGN_CLI = os.environ.get(
    "GMGN_CLI",
    "gmgn-cli",
)

class GMGNCliError(Exception):
    """gmgn-cli 调用或返回内容异常。"""


def _run(args: list[str]) -> dict[str, Any] | list:
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
    chain: str, address: str, resolution: str = "1h", limit: int = 200
) -> list[dict]:
    data = _run(
        [
            "market", "kline",
            "--chain", chain,
            "--address", address,
            "--resolution", resolution,
            "--limit", str(limit),
        ]
    )
    if isinstance(data, list):
        return data
    return data.get("list") or data.get("klines") or []


def wallet_holdings(chain: str, address: str) -> list[dict]:
    """
    单个钱包的代币持仓列表。
    返回每条形如：
        {
          "token": {"chain", "address", "symbol", "name", "logo"},
          "balance": ...,
          "usd_value": ...,
          "price": ...
        }
    不同 cli 版本返回结构有差异，这里做最小兼容。
    """
    data = _run(["wallet", "holdings", "--chain", chain, "--address", address])
    if isinstance(data, list):
        return data
    # 常见包装：{holdings: [...]} 或 {list: [...]}
    return data.get("holdings") or data.get("list") or []