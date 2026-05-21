import base64
import io
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")  # must be before pyplot import
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
_HIST_COLOR = "#888888"
_OBSV_COLOR = "#1e8449"
_FCAST_COLOR = "#b9651b"

# NHC-style wind-radii colors by wind speed (R34 / R50 / R64),
# matched to the ds-storm-impact-harmonisation app.
_NHC_WIND_COLOR = {
    34: "#f5c842",  # gold — tropical storm force
    50: "#f5a623",  # orange — strong tropical storm
    64: "#e8320a",  # red — hurricane force
}

# NHC WSP categorical colour scale, matched to the harmonisation app.
_NHC_WSP_COLOR = {
    0:  "#ffffff",  # white (needs grey outline)
    5:  "#00a000",  # dark green
    10: "#64c832",  # medium green
    20: "#b4e600",  # lime
    30: "#e8dc00",  # yellow
    40: "#c8a832",  # tan
    50: "#a07828",  # brown
    60: "#e06400",  # orange
    70: "#c82800",  # red
    80: "#901828",  # dark red
    90: "#641464",  # purple
}

_OBSV_BUFFER_ALPHA = 0.22
_FCAST_BUFFER_ALPHA = 0.65

_UTC = ZoneInfo("UTC")
_NY = ZoneInfo("America/New_York")


def _format_ny(t: datetime) -> str:
    """Format a UTC timestamp as a compact NY-time string."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=_UTC)
    local = t.astimezone(_NY)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a')} {hour}{local.strftime('%p')} ET"




@dataclass(frozen=True, slots=True)
class StormMark:
    """One vertical line in a strip chart.

    short=True draws a low line with a smaller label (for historical context).
    short=False draws a tall line with a larger label (for current/forecast).
    """
    value: int
    label: str
    color: str
    short: bool = False


# WSP probability band widths (fraction of total probability)
_WSP_BAND_WIDTH_FRAC = {
    0: 0.05, 5: 0.05, 10: 0.10, 20: 0.10, 30: 0.10,
    40: 0.10, 50: 0.10, 60: 0.10, 70: 0.10, 80: 0.10, 90: 0.10,
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


def _pdf_polygon(pdf: WspPdf) -> tuple[list[float], list[float]]:
    """Build (xs, ys) for a contiguous shaded WSP PDF.

    Each band contributes a horizontal segment at height = band_width_frac / pop,
    spanning width = pop. Total area ≈ 1. Bands sorted highest-probability first
    so the dense, certain core sits on the left.
    Returns ([], []) if there's nothing to draw.
    """
    bands = [(p, n) for p, n in pdf.bands if n > 0]
    if not bands:
        return [], []

    # Filter artifact bands: if a band has < 0.1% of the largest band's population
    # its density (bw/pop) is ≥1000× higher, causing it to dominate the y-scale
    # and compress the real distribution to near-zero height.
    max_pop = max(n for _, n in bands)
    min_pop = max(max_pop * 0.001, 50)
    bands = [(p, n) for p, n in bands if n >= min_pop]
    if not bands:
        return [], []

    bands.sort(key=lambda b: b[0], reverse=True)

    xs: list[float] = []
    ys: list[float] = []
    cum = pdf.x_offset
    for pct, pop in bands:
        bw = _WSP_BAND_WIDTH_FRAC.get(int(pct), 0.05)
        density = bw / pop
        xs.extend([cum, cum + pop])
        ys.extend([density, density])
        cum += pop
    return xs, ys


# y-axis layout (data units; matched to ylim below)
_Y_HIST_TOP = 0.06        # short historical lines stop here
_Y_HIST_LABEL = 0.08      # historical labels start here
_Y_PDF_TOP = 0.92         # PDF shaded area scaled to fit below this
_Y_TALL_TOP = 0.95        # current/forecast lines stop here
_Y_TALL_LABEL = 0.98      # current/forecast labels start here
_Y_TOP = 2.05             # ylim upper bound (headroom for two-line labels)


def _strip_chart(
    title: str,
    x_label: str,
    marks: list[StormMark],
    x_max: float | None = None,
    pdf: WspPdf | None = None,
    pdf_fill_color: str = "#888888",
    total_pop: int | None = None,
) -> str:
    # Drop marks that would be outside the chart's x range — their ax.text objects
    # at large data coordinates expand bbox_inches="tight" to data scale.
    nonzero = [
        m for m in marks
        if m.value > 0 and (x_max is None or x_max <= 0 or m.value <= x_max * 1.05)
    ]
    has_pdf = pdf is not None and any(n > 0 for _, n in pdf.bands)
    if not nonzero and not has_pdf and x_max is None:
        return ""

    fig, ax = plt.subplots(figsize=(9, 3.0))
    fig.subplots_adjust(left=0.01, right=0.97, top=0.95, bottom=0.20)

    # PDF as a single contiguous shaded area, scaled to sit under the tall marks.
    # Heights use a compressive ^0.3 so the long flat tail of low-density bands
    # stays visible alongside the tall high-density spike. Area no longer equals
    # probability; the shape conveys "where the WSP mass lives".
    if has_pdf:
        xs, ys = _pdf_polygon(pdf)
        if xs:
            ys_compressed = [y ** 0.3 for y in ys]
            max_y = max(ys_compressed)
            if max_y > 0:
                scale = _Y_PDF_TOP / max_y
                ys_scaled = [y * scale for y in ys_compressed]
                ax.fill_between(
                    xs, ys_scaled, 0,
                    facecolor=pdf_fill_color, alpha=0.45,
                    linewidth=0,
                    zorder=2,
                )

    for m in nonzero:
        if m.short:
            line_top = _Y_HIST_TOP
            label_y = _Y_HIST_LABEL
            fontsize = 6.0
            alpha = 0.85
            linewidth = 0.9
        else:
            line_top = _Y_TALL_TOP
            label_y = _Y_TALL_LABEL
            fontsize = 7.5
            alpha = 1.0
            linewidth = 1.6
        ax.plot(
            [m.value, m.value], [0, line_top],
            color=m.color, linewidth=linewidth, alpha=alpha,
            zorder=4, solid_capstyle="butt",
        )
        ax.text(
            m.value, label_y, m.label,
            rotation=90, ha="center", va="bottom",
            fontsize=fontsize, color=m.color, alpha=alpha, zorder=5,
        )

    ax.set_ylim(0, _Y_TOP)
    ax.set_yticks([])
    if title:
        ax.set_title(title, fontsize=11, fontweight="bold", loc="left")

    ax.set_xlabel(x_label)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(_fmt_pop))
    ax.tick_params(axis="x", which="both", length=4)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)

    if x_max is not None and x_max > 0:
        ax.set_xlim(0, x_max * 1.05)
    else:
        xmin, xmax = ax.get_xlim()
        ax.set_xlim(min(xmin, 0), xmax)

    if total_pop is not None and total_pop > 0:
        xlim = ax.get_xlim()
        xfrac = (total_pop - xlim[0]) / (xlim[1] - xlim[0])
        if 0.0 <= xfrac <= 1.0:
            # Heavy tick + "total pop." label using axes-fraction coordinates.
            # annotation_clip=False lets it draw below the axes without
            # affecting figure size (bbox_inches="tight" removed from savefig).
            ax.annotate(
                "total pop.",
                xy=(xfrac, 0.0),
                xycoords="axes fraction",
                xytext=(xfrac, -0.18),
                textcoords="axes fraction",
                ha="center", va="top",
                fontsize=5.5, color="#333333", fontweight="bold",
                arrowprops=dict(
                    arrowstyle="-",
                    color="#333333",
                    lw=2.0,
                    shrinkA=0, shrinkB=0,
                ),
                annotation_clip=False,
            )

    return _fig_to_img_tag(fig)


def wind_speed_color(wind_speed_kt: int) -> str:
    """NHC R34/R50/R64 wind-radius colour for the given wind speed."""
    return _NHC_WIND_COLOR.get(int(wind_speed_kt), "#888888")


def country_strip_chart(
    iso3: str,
    wind_speed_kt: int,
    marks: list[StormMark],
    x_max: float | None = None,
    pdf: WspPdf | None = None,
    total_pop: int | None = None,
) -> str:
    # Title omitted — surrounding HTML headings carry country / source.
    return _strip_chart(
        title="",
        x_label=f"Population exposed ({wind_speed_kt} kt wind)",
        marks=marks,
        x_max=x_max,
        pdf=pdf,
        pdf_fill_color=wind_speed_color(wind_speed_kt),
        total_pop=total_pop,
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


def _draw_countries(ax, countries: gpd.GeoDataFrame) -> None:
    """Draw adm0 outlines — world background layer."""
    if countries.empty:
        return
    countries.plot(ax=ax, facecolor="#f5f5f5", edgecolor="#aaaaaa", linewidth=0.5, zorder=1)


def _draw_adm1(ax, adm1_gdf: gpd.GeoDataFrame) -> None:
    """Draw adm1 polygons for affected countries with internal division lines."""
    if adm1_gdf.empty:
        return
    adm1_gdf.plot(
        ax=ax, facecolor="#f5f5f5", edgecolor="#bbbbbb", linewidth=0.35, zorder=1
    )
    # Emphasise the national (adm0) border with a slightly thicker line.
    outer = adm1_gdf.dissolve(by="iso_3", as_index=False)
    outer.plot(ax=ax, facecolor="none", edgecolor="#888888", linewidth=0.8, zorder=1)


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
            alpha=_OBSV_BUFFER_ALPHA, label=f"Observed {int(wsp)} kt",
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
            alpha=_FCAST_BUFFER_ALPHA, label=f"Forecast {int(wsp)} kt",
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
        zero.plot(ax=ax, facecolor=_NHC_WSP_COLOR.get(0, "#ffffff"),
                  edgecolor="#888888", linewidth=0.6, alpha=0.7, zorder=2)
    rest = ordered[ordered["percentage"] != 0]
    if not rest.empty:
        colors = [_NHC_WSP_COLOR.get(int(p), "#888888") for p in rest["percentage"]]
        rest.plot(ax=ax, color=colors, edgecolor="none", alpha=0.7, zorder=2)
    for pct in sorted(wsp["percentage"].unique()):
        color = _NHC_WSP_COLOR.get(int(pct), "#888888")
        edgecolor = "#888888" if int(pct) == 0 else "none"
        linewidth = 0.6 if int(pct) == 0 else 0
        proxies.append(mpatches.Patch(
            facecolor=color, alpha=0.7,
            edgecolor=edgecolor, linewidth=linewidth,
            label=f"WSP {wind_threshold_kt} kt ≥{int(pct)}%",
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
            )
            ax.scatter(
                obs.geometry.x, obs.geometry.y,
                color="#222222", s=15, zorder=4,
            )

        if not fcs.empty:
            if not obs.empty:
                bridge_x = [obs.geometry.x.iloc[-1], fcs.geometry.x.iloc[0]]
                bridge_y = [obs.geometry.y.iloc[-1], fcs.geometry.y.iloc[0]]
                ax.plot(
                    bridge_x, bridge_y,
                    color="#444444", linewidth=2, linestyle="--", zorder=3,
                )
            ax.plot(
                fcs.geometry.x, fcs.geometry.y,
                color="#444444", linewidth=2, linestyle="--", zorder=3,
                label=f"{atcf_id} forecast",
            )
            ax.scatter(
                fcs.geometry.x, fcs.geometry.y,
                color="#444444", s=18, marker="D", zorder=4,
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
                    color="#222222",
                    zorder=5,
                    arrowprops=dict(
                        arrowstyle="-",
                        color="#888888",
                        linewidth=0.4,
                        shrinkA=0,
                        shrinkB=1,
                    ),
                    bbox=dict(
                        boxstyle="round,pad=0.18",
                        facecolor="white",
                        edgecolor="#cccccc",
                        linewidth=0.4,
                        alpha=0.9,
                    ),
                )


def _forecast_view_bbox(
    tracks: gpd.GeoDataFrame,
    forecast_features: gpd.GeoDataFrame,
    n_tail_obs: int = 4,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Bbox covering the forecast features plus the most recent N observed points.

    Used to zoom each map onto the action: the full forecast cone/buffers and
    just the tail of the observed track that connects to it.
    """
    obs = tracks[tracks["kind"] == "observed"].sort_values("valid_time")
    obs_tail = obs.tail(n_tail_obs)
    fcs = tracks[tracks["kind"] == "forecast"]
    pieces = [
        g for g in (obs_tail, fcs, forecast_features)
        if g is not None and not g.empty
    ]
    if not pieces:
        # Fall back to all tracks
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
    ax.set_title(title, fontsize=11, fontweight="bold", loc="left")
    if legend_handles:
        ax.legend(
            handles=legend_handles,
            loc="upper right", fontsize=8, framealpha=0.85,
        )


def track_plot_buffers(
    tracks: gpd.GeoDataFrame,
    buffers: gpd.GeoDataFrame,
    background: gpd.GeoDataFrame,
    adm1_gdf: gpd.GeoDataFrame | None = None,
) -> str:
    """Map: storm tracks + 34/50/64 kt observed and forecast-only buffers.

    background is a world-level adm0 layer (e.g. Natural Earth 110m).
    Affected countries in adm1_gdf are rendered with adm1 division lines on top.
    Axis limits clip the view without creating artificial boundary edges.
    """
    if tracks.empty:
        return ""
    fcast_features = (
        buffers[buffers["kind"] == "forecast"] if not buffers.empty else buffers
    )
    xlim, ylim = _forecast_view_bbox(tracks, fcast_features)
    fig, ax = plt.subplots(figsize=(9, 6))
    _draw_countries(ax, background)
    if adm1_gdf is not None and not adm1_gdf.empty:
        _draw_adm1(ax, adm1_gdf)
    obsv_proxies = _draw_obsv_buffers(ax, buffers)
    fcast_proxies = _draw_fcast_buffers(ax, buffers)
    _draw_tracks(ax, tracks)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    handles, _ = ax.get_legend_handles_labels()
    _finalize_map(
        ax,
        title="Storm tracks — observed + forecast-only buffers",
        legend_handles=obsv_proxies + fcast_proxies + handles,
    )
    fig.tight_layout()
    return _fig_to_img_tag(fig)


def track_plot_wsp(
    tracks: gpd.GeoDataFrame,
    buffers: gpd.GeoDataFrame,
    wsp: gpd.GeoDataFrame,
    background: gpd.GeoDataFrame,
    wind_threshold_kt: int = 50,
    adm1_gdf: gpd.GeoDataFrame | None = None,
) -> str:
    """Map: tracks + observed buffers + WSP fcastonly polygons (one threshold).

    background is a world-level adm0 layer (e.g. Natural Earth 110m).
    Affected countries in adm1_gdf are rendered with adm1 division lines on top.
    Axis limits clip the view without creating artificial boundary edges.
    """
    if tracks.empty:
        return ""
    xlim, ylim = _forecast_view_bbox(tracks, wsp)
    fig, ax = plt.subplots(figsize=(9, 6))
    _draw_countries(ax, background)
    if adm1_gdf is not None and not adm1_gdf.empty:
        _draw_adm1(ax, adm1_gdf)
    obsv_proxies = _draw_obsv_buffers(ax, buffers)
    wsp_proxies = _draw_wsp_polygons(ax, wsp, wind_threshold_kt)
    _draw_tracks(ax, tracks)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    handles, _ = ax.get_legend_handles_labels()
    _finalize_map(
        ax,
        title=f"Storm tracks — observed buffers + WSP {wind_threshold_kt} kt forecast",
        legend_handles=obsv_proxies + wsp_proxies + handles,
    )
    fig.tight_layout()
    return _fig_to_img_tag(fig)


def _fig_to_img_tag(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode("utf-8")
    style = "max-width:100%;display:block;margin-bottom:8px"
    return f'<img src="data:image/png;base64,{img_b64}" style="{style}">'
