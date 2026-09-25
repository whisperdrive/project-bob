"""Review a chart before it's shown: a vision model looks at the rendered image and suggests fixes.

Runs on the server inside the agent loop: chartrender.py draws the chart to PNG (no browser), the model reviews
it with an exact data summary, and apply() folds the changes into the spec before the chart is sent to the
page. Only presentation can change (title, chart type, visible range, y-axis range, a note); the data never does.
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
- title: a better title (short, specific); don't put dates in it unless they match the visible range
- kind: "line" or "bar"
- x_start, x_end: indexes (0-based, inclusive) of the periods to show by default, e.g. to skip leading zeros
  or leave out an outlier period; the user can still reset to the full range
- y_min, y_max: y-axis limits, e.g. to keep a single outlier from flattening the rest (points beyond the
  limits are cut off, so say so in the note)
- note: one or two sentences shown under the chart explaining anything the viewer must know (an outlier
  left out of view and its value, units, what the series is). Write numbers with thousands separators and
  sensible rounding (e.g. 3,691,698), never raw decimals.
To keep one outlier from flattening the rest, either end the visible range before its index (x_end =
index - 1) or set y_max a little above max_without_largest; check the indexes in the data summary.
Return JSON only. If the chart is already good, return {{"verdict": "ok", "issues": [], "changes": {{}}}}.

User's question: {question}
Data summary: {summary}
{current}"""

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
        rest = [v for i, v in vals if i != top[0][0]]
        out["series"].append({
            "name": s.get("name"), "range": s.get("range"), "numeric_points": len(vals),
            "min": min(v for _, v in vals), "max": max(v for _, v in vals),
            "first_nonzero_index": nz[0][0] if nz else None, "last_nonzero_index": nz[-1][0] if nz else None,
            "largest_abs": [{"index": i, "label": spec["labels"][i], "value": round(v, 2)} for i, v in top],
            "min_without_largest": round(min(rest), 2) if rest else None,
            "max_without_largest": round(max(rest), 2) if rest else None})
    return out


def _ask(llm, spec: dict, view: dict, question: str, file_id, session) -> dict:
    import base64
    from chartrender import render_png
    image = "data:image/png;base64," + base64.b64encode(render_png(spec, view)).decode()
    current = (f"This image already applies earlier review changes: view {json.dumps(view)}, title "
               f"'{spec.get('title')}', note '{spec.get('note') or ''}'. Check the result and return only further "
               f"changes, or verdict ok." if view or spec.get("note") else "")
    r = create(llm, REVIEW_MODEL, text={"format": SCHEMA}, input=[{"role": "user", "content": [
        {"type": "input_text", "text": PROMPT.format(question=question or "(not given)", current=current,
                                                     summary=json.dumps(summarize(spec), default=str))},
        {"type": "input_image", "image_url": image}]}])
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
    out["changes"] = ch
    return out


def review(spec: dict, question: str, file_id: int | None = None, session: str | None = None,
           interactive: bool = False, rounds: int = 2) -> dict:
    """Render on the server, have the vision model review it, apply, and let it look at the result once
    more (up to `rounds` passes). Returns the combined changes."""
    llm = client(interactive=interactive)
    spec = dict(spec)
    changes, issues, verdict = {}, [], "ok"
    for _ in range(rounds):
        view = {k: changes[k] for k in ("x_start", "x_end", "y_min", "y_max") if k in changes}
        out = _ask(llm, spec, view, question, file_id, session)
        issues += [i for i in out.get("issues", []) if i not in issues]
        if out.get("verdict") == "ok" or not out["changes"]:
            break
        verdict = "revise"
        changes.update(out["changes"])
        for k in ("title", "kind", "note"):
            if k in out["changes"]:
                spec[k] = out["changes"][k]
    if "x_start" in changes and "x_end" in changes and changes["x_start"] > changes["x_end"]:
        changes.pop("x_start"), changes.pop("x_end")
    return {"verdict": verdict, "issues": issues, "changes": changes, "model": REVIEW_MODEL}


def apply(spec: dict, res: dict) -> dict:
    """Fold a review result into the spec the page renders (title, kind, note, view, review summary)."""
    ch = res.get("changes", {})
    if ch.get("title"):
        spec["title"] = ch["title"]
    if ch.get("kind"):
        spec["kind"] = ch["kind"]
    if ch.get("note"):
        spec["note"] = ch["note"]
    spec["view"] = {k: ch.get(k) for k in ("x_start", "x_end", "y_min", "y_max") if ch.get(k) is not None}
    spec["title"] = _fix_title_years(spec)
    spec["review"] = {"verdict": res.get("verdict"), "issues": res.get("issues", []), "changed": list(ch),
                      "model": res.get("model")}
    return spec


def _fix_title_years(spec: dict) -> str:
    """If the title names years outside what's visible (e.g. "2017-2070" while the view ends in 2067),
    replace them with the visible range. Reviewers get this wrong often enough to check it in code."""
    import re
    title, labels, v = spec.get("title") or "", spec.get("labels") or [], spec.get("view") or {}
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", title)]
    shown = [int(m.group(1)) for lab in labels[v.get("x_start", 0):(v.get("x_end", len(labels) - 1)) + 1]
             if (m := re.match(r"(\d{4})", str(lab)))]
    if not years or not shown or all(min(shown) <= y <= max(shown) for y in years):
        return title
    lo, hi = min(shown), max(shown)
    yr = r"(?:19|20)\d{2}"
    for pat, rep in ((rf"\bfrom\s+{yr}\s+(?:to|until|-|–)\s+{yr}\b", f"from {lo} to {hi}"),
                     (rf"\b{yr}\s+to\s+{yr}\b", f"{lo} to {hi}"),
                     (rf"\b{yr}\s*[–-]\s*{yr}\b", f"{lo}–{hi}")):
        fixed = re.sub(pat, rep, title)
        if fixed != title:
            return fixed
    # single stray years: drop them and append the visible span
    return f"{re.sub(rf'\s*\(?\b{yr}\b\)?', '', title).strip()} ({lo}–{hi})"


def describe(spec: dict) -> str:
    """One line for the chat model so its answer matches what the user sees."""
    rv, v = spec.get("review") or {}, spec.get("view") or {}
    if rv.get("verdict") in (None, "skipped"):
        return "The chart was shown without a review."
    if not rv.get("changed"):
        return "A reviewer checked the chart and made no changes."
    bits = []
    if "x_start" in v or "x_end" in v:
        lab = spec["labels"]
        bits.append(f"shows periods {lab[v.get('x_start', 0)]} to {lab[v.get('x_end', len(lab) - 1)]} by default")
    if "y_min" in v or "y_max" in v:
        bits.append(f"y-axis limited to {v.get('y_min', 'auto')}..{v.get('y_max', 'auto')}")
    if spec.get("note"):
        bits.append(f"note shown under the chart: {spec['note']}")
    return (f"A reviewer adjusted how the chart is shown (data unchanged; the user can click Full range): "
            f"title '{spec.get('title')}'; " + "; ".join(bits))
