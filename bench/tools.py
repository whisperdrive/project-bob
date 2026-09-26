"""Tools an agent can call against out/<workbook>/model.db (built by build_map.py).

Call use(<workbook path or model.db path>) first. Each function returns a compact string - that
string is exactly what would be sent to Claude.
"""
import os
import re
import sqlite3
import threading
from collections import deque
from contextlib import contextmanager

DB = None
_local = threading.local()  # per-thread override set by using(), so concurrent requests don't clash
_JUNK_NAME = re.compile(r"^(_|EV__|CIQ|IQ_|Cell)", re.I)


def use(path: str) -> None:
    """Point the tools at a workbook's model.db (accepts the workbook path or the db path)."""
    global DB
    if not path.endswith(".db"):
        path = os.path.join("out", os.path.splitext(os.path.basename(path))[0], "model.db")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found - run build_map.py on the workbook first")
    DB = path


@contextmanager
def using(db_path: str):
    """Point tool calls in this thread at db_path for the duration of the block."""
    prev, _local.db = getattr(_local, "db", None), db_path
    try:
        yield
    finally:
        _local.db = prev


def _path() -> str:
    path = getattr(_local, "db", None) or DB
    if path is None:
        raise RuntimeError("call tools.use(<workbook>) first")
    return path


_SQUASH = re.compile(r"[\s\-_./&()']+")


def _squash(text) -> str:
    """Lower-case text with spaces and common punctuation removed ("Cash-flow (ex. hist)" -> "cashflowexhist")."""
    return _SQUASH.sub("", str(text).lower()) if text is not None else ""


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{_path()}?mode=ro", uri=True)
    db.create_function("squash", 1, _squash, deterministic=True)
    return db


def _label(db, sheet: str, row: int) -> str:
    r = db.execute("SELECT label, units FROM rows WHERE sheet=? AND row=?", (sheet, row)).fetchone()
    return f"{r[0]} [{r[1]}]" if r else "?"


def overview() -> str:
    """Level-0 map: sheets, sections, useful named ranges, model checks. Send this first (and cache it)."""
    db = _db()
    name = os.path.basename(os.path.dirname(_path()))
    out = [f"Workbook: {name}",
           "Each sheet lists its detected layout (label/units columns, timeline span). Refer to line items",
           "as Sheet!rN. Use tools find/rows/trace/cells/sql for detail.", ""]
    sheets = db.execute("""SELECT s.sheet, s.state, s.summary, COUNT(r.row), SUM(r.n_formula), SUM(r.n_const)
                           FROM sheets s LEFT JOIN rows r ON r.sheet = s.sheet
                           GROUP BY s.sheet ORDER BY MIN(s.rowid)""").fetchall()
    for sheet, state, summary, n, nf, nc in sheets:
        if not n:
            continue  # empty divider / cover sheets
        hidden = "" if state == "visible" else f" ({state})"
        out.append(f"## {sheet}{hidden}: {n} line items, {nf} formula cells, {nc} constants; {summary}")
        secs = db.execute("""SELECT section, MIN(row), MAX(row) FROM rows WHERE sheet=?
                             GROUP BY section ORDER BY MIN(row)""", (sheet,)).fetchall()
        out.append("  sections: " + "; ".join(f"{s or '-'} r{a}-{b}" for s, a, b in secs))
    names = [f"{n}={ref}" for n, ref in db.execute("SELECT name, ref FROM names ORDER BY name")
             if not _JUNK_NAME.match(n) and "#REF" not in ref and "!" in ref]
    out.append(f"\nNamed ranges ({len(names)}): " + ", ".join(names))
    return "\n".join(out)


def find(text: str, sheet: str | None = None, limit: int = 25) -> str:
    """Line items whose label or section contains text (case-insensitive; spaces and punctuation ignored,
    so "cash flow" also finds "Cashflow")."""
    db = _db()
    q = ("SELECT sheet,row,section,label,units,samples FROM rows WHERE (label LIKE ? OR section LIKE ? "
         "OR squash(label) LIKE ? OR squash(section) LIKE ?)")
    key = _squash(text)
    args: list = [f"%{text}%", f"%{text}%", f"%{key}%", f"%{key}%"]
    if sheet:
        q += " AND sheet=?"
        args.append(sheet)
    res = db.execute(q + " ORDER BY rowid LIMIT ?", (*args, limit + 1)).fetchall()
    lines = [f"{s}!r{r} {lab} [{u}] (in: {sec}) {('| eg ' + sm) if sm else ''}"
             for s, r, sec, lab, u, sm in res[:limit]]
    if len(res) > limit:
        lines.append(f"... more than {limit} matches; narrow the search")
    return "\n".join(lines) or "no matches"


def rows(sheet: str, r1: int, r2: int | None = None) -> str:
    """Full detail (formula patterns in R1C1, counts, samples) for rows r1..r2 of a sheet."""
    db = _db()
    res = db.execute("""SELECT row,label,units,n_formula,n_const,patterns,samples FROM rows
                        WHERE sheet=? AND row BETWEEN ? AND ? ORDER BY row LIMIT 60""",
                     (sheet, r1, r2 or r1)).fetchall()
    return "\n".join(f"{sheet}!r{r} {lab} [{u}] f={nf} c={nc} | {p} | eg {sm}"
                     for r, lab, u, nf, nc, p, sm in res) or "no line items in range"


def trace(sheet: str, row: int, direction: str = "up", depth: int = 2, limit: int = 60) -> str:
    """Line-item dependency tree. up = precedents (what feeds it), down = dependents (what it feeds)."""
    db = _db()
    if direction == "up":
        q = "SELECT dst_sheet, dst_row FROM edges WHERE src_sheet=? AND src_row=?"
    else:
        q = "SELECT src_sheet, src_row FROM edges WHERE dst_sheet=? AND dst_row=?"
    out = [f"{sheet}!r{row} {_label(db, sheet, row)}"]
    seen = {(sheet, row)}
    queue = deque([(sheet, row, 0)])
    while queue and len(out) < limit:
        s, r, d = queue.popleft()
        if d >= depth:
            continue
        for ns, nr in sorted(db.execute(q, (s, r)).fetchall()):
            if (ns, nr) in seen:
                continue
            seen.add((ns, nr))
            out.append(f"{'  ' * (d + 1)}{'<-' if direction == 'up' else '->'} {ns}!r{nr} {_label(db, ns, nr)}")
            queue.append((ns, nr, d + 1))
            if len(out) >= limit:
                out.append(f"... truncated at {limit}")
                break
    return "\n".join(out)


def cells(sheet: str, addr_from: str, addr_to: str | None = None, limit: int = 80) -> str:
    """Raw cells (formula + cached value) in an A1 range, e.g. cells('Valuation','K100','P105')."""
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    c1, r1 = coordinate_from_string(addr_from)
    c2, r2 = coordinate_from_string(addr_to or addr_from)
    db = _db()
    res = db.execute("""SELECT addr, formula, value FROM cells WHERE sheet=? AND row BETWEEN ? AND ?
                        AND col BETWEEN ? AND ? ORDER BY row, col LIMIT ?""",
                     (sheet, r1, r2, column_index_from_string(c1), column_index_from_string(c2), limit)).fetchall()
    return "\n".join(f"{a}: {f + ' -> ' if f else ''}{v}" for a, f, v in res) or "empty"


def sql(query: str, limit: int = 50) -> str:
    """Read-only SQL over tables cells(sheet,row,col,addr,formula,value), rows(...), edges(...), names(...)."""
    db = _db()
    cur = db.execute(query)
    cols = [d[0] for d in cur.description]
    res = cur.fetchmany(limit + 1)
    lines = ["\t".join(cols)] + ["\t".join(str(x) for x in r) for r in res[:limit]]
    if len(res) > limit:
        lines.append(f"... truncated at {limit} rows")
    return "\n".join(lines)


MAX_POINTS = 400
_RANGE = re.compile(r"^'?(?P<sheet>[^!]+?)'?!\$?(?P<c1>[A-Z]{1,3})\$?(?P<r1>\d+)(?::\$?(?P<c2>[A-Z]{1,3})\$?(?P<r2>\d+))?$")


def _range_cells(db, ref: str) -> tuple[str, list[tuple[int, int]]]:
    """'Valuation!L95:AO95' -> (sheet, [(row, col), ...]) for a single row or single column range."""
    from openpyxl.utils.cell import column_index_from_string
    m = _RANGE.match(ref.strip().replace(" ", ""))
    if not m:
        raise ValueError(f"not a cell range: {ref!r} (use Sheet!L95:AO95)")
    sheet, r1 = m["sheet"], int(m["r1"])
    c1 = column_index_from_string(m["c1"])
    r2, c2 = (int(m["r2"]), column_index_from_string(m["c2"])) if m["c2"] else (r1, c1)
    if r1 != r2 and c1 != c2:
        raise ValueError(f"{ref}: use one row or one column per series")
    pts = [(r1, c) for c in range(c1, c2 + 1)] if r1 == r2 else [(r, c1) for r in range(r1, r2 + 1)]
    if len(pts) > MAX_POINTS:
        raise ValueError(f"{ref}: {len(pts)} points; chart at most {MAX_POINTS}")
    if not db.execute("SELECT 1 FROM sheets WHERE sheet=?", (sheet,)).fetchone():
        raise ValueError(f"no sheet named {sheet!r}")
    return sheet, pts


def _period_label(v) -> str:
    s = str(v)
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else s[:24]


def chart(title: str, series: list[dict], kind: str = "line", x_range: str | None = None,
          units: str | None = None) -> dict:
    """Data for a chart the UI draws. Each series is {"range": "Sheet!L95:AO95", "name": optional}.
    Values are read from the workbook; x labels come from x_range or the sheet's timeline header row."""
    import json as _json
    db = _db()
    out_series, labels = [], None
    for s in series[:8]:
        sheet, pts = _range_cells(db, s["range"])
        vals = {(r, c): v for r, c, v in db.execute(
            f"SELECT row, col, value FROM cells WHERE sheet=? AND row BETWEEN ? AND ? AND col BETWEEN ? AND ?",
            (sheet, min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts)))}
        data = []
        for p in pts:
            v = vals.get(p)
            data.append(v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
        row0 = pts[0][0]
        lab = db.execute("SELECT label, units FROM rows WHERE sheet=? AND row=?", (sheet, row0)).fetchone()
        one_row = pts[0][0] == pts[-1][0]
        name = s.get("name") or (lab[0] if lab and lab[0] and one_row else s["range"])
        out_series.append({"name": name, "range": s["range"], "data": data,
                           "label": lab[0] if lab and one_row else None,
                           "units": lab[1] if lab and one_row else None})
        if labels is None:
            if x_range:
                xs, xpts = _range_cells(db, x_range)
                xv = dict(((r, c), v) for r, c, v in db.execute(
                    "SELECT row, col, value FROM cells WHERE sheet=?", (xs,)) if (r, c) in set(xpts))
                labels = [_period_label(xv.get(p, "")) for p in xpts]
            elif pts[0][0] == pts[-1][0]:  # a row: use the timeline header for those columns
                from openpyxl.utils import get_column_letter
                lay = db.execute("SELECT layout FROM sheets WHERE sheet=?", (sheet,)).fetchone()
                hr = _json.loads(lay[0] or "{}").get("header_row") if lay else None
                hv = dict(db.execute("SELECT col, value FROM cells WHERE sheet=? AND row=?", (sheet, hr))) if hr else {}
                labels = [_period_label(hv[c]) if c in hv else get_column_letter(c) for _, c in pts]
            else:
                labels = [str(r) for r, _ in pts]
    n = max((len(s["data"]) for s in out_series), default=0)
    labels = (labels or [])[:n] + [""] * (n - len(labels or []))
    import chartdata
    spec = {"title": title, "kind": kind if kind in ("line", "bar") else "line", "units": units,
            "labels": labels, "series": out_series}
    return chartdata.enrich(spec, db)


def chartdata_methods(a: dict) -> list[str]:
    import chartdata
    return [chartdata.METHOD_TEXT[m] for m in a["methods"]]


def chart_note(spec: dict) -> str:
    """What the model is told after drawing: enough to describe the chart without re-reading every value."""
    shown = spec.get("period_labels") or spec["labels"]
    lines = [f"Chart shown to the user: '{spec['title']}', {len(shown)} {spec.get('frequency', '')} points "
             f"({shown[0] if shown else ''} to {shown[-1] if shown else ''}); values below are the workbook's."]
    if spec.get("sign") == -1:
        lines.append("All values are negative in the workbook, so the chart shows them as positive (labelled).")
    if spec.get("annual"):
        a = spec["annual"]
        lines.append(f"The user can switch to annual figures by {a['basis']} "
                     f"({', '.join(chartdata_methods(a))}); years marked * are partial.")
    for s in spec["series"]:
        nums = [v for v in s["data"] if v is not None]
        if nums:
            lines.append(f"- {s['name']} ({s['range']}): first {nums[0]:.6g}, last {nums[-1]:.6g}, "
                         f"min {min(nums):.6g}, max {max(nums):.6g}, total {sum(nums):.6g}, {len(nums)} numeric")
        else:
            lines.append(f"- {s['name']} ({s['range']}): no numeric values")
    return "\n".join(lines)
