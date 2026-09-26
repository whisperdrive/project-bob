"""Presentation data for a chart spec, computed once on the server so the page, the server-side render and the
chart review all see the same thing.

- frequency: monthly / quarterly / semi-annual / annual, from the gaps between period dates
- period_labels: periods labelled by their end month, "Sep-2017" (years for annual data)
- sign: -1 when every non-zero value on the chart is negative (e.g. capex), so it's shown as positive;
  1 when positives and negatives are mixed (e.g. sources and uses), so signs stay as they are
- annual: the same series aggregated by financial year: flows summed, balances at year end (or opening
  balances at the start), rates / percentages / indices averaged; partial years marked
The workbook values in spec["series"][i]["data"] are never changed.
"""
import re
import sqlite3
from calendar import month_abbr, monthrange
from datetime import date, timedelta
from statistics import median

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_AVERAGE = re.compile(r"%|\brate|margin|yield|index|factor|multiple|price|ratio|\bdays?\b|per ", re.I)
_LAST = re.compile(r"closing|balance|outstanding|cumulative|\bnav\b|net debt", re.I)
_FIRST = re.compile(r"opening", re.I)
METHOD_TEXT = {"sum": "annual totals", "last": "year-end values", "first": "opening values",
               "average": "annual averages"}


def _d(label) -> date | None:
    m = _DATE.match(str(label or ""))
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def fy_end_month(db: sqlite3.Connection) -> int:
    """The model's financial-year end month (1-12) from a named range or a labelled row; 12 if not found."""
    for name, ref in db.execute("SELECT name, ref FROM names"):
        if re.search(r"fy.?(end.?)?month|year.?end.?month", name, re.I) and "!" in ref:
            sheet, addr = ref.rsplit("!", 1)
            row = db.execute("SELECT value FROM cells WHERE sheet=? AND addr=?",
                             (sheet.strip("'"), addr.replace("$", ""))).fetchone()
            if row and isinstance(row[0], (int, float)) and 1 <= row[0] <= 12:
                return int(row[0])
    for sheet, r in db.execute("SELECT sheet, row FROM rows WHERE label LIKE '%financial year end month%' "
                               "OR label LIKE '%year end month%' LIMIT 5"):
        for (v,) in db.execute("SELECT value FROM cells WHERE sheet=? AND row=?", (sheet, r)):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 12:
                return int(v)
    return 12


def aggregation(name: str, units: str | None) -> str:
    text = f"{name or ''} {units or ''}"
    if _FIRST.search(text):
        return "first"
    if _LAST.search(text):
        return "last"
    if _AVERAGE.search(text) or (units or "").strip().lower() in ("%", "index", "x", "#", "days", "rate"):
        return "average"
    return "sum"


def enrich(spec: dict, db: sqlite3.Connection) -> dict:
    labels = spec.get("labels") or []
    starts = [_d(x) for x in labels]
    n = len(labels)
    if n < 2 or any(s is None for s in starts):
        spec.update(frequency="irregular", period_labels=labels, sign=_sign(spec), annual=None)
        return spec
    gap = median((b - a).days for a, b in zip(starts, starts[1:]))
    freq, per_year = next(((f, k) for lo, hi, f, k in ((26, 33, "monthly", 12), (85, 95, "quarterly", 4),
                                                        (178, 187, "semi-annual", 2), (360, 370, "annual", 1))
                           if lo <= gap <= hi), ("irregular", 0))
    # Period end = the day before the next period starts. The last period has no successor: step whole
    # months for monthly/quarterly/semi-annual data (a day count like 92 would overshoot into the next month).
    ends = [b - timedelta(days=1) for b in starts[1:]]
    if per_year in (12, 4, 2):
        k = 12 // per_year
        y, m = divmod(starts[-1].month - 1 + k, 12)
        ends.append(date(starts[-1].year + y, m + 1, 1) - timedelta(days=1))
        ends = [date(e.year, e.month, monthrange(e.year, e.month)[1]) for e in ends]  # month ends ("Sep-2017")
    else:
        ends.append(starts[-1] + timedelta(days=int(gap)) - timedelta(days=1))
    fy_m = fy_end_month(db)
    fy = lambda e: e.year if e.month <= fy_m else e.year + 1  # financial year a period ends in
    year_label = (lambda y: f"FY{y}") if fy_m != 12 else str
    if freq == "annual":
        period_labels = [year_label(fy(e)) for e in ends]
    elif freq == "irregular":
        period_labels = labels
    else:
        period_labels = [f"{month_abbr[e.month]}-{e.year}" for e in ends]
    spec.update(frequency=freq, period_labels=period_labels, period_ends=[e.isoformat() for e in ends],
                fy_end_month=fy_m, sign=_sign(spec))
    if freq in ("irregular", "annual"):
        spec["annual"] = None
        return spec

    years = sorted({fy(e) for e in ends})
    idx = {y: i for i, y in enumerate(years)}
    period_year = [idx[fy(e)] for e in ends]
    counts = [period_year.count(i) for i in range(len(years))]
    a_series = []
    for s in spec["series"]:
        method = aggregation(s.get("label") or s.get("name"), s.get("units") or spec.get("units"))
        buckets = [[] for _ in years]
        for yi, v in zip(period_year, s["data"]):
            buckets[yi].append(v)
        vals = []
        for b in buckets:
            nums = [v for v in b if isinstance(v, (int, float))]
            if not nums:
                vals.append(None)
            elif method == "sum":
                vals.append(sum(nums))
            elif method == "average":
                vals.append(sum(nums) / len(nums))
            elif method == "first":
                vals.append(nums[0])
            else:
                vals.append(nums[-1])
        a_series.append({"name": s["name"], "data": vals, "method": method})
    partial = [c < per_year for c in counts]
    spec["annual"] = {
        "labels": [year_label(y) + ("*" if p else "") for y, p in zip(years, partial)],
        "series": a_series, "partial": partial, "period_year": period_year,
        "basis": (f"financial year ending {month_abbr[fy_m]}" if fy_m != 12 else "calendar year"),
        "methods": sorted({s["method"] for s in a_series}),
    }
    return spec


def _sign(spec: dict) -> int:
    """-1 if every non-zero value in every series is negative (show them as positive), else 1."""
    nz = [v for s in spec.get("series", []) for v in s.get("data", []) if isinstance(v, (int, float)) and v]
    return -1 if nz and all(v < 0 for v in nz) else 1


def display(spec: dict, mode: str = "periodic") -> dict:
    """The labels and values as shown: periodic or annual, with the sign presentation applied."""
    sign = spec.get("sign", 1)
    if mode == "annual" and spec.get("annual"):
        a = spec["annual"]
        labels, series = a["labels"], a["series"]
    else:
        labels, series = spec.get("period_labels") or spec.get("labels", []), spec.get("series", [])
    return {"labels": labels, "series": [{"name": s["name"], "data": [v * sign + 0.0 if isinstance(v, (int, float))
                                                                       else v for v in s["data"]]}
                                         for s in series]}
