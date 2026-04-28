"""
chart_generator.py — Gold S/R Chart Generator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates a marked-up candlestick chart and returns it as BytesIO.
Called by the Telegram bot on /chart command.

Dependencies (add to requirements.txt):
  mplfinance==0.12.10b0
  pandas==2.2.0
  matplotlib>=3.7.0
"""

from __future__ import annotations
import io
import logging
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")   # non-interactive backend — must be before other mpl imports
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
import mplfinance as mpf
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ── Colour palette (dark terminal style) ──────────────────────────────
DARK_BG      = "#0d1117"
PANEL_BG     = "#161b22"
GRID_COL     = "#21262d"
UP_COL       = "#26a641"    # green candle
DOWN_COL     = "#da3633"    # red candle
WICK_COL     = "#8b949e"
RESIST_COL   = "#ff4d4d"    # resistance lines
SUPPORT_COL  = "#00e676"    # support lines
FIB_COL      = "#ffd700"    # fibonacci lines
PRICE_COL    = "#ffffff"    # current price line
BUY_COL      = "#00e676"    # buy arrow
SELL_COL     = "#ff4d4d"    # sell arrow
TEXT_COL     = "#e6edf3"
MUTED_COL    = "#8b949e"

# ── mplfinance market style ───────────────────────────────────────────
_DARK_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(
        up=UP_COL, down=DOWN_COL,
        wick={"up": UP_COL, "down": DOWN_COL},
        edge={"up": UP_COL, "down": DOWN_COL},
        volume={"up": UP_COL, "down": DOWN_COL},
    ),
    facecolor=DARK_BG,
    figcolor=DARK_BG,
    gridcolor=GRID_COL,
    gridstyle="--",
    gridaxis="horizontal",
    rc={
        "axes.labelcolor": TEXT_COL,
        "axes.edgecolor":  GRID_COL,
        "xtick.color":     MUTED_COL,
        "ytick.color":     TEXT_COL,
        "text.color":      TEXT_COL,
        "font.size":       8,
    },
)


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def _candles_to_df(candles: list) -> pd.DataFrame:
    """Convert list of OHLC dicts to a pandas DataFrame for mplfinance."""
    rows = []
    for c in candles:
        dt_str = c.get("datetime", "")
        try:
            dt = pd.to_datetime(dt_str)
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
    """Stronger levels drawn with a thicker line."""
    if touches >= 4: return 1.8
    if touches >= 3: return 1.4
    if touches >= 2: return 1.1
    return 0.8


def _level_alpha(touches: int) -> float:
    if touches >= 4: return 0.95
    if touches >= 3: return 0.85
    if touches >= 2: return 0.75
    return 0.60


def _touch_badge(touches: int) -> str:
    return "🔥" * min(touches - 1, 3) if touches > 1 else ""


def _fib_label(ratio: float) -> str:
    mapping = {0.236: "23.6%", 0.382: "38.2%", 0.500: "50.0%",
               0.618: "61.8%", 0.786: "78.6%"}
    for r, label in mapping.items():
        if abs(ratio - r) < 0.001:
            return label
    return f"{ratio:.1%}"


def _current_session_label() -> str:
    from datetime import timezone
    h = datetime.now(timezone.utc).hour
    if 7  <= h < 16: return "LONDON"
    if 13 <= h < 22: return "NEW_YORK"
    if 0  <= h < 9:  return "ASIAN"
    return "OFF-HOURS"


# ══════════════════════════════════════════════════════════════════════
# MAIN CHART FUNCTION
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
    Generate a marked-up Gold candlestick chart.

    Args:
        candles:          OHLCV list (oldest first). 40–80 candles recommended.
        levels:           S/R level dicts from signal_engine._cluster_levels()
        fib_levels:       Fibonacci price levels list
        price:            Current live price
        signal_direction: "BUY" / "SELL" / "HOLD" — adds signal arrow on last candle
        tf_label:         "H4" or "H1" — shown in title
        scores:           Optional dict with D1/H4/H1 scores for subtitle

    Returns:
        BytesIO PNG buffer — pass directly to bot.send_photo()
    """
    # ── Trim to last 60 candles for readability ──
    candles = candles[-60:] if len(candles) > 60 else candles
    df      = _candles_to_df(candles)
    if df.empty:
        raise ValueError("No candle data to plot")

    price_range = df["High"].max() - df["Low"].min()
    pad         = price_range * 0.08   # 8% padding above/below

    # ── Split levels into resistance / support ─────────────────────
    resistances = sorted([l for l in levels if l["price"] > price],
                          key=lambda x: x["price"])[:8]
    supports    = sorted([l for l in levels if l["price"] <= price],
                          key=lambda x: x["price"], reverse=True)[:8]

    # ── Build mplfinance hlines ────────────────────────────────────
    # We use addplot for full control over styling
    n = len(df)
    x = np.arange(n)

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=_DARK_STYLE,
        figsize=(14, 8),
        returnfig=True,
        tight_layout=False,
        warn_too_much_data=10000,
        scale_padding={"left": 0.1, "right": 0.25, "top": 0.5, "bottom": 0.3},
    )

    ax = axes[0]
    ax.set_facecolor(PANEL_BG)
    fig.patch.set_facecolor(DARK_BG)

    y_min = df["Low"].min()  - pad
    y_max = df["High"].max() + pad
    ax.set_ylim(y_min, y_max)

    right_x = n - 0.5   # x-coordinate for right-edge labels

    # ── Draw Fibonacci lines ───────────────────────────────────────
    fib_ratios = [0.236, 0.382, 0.500, 0.618, 0.786]
    for i, fib_price in enumerate(fib_levels):
        if not (y_min < fib_price < y_max):
            continue
        ratio_label = _fib_label(fib_ratios[i]) if i < len(fib_ratios) else ""
        ax.axhline(
            y=fib_price, color=FIB_COL,
            linewidth=0.9, linestyle=(0, (3, 5)),  # loosely dotted
            alpha=0.70, zorder=2,
        )
        ax.text(
            right_x, fib_price,
            f"  ◆ {fib_price:.2f}  Fib {ratio_label}",
            color=FIB_COL, fontsize=7, va="center",
            fontweight="normal", alpha=0.85,
        )

    # ── Draw S/R levels ────────────────────────────────────────────
    for level in resistances:
        lp      = level["price"]
        if not (y_min < lp < y_max): continue
        touches = level.get("touches", 1)
        fib_tag = " ◆" if level.get("fib_confluence") else ""
        mtf_tag = " 🔗" if level.get("tf_count", 1) >= 2 else ""
        badge   = _touch_badge(touches)
        lw      = _level_linewidth(touches)
        alpha   = _level_alpha(touches)
        ax.axhline(
            y=lp, color=RESIST_COL,
            linewidth=lw, linestyle="--", alpha=alpha, zorder=3,
        )
        ax.text(
            right_x, lp,
            f"  R {lp:.2f}{fib_tag}{mtf_tag} {badge}",
            color=RESIST_COL, fontsize=7.5, va="center",
            fontweight="bold" if touches >= 3 else "normal",
        )

    for level in supports:
        lp      = level["price"]
        if not (y_min < lp < y_max): continue
        touches = level.get("touches", 1)
        fib_tag = " ◆" if level.get("fib_confluence") else ""
        mtf_tag = " 🔗" if level.get("tf_count", 1) >= 2 else ""
        badge   = _touch_badge(touches)
        lw      = _level_linewidth(touches)
        alpha   = _level_alpha(touches)
        ax.axhline(
            y=lp, color=SUPPORT_COL,
            linewidth=lw, linestyle="--", alpha=alpha, zorder=3,
        )
        ax.text(
            right_x, lp,
            f"  S {lp:.2f}{fib_tag}{mtf_tag} {badge}",
            color=SUPPORT_COL, fontsize=7.5, va="center",
            fontweight="bold" if touches >= 3 else "normal",
        )

    # ── Current price line ─────────────────────────────────────────
    if y_min < price < y_max:
        ax.axhline(
            y=price, color=PRICE_COL,
            linewidth=1.2, linestyle="-", alpha=0.95, zorder=5,
        )
        ax.text(
            right_x, price,
            f"  💹 {price:.2f}",
            color=PRICE_COL, fontsize=8.5, va="center",
            fontweight="bold",
        )

    # ── Signal arrow on latest candle ─────────────────────────────
    if signal_direction in ("BUY", "SELL"):
        last_close = df["Close"].iloc[-1]
        last_high  = df["High"].iloc[-1]
        last_low   = df["Low"].iloc[-1]
        atr_approx = price_range / len(df) * 5

        if signal_direction == "BUY":
            arrow_y   = last_low  - atr_approx * 0.6
            dy        = atr_approx * 0.4
            arrow_col = BUY_COL
            label_y   = arrow_y - atr_approx * 0.3
            label_txt = "▲ BUY"
        else:
            arrow_y   = last_high + atr_approx * 0.6
            dy        = -atr_approx * 0.4
            arrow_col = SELL_COL
            label_y   = arrow_y + atr_approx * 0.3
            label_txt = "▼ SELL"

        ax.annotate(
            "", xy=(n - 1, arrow_y + dy), xytext=(n - 1, arrow_y),
            arrowprops=dict(
                arrowstyle="->", color=arrow_col,
                lw=2.0, mutation_scale=18,
            ),
            zorder=10,
        )
        ax.text(
            n - 1, label_y, label_txt,
            color=arrow_col, fontsize=9, ha="center",
            fontweight="bold", zorder=10,
        )

    # ── Title ──────────────────────────────────────────────────────
    from datetime import timezone as tz_module, timedelta
    eat     = datetime.now(tz_module(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M EAT")
    session = _current_session_label()

    dir_sym = {"BUY": "▲ BUY", "SELL": "▼ SELL", "HOLD": "— HOLD"}.get(signal_direction, "")
    dir_col = {"BUY": BUY_COL, "SELL": SELL_COL, "HOLD": MUTED_COL}.get(signal_direction, MUTED_COL)

    score_txt = ""
    if scores:
        score_txt = (
            f"  |  D1: {scores.get('d1','?')}  "
            f"H4: {scores.get('h4','?')}  "
            f"H1: {scores.get('h1','?')}"
        )

    fig.suptitle(
        f"XAU/USD  {tf_label}  •  {dir_sym}  •  {session}  •  {eat}{score_txt}",
        color=TEXT_COL, fontsize=9, y=0.98,
        fontweight="bold",
    )

    # ── Legend ─────────────────────────────────────────────────────
    legend_items = [
        Line2D([0], [0], color=RESIST_COL, lw=1.4, linestyle="--", label="Resistance"),
        Line2D([0], [0], color=SUPPORT_COL, lw=1.4, linestyle="--", label="Support"),
        Line2D([0], [0], color=FIB_COL,     lw=0.9, linestyle=":",  label="Fibonacci"),
        Line2D([0], [0], color=PRICE_COL,   lw=1.2, linestyle="-",  label="Current Price"),
    ]
    if signal_direction == "BUY":
        legend_items.append(Line2D([0],[0], color=BUY_COL,  lw=0, marker="^", markersize=8, label="BUY Signal"))
    elif signal_direction == "SELL":
        legend_items.append(Line2D([0],[0], color=SELL_COL, lw=0, marker="v", markersize=8, label="SELL Signal"))

    ax.legend(
        handles=legend_items,
        loc="upper left",
        facecolor=PANEL_BG, edgecolor=GRID_COL,
        labelcolor=TEXT_COL, fontsize=7.5,
        framealpha=0.85,
    )

    # ── Watermark ─────────────────────────────────────────────────
    ax.text(
        0.5, 0.5, "QUANTEDGE",
        transform=ax.transAxes,
        color=TEXT_COL, fontsize=32, alpha=0.04,
        ha="center", va="center", fontweight="bold",
        rotation=30, zorder=0,
    )

    # ── Save to buffer ─────────────────────────────────────────────
    plt.tight_layout(rect=[0, 0, 0.82, 0.96])   # right margin for labels
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=DARK_BG, edgecolor="none")
    buf.seek(0)
    plt.close(fig)
    return buf
