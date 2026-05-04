"""
chart_generator.py — Gold S/R Chart Generator  (TradingView-style)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates a professional TradingView-style candlestick chart.

Dependencies:
  mplfinance==0.12.10b0
  pandas==2.2.0
  matplotlib>=3.7.0
  numpy>=1.24.0
"""

from __future__ import annotations
import io
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
import matplotlib.gridspec as gridspec
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ── TradingView Dark colour palette ───────────────────────────────────
BG          = "#131722"
PANEL_BG    = "#131722"
GRID_COL    = "#1e222d"
BORDER_COL  = "#2a2e39"
UP_COL      = "#26a69a"        # TV teal-green
DOWN_COL    = "#ef5350"        # TV red
RESIST_COL  = "#ef5350"
SUPPORT_COL = "#26a69a"
FIB_COL     = "#b2904f"
PRICE_COL   = "#ffffff"
BUY_COL     = "#26a69a"
SELL_COL    = "#ef5350"
TEXT_COL    = "#d1d4dc"
MUTED_COL   = "#5d606b"


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def _candles_to_df(candles: list) -> pd.DataFrame:
    rows = []
    for c in candles:
        try:
            dt = pd.to_datetime(c.get("datetime", ""))
        except Exception:
            dt = pd.Timestamp.now()
        rows.append({
            "Date":   dt,
            "Open":   float(c["open"]),
            "High":   float(c["high"]),
            "Low":    float(c["low"]),
            "Close":  float(c["close"]),
            "Volume": float(c.get("volume", 0)),
        })
    df = pd.DataFrame(rows).set_index("Date").sort_index()
    return df


def _level_linewidth(touches: int) -> float:
    return min(0.55 + touches * 0.22, 1.6)


def _level_alpha(touches: int) -> float:
    return min(0.45 + touches * 0.13, 0.88)


def _fib_label(ratio: float) -> str:
    mapping = {0.236: "0.236", 0.382: "0.382", 0.500: "0.500",
               0.618: "0.618", 0.786: "0.786"}
    for r, label in mapping.items():
        if abs(ratio - r) < 0.001:
            return label
    return f"{ratio:.3f}"


def _current_session_label() -> str:
    h = datetime.now(timezone.utc).hour
    if 7  <= h < 16: return "London"
    if 13 <= h < 22: return "New York"
    if 0  <= h < 9:  return "Asian"
    return "Off-hours"


def _price_tag(ax, price: float, label: str, color: str,
               x_pos: float, fontsize: float = 7.2, bold: bool = False):
    """TV-style filled price tag box on the right edge."""
    ax.text(
        x_pos, price,
        f" {label} ",
        color="#ffffff",
        fontsize=fontsize,
        va="center", ha="left",
        fontfamily="monospace",
        fontweight="bold" if bold else "normal",
        bbox=dict(
            boxstyle="square,pad=0.22",
            facecolor=color,
            edgecolor="none",
            alpha=0.92,
        ),
        clip_on=False,
        zorder=20,
    )


def _sr_label(ax, price: float, color: str, x_pos: float,
              touches: int, fib_conf: bool, mtf: bool):
    """Right-side price label with optional badges."""
    badges = ""
    if fib_conf: badges += "◆"
    if mtf:      badges += "⬡"
    dot_str = "●" * min(touches - 1, 3) if touches > 1 else ""
    label   = f"{price:.2f}"
    if badges or dot_str:
        label += f"  {badges}{dot_str}"

    ax.text(
        x_pos, price,
        f" {label} ",
        color="#ffffff",
        fontsize=7.0,
        va="center", ha="left",
        fontfamily="monospace",
        fontweight="bold" if touches >= 3 else "normal",
        bbox=dict(
            boxstyle="square,pad=0.22",
            facecolor=color,
            edgecolor="none",
            alpha=0.80 if touches >= 2 else 0.60,
        ),
        clip_on=False,
        zorder=20,
    )


# ══════════════════════════════════════════════════════════════════════
# CANDLE DRAWING — pure matplotlib for full control
# ══════════════════════════════════════════════════════════════════════

def _draw_candles(ax, df: pd.DataFrame):
    opens  = df["Open"].values
    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values
    n      = len(df)
    cw     = 0.62   # candle body width

    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        bull = c >= o
        col  = UP_COL if bull else DOWN_COL

        # Wick
        ax.plot([i, i], [l, h], color=col, linewidth=0.9,
                zorder=4, solid_capstyle="butt")

        # Body
        body_bot = min(o, c)
        body_h   = max(abs(c - o), (h - l) * 0.006, 0.5)

        rect = mpatches.Rectangle(
            (i - cw / 2, body_bot), cw, body_h,
            facecolor=col, edgecolor="none",
            linewidth=0, zorder=5,
        )
        ax.add_patch(rect)


def _draw_volume(ax, df: pd.DataFrame):
    n      = len(df)
    opens  = df["Open"].values
    closes = df["Close"].values
    vols   = df["Volume"].values
    if vols.max() == 0:
        ax.set_visible(False)
        return

    for i in range(n):
        bull = closes[i] >= opens[i]
        col  = UP_COL + "44" if bull else DOWN_COL + "44"
        ax.bar(i, vols[i], width=0.62, color=col, linewidth=0, zorder=3)

    ax.set_xlim(-1, n + 0.5)
    ax.set_ylim(0, vols.max() * 3.5)
    ax.set_yticks([])
    ax.set_facecolor(PANEL_BG)
    for spine in ax.spines.values():
        spine.set_color(BORDER_COL)
        spine.set_linewidth(0.6)
    ax.tick_params(bottom=False, left=False)
    ax.xaxis.set_visible(False)


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def generate_chart(
    candles: list,
    levels: list,
    fib_levels: list,
    price: float,
    signal_direction: str = "HOLD",
    tf_label: str = "H4",
    scores: Optional[dict] = None,
) -> io.BytesIO:
    """
    Generate a TradingView-style Gold candlestick chart.
    Returns BytesIO PNG — pass directly to bot.send_photo().
    """
    candles = candles[-60:] if len(candles) > 60 else candles
    # Strip zero/malformed candles so they can never distort the chart
    candles = [c for c in candles if c.get("high", 0) > 0 and c.get("low", 0) > 0]
    df = _candles_to_df(candles)
    if df.empty:
        raise ValueError("No candle data to plot")

    n  = len(df)
    # Derive y-axis bounds purely from the candle data — never from levels
    candle_low  = df["Low"].min()
    candle_high = df["High"].max()
    pr          = candle_high - candle_low
    if pr < 1:
        raise ValueError("Candle price range is too small to plot")
    y_min = candle_low  - pr * 0.07
    y_max = candle_high + pr * 0.10

    # Clamp any passed-in levels/fibs to a ±15% window around current price
    # so a rogue zero level can never blow out the axis
    _guard = price * 0.15
    levels    = [l for l in (levels    or []) if abs(l["price"] - price) < _guard]
    fib_levels = [f for f in (fib_levels or []) if abs(f - price) < _guard]

    # ── Figure & grid ─────────────────────────────────────────────
    has_vol = df["Volume"].sum() > 0
    hr      = [5.5, 1.0] if has_vol else [1]
    fig     = plt.figure(figsize=(16, 9), dpi=150, facecolor=BG)
    gs      = gridspec.GridSpec(
        2 if has_vol else 1, 1,
        height_ratios=hr,
        hspace=0,
        left=0.04, right=0.845,
        top=0.905, bottom=0.065,
    )
    ax_main = fig.add_subplot(gs[0])
    ax_vol  = fig.add_subplot(gs[1], sharex=ax_main) if has_vol else None

    # ── Axis styling ──────────────────────────────────────────────
    ax_main.set_facecolor(PANEL_BG)
    for spine in ax_main.spines.values():
        spine.set_color(BORDER_COL)
        spine.set_linewidth(0.7)

    ax_main.set_xlim(-1, n + 0.5)
    ax_main.set_ylim(y_min, y_max)

    # Grid
    ax_main.yaxis.set_major_locator(mticker.MaxNLocator(nbins=10, min_n_ticks=6))
    ax_main.grid(axis="y", color=GRID_COL, linewidth=0.55, zorder=0)
    ax_main.grid(axis="x", color=GRID_COL, linewidth=0.35, zorder=0)

    # Y-axis ticks on RIGHT (TV style)
    ax_main.yaxis.set_label_position("right")
    ax_main.yaxis.tick_right()
    ax_main.tick_params(
        axis="y", which="both",
        right=True, left=False,
        colors=MUTED_COL, labelsize=7.2,
        length=3, width=0.5,
    )
    ax_main.tick_params(axis="x", bottom=False, labelbottom=False)

    # Format Y ticks as plain prices
    ax_main.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda val, _: f"{val:.2f}")
    )

    # ── Draw candles & volume ─────────────────────────────────────
    _draw_candles(ax_main, df)
    if ax_vol is not None:
        _draw_volume(ax_vol, df)

    # ── X-axis date labels ────────────────────────────────────────
    dates    = df.index
    step     = max(n // 8, 1)
    tick_idx = list(range(0, n, step))
    ref_ax   = ax_vol if ax_vol is not None else ax_main
    ref_ax.set_xticks(tick_idx)
    ref_ax.set_xticklabels(
        [dates[i].strftime("%b %d\n%H:%M") for i in tick_idx],
        color=MUTED_COL, fontsize=6.5, ha="center",
    )
    ref_ax.tick_params(axis="x", length=3, width=0.5)

    label_x = n + 0.9   # x for right-edge labels

    # ── Fibonacci lines ───────────────────────────────────────────
    fib_ratios = [0.236, 0.382, 0.500, 0.618, 0.786]
    for i, fp in enumerate(fib_levels):
        if not (y_min < fp < y_max):
            continue
        rl = _fib_label(fib_ratios[i]) if i < len(fib_ratios) else ""
        ax_main.axhline(
            y=fp, color=FIB_COL,
            linewidth=0.65, linestyle=(0, (5, 7)),
            alpha=0.60, zorder=2,
        )
        # Subtle inline label at left
        ax_main.text(
            1.5, fp, f" Fib {rl}",
            color=FIB_COL, fontsize=6.0,
            va="center", ha="left",
            fontfamily="monospace",
            alpha=0.70, zorder=6,
        )
        # Right price tag (amber box)
        _price_tag(ax_main, fp, f"{fp:.2f}", FIB_COL, label_x,
                   fontsize=6.8, bold=False)

    # ── S/R levels ────────────────────────────────────────────────
    resistances = sorted(
        [l for l in levels if l["price"] > price],
        key=lambda x: x["price"]
    )[:8]
    supports = sorted(
        [l for l in levels if l["price"] <= price],
        key=lambda x: x["price"], reverse=True
    )[:8]

    for lvl_list, col in [(resistances, RESIST_COL), (supports, SUPPORT_COL)]:
        for lvl in lvl_list:
            lp      = lvl["price"]
            if not (y_min < lp < y_max):
                continue
            touches  = lvl.get("touches", 1)
            fib_conf = lvl.get("fib_confluence", False)
            mtf      = lvl.get("tf_count", 1) >= 2
            lw       = _level_linewidth(touches)
            alpha    = _level_alpha(touches)

            ax_main.axhline(
                y=lp, color=col,
                linewidth=lw, linestyle="--",
                alpha=alpha, zorder=3,
            )
            _sr_label(ax_main, lp, col, label_x, touches, fib_conf, mtf)

    # ── Current price line + tag ──────────────────────────────────
    if y_min < price < y_max:
        ax_main.axhline(
            y=price, color=PRICE_COL,
            linewidth=1.0, linestyle="-",
            alpha=1.0, zorder=7,
        )
        _price_tag(ax_main, price, f"{price:.2f}",
                   PRICE_COL, label_x, fontsize=7.5, bold=True)

    # ── Signal arrow ──────────────────────────────────────────────
    if signal_direction in ("BUY", "SELL"):
        last_high = df["High"].iloc[-1]
        last_low  = df["Low"].iloc[-1]
        unit      = pr / n * 4.5

        if signal_direction == "BUY":
            tail_y = last_low  - unit * 1.3
            head_y = last_low  - unit * 0.25
            col    = BUY_COL
            lbl_y  = tail_y  - unit * 0.65
            txt    = "▲ BUY"
        else:
            tail_y = last_high + unit * 1.3
            head_y = last_high + unit * 0.25
            col    = SELL_COL
            lbl_y  = tail_y  + unit * 0.65
            txt    = "▼ SELL"

        ax_main.annotate(
            "",
            xy=(n - 1, head_y), xytext=(n - 1, tail_y),
            arrowprops=dict(
                arrowstyle="-|>", color=col,
                lw=2.0, mutation_scale=16,
            ),
            zorder=12,
        )
        ax_main.text(
            n - 1, lbl_y, txt,
            color=col, fontsize=9,
            ha="center", va="center",
            fontweight="bold",
            fontfamily="monospace",
            zorder=12,
        )

    # ── Title bar ─────────────────────────────────────────────────
    eat     = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M")
    session = _current_session_label()
    dir_sym = {"BUY": "▲ BUY", "SELL": "▼ SELL", "HOLD": "● HOLD"}.get(
        signal_direction, "")
    dir_col = {"BUY": BUY_COL, "SELL": SELL_COL, "HOLD": MUTED_COL}.get(
        signal_direction, MUTED_COL)

    # Instrument
    fig.text(0.048, 0.952, "XAU/USD",
             fontsize=13, color=TEXT_COL, fontweight="bold", va="center")
    # Timeframe
    fig.text(0.130, 0.952, tf_label,
             fontsize=10, color=MUTED_COL, va="center")
    # Dot separator
    fig.text(0.158, 0.952, "·",
             fontsize=11, color=MUTED_COL, va="center")
    # Direction
    fig.text(0.168, 0.952, dir_sym,
             fontsize=10, color=dir_col, fontweight="bold", va="center")
    # Dot separator
    fig.text(0.240, 0.952, "·",
             fontsize=11, color=MUTED_COL, va="center")
    # Session + time
    fig.text(0.250, 0.952, f"{session}  ·  {eat} EAT",
             fontsize=8.5, color=MUTED_COL, va="center")

    # Scores row
    if scores:
        fig.text(
            0.048, 0.933,
            f"D1: {scores.get('d1','?')}   H4: {scores.get('h4','?')}   H1: {scores.get('h1','?')}",
            fontsize=7.5, color=MUTED_COL, va="center",
        )

    # Separator line
    sep = plt.Line2D(
        [0.04, 0.96], [0.922, 0.922],
        transform=fig.transFigure,
        color=BORDER_COL, linewidth=0.7,
    )
    fig.add_artist(sep)

    # ── Legend ────────────────────────────────────────────────────
    legend_items = [
        Line2D([0], [0], color=RESIST_COL, lw=1.1, ls="--", label="Resistance"),
        Line2D([0], [0], color=SUPPORT_COL, lw=1.1, ls="--", label="Support"),
        Line2D([0], [0], color=FIB_COL,     lw=0.9, ls=(0,(4,5)), label="Fibonacci"),
        Line2D([0], [0], color=PRICE_COL,   lw=1.0, ls="-",  label="Price"),
    ]
    if signal_direction == "BUY":
        legend_items.append(
            Line2D([0],[0], color=BUY_COL, lw=0, marker="^",
                   markersize=7, label="Buy Signal"))
    elif signal_direction == "SELL":
        legend_items.append(
            Line2D([0],[0], color=SELL_COL, lw=0, marker="v",
                   markersize=7, label="Sell Signal"))

    leg = ax_main.legend(
        handles=legend_items,
        loc="upper left",
        facecolor="#1e222d",
        edgecolor=BORDER_COL,
        labelcolor=TEXT_COL,
        fontsize=7.0,
        framealpha=0.90,
        borderpad=0.55,
        labelspacing=0.40,
        handlelength=1.8,
    )

    # ── Watermark ─────────────────────────────────────────────────
    ax_main.text(
        0.5, 0.5, "QUANTEDGE",
        transform=ax_main.transAxes,
        color=TEXT_COL, fontsize=40, alpha=0.022,
        ha="center", va="center",
        fontweight="bold", rotation=25, zorder=0,
    )

    # ── Render ────────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150,
                facecolor=BG, edgecolor="none",
                bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf
