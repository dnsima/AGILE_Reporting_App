"""Chart rendering for the quarterly reports.

Every figure here is destined for a printed Word document, so the choices are
made for that medium: light surface only, no interactivity, and a data table
behind each figure in the annex. The table is not decoration -- it is the
accessibility relief for the one palette slot that sits below 3:1 contrast on a
light surface, and it is what a reader checks a number against.

Form follows the data's job rather than habit:

* progress against a target is magnitude, so it is a bar with a target line,
  one hue, and the value at each tip;
* participation against completion is a before-and-after per item, so it is a
  dumbbell rather than two bars competing for the same slot;
* the cohort comparison puts enrolment counts beside completion percentages,
  which are measures of different scale, so it is small multiples rather than
  the two-axis chart that would otherwise be tempting.

The categorical palette is the validated default, used in fixed slot order and
never cycled. Three slots is the ceiling here, which every figure respects.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")  # no display; these render straight to PNG bytes

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# --- palette ---------------------------------------------------------------
# Validated with the data-viz validator on the light surface, all pairs:
# CVD dE 9.2, normal-vision dE 24.0. Aqua sits at 2.74:1 contrast, below the
# 3:1 bar, so every figure using it ships direct labels and an annex table.
SURFACE = "#fcfcfb"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#86857e"
GRID = "#e5e4de"
REFERENCE = "#52514e"

DPI = 200
#: Bars are capped rather than filling their slot, so the band keeps its air.
MAX_BAR_FRACTION = 0.62
#: A figure taller than this will not sit on one page with its caption, and a
#: chart split across a page break is a chart nobody reads.
PAGE_HEIGHT = 7.2


@dataclass
class Chart:
    """A rendered figure: the image, its caption, and what it says."""

    caption: str
    png: bytes
    alt_text: str
    width_inches: float = 6.3


def _style(ax, *, xlabel: str | None = None, ylabel: str | None = None) -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(1.0)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=7.5, length=0)
    if xlabel:
        ax.set_xlabel(xlabel, color=TEXT_SECONDARY, fontsize=8)
    if ylabel:
        ax.set_ylabel(ylabel, color=TEXT_SECONDARY, fontsize=8)


def _thousands(ax, axis: str = "x") -> None:
    formatter = FuncFormatter(lambda value, _pos: f"{value:,.0f}")
    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(formatter)


def _finish(fig, caption: str, alt_text: str, width: float = 6.3) -> Chart:
    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format="png",
        dpi=DPI,
        bbox_inches="tight",
        facecolor=SURFACE,
        edgecolor="none",
    )
    plt.close(fig)
    return Chart(caption=caption, png=buffer.getvalue(), alt_text=alt_text, width_inches=width)


def _cap_thickness(fig, ax, bars, rows: int, *, cap_inches: float = 0.26) -> None:
    """Hold bars to a thickness in inches, measured after layout.

    A fraction of the slot is not a cap. With two rows, 0.62 of the slot is a
    bar half an inch thick -- several times the specified ceiling, and why
    few-row charts come out as blocks. Estimating the plot height in advance
    does not work either, because ``tight_layout`` decides it. So the layout
    is resolved first and the bars are then held to the cap, which leaves the
    rest of the slot as the air it is meant to be.
    """
    fig.canvas.draw()
    plot_inches = ax.get_position().height * fig.get_figheight()
    slot = plot_inches / max(rows, 1)
    if slot <= 0:
        return
    units = min(MAX_BAR_FRACTION, cap_inches / slot)
    for patch in bars:
        centre = patch.get_y() + patch.get_height() / 2
        patch.set_height(units)
        patch.set_y(centre - units / 2)


def _short(text: str, limit: int = 46) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------
def progress_against_target(
    rows: list[tuple[str, float]],
    *,
    caption: str,
    target_label: str = "Target (100%)",
) -> Chart | None:
    """Horizontal bars of percent-of-target, one hue, with a target line.

    The job is magnitude against a limit, so it is one hue and a reference
    line rather than a second colour: nothing here needs telling apart.
    """
    rows = [(label, value) for label, value in rows if value is not None]
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: row[1])
    labels = [_short(label) for label, _ in rows]
    values = [value for _, value in rows]

    height = min(PAGE_HEIGHT, max(1.8, 0.30 * len(rows) + 1.0))
    fig, ax = plt.subplots(figsize=(6.3, height))
    positions = range(len(rows))
    bars = ax.barh(
        list(positions),
        values,
        height=MAX_BAR_FRACTION,
        color=SERIES[0],
        zorder=3,
    )
    ax.set_yticks(list(positions))
    ax.set_yticklabels(labels, fontsize=7.5, color=TEXT_PRIMARY)

    ceiling = max(max(values), 100.0)
    ax.set_xlim(0, ceiling * 1.30)  # headroom so a tip label clears the line
    ax.axvline(100, color=REFERENCE, linewidth=1.2, zorder=2)
    # Blended transform: x in data units, y at the top of the axes, so the
    # label rides the line wherever it falls and is never clipped away.
    ax.text(
        100,
        1.01,
        target_label,
        transform=ax.get_xaxis_transform(),
        ha="center",
        va="bottom",
        fontsize=7,
        color=TEXT_SECONDARY,
    )

    # Values ride the tips: with this few bars the axis is redundant, and a
    # direct label is read where an axis tick is not.
    for index, value in enumerate(values):
        ax.annotate(
            f"{value:,.1f}%",
            xy=(value, index),
            xytext=(4, 0),
            textcoords="offset points",
            va="center",
            fontsize=7.5,
            color=TEXT_PRIMARY,
            zorder=5,
            # The surface behind the text is the same mechanism as the ring on
            # an overlapping dot: white separates, a stroke would add ink.
            bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.0},
        )

    _style(ax, xlabel="Percentage of target achieved")
    ax.set_xticks([])
    fig.tight_layout()
    _cap_thickness(fig, ax, bars, len(rows))
    return _finish(
        fig,
        caption,
        alt_text=(
            f"Horizontal bar chart of percentage of target achieved for "
            f"{len(rows)} indicators, against a 100% target line. "
            + "; ".join(f"{label} {value:,.1f}%" for label, value in rows[:6])
        ),
    )


def grouped_by_state(
    states: list[str],
    series: list[tuple[str, list[float | None]]],
    *,
    caption: str,
    value_label: str,
    target: float | None = None,
    percent: bool = False,
) -> Chart | None:
    """Grouped horizontal bars: up to three series across the states."""
    series = [(name, values) for name, values in series if any(v is not None for v in values)]
    if not states or not series:
        return None
    series = series[:3]  # the slot ceiling; a fourth would fold to "Other"

    count = len(series)
    band = MAX_BAR_FRACTION / count
    height = min(PAGE_HEIGHT, max(2.4, 0.22 * len(states) * count / 2 + 1.2))
    fig, ax = plt.subplots(figsize=(6.3, height))

    for offset, (name, values) in enumerate(series):
        positions = [
            index + (offset - (count - 1) / 2) * band for index in range(len(states))
        ]
        ax.barh(
            positions,
            [0 if v is None else v for v in values],
            height=band * 0.78,  # the leftover is the surface gap between bars
            color=SERIES[offset],
            label=name,
            zorder=3,
        )

    ax.set_yticks(range(len(states)))
    ax.set_yticklabels(states, fontsize=7.5, color=TEXT_PRIMARY)
    ax.invert_yaxis()

    if target is not None:
        ax.axvline(target, color=REFERENCE, linewidth=1.2, zorder=2)
        ax.annotate(
            f"Target {target:,.0f}{'%' if percent else ''}",
            xy=(target, -0.6),
            xytext=(3, 0),
            textcoords="offset points",
            fontsize=7,
            color=TEXT_SECONDARY,
            va="bottom",
        )

    _style(ax, xlabel=value_label)
    ax.xaxis.grid(True, color=GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)
    if not percent:
        _thousands(ax)
    legend = ax.legend(
        loc="lower right",
        frameon=False,
        fontsize=7.5,
        ncols=min(count, 3),
    )
    for text in legend.get_texts():
        text.set_color(TEXT_SECONDARY)

    fig.tight_layout()
    return _finish(
        fig,
        caption,
        alt_text=(
            f"Grouped horizontal bar chart across {len(states)} states with "
            f"{count} series: {', '.join(name for name, _ in series)}."
        ),
    )


def conversion_dumbbell(
    rows: list[tuple[str, float, float]],
    *,
    caption: str,
    start_label: str,
    end_label: str,
) -> Chart | None:
    """Before-and-after per item: one hue in two shades, joined by a rule.

    Two bars per item would invite the reader to compare across items when the
    point is the gap within each one.
    """
    rows = [row for row in rows if row[1] is not None and row[2] is not None]
    if not rows:
        return None

    labels = [_short(label, 40) for label, _, _ in rows]
    starts = [start for _, start, _ in rows]
    ends = [end for _, _, end in rows]

    height = min(PAGE_HEIGHT, max(2.2, 0.62 * len(rows) + 1.3))
    fig, ax = plt.subplots(figsize=(6.3, height))
    for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
        ax.plot(
            [start, end],
            [index, index],
            color=GRID,
            linewidth=2.0,
            solid_capstyle="round",
            zorder=2,
        )
        ax.scatter(
            [start], [index], s=70, color=SERIES[0], zorder=3,
            edgecolors=SURFACE, linewidths=2.0,
        )
        ax.scatter(
            [end], [index], s=70, color=SERIES[1], zorder=4,
            edgecolors=SURFACE, linewidths=2.0,
        )
        conversion = 100.0 * end / start if start else 0.0
        ax.annotate(
            f"{conversion:,.1f}%",
            xy=(max(start, end), index),
            xytext=(8, 0),
            textcoords="offset points",
            va="center",
            fontsize=7.5,
            color=TEXT_PRIMARY,
        )

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=7.5, color=TEXT_PRIMARY)
    # Margins, or two rows sit pinned to the top and bottom edges with a void
    # between them that the reader mistakes for missing data.
    ax.set_ylim(len(rows) - 0.4, -0.7)
    ax.set_xlim(0, max(max(starts), max(ends)) * 1.22)
    _style(ax, xlabel="Participants")
    _thousands(ax)
    ax.xaxis.grid(True, color=GRID, linewidth=1.0, zorder=0)
    ax.set_axisbelow(True)

    handles = [
        plt.Line2D([], [], marker="o", linestyle="", markersize=7,
                   color=SERIES[0], label=start_label),
        plt.Line2D([], [], marker="o", linestyle="", markersize=7,
                   color=SERIES[1], label=end_label),
    ]
    # Above the plot, not inside it: with few rows there is no interior space
    # that does not read as part of the data.
    legend = ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0, 1.01),
        ncols=2,
        frameon=False,
        fontsize=7.5,
    )
    for text in legend.get_texts():
        text.set_color(TEXT_SECONDARY)

    fig.tight_layout()
    return _finish(
        fig,
        caption,
        alt_text=(
            f"Dumbbell chart of {start_label} against {end_label} for "
            f"{len(rows)} items, labelled with the conversion rate between them."
        ),
    )


def small_multiples(
    panels: list[tuple[str, list[tuple[str, float | None]], str]],
    *,
    caption: str,
) -> Chart | None:
    """One panel per measure, because measures of different scale never share an axis.

    Each panel carries its own unit suffix: a completion rate rendered as "49"
    beside an enrolment count of 2,400,214 tells the reader the wrong thing
    about what it measures.
    """
    panels = [
        (title, [(label, value) for label, value in rows if value is not None], suffix)
        for title, rows, suffix in panels
    ]
    panels = [panel for panel in panels if panel[1]]
    if not panels:
        return None

    fig, axes = plt.subplots(
        1, len(panels), figsize=(6.3, 2.6), facecolor=SURFACE
    )
    if len(panels) == 1:
        axes = [axes]

    for ax, (title, rows, suffix) in zip(axes, panels, strict=True):
        labels = [_short(label, 18) for label, _ in rows]
        values = [value for _, value in rows]
        ax.bar(
            range(len(rows)),
            values,
            width=MAX_BAR_FRACTION,
            color=SERIES[0],
            zorder=3,
        )
        ax.set_xticks(range(len(rows)))
        ax.set_xticklabels(labels, fontsize=7, color=TEXT_PRIMARY, rotation=20, ha="right")
        ax.set_title(_short(title, 34), fontsize=8, color=TEXT_PRIMARY, pad=8)
        decimals = 1 if suffix == "%" else 0
        for index, value in enumerate(values):
            ax.annotate(
                f"{value:,.{decimals}f}{suffix}",
                xy=(index, value),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=7,
                color=TEXT_PRIMARY,
            )
        ax.set_ylim(0, max(values) * 1.25 if values else 1)
        _style(ax)
        ax.set_yticks([])

    fig.tight_layout()
    return _finish(
        fig,
        caption,
        alt_text=(
            "Small multiples, one panel per indicator, comparing financing "
            f"cohorts across {len(panels)} measures."
        ),
    )


def status_counts(
    rows: list[tuple[str, int]],
    *,
    caption: str,
    total: int | None = None,
) -> Chart | None:
    """A handful of counts. One hue: these are stages, not identities."""
    rows = [(label, value) for label, value in rows if value is not None]
    if not rows:
        return None

    labels = [_short(label, 52) for label, _ in rows]
    values = [value for _, value in rows]
    height = max(1.6, 0.5 * len(rows) + 0.9)
    fig, ax = plt.subplots(figsize=(6.3, height))
    bars = ax.barh(
        range(len(rows)),
        values,
        height=MAX_BAR_FRACTION,
        color=SERIES[0],
        zorder=3,
    )
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=8, color=TEXT_PRIMARY)
    ax.invert_yaxis()

    ceiling = max(values + ([total] if total else []))
    ax.set_xlim(0, ceiling * 1.2)
    for index, value in enumerate(values):
        suffix = f" of {total}" if total else ""
        ax.annotate(
            f"{value:,.0f}{suffix}",
            xy=(value, index),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=TEXT_PRIMARY,
        )
    _style(ax)
    ax.set_xticks([])
    fig.tight_layout()
    _cap_thickness(fig, ax, bars, len(rows))
    return _finish(
        fig,
        caption,
        alt_text="; ".join(f"{label}: {value:,.0f}" for label, value in rows),
    )
