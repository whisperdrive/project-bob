"""Tool-use loop: a Foundry model answers questions about a workbook using the tools in tools.py.

ask() is a generator of events (dicts) so a UI can show each tool call as it happens:
    {"type": "tool_call", "name", "args"}   {"type": "tool_result", "name", "output", "chars"}
    {"type": "answer", "text"}              {"type": "usage", "input_tokens", "cached_tokens", "output_tokens", "calls"}
    {"type": "error", "text"}
"""
import json

import tools
import chartreview
import ratelimit
from llm import client, create

MAX_STEPS = 12  # model turns per question; each turn may call several tools

TOOLS = [
    {"type": "function", "name": "find",
     "description": "Search line items whose label or section contains text (case-insensitive). "
                    "Returns Sheet!rN label [units] and sample values.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"},
         "sheet": {"type": "string", "description": "optional sheet name to restrict the search"}},
         "required": ["text"]}},
    {"type": "function", "name": "rows",
     "description": "Full detail for rows r1..r2 of a sheet: formula patterns (R1C1), counts, sample values.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "r1": {"type": "integer"}, "r2": {"type": "integer"}},
         "required": ["sheet", "r1"]}},
    {"type": "function", "name": "trace",
     "description": "Dependency tree of a line item. direction 'up' = precedents (what feeds it), "
                    "'down' = dependents (what it feeds). Follows what the current scenario actually uses; "
                    "lookup candidates that aren't selected are counted, not listed, unless include_inactive.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "row": {"type": "integer"},
         "direction": {"type": "string", "enum": ["up", "down"]},
         "depth": {"type": "integer", "description": "levels to follow, default 2"},
         "include_inactive": {"type": "boolean", "description": "also list rows a SUMIFS / INDEX-MATCH / "
                              "CHOOSE considers but doesn't select in the current scenario (default false)"}},
         "required": ["sheet", "row"]}},
    {"type": "function", "name": "cells",
     "description": "Raw cells (formula and cached value) in an A1 range, e.g. sheet='Valuation', "
                    "addr_from='K100', addr_to='P105'. Max 80 cells.",
     "parameters": {"type": "object", "properties": {
         "sheet": {"type": "string"}, "addr_from": {"type": "string"}, "addr_to": {"type": "string"}},
         "required": ["sheet", "addr_from"]}},
    {"type": "function", "name": "chart",
     "description": "Draw an interactive chart for the user from workbook cells. Pass cell ranges, never "
                    "typed-out numbers: values are read from the workbook. Each series is one row or one column "
                    "range. Period labels come from the sheet's timeline automatically for row ranges. Returns a "
                    "short summary (first/last/min/max/total) of what was drawn.",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"},
         "series": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
             "range": {"type": "string", "description": "e.g. Valuation!L95:AO95"},
             "name": {"type": "string", "description": "legend label; defaults to the line item label"}},
             "required": ["range"]}},
         "kind": {"type": "string", "enum": ["line", "bar"]},
         "units": {"type": "string", "description": "only if the workbook states them (the row's units column, "
                                                    "or the sheet's unit header), e.g. A$'000; omit if unsure"},
         "x_range": {"type": "string", "description": "optional range holding the x-axis labels"},
         "partial_ok": {"type": "boolean", "description": "set true only if the user asked for part of the "
                        "timeline (e.g. actuals only); otherwise a series covering part of the timeline is "
                        "held back with a warning so you can chart the full row instead"}},
         "required": ["title", "series"]}},
    {"type": "function", "name": "sql",
     "description": "Read-only SQLite query. Tables: cells(sheet,row,col,addr,formula,value), "
                    "rows(sheet,row,section,label,units,n_formula,n_const,patterns,samples), "
                    "edges(src_sheet,src_row,dst_sheet,dst_row) meaning src depends on dst, "
                    "names(name,ref), sheets(sheet,state,layout,summary). Max 50 rows returned.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

SYSTEM = """You answer questions about an Excel financial model. You cannot see the workbook directly;
use the tools to look things up. Work efficiently: find the relevant line items, check values with
rows/cells, and follow dependencies with trace. Cite line items as Sheet!rN (and cells as Sheet!A1)
so the user can check them. Give numbers with units and the period they refer to. If the tools don't
show something, say so rather than guessing.

When the user asks for a chart, plot or graph, or a trend would be clearer as one, find the right line
item and its timeline columns, then call the chart tool with those cell ranges. The chart appears in the
chat, so don't write plotting code, SVG or tables of the charted numbers; describe what it shows instead.
State units only when the workbook gives them; otherwise say the units aren't labelled.
For anything over time, chart the series that covers the whole timeline, actuals and forecast together:
usually a calculated row on an operations or valuation sheet, not an input or actuals row, and not an
"ex. historicals" row unless the user asks for that. The chart shades the model's Actuals / Business plan /
Forecast periods automatically. If the chart tool says a series only covers part of the timeline, chart the
suggested full rows instead (or set partial_ok if the user asked for that part only).

Workbook overview:
"""


class _HeldBack(Exception):
    """A chart that wasn't shown (e.g. it only covers part of the timeline); the message goes to the model."""


def _run_tool(name: str, args: dict) -> str:
    fn = {"find": tools.find, "rows": tools.rows, "trace": tools.trace,
          "cells": tools.cells, "sql": tools.sql}.get(name)
    if fn is None:
        return f"unknown tool {name}"
    try:
        return fn(**args)
    except Exception as e:  # tool errors go back to the model so it can correct itself
        return f"error: {type(e).__name__}: {e}"


def ask(question: str, db_path: str, model: str, history: list | None = None, interactive: bool = True,
        context: str = "", on_usage=None, file_id: int | None = None, session: str | None = None):
    """history: earlier [{"role": "user"|"assistant", "content": str}] turns of this conversation.
    context: extra text for the instructions (e.g. confirmed target / valuation date, changes vs last version).
    on_usage(model, usage): called after every model response, so tokens are logged even if a later call fails."""
    # Each tool call runs inside tools.using(), so concurrent questions on different workbooks don't clash.
    with tools.using(db_path):
        instructions = SYSTEM + tools.overview() + (f"\n\n{context}" if context else "")
    items: list = [*(history or []), {"role": "user", "content": question}]
    usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "calls": 0}
    llm = client(interactive)
    for _ in range(MAX_STEPS):
        wait = ratelimit.wait_needed(model, ratelimit.estimate_tokens(instructions, items, TOOLS))
        if wait:
            lim = ratelimit.limits(model)
            yield {"type": "waiting", "seconds": round(wait), "model": model,
                   "tpm_budget": lim["tpm_budget"], "rpm_budget": lim["rpm_budget"]}
        r = create(llm, model, instructions=instructions, input=items, tools=TOOLS)
        usage["calls"] += 1
        if on_usage and r.usage:
            on_usage(model, r.usage)
        if r.usage:
            usage["input_tokens"] += r.usage.input_tokens
            usage["output_tokens"] += r.usage.output_tokens
            details = getattr(r.usage, "input_tokens_details", None)
            usage["cached_tokens"] += getattr(details, "cached_tokens", 0) or 0
        calls = [o for o in r.output if o.type == "function_call"]
        if not calls:
            yield {"type": "answer", "text": r.output_text}
            yield {"type": "usage", **usage}
            return
        items += r.output  # includes reasoning items, which reasoning models need sent back
        for c in calls:
            args = json.loads(c.arguments or "{}")
            yield {"type": "tool_call", "name": c.name, "args": args}
            with tools.using(db_path):
                if c.name == "chart":
                    try:
                        partial_ok = bool(args.pop("partial_ok", False))
                        spec = tools.chart(**args)
                        note = tools.chart_note(spec)
                        if "WARNING:" in note and not partial_ok:
                            # Don't show a half-empty chart; let the model redraw with the full rows.
                            raise _HeldBack(note + "\nThis chart was NOT shown to the user. Chart the full-timeline "
                                                   "rows instead, or call chart again with partial_ok=true if the "
                                                   "user asked for this part of the timeline only.")
                        # Review before showing: render on the server, vision-model check, apply fixes.
                        yield {"type": "chart_review", "title": spec.get("title")}
                        try:
                            spec = chartreview.apply(spec, chartreview.review(
                                spec, question, file_id, session, interactive))
                        except Exception as e:
                            spec["review"] = {"verdict": "skipped", "changed": [], "model": None,
                                              "issues": [f"Review unavailable: {type(e).__name__}"]}
                        yield {"type": "chart", "spec": spec}
                        out = tools.chart_note(spec) + "\n" + chartreview.describe(spec)
                    except _HeldBack as e:
                        out = str(e)
                    except Exception as e:
                        out = f"error: {type(e).__name__}: {e}"
                else:
                    out = _run_tool(c.name, args)
            yield {"type": "tool_result", "name": c.name, "output": out, "chars": len(out)}
            items.append({"type": "function_call_output", "call_id": c.call_id, "output": out})
    yield {"type": "error", "text": f"Stopped after {MAX_STEPS} model turns without a final answer."}
    yield {"type": "usage", **usage}
