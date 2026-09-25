"""Review a chart before it's shown: a vision model looks at the rendered image and suggests fixes.

The browser draws the chart, sends a PNG plus a data summary here, and applies the returned changes.
Only presentation can change (title, chart type, visible range, y-axis range, a note); the data never does.
"""
import json

from llm import client, create
import usage

REVIEW_MODEL = "gpt-4o"  # needs image input

PROMPT = """You are reviewing a chart before it is shown to a financial analyst. The image is the chart as
drawn. The data summary below is exact. Check it like a careful analyst would:
- Can you read the trend the user asked about, or is it hidden (e.g. one huge value squashing everything else,
  a long run of zeros or blanks at the start or end, too many points to read)?
- Is the title specific and accurate? Are the units right? Only state units the data summary gives; if none
  are given, say "units not labelled in the workbook" in the note rather than guessing.
- Is line or bar the right form (line for a trend over time, bar for a few discrete periods)?
- Are labels, ticks and the legend readable?

You can only change presentation, never the numbers. Allowed changes:
- title: a better title (short, specific)
- kind: "line" or "bar"
- x_start, x_end: indexes (0-based, inclusive) of the periods to show by default, e.g. to skip leading zeros
  or leave out an outlier period; the user can still reset to the full range
- y_min, y_max: y-axis limits, e.g. to keep a single outlier from flattening the rest (points beyond the
  limits are cut off, so say so in the note)
- note: one or two sentences shown under the chart explaining anything the viewer must know (an outlier
  left out of view and its value, units, what the series is)
Return JSON only. If the chart is already good, return {{"verdict": "ok", "issues": [], "changes": {{}}}}.

User's question: {question}
Data summary: {summary}"""

_N = {"type": ["number", "null"]}
_S = {"type": ["string", "null"]}
_CHANGES = {"title": _S, "kind": {"type": ["string", "null"], "enum": ["line", "bar", None]},
            "x_start": {"type": ["integer", "null"]}, "x_end": {"type": ["integer", "null"]},
            "y_min": _N, "y_max": _N, "note": _S}
SCHEMA = {"type": "json_schema", "name": "chart_review", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["verdict", "issues", "changes"],
    "properties": {"verdict": {"type": "string", "enum": ["ok", "revise"]},
                   "issues": {"type": "array", "items": {"type": "string"}},
                   "changes": {"type": "object", "additionalProperties": False, "required": list(_CHANGES),
                               "properties": _CHANGES}}}}


def summarize(spec: dict) -> dict:
    """What the reviewer needs to know about the data, exactly (it can't read numbers off pixels reliably)."""
    out = {"title": spec.get("title"), "units": spec.get("units"), "kind": spec.get("kind"),
           "periods": len(spec.get("labels", [])), "first_label": (spec.get("labels") or [None])[0],
           "last_label": (spec.get("labels") or [None])[-1], "series": []}
    for s in spec.get("series", []):
        vals = [(i, v) for i, v in enumerate(s.get("data", [])) if isinstance(v, (int, float))]
        nz = [(i, v) for i, v in vals if v]
        if not vals:
            out["series"].append({"name": s.get("name"), "range": s.get("range"), "numeric_points": 0})
            continue
        top = sorted(vals, key=lambda iv: -abs(iv[1]))[:3]
        out["series"].append({
            "name": s.get("name"), "range": s.get("range"), "numeric_points": len(vals),
            "min": min(v for _, v in vals), "max": max(v for _, v in vals),
            "first_nonzero_index": nz[0][0] if nz else None, "last_nonzero_index": nz[-1][0] if nz else None,
            "largest_abs": [{"index": i, "label": spec["labels"][i], "value": v} for i, v in top]})
    return out


def review(spec: dict, image_data_url: str, question: str, file_id: int | None = None,
           session: str | None = None) -> dict:
    llm = client(interactive=False)
    r = create(llm, REVIEW_MODEL, text={"format": SCHEMA}, input=[{"role": "user", "content": [
        {"type": "input_text", "text": PROMPT.format(question=question or "(not given)",
                                                     summary=json.dumps(summarize(spec), default=str))},
        {"type": "input_image", "image_url": image_data_url}]}])
    if r.usage:
        usage.record(REVIEW_MODEL, r.usage, "chart review", file_id, session)
    out = json.loads(r.output_text)
    n = len(spec.get("labels", []))
    ch = {k: v for k, v in out.get("changes", {}).items() if v is not None}
    for k in ("title", "kind"):  # only report real changes
        if k in ch and ch[k] == spec.get(k):
            ch.pop(k)
    for k in ("x_start", "x_end"):  # keep indexes inside the data
        if k in ch:
            ch[k] = max(0, min(n - 1, int(ch[k])))
    if "x_start" in ch and "x_end" in ch and ch["x_start"] > ch["x_end"]:
        ch.pop("x_start"), ch.pop("x_end")
    out["changes"] = ch
    out["model"] = REVIEW_MODEL
    return out
