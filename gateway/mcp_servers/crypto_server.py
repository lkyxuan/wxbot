# -*- coding: utf-8 -*-
"""
加密货币行情 MCP 服务（示例产品）
==================================

把「查币价 / 查行情 / 查热门币」包装成 MCP 工具，供网关里的智能体调用。
数据来自 CoinGecko 公共接口（免费、无需 Key，有频率限制）。
设置环境变量 COINGECKO_API_KEY 后会自动带上 Demo Key，额度更高。

以后要接你自己的数据库 / 程序，照着下面的 @server.tool() 再加函数即可：
函数的参数类型和 docstring 会自动变成工具说明，AI 据此决定什么时候调用。

单独运行（调试用）：python crypto_server.py
"""

import os

import requests
from mcp.server.mcpserver import MCPServer

API_BASE = os.environ.get("COINGECKO_API_BASE", "https://api.coingecko.com/api/v3")
TIMEOUT = 15

server = MCPServer("crypto")

# 常见代号直接映射到 CoinGecko ID，省掉一次搜索；其余代号走 /search 兜底
COMMON_IDS = {
    "btc": "bitcoin", "eth": "ethereum", "usdt": "tether", "usdc": "usd-coin",
    "bnb": "binancecoin", "sol": "solana", "xrp": "ripple", "doge": "dogecoin",
    "ada": "cardano", "trx": "tron", "ton": "the-open-network", "avax": "avalanche-2",
    "link": "chainlink", "dot": "polkadot", "ltc": "litecoin", "bch": "bitcoin-cash",
    "sui": "sui", "pepe": "pepe", "shib": "shiba-inu", "arb": "arbitrum", "op": "optimism",
}


def _get(path, params=None):
    """请求 CoinGecko；失败时抛出带中文说明的异常，由 MCP 作为工具错误返回给 AI。"""
    headers = {"accept": "application/json"}
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if key:
        headers["x-cg-demo-api-key"] = key
    resp = requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 429:
        raise RuntimeError("CoinGecko 请求太频繁，请稍后再试")
    resp.raise_for_status()
    return resp.json()


def _resolve_id(symbol_or_name):
    """把 BTC / 比特币 / bitcoin 这类输入解析成 CoinGecko 的币种 ID。"""
    q = symbol_or_name.strip().lower()
    if q in COMMON_IDS:
        return COMMON_IDS[q]
    coins = _get("/search", {"query": q}).get("coins", [])
    if not coins:
        return None
    # 代号完全匹配优先，其次取市值排名最靠前的
    exact = [c for c in coins if (c.get("symbol") or "").lower() == q]
    pool = exact or coins
    pool.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
    return pool[0]["id"]


def _fmt_num(value):
    """大数字转成 1.23万亿 / 4.56亿 这种中文读法，便于在微信里阅读。"""
    if value is None:
        return "未知"
    v = float(value)
    for unit, size in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if abs(v) >= size:
            return f"{v / size:.2f}{unit}"
    return f"{v:,.6g}"


@server.tool()
def get_price(symbols: list[str], vs_currency: str = "usd") -> str:
    """查询一个或多个币种的最新价格和 24 小时涨跌幅。

    symbols: 币种代号或名称列表，例如 ["BTC", "ETH", "solana"]
    vs_currency: 计价货币，usd 或 cny 等，默认 usd
    """
    vs = vs_currency.strip().lower() or "usd"
    resolved, missing = {}, []
    for s in symbols[:20]:
        cid = _resolve_id(s)
        if cid:
            resolved[cid] = s
        else:
            missing.append(s)
    lines = []
    if resolved:
        data = _get("/simple/price", {
            "ids": ",".join(resolved),
            "vs_currencies": vs,
            "include_24hr_change": "true",
            "include_market_cap": "true",
        })
        for cid, original in resolved.items():
            row = data.get(cid)
            if not row or vs not in row:
                missing.append(original)
                continue
            change = row.get(f"{vs}_24h_change")
            change_text = f"{change:+.2f}%" if change is not None else "未知"
            lines.append(
                f"{original.upper()}（{cid}）：{row[vs]:,} {vs.upper()}，"
                f"24h {change_text}，市值 {_fmt_num(row.get(f'{vs}_market_cap'))} {vs.upper()}"
            )
    if missing:
        lines.append("没找到：" + "、".join(missing))
    return "\n".join(lines) or "没有查到任何币种"


@server.tool()
def get_coin_detail(symbol: str, vs_currency: str = "usd") -> str:
    """查询单个币种的详细行情：排名、价格、24h/7d 涨跌、成交量、历史最高价等。

    symbol: 币种代号或名称，例如 "ETH"
    vs_currency: 计价货币，默认 usd
    """
    vs = vs_currency.strip().lower() or "usd"
    cid = _resolve_id(symbol)
    if not cid:
        return f"没找到币种：{symbol}"
    rows = _get("/coins/markets", {
        "vs_currency": vs,
        "ids": cid,
        "price_change_percentage": "24h,7d",
    })
    if not rows:
        return f"没有 {symbol} 的行情数据"
    r = rows[0]

    def pct(key):
        v = r.get(key)
        return f"{v:+.2f}%" if v is not None else "未知"

    unit = vs.upper()
    return "\n".join([
        f"{r.get('name')}（{(r.get('symbol') or '').upper()}），市值排名 #{r.get('market_cap_rank')}",
        f"价格：{r.get('current_price'):,} {unit}",
        f"24h：{pct('price_change_percentage_24h_in_currency')}，7d：{pct('price_change_percentage_7d_in_currency')}",
        f"24h 区间：{r.get('low_24h')} ~ {r.get('high_24h')} {unit}",
        f"市值：{_fmt_num(r.get('market_cap'))} {unit}，24h 成交量：{_fmt_num(r.get('total_volume'))} {unit}",
        f"历史最高：{r.get('ath')} {unit}（距今 {pct('ath_change_percentage')}）",
    ])


@server.tool()
def get_trending() -> str:
    """查询 CoinGecko 上最近 24 小时搜索最热门的币种。"""
    coins = _get("/search/trending").get("coins", [])[:10]
    if not coins:
        return "暂时没有热门币数据"
    lines = []
    for i, c in enumerate(coins, 1):
        item = c.get("item", {})
        rank = item.get("market_cap_rank")
        lines.append(f"{i}. {item.get('name')}（{(item.get('symbol') or '').upper()}），市值排名 #{rank if rank else '未知'}")
    return "\n".join(lines)


if __name__ == "__main__":
    server.run()
