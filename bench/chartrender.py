"""Render a chart spec (from tools.chart) to PNG on the server, for the chart review. No browser involved.

Styled like the in-app chart (same series colours, light grid, sparse x labels) so the reviewer sees what the
user will see, and it applies the same framing (visible window, y-axis limits) when a view is given.
"""
import io

import matplotlib

matplotlib.use("Agg")  # no display
import matplotlib.pyplot as plt  # noqa: E402

COLOURS = ["#1d6b47", "#3f6fb0", "#b7791f", "#9b4d8f", "#2f8f8f", "#a33a22"]


def render_png(spec: dict, view: dict | None = None, width_px: int = 1200, height_px: int = 560,
               mode: str = "periodic") -> bytes:
    from chartdata import display
    shown = display(spec, mode)  # formatted period labels, sign presentation applied
    labels, series = shown["labels"], shown["series"]
    n = len(labels)
    fig, ax = plt.subplots(figsize=(width_px / 100, height_px / 100), dpi=100)
    xs = list(range(n))
    kind = spec.get("kind") or "line"
    width = 0.8 / max(1, len(series))
    for i, s in enumerate(series):
        c = COLOURS[i % len(COLOURS)]
        ys = [v if isinstance(v, (int, float)) else float("nan") for v in s.get("data", [])]
        if kind == "bar":
            ax.bar([x + (i - (len(series) - 1) / 2) * width for x in xs], ys, width=width, color=c, label=s.get("name"))
        else:
            ax.plot(xs, ys, color=c, linewidth=1.8, label=s.get("name"), marker="o" if n <= 60 else None, markersize=3)
    # Actuals / Business plan / Forecast spans, shaded like the page (clipped to the visible window;
    # labels only where the span is wide enough to read)
    vw = view or {}
    lo_v = vw.get("x_start") or 0
    hi_v = vw.get("x_end") if vw.get("x_end") is not None else n - 1
    for k, ph in enumerate(shown.get("phases") or []):
        a, b = max(ph["start"], lo_v), min(ph["end"], hi_v)
        if a > b:
            continue
        if k % 2 == 0:
            ax.axvspan(a - 0.5, b + 0.5, color="#1b2320", alpha=0.05, linewidth=0)
        if (b - a + 1) >= 0.08 * (hi_v - lo_v + 1):
            ax.text((a + b) / 2, 1.0, ph["name"], transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                    fontsize=8, color="#5d6a64", clip_on=False)
    ax.set_title(spec.get("title") or "", loc="left", fontsize=13, fontweight="bold", pad=16)
    units = spec.get("units") or "units not labelled"
    if spec.get("sign") == -1:
        units += " (negative values shown as positive)"
    ax.set_ylabel(units, fontsize=9, color="#5d6a64")
    step = max(1, n // 10)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels([str(labels[i]) for i in xs[::step]], fontsize=8, color="#5d6a64")
    ax.tick_params(axis="y", labelsize=8, colors="#5d6a64")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}" if abs(v) >= 10 else f"{v:,.3g}"))
    ax.grid(axis="y", color="#d8ded9", linewidth=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    if len(series) > 1:
        ax.legend(fontsize=8, frameon=False)
    v = view or {}
    if v.get("x_start") is not None or v.get("x_end") is not None:
        ax.set_xlim(v.get("x_start", 0) - 0.5, (v.get("x_end") if v.get("x_end") is not None else n - 1) + 0.5)
    # Like the page: the y-axis fits the visible periods, unless the view sets limits.
    lo_i = v.get("x_start") or 0
    hi_i = v.get("x_end") if v.get("x_end") is not None else n - 1
    visible = [val for s in series for val in s.get("data", [])[lo_i:hi_i + 1] if isinstance(val, (int, float))]
    if visible:
        lo, hi = min(visible), max(visible)
        pad = (hi - lo) * 0.05 or abs(hi) * 0.05 or 1
        y_lo = v["y_min"] if v.get("y_min") is not None else lo - pad
        y_hi = v["y_max"] if v.get("y_max") is not None else hi + pad
        ax.set_ylim(y_lo, y_hi)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
