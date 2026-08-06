import base64
import html as _html
import io
import logging
import warnings
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import geopandas as gpd
import matplotlib
import numpy as np
matplotlib.use("Agg")  # must be before pyplot import
from PIL import Image

# Email content width (matches the body max-width in run_alert.py). Image width
# attributes are capped here so Outlook desktop — which ignores max-width CSS —
# never renders a chart wider than the layout.
_EMAIL_CONTENT_WIDTH_PX = 900
import matplotlib.patches as mpatches
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.transforms as mtransforms
from matplotlib.lines import Line2D

from src.landfall import densify_track

# ---------------------------------------------------------------------------
# HDX v2 design tokens (ds-knowledge-base methods/style-guide.md; full mirror in
# ds-knowledge-base-internal/style-reference/tokens.md). Values are lifted
# rather than imported because matplotlib and email HTML cannot pull the HDX
# CSS bundle — the style guide sanctions exactly this for outputs that can't
# import wholesale.
# ---------------------------------------------------------------------------
INK = "#1f2324"        # --hdx-neutral-9   primary text
INK_2 = "#5e6a6b"      # --hdx-neutral-7   secondary text
INK_3 = "#7e8e8f"      # --hdx-neutral-6   muted text
LINE = "#e2e8e8"       # --hdx-neutral-15  hairlines, grid
GREY_FILL = "#d8e0e1"  # --hdx-neutral-2
GREY_EDGE = "#9db1b3"  # --hdx-neutral-5
PANEL = "#ffffff"      # --hdx-neutral-0

# Chart text. Roboto per the style guide; the stack degrades to the nearest
# grotesque rather than matplotlib's DejaVu default when Roboto isn't installed
# (it is not, on the Databricks runner).
_FONT_STACK = ["Roboto", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]

_HIST_COLOR = GREY_EDGE

# Wind thresholds are an ordered severity scale, so each gets one step of the
# HDX status ramp — amber at tropical-storm force through deep red at hurricane
# force — paired with a pale step of the same hue for the distribution fill.
# Status colors are reserved for exactly this (state, not series identity).
_WIND_RAMP = {
    34: ("#f6e9d4", "#d48f2a"),  # --hdx-warning-1 / --hdx-warning-5
    50: ("#f3dad7", "#d06a5e"),  # --hdx-error-1   / --hdx-error-4
    64: ("#e7b5af", "#9d372b"),  # --hdx-error-2   / --hdx-error-6
}
_NHC_WIND_COLOR = {k: v[1] for k, v in _WIND_RAMP.items()}

# Wind-speed probability: the HDX primary (blue) ramp, pale to deep.
#
# This replaces NHC's own green-lime-yellow-tan-brown-orange-red-purple scale.
# That scale is a rainbow: it is not monotonic in lightness, so a reader cannot
# tell which of two bands is the higher probability without consulting the key,
# and several adjacent pairs collapse under common colour-vision deficiencies.
# One hue, light to dark, encodes an ordered quantity correctly by construction.
#
# Blue rather than the amber/red used elsewhere: the same map also carries the
# observed wind swaths on the severity ramp, and probability is a different
# quantity that must not read as another severity band.
_NHC_WSP_COLOR = {
    0:  "#ffffff",  # --hdx-neutral-0   (needs an outline to be visible)
    5:  "#e8effb",  # --hdx-primary-05
    10: "#d1e0f7",  # --hdx-primary-1
    20: "#bad0f3",  # --hdx-primary-15
    30: "#a3c0ef",  # --hdx-primary-2
    40: "#74a1e8",  # --hdx-primary-3
    50: "#4681e0",  # --hdx-primary-4
    60: "#1862d8",  # --hdx-primary-5
    70: "#134ead",  # --hdx-primary-6
    80: "#0e3b82",  # --hdx-primary-7
    90: "#0a2756",  # --hdx-primary-8
}

# Swath opacity. Both are lower than they were: over a tile basemap an opaque
# swath hides the coastlines and place names that are the reason for having one.
_OBSV_BUFFER_ALPHA = 0.20
_FCAST_BUFFER_ALPHA = 0.52
# The probability bands nest, so the visible colour of an inner band is its own
# fill over everything outside it. Keep this high enough that the stack does not
# drift far from the legend swatches, low enough to read the coastline through.
_WSP_ALPHA = 0.72

# White casing under the track lines. Over the deepest probability bands a dark
# track on dark navy is invisible, and the track is the one thing on the map
# that must always read.
_TRACK_CASING = path_effects.withStroke(linewidth=3.4, foreground=PANEL)

_UTC = ZoneInfo("UTC")
_NY = ZoneInfo("America/New_York")


def _format_ny(t: datetime) -> str:
    """Format a UTC timestamp as a compact NY-time string."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=_UTC)
    local = t.astimezone(_NY)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a')} {hour}{local.strftime('%p')}"




@dataclass(frozen=True, slots=True)
class StormMark:
    """One vertical line in a strip chart.

    short=True draws a low line with a smaller label (for historical context).
    short=False draws a tall line with a larger label (for current/forecast).
    bold_prefix renders as a separate bold text element above the main label.
    """
    value: int
    label: str
    color: str
    short: bool = False
    bold: bool = False
    bold_prefix: str = ""


# WSP band edges: the `percentage` column is the band's LOWER probability
# edge; consecutive edges bound each band. Midpoints are the representative
# per-band probability, matching run_alert's expected-population weighting.
_WSP_BAND_EDGES = [0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
_WSP_BAND_MID = {
    lo: (lo + hi) / 200.0
    for lo, hi in zip(_WSP_BAND_EDGES[:-1], _WSP_BAND_EDGES[1:], strict=False)
}


@dataclass(frozen=True, slots=True)
class WspPdf:
    """PDF overlay for a strip chart: WSP fcastonly probability bands.

    Each row is one band: (percentage, pop_exposed). x_offset shifts the PDF's
    starting position (e.g. by the cumulative observed exposure).
    """
    bands: list[tuple[int, int]]
    x_offset: float
    color: str


def _fmt_pop(x: float, _pos: object) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}K"
    return str(int(x))


def _pdf_atoms(
    pdf: WspPdf, total_pop: float | None
) -> list[tuple[float, float]]:
    """The forecast-exposure distribution as (value, probability) atoms.

    Built under the comonotone ("one severity draw") model: a single
    U ~ Uniform(0,1) decides the storm's realised severity, and a location is
    exposed iff its WSP probability >= U. Then total exposure is discrete:

    - with probability 1 - p_max nothing beyond the already-observed floor
      happens -> an atom AT the floor (usually zero). This is the near-delta
      the plain band layout hid: a country whose highest band is 10% has ~90%
      of its probability mass sitting right there;
    - the cumulative band population C_k (bands sorted by descending band
      probability p_1 > ... > p_m, midpoint-represented) occurs with
      probability p_k - p_{k+1}, and C_m with probability p_m.

    Probabilities sum to 1 by construction. Values are clamped at the
    country's total population; clamped atoms merge, accumulating their
    probability at the cap.
    """
    bands = [(p, n) for p, n in pdf.bands if n > 0]
    if not bands:
        return []
    # Sort by representative probability, descending.
    bands.sort(key=lambda b: -_WSP_BAND_MID.get(int(b[0]), 0.0))
    probs = [_WSP_BAND_MID.get(int(p), 0.0) for p, _ in bands]
    atoms: dict[float, float] = {}

    def _add(x: float, w: float) -> None:
        if w <= 0:
            return
        if total_pop and total_pop > 0:
            x = min(x, float(total_pop))
        atoms[x] = atoms.get(x, 0.0) + w

    _add(pdf.x_offset, 1.0 - probs[0])
    cum = pdf.x_offset
    for k, ((_, pop), p_k) in enumerate(zip(bands, probs, strict=True)):
        cum += pop
        p_next = probs[k + 1] if k + 1 < len(probs) else 0.0
        _add(cum, p_k - p_next)
    return sorted(atoms.items())


def _kernel_density(
    atoms: list[tuple[float, float]], grid, sigma: float,
    upper: float | None = None,
):
    """Gaussian-kernel density of weighted atoms on `grid`, reflected at zero
    and (when given) at `upper`.

    Reflection keeps boundary mass on the physical side of each limit: the
    delta-at-zero stays a visible spike instead of losing half its area to
    negative exposure, and mass at the total-population cap piles up AT the
    cap instead of smearing past more people than the country has.
    """
    dens = np.zeros_like(grid, dtype=float)
    for x, w in atoms:
        dens += w * np.exp(-0.5 * ((grid - x) / sigma) ** 2)
        dens += w * np.exp(-0.5 * ((grid + x) / sigma) ** 2)
        if upper is not None:
            dens += w * np.exp(-0.5 * ((grid - (2 * upper - x)) / sigma) ** 2)
    return dens


# ---------------------------------------------------------------------------
# Vertical layout, in data units (the y-limit is computed per chart).
#
# The chart is a number line read bottom-up: the probabilistic forecast spread
# is a low density strip on the baseline, marks rise out of it, and every label
# is HORIZONTAL above its mark. Rotated labels were the previous design and the
# reason the charts were unreadable — 90-degree text at 6pt is decoration, not
# information.
#
# Each tier gets its own label band so the anti-collision pass only ever has to
# separate a handful of labels at a time, rather than every label in the chart.
# ---------------------------------------------------------------------------
_Y_PDF_TOP = 0.30         # WSP density strip occupies 0 .. here
_Y_HIST_TOP = 0.22        # historical ticks stop just inside the strip
_Y_HIST_LABEL = 0.34      # historical label band (2 staggered rows)
_Y_HIST_ROW_GAP = 0.20
# Source estimates (CHD / ADAM / GDACS) render as horizontal bars in fixed
# lanes above the historical band; the headline tier and the y-limit are
# computed per chart from the number of lanes — see _strip_chart.

_HIST_FONTSIZE = 6.8
_SRC_FONTSIZE = 7.2
_BOLD_FONTSIZE = 8.4

_FIG_W_IN = 9.0


def _label_half_widths(
    ax, fig, labels: list[str], fontsize: float, bold: bool = False,
    rotation: float = 0, bold_flags: list[bool] | None = None,
) -> list[float]:
    """Half the horizontal footprint of each label, in data units.

    Measured with the real renderer rather than estimated from a character
    count: a per-character width guess is wrong by enough on all-caps source
    names ("GDACS") to let labels touch, which is the specific defect this
    layout exists to fix. The caller must have set the final x-limits first,
    because data-per-pixel is what the display-to-data conversion depends on.
    """
    renderer = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    out: list[float] = []
    for i, label in enumerate(labels):
        b = bold_flags[i] if bold_flags is not None else bold
        probe = ax.text(
            0, 0, label, fontsize=fontsize, ha="center", rotation=rotation,
            fontweight="bold" if b else "normal",
        )
        bb = probe.get_window_extent(renderer=renderer)
        probe.remove()
        x0 = inv.transform((bb.x0, 0))[0]
        x1 = inv.transform((bb.x1, 0))[0]
        out.append(abs(x1 - x0) / 2.0)
    return out


def _place_labels(
    items: list[tuple[float, float]], x_lo: float, x_hi: float, pad: float,
    droppable: bool = False, protected: list[bool] | None = None,
) -> tuple[list[float], list[bool]]:
    """Spread labels so they stop overlapping, keeping the group centred.

    `items` is (anchor_value, half_width) in data units, in the caller's order.
    Returns (placed_x, keep) in that same order. When an adjacent pair collides
    both are pushed equally in opposite directions, so a cluster resolves
    symmetrically about its own centre instead of drifting one way.

    Separation and edge-clamping have to ALTERNATE, not run once each. Clamping
    after a single separation pass is what produced "Paloma 2008Gustav 2008":
    two labels near x=0 were pushed apart, then both clamped back to the left
    edge, silently undoing the separation. Each clamp is followed by another
    separation pass so the group settles inside the frame.

    `droppable` allows the last resort — when a row genuinely has more label
    than axis, the lower-valued labels are marked `keep=False`. Their ticks
    still draw; only the text is withheld, so the distribution stays honest and
    nothing overprints. `protected` (aligned with `items`) exempts a label
    from that drop — used for the "most similar storms", which must always be
    named; a clash between two protected labels is left overprinting rather
    than silently unnaming one.
    """
    if not items:
        return [], []
    order = sorted(range(len(items)), key=lambda i: items[i][0])
    hw = [items[i][1] for i in order]
    prot = [bool(protected[i]) if protected else False for i in order]
    keep = [True] * len(order)
    margin = pad * 0.4

    def _settle(idx: list[int]) -> list[float]:
        pos = [float(items[order[i]][0]) for i in idx]
        for _ in range(6):
            for _ in range(200):
                moved = False
                for k in range(len(pos) - 1):
                    needed = hw[idx[k]] + hw[idx[k + 1]] + pad
                    gap = pos[k + 1] - pos[k]
                    if gap < needed:
                        push = (needed - gap) / 2
                        pos[k] -= push
                        pos[k + 1] += push
                        moved = True
                if not moved:
                    break
            clamped = False
            for k, p in enumerate(pos):
                # `margin` past the half-width: a label clamped to exactly
                # x_lo + hw sits flush on the axis and its first glyph gets
                # shaved, because the measured extent excludes side bearing.
                lo = x_lo + hw[idx[k]] + margin
                hi = x_hi - hw[idx[k]] - margin
                c = min(max(p, lo), hi) if lo <= hi else (lo + hi) / 2
                if c != p:
                    pos[k], clamped = c, True
            if not clamped:
                break
        return pos

    live = list(range(len(order)))
    pos = _settle(live)
    if droppable:
        # Whatever the settle could not resolve is a genuine overflow. Drop the
        # smallest-valued offender and retry; bigger storms are the ones a
        # reader is comparing against. Protected labels never drop — if both
        # sides of a clash are protected, move on to the next clash.
        while len(live) > 1:
            dropped = False
            for k in range(len(pos) - 1):
                if pos[k + 1] - pos[k] >= hw[live[k]] + hw[live[k + 1]] + pad * 0.5:
                    continue
                victim = next(
                    (j for j in (k, k + 1) if not prot[live[j]]), None
                )
                if victim is None:
                    continue
                keep[live[victim]] = False
                live.pop(victim)
                pos = _settle(live)
                dropped = True
                break
            if not dropped:
                break

    out = [0.0] * len(items)
    out_keep = [False] * len(items)
    for k, i in enumerate(live):
        out[order[i]] = pos[k]
        out_keep[order[i]] = True
    # Dropped labels still need a position for the (unused) leader calculation.
    for i, o in enumerate(order):
        if not out_keep[o]:
            out[o] = float(items[o][0])
    return out, out_keep


def _leader(ax, x_from: float, y_from: float, x_to: float, y_to: float,
            color: str) -> None:
    """Hairline from a displaced label back to the mark it belongs to.

    Only drawn when the label actually moved: a leader under an unshifted label
    is noise.
    """
    ax.plot([x_from, x_to], [y_from, y_to], color=color, lw=0.6, alpha=0.55,
            zorder=3, solid_capstyle="butt", clip_on=False)


def _draw_tier(
    ax, fig, marks: list[StormMark], x_lo: float, x_hi: float, *,
    tick_top: float, label_y: float, fontsize: float, linewidth: float,
    color: str | None, text_color: str, rows: int = 1, row_gap: float = 0.0,
    bold: bool = False, marker: bool = False, halo: bool = False,
    droppable: bool = False,
) -> None:
    """Draw one tier of marks: a tick per mark, plus its horizontal label.

    `rows` > 1 staggers labels over that many bands, which is what keeps a long
    run of historical storms legible without shrinking the type. Collision is
    resolved within a row, so staggering roughly halves how far anything has to
    move away from its true value.
    """
    if not marks:
        return
    x_range = x_hi - x_lo
    ordered = sorted(marks, key=lambda m: float(m.value))
    labels = [
        (f"{m.bold_prefix}\n{m.label}" if m.bold_prefix else m.label)
        for m in ordered
    ]
    half = _label_half_widths(ax, fig, labels, fontsize, bold=bold)
    # Minimum gap between labels, measured rather than taken as a fraction of
    # the axis. A fraction is wrong at both ends: on a 12M-wide axis 0.4% is
    # 48k data units, far less than a space, so labels ended up touching.
    pad = _label_half_widths(ax, fig, ["nn"], fontsize)[0] * 2

    # Place each row independently: labels in different rows cannot collide, so
    # forcing them apart horizontally would displace them for nothing.
    placed = [0.0] * len(ordered)
    show = [True] * len(ordered)
    for row in range(rows):
        idx = [i for i in range(len(ordered)) if i % rows == row]
        if not idx:
            continue
        row_pos, row_keep = _place_labels(
            [(float(ordered[i].value), half[i]) for i in idx],
            x_lo, x_hi, pad, droppable=droppable,
        )
        for i, p, k in zip(idx, row_pos, row_keep, strict=True):
            placed[i], show[i] = p, k

    for i, (m, lb, px) in enumerate(zip(ordered, labels, placed, strict=True)):
        c = color or m.color
        actual = float(m.value)
        row_y = label_y + (i % rows) * row_gap if rows > 1 else label_y
        ax.plot([actual, actual], [0, tick_top], color=c, lw=linewidth,
                zorder=4, solid_capstyle="butt")
        if marker:
            ax.plot([actual], [tick_top], marker="o", ms=6.5, mfc=c,
                    mec=PANEL, mew=1.3, zorder=6, clip_on=False)
        if not show[i]:
            # No room for the text; the tick above still carries the value.
            continue
        if abs(px - actual) > x_range * 0.004:
            _leader(ax, px, row_y - 0.035, actual,
                    tick_top + (0.06 if marker else 0.02), c)
        if m.bold_prefix:
            # Two texts on one anchor: the name in the tier colour and bold, the
            # qualifier under it in muted ink. A single string can't carry two
            # weights, and the qualifier in full colour competes with the mark.
            n_sub = m.label.count("\n") + 1
            ax.text(px, row_y, m.bold_prefix + "\n" * n_sub, ha="center",
                    va="bottom", fontsize=fontsize, color=c,
                    fontweight="bold", zorder=5, clip_on=False,
                    linespacing=1.25)
            ax.text(px, row_y, "\n" + m.label, ha="center", va="bottom",
                    fontsize=fontsize - 0.6, color=text_color, zorder=5,
                    clip_on=False, linespacing=1.25)
        else:
            t = ax.text(px, row_y, lb, ha="center", va="bottom",
                        fontsize=fontsize, color=text_color,
                        fontweight="bold" if bold else "normal",
                        zorder=5, clip_on=False, linespacing=1.25)
            if halo:
                # Historical labels sit low, where the taller source and
                # headline ticks pass through them. A surface-coloured stroke
                # keeps the text readable at a crossing instead of relying on
                # the upstream spacing filter never leaving one.
                t.set_path_effects([
                    path_effects.withStroke(linewidth=2.2, foreground=PANEL)
                ])


# Minimum number of past storms (nonzero exposure at this country + threshold)
# before a historical density curve is drawn. Below this a "distribution" is
# two or three bumps pretending to be a shape — the ticks alone say more.
_HIST_KDE_MIN_STORMS = 5


def _strip_chart(
    title: str,
    x_label: str,
    marks: list[StormMark],
    x_max: float | None = None,
    pdf: WspPdf | None = None,
    pdf_fill_color: str = GREY_FILL,
    pdf_edge_color: str = GREY_EDGE,
    pdf_style: str = "density",
    hist_values: list[float] | None = None,
    hist_fill_color: str = GREY_FILL,
    hist_edge_color: str = GREY_EDGE,
    total_pop: int | None = None,
    wind_chip_kt: int | None = None,
) -> str:
    """Compact source-comparison chart for one country + wind threshold.

    Everything lives on ONE axis — population exposed:

    - source-estimate dots sit directly on the axis line, their labels in a
      row just below it (so they can't collide with storm names above);
    - historical storms are ticks rising from the axis with VERTICAL name
      labels — vertical packs several times more names into the same width;
    - `hist_values` (the FULL unfiltered set of past nonzero exposures, not
      just the labelled ticks) draws a grey historical density curve — only
      when there are at least _HIST_KDE_MIN_STORMS of them;
    - `pdf` draws the probabilistic (WSP) forecast along the baseline, in one
      of two styles: ``"density"`` (the smoothed comonotone pmf, capped at the
      total population) or ``"exceedance"`` (the chance that final exposure
      exceeds each value — starts at 100% and falls to zero). The email passes
      no pdf at all; both styles render on the full-detail page;
    - a dashed line marks exposure already observed;
    - the chip in the top-right names the wind threshold, replacing both the
      old x-axis label and the per-chart heading.
    """
    # Drop marks that would be outside the chart's x range — their ax.text
    # objects at large data coordinates expand bbox_inches="tight" to data
    # scale.
    nonzero = [
        m for m in marks
        if m.value > 0 and (x_max is None or x_max <= 0 or m.value <= x_max * 1.05)
    ]
    has_pdf = pdf is not None and any(n > 0 for _, n in pdf.bands)
    if not nonzero and not has_pdf and x_max is None:
        return ""

    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = _FONT_STACK

    hist = [m for m in nonzero if m.short]
    _tall = [m for m in nonzero if not m.short]
    obs = [m for m in _tall if m.bold or m.bold_prefix]
    srcs = [m for m in _tall if not (m.bold or m.bold_prefix)]

    # Vertical layout (data units above the axis). The band for vertical
    # storm-name labels dominates; charts with no history stay shallow.
    _y_strip = 0.52          # forecast-distribution curve (tall on purpose)
    _y_rug = 0.16            # historical ticks
    _y_rug_label = 0.22      # vertical names start here
    y_top = 1.30 if (hist or obs) else 0.62

    # Fixed physical furniture: the axes area scales with y_top, the space
    # below the axis (dot labels, tick numbers, total-population marker) is a
    # constant depth.
    axes_in = y_top * 0.78
    fig_h = axes_in + 0.66
    fig, ax = plt.subplots(figsize=(_FIG_W_IN, fig_h))
    fig.patch.set_facecolor(PANEL)
    fig.subplots_adjust(
        left=0.02, right=0.98,
        top=1 - 0.06 / fig_h, bottom=0.60 / fig_h,
    )

    if x_max is not None and x_max > 0:
        x_hi = x_max * 1.12
    else:
        x_hi = max([float(m.value) for m in nonzero] or [1.0]) * 1.20
    x_lo = 0.0
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(0, y_top)
    # x in data coordinates, y in axes fraction — for everything below the
    # axis line.
    below = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)

    # Baseline curves. Historical first (grey, behind), then the forecast
    # probabilistic curve if a pdf was passed — the email passes none.
    sigma = (x_hi - x_lo) * 0.022
    legend_rows: list[tuple[str, str, float]] = []  # (label, colour, alpha)

    # Historical density: a curve over ALL past nonzero exposures at this
    # country + threshold (hist_values, not the filtered ticks — the ticks
    # drop storms that crowd each other, which would bias the shape). Only
    # drawn when there are enough storms for a distribution to mean anything.
    # Same total-population cap as the forecast curve: past exposure can't
    # exceed the country, so mass near the cap reflects back rather than
    # smearing past it. Tinted to the wind threshold, but paler than the
    # forecast curve so the two stay tellable-apart on the full page.
    hvals = [float(v) for v in (hist_values or []) if v > 0]
    if len(hvals) >= _HIST_KDE_MIN_STORMS:
        h_cap = float(total_pop) if total_pop and total_pop > 0 else None
        h_sigma = (x_hi - x_lo) * 0.045
        h_end = min(max(hvals) + 3 * h_sigma, x_hi)
        if h_cap is not None:
            h_end = min(h_end, h_cap)
        hgrid = np.linspace(0.0, h_end, 480)
        w = 1.0 / len(hvals)
        hdens = _kernel_density(
            [(min(v, h_cap) if h_cap is not None else v, w) for v in hvals],
            hgrid, h_sigma, upper=h_cap,
        )
        if hdens.max() > 0:
            hdens = hdens * 0.34 / hdens.max()
            ax.fill_between(hgrid, hdens, 0, facecolor=hist_fill_color,
                            alpha=0.5, linewidth=0, zorder=1)
            ax.plot(hgrid, hdens, color=hist_edge_color, lw=0.9, alpha=0.6,
                    zorder=1.5)
            legend_rows.append((
                f"past storms distribution ({len(hvals)} storms)",
                hist_edge_color, 0.55))

    # The forecast probabilistic distribution — the smoothed comonotone atom
    # pmf from the WSP bands (see _pdf_atoms). Mass sums to 1, which is why
    # the density usually spikes hard at the already-observed floor; support
    # is HARD-capped at the total population (grid stops there, and boundary
    # reflection piles cap-adjacent mass at the cap rather than smearing it
    # past). "exceedance" integrates the same smoothed density into a survival
    # curve P(exposure >= x): anchored at 100% on the left, falling to zero.
    if has_pdf:
        cap = float(total_pop) if total_pop and total_pop > 0 else None
        atoms = _pdf_atoms(pdf, cap)
        if atoms:
            end = max(x for x, _ in atoms) + 4 * sigma
            if cap is not None:
                end = min(end, cap)
            end = min(end, x_hi)
            fgrid = np.linspace(0.0, end, 480)
            fdens = _kernel_density(atoms, fgrid, sigma, upper=cap)
            if pdf_style == "exceedance" and fdens.sum() > 0:
                cdf = np.cumsum(fdens)
                surv = 1.0 - cdf / cdf[-1]
                ys = surv * _y_strip
                ax.fill_between(fgrid, ys, 0, facecolor=pdf_fill_color,
                                alpha=0.9, linewidth=0, zorder=2)
                ax.plot(fgrid, ys, color=pdf_edge_color, lw=1.0,
                        alpha=0.9, zorder=3)
                # Probability rulings so the curve's vertical scale is
                # readable: 100% at the anchor, a dotted 50% line.
                ax.plot([x_lo, end], [_y_strip / 2] * 2, color=INK_3,
                        lw=0.6, ls=(0, (1, 2)), alpha=0.7, zorder=2.5)
                for frac, lab in ((1.0, "100%"), (0.5, "50%")):
                    ax.text(x_lo + (x_hi - x_lo) * 0.002,
                            frac * _y_strip + 0.015, lab, ha="left",
                            va="bottom", fontsize=6.2, color=INK_3,
                            zorder=6).set_path_effects([
                                path_effects.withStroke(
                                    linewidth=2.0, foreground=PANEL)])
                legend_rows.append((
                    "forecast chance exposure exceeds each value",
                    pdf_edge_color, 1.0))
            elif fdens.max() > 0:
                fdens = fdens * _y_strip / fdens.max()
                ax.fill_between(fgrid, fdens, 0, facecolor=pdf_fill_color,
                                alpha=0.9, linewidth=0, zorder=2)
                ax.plot(fgrid, fdens, color=pdf_edge_color, lw=1.0,
                        alpha=0.9, zorder=3)
                legend_rows.append((
                    "forecast probabilistic distribution", pdf_edge_color,
                    1.0))

    # Historical storms: a tick each, VERTICAL name labels above. Vertical
    # packs several times more names per axis-inch than horizontal, so most
    # storms get named; genuinely unresolvable crowding drops the smallest —
    # except the "most similar storms" (marked bold upstream), which are the
    # comparators the table points the reader at: always named, in bold.
    if hist:
        ordered = sorted(hist, key=lambda m: -float(m.value))
        labels = [m.label.replace("\n", " ") for m in ordered]
        bolds = [m.bold for m in ordered]
        half = _label_half_widths(
            ax, fig, labels, _HIST_FONTSIZE, rotation=90, bold_flags=bolds)
        pad = half[0] * 0.9 if half else 0.0
        placed, keep = _place_labels(
            [(float(m.value), h) for m, h in zip(ordered, half, strict=True)],
            x_lo, x_hi, pad, droppable=True, protected=bolds,
        )
        for m, lb, px, kp in zip(ordered, labels, placed, keep, strict=True):
            x_v = float(m.value)
            ax.plot([x_v, x_v], [0, _y_rug], color=INK_3,
                    lw=1.3 if m.bold else 0.9, alpha=0.85 if m.bold else 0.75,
                    zorder=3, solid_capstyle="butt")
            if not kp:
                continue
            if abs(px - x_v) > (x_hi - x_lo) * 0.004:
                _leader(ax, px, _y_rug_label - 0.015, x_v, _y_rug + 0.015,
                        INK_3)
            txt = ax.text(px, _y_rug_label, lb, rotation=90,
                          rotation_mode="anchor", ha="left", va="center",
                          fontsize=_HIST_FONTSIZE,
                          color=INK_2 if m.bold else INK_3,
                          fontweight="bold" if m.bold else "normal",
                          zorder=5)
            txt.set_path_effects([
                path_effects.withStroke(linewidth=2.0, foreground=PANEL)
            ])

    # Source estimates: dots ON the axis line, labels in a row just below it.
    if srcs:
        labels = [
            f"{m.label}  {_fmt_pop(float(m.value), None)}" for m in srcs
        ]
        half = _label_half_widths(ax, fig, labels, _SRC_FONTSIZE)
        pad = _label_half_widths(ax, fig, ["nn"], _SRC_FONTSIZE)[0] * 2
        placed, _keep = _place_labels(
            [(float(m.value), h) for m, h in zip(srcs, half, strict=True)],
            x_lo, x_hi, pad,
        )
        for m, lbl, px in zip(srcs, labels, placed, strict=True):
            x_v = float(m.value)
            ax.plot([x_v], [0], marker="o", ms=8.5, mfc=m.color,
                    mec=PANEL, mew=1.2, zorder=6, clip_on=False)
            if abs(px - x_v) > (x_hi - x_lo) * 0.004:
                ax.annotate(
                    "", xy=(x_v, -0.02), xycoords=below,
                    xytext=(px, -0.12), textcoords=below,
                    arrowprops=dict(arrowstyle="-", color=m.color, lw=0.6,
                                    alpha=0.55, shrinkA=2, shrinkB=3),
                    annotation_clip=False,
                )
            txt = ax.text(px, -0.14, lbl, transform=below, ha="center",
                          va="top", fontsize=_SRC_FONTSIZE, color=INK_2,
                          zorder=6, clip_on=False)
            txt.set_path_effects([
                path_effects.withStroke(linewidth=2.2, foreground=PANEL)
            ])

    # Observed-so-far reference: a dashed line, labelled at its top.
    for m in obs:
        x_v = float(m.value)
        ax.plot([x_v, x_v], [0, y_top * 0.86], color=INK, lw=1.1,
                ls=(0, (3, 2)), alpha=0.8, zorder=5, solid_capstyle="butt")
        _hw = _label_half_widths(ax, fig, [m.label], 6.8)[0]
        x_t = min(max(x_v, x_lo + _hw), x_hi - _hw)
        t_obs = ax.text(x_t, y_top * 0.88, m.label, ha="center",
                        va="bottom", fontsize=6.8, color=INK_2, zorder=6)
        t_obs.set_path_effects([
            path_effects.withStroke(linewidth=2.0, foreground=PANEL)
        ])

    # Wind-threshold marker, top-right: plain bold text in the threshold
    # colour (the pill box was visual noise), replacing the x-axis label and
    # the per-chart heading.
    if wind_chip_kt is not None:
        c = _NHC_WIND_COLOR.get(int(wind_chip_kt), INK_2)
        ax.text(
            0.998, 0.97, f"{int(wind_chip_kt)} kt",
            transform=ax.transAxes, ha="right", va="top",
            fontsize=10, color=c, fontweight="bold", zorder=7,
        )

    # Mini-legend for the baseline curves, top-left — fixed position so every
    # chart reads the same way. Vertical storm names stop below it.
    for i, (lbl, c, a) in enumerate(legend_rows):
        y_f = 0.965 - i * 0.115
        # A drawn swatch, not a text glyph — U+25AC has no glyph in the
        # fallback fonts and rendered as a hollow box.
        ax.plot([0.004, 0.024], [y_f - 0.028, y_f - 0.028], color=c, lw=2.2,
                alpha=a, transform=ax.transAxes, zorder=7, clip_on=False,
                solid_capstyle="butt")
        ax.text(0.032, y_f, lbl, transform=ax.transAxes, ha="left",
                va="top", fontsize=6.8, color=INK_2, zorder=7).set_path_effects(
            [path_effects.withStroke(linewidth=2.0, foreground=PANEL)])

    ax.set_yticks([])
    if title:
        ax.set_title(title, fontsize=11, fontweight="bold", loc="left",
                     color=INK)
    ax.set_xlabel("")
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(_fmt_pop))
    # Big pad drops the numeric labels below the source-label row.
    ax.tick_params(axis="x", which="both", length=3, color=LINE,
                   labelsize=7.5, labelcolor=INK_3, pad=22)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(LINE)

    if total_pop is not None and total_pop > 0:
        xfrac = (total_pop - x_lo) / (x_hi - x_lo)
        _crowded = any(
            abs(float(m.value) - total_pop) < (x_hi - x_lo) * 0.03
            for m in srcs
        )
        if 0.0 <= xfrac <= 1.0 and not _crowded:
            ax.annotate(
                "total population",
                xy=(xfrac, 0.0), xycoords="axes fraction",
                xytext=(xfrac, -0.52 / axes_in), textcoords="axes fraction",
                ha="center", va="top", fontsize=6.6, color=INK_3,
                arrowprops=dict(arrowstyle="-", color=INK_3, lw=1.1,
                                shrinkA=0, shrinkB=0),
                annotation_clip=False,
            )

    return _fig_to_img_tag(fig, alt=title)


def wind_speed_color(wind_speed_kt: int) -> str:
    """Mark colour for the R34/R50/R64 wind threshold (HDX status ramp)."""
    return _NHC_WIND_COLOR.get(int(wind_speed_kt), INK_2)


def wind_speed_fill(wind_speed_kt: int) -> str:
    """Pale same-hue step, for the WSP density strip under the marks."""
    return _WIND_RAMP.get(int(wind_speed_kt), (GREY_FILL, INK_2))[0]


def country_strip_chart(
    iso3: str,
    wind_speed_kt: int,
    marks: list[StormMark],
    x_max: float | None = None,
    pdf: WspPdf | None = None,
    pdf_style: str = "density",
    hist_values: list[float] | None = None,
    total_pop: int | None = None,
) -> str:
    # Title omitted — surrounding HTML headings carry country / source; the
    # in-chart chip carries quantity + threshold.
    return _strip_chart(
        title="",
        x_label="",
        marks=marks,
        x_max=x_max,
        pdf=pdf,
        pdf_fill_color=wind_speed_fill(wind_speed_kt),
        pdf_edge_color=wind_speed_color(wind_speed_kt),
        pdf_style=pdf_style,
        hist_values=hist_values,
        hist_fill_color=wind_speed_fill(wind_speed_kt),
        hist_edge_color=wind_speed_color(wind_speed_kt),
        total_pop=total_pop,
        wind_chip_kt=wind_speed_kt,
    )


def gdacs_strip_chart(
    iso3: str,
    wind_speed_kt: int,
    marks: list[StormMark],
    x_max: float | None = None,
) -> str:
    return _strip_chart(
        title="",
        x_label=f"Population exposed ({wind_speed_kt} kt wind) — GDACS",
        marks=marks,
        x_max=x_max,
    )


def adam_strip_chart(
    iso3: str,
    wind_speed_kt: int,
    marks: list[StormMark],
    x_max: float | None = None,
) -> str:
    return _strip_chart(
        title="",
        x_label=f"Population exposed ({wind_speed_kt} kt wind) — ADAM",
        marks=marks,
        x_max=x_max,
    )


# Tile basemap. CartoDB Voyager: light, but with a real ocean blue and place
# names, while the wind swaths stay the loudest thing on the map. Everything
# here is plotted in EPSG:4326; contextily warps the tiles rather than us
# reprojecting the storm geometry.
_BASEMAP_CRS = "EPSG:4326"
# Cap the zoom: contextily's auto-zoom fetches a lot of tiles for a basin-scale
# view, and detail past this is invisible at email render size anyway.
_BASEMAP_MAX_ZOOM = 6
_basemap_failed = False


def _add_basemap(ax) -> bool:
    """Draw a tile basemap under the storm layers. True if it landed.

    Tiles are a network call inside a scheduled job, so failure has to be
    survivable: on any error this returns False and the caller falls back to the
    packaged Natural Earth polygons, which need no network. The first failure
    latches, so one outage doesn't mean a timeout per map for the rest of the
    run.
    """
    global _basemap_failed
    if _basemap_failed:
        return False
    try:
        import contextily as ctx

        ctx.add_basemap(
            ax,
            crs=_BASEMAP_CRS,
            # Voyager over Positron: proper light-blue ocean and warmer land,
            # so sea reads as sea instead of a grey void.
            source=ctx.providers.CartoDB.Voyager,
            # One zoom level deeper than contextily's choice: the maps render
            # at 150 dpi (see _fig_to_img_tag), so the auto zoom — picked for
            # the 100 dpi canvas — comes out soft.
            zoom_adjust=1,
            zorder=0,
            attribution_size=5,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — any tile failure is non-fatal
        _basemap_failed = True
        logging.getLogger(__name__).warning(
            f"Basemap tiles unavailable ({exc}); using the offline boundary "
            f"layer instead."
        )
        return False


def _drop_tiny_parts(geom, min_area: float = 0.05):
    """Drop polygon parts smaller than min_area (sq degrees) from a MultiPolygon."""
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type == "MultiPolygon":
        from shapely.geometry import MultiPolygon
        parts = [p for p in geom.geoms if p.area >= min_area]
        if not parts:
            return geom  # keep at least something
        return MultiPolygon(parts) if len(parts) > 1 else parts[0]
    return geom


def _draw_countries(ax, countries: gpd.GeoDataFrame, on_basemap: bool) -> None:
    """Draw adm0 outlines — the offline world background layer.

    Skipped entirely when tiles loaded: Positron already draws coastlines and
    national borders, and a second set of outlines over them is just noise. This
    layer exists so the map still reads when the tile fetch fails.
    """
    if countries.empty or on_basemap:
        return
    countries.plot(ax=ax, facecolor="#f4f5f7", edgecolor="#cdd2d9",
                   linewidth=0.5, zorder=1)


def _draw_adm1(ax, adm1_gdf: gpd.GeoDataFrame, on_basemap: bool) -> None:
    """Draw adm1 polygons for affected countries with internal division lines."""
    if adm1_gdf.empty:
        return
    adm1_gdf.plot(
        ax=ax,
        facecolor="none" if on_basemap else "#f4f5f7",
        edgecolor=GREY_EDGE if on_basemap else "#cdd2d9",
        linewidth=0.35, zorder=1,
    )
    # Emphasise the national (adm0) border with a slightly thicker line.
    outer = adm1_gdf.dissolve(by="iso_3", as_index=False)
    outer.plot(ax=ax, facecolor="none",
               edgecolor=INK_2 if on_basemap else "#9aa0a8",
               linewidth=0.8, zorder=1)


def _draw_obsv_buffers(ax, buffers: gpd.GeoDataFrame) -> list[mpatches.Patch]:
    """Plot observed buffers using NHC wind-speed colors. Largest (34 kt) first."""
    proxies: list[mpatches.Patch] = []
    obs = buffers[buffers["kind"] == "observed"]
    # Draw widest (lowest wind speed) first so higher-wind zones sit on top.
    valid = obs[~(obs.geometry.is_empty | obs.geometry.isna())].sort_values("wind_speed_kt")
    if valid.empty:
        return proxies
    colors = [_NHC_WIND_COLOR.get(int(w), "#888888") for w in valid["wind_speed_kt"]]
    valid.plot(ax=ax, color=colors, edgecolor="none", alpha=_OBSV_BUFFER_ALPHA, zorder=2)
    for wsp in sorted(valid["wind_speed_kt"].unique()):
        proxies.append(mpatches.Patch(
            facecolor=_NHC_WIND_COLOR.get(int(wsp), "#888888"),
            alpha=_OBSV_BUFFER_ALPHA, label=f"{int(wsp)} kt",
        ))
    return proxies


def _draw_fcast_buffers(ax, buffers: gpd.GeoDataFrame) -> list[mpatches.Patch]:
    """Plot forecast-only buffers using NHC wind-speed colors."""
    proxies: list[mpatches.Patch] = []
    fcs = buffers[buffers["kind"] == "forecast"]
    valid = fcs[~(fcs.geometry.is_empty | fcs.geometry.isna())].sort_values("wind_speed_kt")
    if valid.empty:
        return proxies
    colors = [_NHC_WIND_COLOR.get(int(w), "#888888") for w in valid["wind_speed_kt"]]
    valid.plot(ax=ax, color=colors, edgecolor="none", alpha=_FCAST_BUFFER_ALPHA, zorder=2)
    for wsp in sorted(valid["wind_speed_kt"].unique()):
        proxies.append(mpatches.Patch(
            facecolor=_NHC_WIND_COLOR.get(int(wsp), "#888888"),
            alpha=_FCAST_BUFFER_ALPHA, label=f"{int(wsp)} kt",
        ))
    return proxies


def _draw_wsp_polygons(
    ax,
    wsp: gpd.GeoDataFrame,
    wind_threshold_kt: int,
) -> list[mpatches.Patch]:
    """Plot WSP fcastonly polygons (widest/lowest probability first), matching
    the harmonisation app's NHC categorical palette. The 0% band is white with
    a faint grey outline so it remains visible.
    """
    proxies: list[mpatches.Patch] = []
    if wsp.empty:
        return proxies
    # Draw low-to-high so higher-probability (darker) bands sit on top.
    ordered = wsp.sort_values("percentage")
    # 0% band gets an outline — one call; remaining bands batched into one call.
    zero = ordered[ordered["percentage"] == 0]
    if not zero.empty:
        zero.plot(ax=ax, facecolor=_NHC_WSP_COLOR.get(0, PANEL),
                  edgecolor=GREY_EDGE, linewidth=0.6, alpha=_WSP_ALPHA, zorder=2)
    rest = ordered[ordered["percentage"] != 0]
    if not rest.empty:
        colors = [_NHC_WSP_COLOR.get(int(p), GREY_EDGE) for p in rest["percentage"]]
        rest.plot(ax=ax, color=colors, edgecolor="none", alpha=_WSP_ALPHA, zorder=2)
    for pct in sorted(wsp["percentage"].unique()):
        color = _NHC_WSP_COLOR.get(int(pct), GREY_EDGE)
        edgecolor = GREY_EDGE if int(pct) == 0 else "none"
        linewidth = 0.6 if int(pct) == 0 else 0
        proxies.append(mpatches.Patch(
            facecolor=color, alpha=_WSP_ALPHA,
            edgecolor=edgecolor, linewidth=linewidth,
            label=f"≥{int(pct)}%",
        ))
    return proxies


def _draw_tracks(ax, tracks: gpd.GeoDataFrame) -> None:
    for atcf_id, storm in tracks.groupby("atcf_id"):
        obs = storm[storm["kind"] == "observed"].sort_values("valid_time")
        fcs = storm[storm["kind"] == "forecast"].sort_values("valid_time")

        if not obs.empty:
            ax.plot(
                obs.geometry.x, obs.geometry.y,
                color="#222222", linewidth=2, zorder=3,
                label=f"{atcf_id} observed",
                path_effects=[_TRACK_CASING],
            )
            ax.scatter(
                obs.geometry.x, obs.geometry.y,
                color="#222222", s=15, zorder=4,
                edgecolors="white", linewidths=0.6,
            )

        if not fcs.empty:
            # The forecast line follows the same 30-minute PCHIP densification
            # the wind buffers are built from (src/landfall.densify_track), so
            # the dashed line curves WITH its swath instead of cutting straight
            # doglegs between six-hourly points. The densified path starts at
            # the last observed position, which also covers the bridge.
            _dt, _dlon, _dlat, _dw = densify_track(storm)
            if len(_dlon):
                ax.plot(
                    _dlon, _dlat,
                    color="#444444", linewidth=2, linestyle="--", zorder=3,
                    label=f"{atcf_id} forecast",
                    path_effects=[_TRACK_CASING],
                )
            else:
                ax.plot(
                    fcs.geometry.x, fcs.geometry.y,
                    color="#444444", linewidth=2, linestyle="--", zorder=3,
                    label=f"{atcf_id} forecast",
                    path_effects=[_TRACK_CASING],
                )
            ax.scatter(
                fcs.geometry.x, fcs.geometry.y,
                color="#444444", s=18, marker="D", zorder=4,
                edgecolors="white", linewidths=0.6,
            )

            # Label every forecast point, alternating offset to reduce overlap
            offsets = [(8, 10), (8, -14), (-10, 10), (-10, -14)]
            for i, (_, row) in enumerate(fcs.iterrows()):
                dx, dy = offsets[i % len(offsets)]
                ax.annotate(
                    _format_ny(row["valid_time"]),
                    xy=(row.geometry.x, row.geometry.y),
                    xytext=(dx, dy),
                    textcoords="offset points",
                    fontsize=6.5,
                    color="#333333",
                    zorder=5,
                    arrowprops=dict(
                        arrowstyle="-",
                        color="#b0b0b0",
                        linewidth=0.4,
                        shrinkA=0,
                        shrinkB=1,
                    ),
                    bbox=dict(
                        boxstyle="round,pad=0.3",
                        facecolor="white",
                        edgecolor="none",
                        alpha=0.8,
                    ),
                )


def _forecast_view_bbox(
    tracks: gpd.GeoDataFrame,
    forecast_features: gpd.GeoDataFrame,
    obsv_buffers: gpd.GeoDataFrame | None = None,
    n_tail_obs: int = 5,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Bbox covering the forecast features plus the most recent N observed points.

    obsv_buffers: when provided and there is no forecast, these are added to the
    bbox so the map shows the full cross-sectional width of the current
    wind-radius rings rather than just the tight bounding box of the track tail.
    """
    obs = tracks[tracks["kind"] == "observed"].sort_values("valid_time")
    obs_tail = obs.tail(n_tail_obs)
    fcs = tracks[tracks["kind"] == "forecast"]

    pieces = [g for g in (obs_tail, fcs, forecast_features) if g is not None and not g.empty]

    # No forecast: expand to show the observed buffer width
    if fcs.empty and forecast_features.empty and obsv_buffers is not None and not obsv_buffers.empty:
        pieces.append(obsv_buffers)

    if not pieces:
        pieces = [tracks]

    minx = min(p.total_bounds[0] for p in pieces)
    miny = min(p.total_bounds[1] for p in pieces)
    maxx = max(p.total_bounds[2] for p in pieces)
    maxy = max(p.total_bounds[3] for p in pieces)
    pad_x = (maxx - minx) * 0.10 or 2
    pad_y = (maxy - miny) * 0.10 or 2
    return (minx - pad_x, maxx + pad_x), (miny - pad_y, maxy + pad_y)


def _finalize_map(ax, title: str, legend_handles: list) -> None:
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("")
    ax.set_ylabel("")
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.set_title(
        title, fontsize=12, fontweight="bold", loc="left",
        color="#222222", pad=12,
    )
    if legend_handles:
        ax.legend(
            handles=legend_handles,
            loc="upper right", fontsize=8, framealpha=0.85,
        )


def _add_time_note(ax, tracks: gpd.GeoDataFrame) -> None:
    """Small footnote clarifying all point times are ET (forecast points only).

    Bottom-RIGHT: contextily puts the tile attribution bottom-left, and the two
    overprinted each other into an unreadable smudge.
    """
    if tracks.empty or not (tracks["kind"] == "forecast").any():
        return
    ax.text(
        0.99, 0.01, "Times shown in ET (America/New_York)",
        transform=ax.transAxes, fontsize=7, color=INK_3,
        ha="right", va="bottom", zorder=6,
        path_effects=[path_effects.withStroke(linewidth=2.0, foreground=PANEL)],
    )


def _track_legend_handles(tracks: gpd.GeoDataFrame) -> list:
    """Legend proxies for the observed/forecast track lines (no 'track' suffix)."""
    handles: list = []
    if not tracks.empty and (tracks["kind"] == "observed").any():
        handles.append(Line2D([0], [0], color="#222222", lw=2, label="Observed"))
    if not tracks.empty and (tracks["kind"] == "forecast").any():
        handles.append(
            Line2D([0], [0], color="#444444", lw=2, ls=(0, (4, 2)), label="Forecast")
        )
    return handles


def _add_stacked_legends(ax, fig, groups: list[tuple[str, list]]) -> None:
    """Add several titled legends as one tight vertical stack off the right edge.

    groups is an ordered list of (title, handles). Each legend's height is
    measured after creation so the next sits directly beneath it (a contiguous
    stack), regardless of entry counts. The off-axes legends are captured by
    savefig(bbox_inches="tight").
    """
    # Equal-aspect axes are LETTERBOXED at draw time: a very wide map (Beryl's
    # Atlantic crossing) ends up a fraction of the subplot cell's height. The
    # legend offsets are fractions of the axes height, so they must be
    # computed against the aspect-applied box — measured before apply_aspect
    # they come out too small and the stacked legends overlap.
    ax.apply_aspect()
    renderer = fig.canvas.get_renderer()
    ax_h = ax.get_window_extent(renderer=renderer).height
    y = 1.0
    for title, handles in groups:
        if not handles:
            continue
        leg = ax.legend(
            handles=handles, title=title,
            loc="upper left", bbox_to_anchor=(1.03, y),
            fontsize=8, title_fontsize=8.5, framealpha=0.92,
            facecolor="white", edgecolor="#e2e2e2", fancybox=True,
            borderaxespad=0, borderpad=0.6, labelspacing=0.35,
            handlelength=1.5, handletextpad=0.6,
        )
        leg._legend_box.align = "left"
        _t = leg.get_title()
        _t.set_ha("left")
        _t.set_color("#333333")
        _t.set_fontweight("semibold")
        ax.add_artist(leg)
        # add_artist clips to the axes patch; these sit off the right edge, so
        # disable clipping or they vanish (and drop out of the tight bbox).
        leg.set_clip_on(False)
        leg_h = leg.get_window_extent(renderer=renderer).height
        y -= leg_h / ax_h + 0.012  # tight gap → reads as a single stack


# Exposure choropleth bins (population exposed, 34 kt). Fixed across storms so
# a repeat reader learns the scale once; log-spaced because exposure spans five
# orders of magnitude. HDX brand ramp — sequential, one hue, light -> dark —
# which is also gghdx's default sequential, so it reads as "an HDX choropleth".
_EXP_BINS: list[tuple[float, str]] = [
    (10_000, "#e9f5f1"),      # --hdx-brand-05
    (100_000, "#a8d5c9"),     # --hdx-brand-2
    (1_000_000, "#51ac92"),   # --hdx-brand-4
    (5_000_000, "#1e795f"),   # --hdx-brand-6
    (float("inf"), "#0f3c30"),  # --hdx-brand-8
]
_EXP_BIN_LABELS = ["< 10K", "10K – 100K", "100K – 1M", "1M – 5M", "> 5M"]


def _exp_bin_color(pop: float) -> str:
    for cut, color in _EXP_BINS:
        if pop < cut:
            return color
    return _EXP_BINS[-1][1]


def track_plot_exposure(
    tracks: gpd.GeoDataFrame,
    buffers: gpd.GeoDataFrame,
    background: gpd.GeoDataFrame,
    adm1_gdf: gpd.GeoDataFrame,
    adm1_exp,
    adm0_exp: dict[str, int],
    adm0_gdf: gpd.GeoDataFrame | None = None,
    storm_name: str = "",
) -> str:
    """The single storm map for the condensed email: track, observed and
    forecast wind swaths, and population exposed shaded by admin-1.

    This replaces the deterministic/probabilistic map PAIR in the email — the
    reader's question is "who is in the path", which neither map answered
    directly. The swaths keep their familiar translucent fills from the
    deterministic map; the exposure choropleth is painted OVER them, so the
    fills read at full strength over open water while land carries the
    exposure shading — each surface answers the question it is best at.

    adm1_exp: rows of (iso3, fm_pcode, pop_exposed) — consolidated 34 kt MAX
    across sources, same numbers as the attached workbook. adm0_exp covers
    countries with national exposure but no admin-1 rows; their whole adm0
    polygon (from adm0_gdf) gets the national bin so small territories don't
    vanish from the map.
    """
    if tracks.empty:
        return ""
    _fcast_buf = buffers[buffers["kind"] == "forecast"] if not buffers.empty else buffers
    _obsv_buf = buffers[buffers["kind"] == "observed"] if not buffers.empty else buffers
    xlim, ylim = _forecast_view_bbox(tracks, _fcast_buf, obsv_buffers=_obsv_buf)
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.set_aspect("equal")
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    on_basemap = _add_basemap(ax)
    _draw_countries(ax, background, on_basemap)

    # Swath fills go down FIRST; the exposure choropleth (also zorder 2, drawn
    # later) covers them on shaded land. Net effect: swaths show over water and
    # unshaded land, exposure wins where it exists.
    obsv_proxies = _draw_obsv_buffers(ax, buffers)
    fcast_proxies = _draw_fcast_buffers(ax, buffers)

    # --- exposure shading (drawn over the fills; edges added after) --------
    shaded_iso3s: set[str] = set()
    if adm1_exp is not None and len(adm1_exp) and not adm1_gdf.empty:
        merged = adm1_gdf.merge(
            adm1_exp[adm1_exp["pop_exposed"] > 0],
            left_on="adm1_id", right_on="fm_pcode", how="inner",
        )
        if not merged.empty:
            merged.plot(
                ax=ax,
                color=[_exp_bin_color(p) for p in merged["pop_exposed"]],
                edgecolor=PANEL, linewidth=0.4, alpha=0.9, zorder=2,
            )
            shaded_iso3s = set(merged["iso3"])
    # National fallback for countries the admin-1 layer doesn't cover — but
    # ONLY small territories. Painting all of Mexico one shade because its
    # admin-1 rows are missing asserts sub-national knowledge we don't have;
    # for an island the adm0 and the affected area are the same thing.
    _FALLBACK_MAX_DEG2 = 5.0
    fallback_geoms: dict[str, object] = {}
    if adm0_gdf is not None and not adm0_gdf.empty:
        fb = adm0_gdf[
            adm0_gdf["iso_3"].isin(
                {k for k, v in adm0_exp.items() if v > 0} - shaded_iso3s
            )
        ]
        with warnings.catch_warnings():
            # Area in squared degrees is exactly what we want here — it is a
            # size-on-this-map test, not a physical area — so the
            # geographic-CRS warning is noise.
            warnings.simplefilter("ignore", UserWarning)
            fb = fb[fb.geometry.area < _FALLBACK_MAX_DEG2]
        if not fb.empty:
            fb.plot(
                ax=ax,
                color=[_exp_bin_color(adm0_exp[i]) for i in fb["iso_3"]],
                edgecolor=PANEL, linewidth=0.4, alpha=0.9, zorder=2,
            )
            fallback_geoms = dict(zip(fb["iso_3"], fb.geometry, strict=True))
    # Country outlines over the shading so units still group into countries.
    if not adm1_gdf.empty:
        outer = adm1_gdf.dissolve(by="iso_3", as_index=False)
        outer.plot(ax=ax, facecolor="none", edgecolor=INK_2, linewidth=0.7,
                   zorder=3)

    # Forecast swath EDGES over the choropleth: the fills sit under the
    # exposure shading (bold greens win on land), so without edges the swath
    # disappears exactly where it matters most. The outline carries it across
    # shaded countries.
    if not _fcast_buf.empty:
        _valid_f = _fcast_buf[
            ~(_fcast_buf.geometry.is_empty | _fcast_buf.geometry.isna())
        ]
        for _wsp_v, _grp in _valid_f.groupby("wind_speed_kt"):
            _grp.plot(ax=ax, facecolor="none",
                      edgecolor=_NHC_WIND_COLOR.get(int(_wsp_v), INK_2),
                      linewidth=1.5, alpha=0.95, zorder=3)

    # Small-island visibility: a shaded polygon that covers well under a
    # thousandth of the view is invisible at email render size, and for a
    # Lesser-Antilles storm that is every affected country. Mark those with a
    # bin-coloured ring at the centroid so the exposure reads at a glance.
    view_area = (xlim[1] - xlim[0]) * (ylim[1] - ylim[0])
    marked_any = False
    country_geom: dict[str, object] = dict(fallback_geoms)
    if shaded_iso3s and not adm1_gdf.empty:
        for iso3_v, grp in adm1_gdf[adm1_gdf["iso_3"].isin(shaded_iso3s)].groupby("iso_3"):
            country_geom[iso3_v] = grp.geometry.union_all()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        geom_area = {k: (g.area if g is not None and not g.is_empty else 0.0)
                     for k, g in country_geom.items()}
    for iso3_v, geom in country_geom.items():
        if geom is None or geom.is_empty:
            continue
        if geom_area[iso3_v] / view_area < 0.0008:
            c = geom.centroid
            ax.plot(
                [c.x], [c.y], marker="o", ms=11,
                mfc=_exp_bin_color(adm0_exp.get(iso3_v, 0)),
                mec=INK_2, mew=1.2, alpha=0.95, zorder=4, clip_on=True,
            )
            marked_any = True

    _draw_tracks(ax, tracks)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)

    _finalize_map(
        ax,
        title=(
            f"{storm_name}: forecast track and population exposed"
            if storm_name else "Forecast track and population exposed"
        ),
        legend_handles=[],
    )
    fig.tight_layout()
    exp_handles = [
        mpatches.Patch(facecolor=c, edgecolor=LINE, label=lbl)
        for (_, c), lbl in zip(_EXP_BINS, _EXP_BIN_LABELS, strict=True)
    ]
    if marked_any:
        exp_handles.append(Line2D(
            [0], [0], marker="o", ls="", ms=9, mfc=PANEL, mec=INK_2, mew=1.2,
            label="small territory",
        ))
    _add_stacked_legends(ax, fig, [
        ("Population\nexposed", exp_handles),
        ("Forecasted\nwind swaths", fcast_proxies),
        ("Observed\nwind swaths", obsv_proxies),
        ("Tracks", _track_legend_handles(tracks)),
    ])
    _add_time_note(ax, tracks)
    ax.text(
        0.99, 0.045, "Shading: admin-1 where available",
        transform=ax.transAxes, fontsize=6.5, color=INK_3,
        ha="right", va="bottom", zorder=6,
        path_effects=[path_effects.withStroke(linewidth=2.0, foreground=PANEL)],
    )
    return _fig_to_img_tag(
        fig,
        alt=(f"{storm_name}: forecast track and population exposed map"
             if storm_name else "Forecast track and population exposed map"),
        dpi=150,
    )


def track_plot_buffers(
    tracks: gpd.GeoDataFrame,
    buffers: gpd.GeoDataFrame,
    background: gpd.GeoDataFrame,
    adm1_gdf: gpd.GeoDataFrame | None = None,
    storm_name: str = "",
) -> str:
    """Map: storm tracks + 34/50/64 kt observed and forecast-only buffers.

    background is a world-level adm0 layer (e.g. Natural Earth 110m).
    Affected countries in adm1_gdf are rendered with adm1 division lines on top.
    Axis limits clip the view without creating artificial boundary edges.
    """
    if tracks.empty:
        return ""
    _fcast_buf = buffers[buffers["kind"] == "forecast"] if not buffers.empty else buffers
    _obsv_buf = buffers[buffers["kind"] == "observed"] if not buffers.empty else buffers
    xlim, ylim = _forecast_view_bbox(tracks, _fcast_buf, obsv_buffers=_obsv_buf)
    fig, ax = plt.subplots(figsize=(9, 6))
    # Aspect and limits first, in that order: contextily picks its tile extent
    # from the axes' current view, and set_aspect can move the limits. Fetching
    # before either is settled leaves part of the frame with no tiles under it.
    ax.set_aspect("equal")
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    on_basemap = _add_basemap(ax)
    _draw_countries(ax, background, on_basemap)
    if adm1_gdf is not None and not adm1_gdf.empty:
        _draw_adm1(ax, adm1_gdf, on_basemap)
    obsv_proxies = _draw_obsv_buffers(ax, buffers)
    fcast_proxies = _draw_fcast_buffers(ax, buffers)
    _draw_tracks(ax, tracks)
    # add_basemap snaps the view out to whole tiles; restore the intended frame.
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    # No on-plot legend — titled legends stacked off the right edge.
    _finalize_map(
        ax,
        title=(
            f"{storm_name}: track and swaths forecast" if storm_name
            else "Storm tracks — observed + forecast wind swaths"
        ),
        legend_handles=[],
    )
    # tight_layout first so the axes is at its final size before the off-axes
    # legends are measured and stacked against it.
    fig.tight_layout()
    _add_stacked_legends(ax, fig, [
        ("Observed\nwind swaths", obsv_proxies),
        ("Forecasted\nwind swaths", fcast_proxies),
        ("Tracks", _track_legend_handles(tracks)),
    ])
    _add_time_note(ax, tracks)
    return _fig_to_img_tag(
        fig,
        alt=(f"{storm_name}: forecast track and wind swaths map" if storm_name
             else "Storm tracks and forecast wind swaths map"),
        dpi=150,
    )


def track_plot_wsp(
    tracks: gpd.GeoDataFrame,
    buffers: gpd.GeoDataFrame,
    wsp: gpd.GeoDataFrame,
    background: gpd.GeoDataFrame,
    wind_threshold_kt: int = 50,
    adm1_gdf: gpd.GeoDataFrame | None = None,
    storm_name: str = "",
) -> str:
    """Map: tracks + observed buffers + WSP fcastonly polygons (one threshold).

    background is a world-level adm0 layer (e.g. Natural Earth 110m).
    Affected countries in adm1_gdf are rendered with adm1 division lines on top.
    Axis limits clip the view without creating artificial boundary edges.
    """
    # Omit the probabilistic section entirely when no WSP polygons are available
    # (otherwise this would render as a bare track/buffer plot with no probabilities).
    if tracks.empty or wsp.empty:
        return ""
    xlim, ylim = _forecast_view_bbox(tracks, wsp)
    fig, ax = plt.subplots(figsize=(9, 6))
    # Aspect and limits first, in that order: contextily picks its tile extent
    # from the axes' current view, and set_aspect can move the limits. Fetching
    # before either is settled leaves part of the frame with no tiles under it.
    ax.set_aspect("equal")
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    on_basemap = _add_basemap(ax)
    _draw_countries(ax, background, on_basemap)
    if adm1_gdf is not None and not adm1_gdf.empty:
        _draw_adm1(ax, adm1_gdf, on_basemap)
    obsv_proxies = _draw_obsv_buffers(ax, buffers)
    wsp_proxies = _draw_wsp_polygons(ax, wsp, wind_threshold_kt)
    _draw_tracks(ax, tracks)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)

    title = (
        f"{storm_name}: {wind_threshold_kt}-knot wind speed probabilities"
        if storm_name else f"{wind_threshold_kt}-knot wind speed probabilities"
    )
    # No on-plot legend — three titled legends are stacked off the right edge.
    _finalize_map(ax, title=title, legend_handles=[])

    # tight_layout first so the axes is at its final size before the off-axes
    # legends are measured and stacked against it.
    fig.tight_layout()
    _add_stacked_legends(ax, fig, [
        (f"Probability of\n≥{wind_threshold_kt} kt winds", wsp_proxies),
        ("Observed\nwind swaths", obsv_proxies),
        ("Tracks", _track_legend_handles(tracks)),
    ])
    _add_time_note(ax, tracks)
    return _fig_to_img_tag(fig, alt=title, dpi=150)


def _fig_to_img_tag(fig: plt.Figure, alt: str = "", dpi: int = 100) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    png = buf.read()
    # Emit explicit width/height + alt. Outlook desktop (Word engine) ignores
    # max-width CSS and otherwise renders at the raw pixel size, so without a
    # width attribute the legends-widened maps overflow; with one it scales
    # correctly. Gmail/Apple Mail/mobile keep scaling responsively via
    # max-width:100%;height:auto. alt gives a graceful state while loading or if
    # an image ever fails to load.
    nat_w, nat_h = Image.open(io.BytesIO(png)).size
    w = min(nat_w, _EMAIL_CONTENT_WIDTH_PX)
    h = max(1, round(w * nat_h / nat_w))
    img_b64 = base64.b64encode(png).decode("utf-8")
    alt_attr = _html.escape(alt, quote=True)
    style = f"width:{w}px;max-width:100%;height:auto;display:block;margin-bottom:8px"
    return (
        f'<img src="data:image/png;base64,{img_b64}" '
        f'width="{w}" height="{h}" alt="{alt_attr}" style="{style}">'
    )
