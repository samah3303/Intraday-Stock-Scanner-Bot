"""
AlphaQuant AI — Model Context Protocol (MCP) Stdio Server
==========================================================
Exposes 7 quantitative scanner tools via stdio MCP interface:
1. scan_market
2. get_scan_results
3. get_bot_status
4. get_system_logs
5. get_watchlist
6. analyze_stock
7. generate_morning_brief

Run: python mcp_server.py
"""

import asyncio
import logging
import sys
import json

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("AlphaQuant_MCP_Server")

server = Server("alphaquant-scanner-mcp")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """Expose 7 quantitative scanner tools to MCP clients."""
    return [
        types.Tool(
            name="scan_market",
            description="Trigger on-demand intraday 6-rule breakout scan across screened candidates.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="get_scan_results",
            description="Fetch the latest scan results, matched stocks, and AI confidence scores.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="get_bot_status",
            description="Get real-time operational status of AlphaQuant AI bot motor.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="get_system_logs",
            description="Fetch recent system logs from in-memory ring buffer.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="get_premarket_candidates",
            description="Fetch today's 08:45 AM pre-market screened candidate stocks.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="analyze_stock",
            description="Run DeepSeek Agent multi-step analysis on a specific NSE stock symbol.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE stock symbol (e.g. RELIANCE)"},
                    "close": {"type": "number", "description": "Latest close price"},
                    "open": {"type": "number", "description": "09:15 open price"},
                    "high": {"type": "number", "description": "09:15 high price"},
                    "low": {"type": "number", "description": "09:15 low price"},
                    "ema20": {"type": "number", "description": "5-min 20 EMA value"}
                },
                "required": ["symbol"]
            }
        ),
        types.Tool(
            name="generate_morning_brief",
            description="Generate a DeepSeek markdown morning market brief for Telegram.",
            inputSchema={
                "type": "object",
                "properties": {
                    "nifty_signal": {"type": "string", "description": "BULLISH or BEARISH"}
                }
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Execute MCP tool calls with lazy imports from app.py and shared modules."""
    args = arguments or {}

    if name == "scan_market":
        from app import run_strategy_scan, LATEST_SCAN_RESULTS
        run_strategy_scan()
        return [types.TextContent(type="text", text=json.dumps(LATEST_SCAN_RESULTS, indent=2))]

    elif name == "get_scan_results":
        from app import LATEST_SCAN_RESULTS
        return [types.TextContent(type="text", text=json.dumps(LATEST_SCAN_RESULTS, indent=2))]

    elif name == "get_bot_status":
        from app import BOT_STATUS, NSE_STOCKS, PREMARKET_CANDIDATES
        status = {
            "bot_status": BOT_STATUS,
            "master_stocks_cached": len(NSE_STOCKS),
            "premarket_candidates_count": len(PREMARKET_CANDIDATES)
        }
        return [types.TextContent(type="text", text=json.dumps(status, indent=2))]

    elif name == "get_system_logs":
        from app import SYSTEM_LOGS
        recent_logs = SYSTEM_LOGS[-50:] if SYSTEM_LOGS else []
        return [types.TextContent(type="text", text="\n".join(recent_logs))]

    elif name in ("get_premarket_candidates", "get_watchlist"):
        from app import PREMARKET_CANDIDATES
        return [types.TextContent(type="text", text=json.dumps({"candidates": PREMARKET_CANDIDATES, "count": len(PREMARKET_CANDIDATES)}, indent=2))]

    elif name == "analyze_stock":
        from shared.deepseek_agent import analyze_hit_agent
        symbol = str(args.get("symbol", "RELIANCE")).upper()
        hit = {
            "symbol": symbol,
            "open": float(args.get("open", 1000.0)),
            "high": float(args.get("high", 1020.0)),
            "low": float(args.get("low", 1000.0)),
            "close": float(args.get("close", 1018.0)),
            "ema20": float(args.get("ema20", 1010.0)),
            "wick_pct": 10.0
        }
        res = analyze_hit_agent(hit, nifty_bullish=True)
        return [types.TextContent(type="text", text=json.dumps(res, indent=2))]

    elif name == "generate_morning_brief":
        from shared.deepseek_agent import generate_morning_brief
        signal = str(args.get("nifty_signal", "BULLISH"))
        brief = generate_morning_brief(signal)
        return [types.TextContent(type="text", text=brief)]

    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    """Main entry point for MCP stdio server."""
    options = server.create_initialization_options()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


if __name__ == "__main__":
    asyncio.run(main())
