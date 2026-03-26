"""
Legal Research MCP Server.
Exposes search_cases, read_judgment, find_citations, get_cases_by_statute
as MCP tools that Gemini CLI connects to via the extension system.
"""

import sys
import json
from pathlib import Path

# Add parent to path for tool imports
sys.path.append(str(Path(__file__).parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from agent.tools import (
    search_cases,
    read_judgment,
    find_citations,
    get_cases_by_statute,
    grep_judgments,
    get_context_around,
)

server = Server("legal-research")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_cases",
            description=(
                "Search across 38,033 Supreme Court of India judgments (1950–2026). "
                "Find cases by legal concept, statute, principle, party name, or judge. "
                "Returns titles, citations, truncated snippets, and outcome. "
                "WARNING: The 'judge' field is ONLY the authoring judge, NOT the full bench. "
                "WARNING: Snippets are truncated. You MUST call read_judgment on top 2-3 results before answering."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language or keyword query"
                    },
                    "year_from": {"type": "integer", "description": "Filter from year"},
                    "year_to":   {"type": "integer", "description": "Filter to year"},
                    "disposal_nature": {
                        "type": "string",
                        "description": "Outcome filter: 'Dismissed', 'Appeal(s) allowed', etc."
                    },
                    "judge":  {"type": "string", "description": "Filter by judge name"},
                    "court":  {"type": "string", "description": "Filter by court, partial name works: 'Bombay', 'Karnataka', 'Supreme'. Omit to search all courts."},
                    "top_k":  {"type": "integer", "description": "Results to return (max 20)"}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="read_judgment",
            description=(
                "MANDATORY after every search_cases call — read at least the top 2-3 results. "
                "Returns the judgment text with a [FULL BENCH: ...] header listing ALL judges. "
                "ALWAYS use this bench info instead of the 'judge' field from search_cases (which is only the author). "
                "section='held' gives ratio decidendi, 'facts' gives background, 'all' gives full text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Case path from search results e.g. '2026_1_1_23'"
                    },
                    "section": {
                        "type": "string",
                        "enum": ["all", "headnotes", "facts", "held"],
                        "description": "Section to read"
                    }
                },
                "required": ["path"]
            }
        ),
        types.Tool(
            name="find_citations",
            description=(
                "Find all Supreme Court judgments that cite a specific case. "
                "Searches full judgment text for case name mentions. "
                "Use to build chains of authority or track how landmark cases are applied."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "case_name": {
                        "type": "string",
                        "description": "Case name to find citations for e.g. 'Vidya Drolia' or 'Booz Allen'"
                    },
                    "year_from": {"type": "integer", "description": "Filter from year"},
                    "top_k":    {"type": "integer", "description": "Number of citing cases"}
                },
                "required": ["case_name"]
            }
        ),
        types.Tool(
            name="get_cases_by_statute",
            description=(
                "Find all judgments interpreting a specific Act or section of an Act. "
                "Use when query involves specific statutory provisions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "act":       {"type": "string", "description": "Act name e.g. 'Arbitration and Conciliation Act'"},
                    "section":   {"type": "string", "description": "Section number e.g. '11'"},
                    "year_from": {"type": "integer", "description": "Filter from year"},
                    "top_k":    {"type": "integer", "description": "Number of results"}
                },
                "required": ["act"]
            }
        ),
        types.Tool(
            name="grep_judgments",
            description=(
                "Exact phrase / regex search on judgment text files. "
                "RECOMMENDED: pass 'paths' from search_cases results to grep only those files. "
                "For OCR/pasted text: extract a SHORT 5-8 word distinctive phrase, spaces auto-become \\s+ "
                "so line-breaks in corpus don't block matching. Set multiline=true for cross-line spans."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "SHORT distinctive phrase (5-8 words) or regex. "
                                       "Spaces are auto-converted to \\s+ unless normalize_whitespace=false. "
                                       "For OCR text: pick a clean sub-phrase, avoid hyphenated line-breaks."
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of case path IDs from search_cases (e.g. ['2009_6_152_159']). "
                                       "When provided, grep runs only on these files."
                    },
                    "context_lines": {"type": "integer", "description": "Lines of context around each match (default 2)"},
                    "top_k":         {"type": "integer", "description": "Max files to return (default 5, max 20)"},
                    "multiline":     {
                        "type": "boolean",
                        "description": "If true, pattern can span multiple lines (rg --multiline). "
                                       "Use when matching text that wraps across lines in the corpus."
                    },
                    "normalize_whitespace": {
                        "type": "boolean",
                        "description": "If true (default), spaces in pattern become \\s+ to tolerate "
                                       "line-breaks and extra whitespace. Set false for hand-crafted regex."
                    }
                },
                "required": ["pattern"]
            }
        ),
        types.Tool(
            name="get_context_around",
            description=(
                "Get the paragraph context around an exact phrase in a specific judgment. "
                "Use after grep_judgments finds a case — read the surrounding context of a matched phrase. "
                "Returns ±2000 chars around the phrase, bounded by paragraph breaks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path":         {"type": "string", "description": "Case path e.g. '2009_6_152_159'"},
                    "phrase":       {"type": "string", "description": "Exact phrase to find context around"},
                    "window_chars": {"type": "integer", "description": "Characters of context on each side (default 2000)"}
                },
                "required": ["path", "phrase"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "search_cases":
            result = search_cases(**arguments)
        elif name == "read_judgment":
            result = read_judgment(**arguments)
        elif name == "find_citations":
            result = find_citations(**arguments)
        elif name == "get_cases_by_statute":
            result = get_cases_by_statute(**arguments)
        elif name == "grep_judgments":
            result = grep_judgments(**arguments)
        elif name == "get_context_around":
            result = get_context_around(**arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as e:
        result = {"error": str(e)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
