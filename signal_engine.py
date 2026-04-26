"""
signal_engine.py — Gold Signal Engine v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
All 15 win-rate improvements implemented:
  1.  HTF D1 veto (D1 bias blocks counter-trend H1/H4 signals)
  2.  Combined score gate (total score across TFs must meet minimum)
  3.  Level TF confluence (D1+H4 level = higher strength than H4 only)
  4.  Candle close confirmation (analyze closed candles, not live)
  5.  Entry zone tagging (IDEAL / FAIR / EXTENDED ⚠️)
  6.  Rejection candle gate on S/R proximity alerts
  7.  RSI divergence detection (bullish/bearish near S/R)
  8.  Consecutive candle momentum scoring
  9.  ATR regime filter (suppress signals in consolidation)
  10. Fibonacci confluence (Fib levels boost S/R strength)
  11. News blackout window (±30 min of NFP / CPI / FOMC)
  12. Session-open filter (suppress first 15 min of London/NY open)
  13. Daily flip limit (pause alerts after N flips per day)
  14. Dynamic SL at nearest S/R level + buffer
  15. Partial TP — TP1 at 1:1, TP2 at configured R:R
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Edit only the Config dataclass. Nothing else needs to change.
"""

import asyncio
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from data_fetcher import DataFetcher

logger = logging.getLogger(__name__)


# ╔══════════════════════════════════════════════════════════════════════╗
# ║                        ⚙️  CONFIGURATION                           ║
# ╚══════════════════════════════════════════════════════════════════════╝

@dataclass
class Config:
    # ── S/R Detection ─────────────────────────────────────────────────
    sr_swing_n: int = 5
    # Candles each side to confirm swing high/low. Range: 3–10

    sr_cluster_pct: float = 0.005
    # % tolerance for merging nearby levels. 0.005 = 0.5%

    sr_alert_pct: float = 0.003
    # Proximity to a level that triggers an alert. 0.3% ≈ $9 at $3000

    sr_alert_cooldown_mult: float = 3.0
    # Must move this × sr_alert_pct away before same level re-alerts

    fib_confluence_pct: float = 0.002
    # How close a level must be to a Fib line to count as confluence. 0.2%

    # ── ATR / Trade Sizing ─────────────────────────────────────────────
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    # Fallback SL if no nearby S/R level. SL = entry ± ATR × mult.

    tp_rr: float = 2.0
    # TP2 (final target) R:R. TP1 is always 1:1.

    dynamic_sl_buffer_mult: float = 0.3
    # SL placed this × ATR beyond the nearest S/R level. Range: 0.2–0.5

    # ── Scoring Thresholds ─────────────────────────────────────────────
    bias_threshold: int = 3
    # Min score for a single TF to declare BUY/SELL. Range: 2–5

    confluence_min_tfs: int = 2
    # Min TFs that must agree on direction.

    combined_score_min: int = 6
    # [Improvement #2] Minimum SUM of scores across agreeing TFs.
    # Prevents two weak signals (e.g. +3, +3) from firing.
    # Recommended: 6–10

    # ── Scoring Weights ────────────────────────────────────────────────
    w_candle_body: int = 1
    w_momentum: int = 1
    w_sr_position: int = 1
    w_sr_proximity: int = 2
    w_level_strength: int = 1
    w_candle_pattern: int = 2
    w_trend_context: int = 1
    w_session: int = 1
    w_divergence: int = 3      # [Improvement #7] RSI divergence — highest weight
    w_consecutive: int = 1     # [Improvement #8] Per consecutive candle, capped below
    w_fib_confluence: int = 1  # [Improvement #10] Fib level match bonus

    consecutive_candle_cap: int = 2
    # Max bonus from consecutive candles. Caps w_consecutive × N.

    # ── ATR Regime Filter [Improvement #9] ────────────────────────────
    atr_regime_threshold: float = 0.6
    # Suppress signals when current ATR < this × 20-period ATR average.
    # Indicates consolidation. 0.6 = 60% of average range.

    atr_regime_lookback: int = 20
    # How many ATR values to average for regime comparison.

    # ── Candle Close Confirmation [Improvement #4] ────────────────────
    use_confirmed_close: bool = True
    # If True, signals are based on the last CLOSED candle (candles[-2]),
    # not the potentially open current candle (candles[-1]).

    # ── Entry Zone [Improvement #5] ───────────────────────────────────
    entry_zone_atr_mult: float = 0.3
    # Entry zone width = ATR × this, measured from nearest S/R level.

    # ── News Blackout [Improvement #11] ───────────────────────────────
    news_blackout_minutes: int = 30
    # Suppress signals this many minutes before AND after major events.

    # ── Session Open Filter [Improvement #12] ─────────────────────────
    session_open_blackout_minutes: int = 15
    # Suppress signals this many minutes after London/NY session open.

    # ── Daily Flip Limit [Improvement #13] ────────────────────────────
    daily_flip_limit: int = 3
    # Pause signal-flip alerts after this many flips in one day.
    # Prevents loss accumulation in choppy, trend-less markets.

    # ── Alert Interval ─────────────────────────────────────────────────
    check_interval_minutes: int = 5


# ── Module-level FOMC dates (UTC, 18:00 = 2pm ET) ─────────────────────
# Update annually. Source: federalreserve.gov
_FOMC_DATES: list[tuple] = [
    (2025, 1, 29), (2025, 3, 19), (2025, 5, 7),  (2025, 6, 18),
    (2025, 7, 30), (2025, 9, 17), (2025, 10, 29),(2025, 12, 10),
    (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
    (2026, 7, 29), (2026, 9, 16), (2026, 10, 28),(2026, 12, 9),
]
_FOMC_HOUR_UTC = 18

CFG = Config()


# ══════════════════════════════════════════════════════════════════════
# MODULE-LEVEL UTILITIES
# ══════════════════════════════════════════════════════════════════════

def _eat_now() -> str:
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y-%m-%d %H:%M EAT")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _current_session() -> str:
    h = _utc_now().hour
    if 7  <= h < 16: return "LONDON"
    if 13 <= h < 22: return "NEW_YORK"
    if 0  <= h < 9:  return "ASIAN"
    return "OFF"


def _is_session_open_window(blackout_minutes: int) -> bool:
    """[Improvement #12] True if within blackout_minutes of London or NY open."""
    h, m = _utc_now().hour, _utc_now().minute
    total = h * 60 + m
    london_open = 7 * 60        # 07:00 UTC
    ny_open     = 13 * 60       # 13:00 UTC
    for open_min in (london_open, ny_open):
        if 0 <= total - open_min < blackout_minutes:
            return True
    return False


def _first_friday_of_month(year: int, month: int) -> datetime:
    """Returns datetime of first Friday of the given month at 13:30 UTC (NFP time)."""
    first_weekday = calendar.weekday(year, month, 1)   # 0=Mon
    days_to_fri   = (4 - first_weekday) % 7
    return datetime(year, month, 1 + days_to_fri, 13, 30, tzinfo=timezone.utc)


def _get_news_events(blackout_minutes: int) -> list[datetime]:
    """
    [Improvement #11] Build list of upcoming high-impact Gold news events.
    Covers: NFP (1st Friday/month 13:30 UTC), FOMC (hardcoded), CPI (2nd Wed/month 13:30 UTC).
    """
    now    = _utc_now()
    events = []

    # NFP — first Friday of each month
    for delta_month in range(-1, 3):
        yr = now.year + (now.month + delta_month - 1) // 12
        mo = (now.month + delta_month - 1) % 12 + 1
        try:
            events.append(_first_friday_of_month(yr, mo))
        except ValueError:
            pass

    # FOMC
    for y, mo, d in _FOMC_DATES:
        events.append(datetime(y, mo, d, _FOMC_HOUR_UTC, 0, tzinfo=timezone.utc))

    # CPI — approximate: 2nd Wednesday of each month at 13:30 UTC
    for delta_month in range(-1, 3):
        yr = now.year + (now.month + delta_month - 1) // 12
        mo = (now.month + delta_month - 1) % 12 + 1
        try:
            first_wd = calendar.weekday(yr, mo, 1)
            days_to_wed = (2 - first_wd) % 7          # first Wednesday
            second_wed  = 1 + days_to_wed + 7          # second Wednesday
            if second_wed <= calendar.monthrange(yr, mo)[1]:
                events.append(datetime(yr, mo, second_wed, 13, 30, tzinfo=timezone.utc))
        except ValueError:
            pass

    return events


def _is_news_blackout(blackout_minutes: int) -> tuple[bool, str]:
    """Returns (is_blocked, event_description)."""
    now    = _utc_now()
    window = timedelta(minutes=blackout_minutes)
    for event in _get_news_events(blackout_minutes):
        diff = abs(now - event)
        if diff <= window:
            direction = "in" if event > now else "ago"
            mins = int(diff.total_seconds() / 60)
            return True, f"{mins}min {direction}"
    return False, ""


# ══════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════

class SignalEngine:
    def __init__(self, td_key: str, fh_key: str, config: Config = CFG):
        self.fetcher = DataFetcher(td_key, fh_key)
        self.cfg     = config

        # Alert state
        self.last_signal:       Optional[str]  = None
        self.alerted_levels:    set            = set()

        # [Improvement #13] Daily flip tracking
        self.daily_flip_count:  int            = 0
        self.last_flip_date:    Optional[date] = None

    # ──────────────────────────────────────────────────────────────────
    # S/R DETECTION
    # ──────────────────────────────────────────────────────────────────

    def _find_sr_levels(self, candles: list, tf_label: str = "H4") -> list:
        """
        [Improvement #3] Swing detection with tf_label tracking.
        Each level records which TF it came from; levels confirmed on
        multiple TFs get a higher tf_count after clustering.
        """
        n      = self.cfg.sr_swing_n
        highs  = [c["high"] for c in candles]
        lows   = [c["low"]  for c in candles]
        levels = []

        for i in range(n, len(candles) - n):
            if all(highs[i] >= highs[i-j] for j in range(1, n+1)) and \
               all(highs[i] >= highs[i+j] for j in range(1, n+1)):
                levels.append({"price": highs[i], "type": "resistance",
                                "touches": 1, "tf_count": 1, "tfs": {tf_label}})

            if all(lows[i] <= lows[i-j] for j in range(1, n+1)) and \
               all(lows[i] <= lows[i+j] for j in range(1, n+1)):
                levels.append({"price": lows[i], "type": "support",
                                "touches": 1, "tf_count": 1, "tfs": {tf_label}})

        return self._cluster_levels(levels)

    def _cluster_levels(self, levels: list) -> list:
        """Merge nearby levels. Levels from different TFs accumulate tf_count."""
        if not levels:
            return []
        sorted_lvls = sorted(levels, key=lambda x: x["price"])
        clustered   = [{**sorted_lvls[0], "tfs": set(sorted_lvls[0].get("tfs", set()))}]

        for lvl in sorted_lvls[1:]:
            last = clustered[-1]
            if abs(lvl["price"] - last["price"]) / last["price"] < self.cfg.sr_cluster_pct:
                last["price"]   = round((last["price"] + lvl["price"]) / 2, 2)
                last["touches"] += lvl.get("touches", 1)
                merged_tfs       = last["tfs"] | lvl.get("tfs", set())
                last["tfs"]      = merged_tfs
                last["tf_count"] = len(merged_tfs)
            else:
                entry = {**lvl, "tfs": set(lvl.get("tfs", set()))}
                clustered.append(entry)

        return sorted(clustered, key=lambda x: x["price"], reverse=True)

    # ──────────────────────────────────────────────────────────────────
    # FIBONACCI LEVELS [Improvement #10]
    # ──────────────────────────────────────────────────────────────────

    def _calc_fibonacci_levels(self, candles: list) -> list[float]:
        """
        Calculate key Fibonacci retracement levels from the most recent
        significant swing high and low in D1 data.
        Returns a list of price levels (0.236, 0.382, 0.5, 0.618, 0.786).
        """
        if len(candles) < 20:
            return []
        recent = candles[-50:] if len(candles) >= 50 else candles
        swing_high = max(c["high"] for c in recent)
        swing_low  = min(c["low"]  for c in recent)
        diff       = swing_high - swing_low
        if diff < 1:
            return []
        ratios = [0.236, 0.382, 0.500, 0.618, 0.786]
        return [round(swing_high - r * diff, 2) for r in ratios]

    def _enrich_levels_with_fib(self, levels: list, fib_levels: list[float]) -> list:
        """
        [Improvement #10] Mark S/R levels that align with a Fibonacci level.
        Adds fib_confluence=True and increments touches for those levels.
        """
        threshold = self.cfg.fib_confluence_pct
        for lvl in levels:
            for fib in fib_levels:
                if abs(lvl["price"] - fib) / fib < threshold:
                    lvl["fib_confluence"] = True
                    lvl["touches"]        += 1
                    break
            else:
                lvl.setdefault("fib_confluence", False)
        return levels

    # ──────────────────────────────────────────────────────────────────
    # INDICATORS
    # ──────────────────────────────────────────────────────────────────

    def _calc_atr(self, candles: list) -> tuple[float, float]:
        """
        [Improvement #9] Returns (current_atr, regime_avg_atr).
        current_atr    = ATR over last atr_period candles.
        regime_avg_atr = mean of last atr_regime_lookback ATR values.
        Used to detect consolidation.
        """
        period = self.cfg.atr_period
        trs = [
            max(
                candles[i]["high"] - candles[i]["low"],
                abs(candles[i]["high"] - candles[i-1]["close"]),
                abs(candles[i]["low"]  - candles[i-1]["close"]),
            )
            for i in range(1, len(candles))
        ]
        if not trs:
            return 15.0, 15.0

        recent_trs  = trs[-period:] if len(trs) >= period else trs
        current_atr = sum(recent_trs) / len(recent_trs)

        lookback = self.cfg.atr_regime_lookback
        regime_trs  = trs[-lookback:] if len(trs) >= lookback else trs
        regime_avg  = sum(regime_trs) / len(regime_trs)

        return current_atr, regime_avg

    def _calc_rsi(self, candles: list, period: int = 14) -> list[float]:
        """[Improvement #7] Wilder-smoothed RSI. Returns list aligned to candles."""
        closes = [c["close"] for c in candles]
        if len(closes) < period + 1:
            return []

        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0)  for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]

        avg_gain = sum(gains[:period])  / period
        avg_loss = sum(losses[:period]) / period

        rsi_values: list[float] = []
        for i in range(period, len(deltas)):
            if avg_loss == 0:
                rsi_values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(round(100 - 100 / (1 + rs), 2))
            avg_gain = (avg_gain * (period - 1) + gains[i])  / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        return rsi_values

    # ──────────────────────────────────────────────────────────────────
    # SCORING COMPONENTS
    # ──────────────────────────────────────────────────────────────────

    def _detect_candle_pattern(self, candles: list, levels: list, price: float) -> int:
        """Engulfing candles and pin bars/shooting stars near key S/R."""
        if len(candles) < 2:
            return 0

        # [Improvement #4] Use confirmed closed candle when configured
        idx  = -2 if self.cfg.use_confirmed_close and len(candles) >= 3 else -1
        cur  = candles[idx]
        prev = candles[idx - 1]

        body_cur   = abs(cur["close"]  - cur["open"])
        body_prev  = abs(prev["close"] - prev["open"])
        lower_wick = min(cur["open"], cur["close"]) - cur["low"]
        upper_wick = cur["high"] - max(cur["open"], cur["close"])

        supports    = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        near_sup    = supports[0]["price"]     if supports    else None
        near_res    = resistances[-1]["price"] if resistances else None
        threshold   = self.cfg.sr_alert_pct * 4

        score = 0

        # Bullish engulfing near support
        if (cur["close"] > cur["open"] and prev["close"] < prev["open"]
                and body_cur > body_prev
                and near_sup and abs(price - near_sup) / price < threshold):
            score += self.cfg.w_candle_pattern

        # Bearish engulfing near resistance
        if (cur["close"] < cur["open"] and prev["close"] > prev["open"]
                and body_cur > body_prev
                and near_res and abs(price - near_res) / price < threshold):
            score -= self.cfg.w_candle_pattern

        # Pin bar / hammer at support
        if (body_cur > 0 and lower_wick > 2 * body_cur
                and near_sup and abs(price - near_sup) / price < threshold):
            score += self.cfg.w_candle_pattern

        # Shooting star at resistance
        if (body_cur > 0 and upper_wick > 2 * body_cur
                and near_res and abs(price - near_res) / price < threshold):
            score -= self.cfg.w_candle_pattern

        return score

    def _detect_rsi_divergence(
        self, candles: list, rsi_values: list[float], price: float, levels: list
    ) -> int:
        """
        [Improvement #7] RSI divergence near S/R levels.
        Bullish: price lower low + RSI higher low → +w_divergence
        Bearish: price higher high + RSI lower high → -w_divergence
        Only fires when price is within 4× sr_alert_pct of a key level.
        """
        if len(rsi_values) < 10 or len(candles) < 10:
            return 0

        supports    = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        near_sup    = supports[0]["price"]     if supports    else None
        near_res    = resistances[-1]["price"] if resistances else None
        threshold   = self.cfg.sr_alert_pct * 4

        near_support    = near_sup and abs(price - near_sup) / price < threshold
        near_resistance = near_res and abs(price - near_res) / price < threshold
        if not near_support and not near_resistance:
            return 0

        lookback     = min(20, len(candles) - 1, len(rsi_values) - 1)
        rec_closes   = [c["close"] for c in candles[-lookback:]]
        rec_rsi      = rsi_values[-lookback:]

        # Swing lows in price
        lows = [
            (i, rec_closes[i], rec_rsi[i])
            for i in range(1, len(rec_closes) - 1)
            if rec_closes[i] < rec_closes[i-1] and rec_closes[i] < rec_closes[i+1]
        ]
        # Swing highs in price
        highs = [
            (i, rec_closes[i], rec_rsi[i])
            for i in range(1, len(rec_closes) - 1)
            if rec_closes[i] > rec_closes[i-1] and rec_closes[i] > rec_closes[i+1]
        ]

        # Bullish divergence
        if near_support and len(lows) >= 2:
            p1, p2 = lows[-2][1], lows[-1][1]
            r1, r2 = lows[-2][2], lows[-1][2]
            if p2 < p1 and r2 > r1:
                return self.cfg.w_divergence

        # Bearish divergence
        if near_resistance and len(highs) >= 2:
            p1, p2 = highs[-2][1], highs[-1][1]
            r1, r2 = highs[-2][2], highs[-1][2]
            if p2 > p1 and r2 < r1:
                return -self.cfg.w_divergence

        return 0

    def _score_consecutive_candles(self, candles: list) -> int:
        """
        [Improvement #8] Score consecutive same-direction candle closes.
        3 consecutive bullish closes → +2 (capped at consecutive_candle_cap).
        Opposite for bearish. Measures momentum conviction.
        """
        if len(candles) < 4:
            return 0

        idx   = -2 if self.cfg.use_confirmed_close and len(candles) >= 3 else -1
        ref   = candles[idx]
        bull  = ref["close"] > ref["open"]
        count = 0

        for i in range(idx, max(idx - 5, -len(candles)) - 1, -1):
            c = candles[i]
            if bull and c["close"] > c["open"]:
                count += 1
            elif not bull and c["close"] < c["open"]:
                count += 1
            else:
                break

        bonus = min(count - 1, self.cfg.consecutive_candle_cap) * self.cfg.w_consecutive
        return bonus if bull else -bonus

    def _check_atr_regime(self, current_atr: float, regime_avg: float) -> tuple[bool, str]:
        """
        [Improvement #9] Returns (signal_allowed, regime_label).
        Suppresses signals when Gold is in a tight consolidation range.
        """
        if regime_avg == 0:
            return True, "NORMAL"
        ratio = current_atr / regime_avg
        if ratio < self.cfg.atr_regime_threshold:
            return False, f"CONSOLIDATING ({ratio:.0%} of avg ATR)"
        elif ratio > 1.5:
            return True, f"VOLATILE ({ratio:.0%})"
        return True, f"NORMAL ({ratio:.0%})"

    def _score_trend_context(self, candles: list, price: float) -> int:
        """Price vs 20-period SMA. Above = bullish context, below = bearish."""
        window = min(20, len(candles))
        if window < 5:
            return 0
        sma = sum(c["close"] for c in candles[-window:]) / window
        if price > sma * 1.001:
            return  self.cfg.w_trend_context
        elif price < sma * 0.999:
            return -self.cfg.w_trend_context
        return 0

    def _score_session(self) -> int:
        """London/NY sessions get a liquidity bonus."""
        return self.cfg.w_session if _current_session() in ("LONDON", "NEW_YORK") else 0

    def _check_rejection_candle(self, candles: list, level_price: float, is_resistance: bool) -> bool:
        """
        [Improvement #6] Returns True only if the most recent closed candle
        shows a wick rejection away from the level.
        For resistance: upper wick > body (rejection downward).
        For support:    lower wick > body (rejection upward).
        """
        if len(candles) < 2:
            return False
        c          = candles[-2]   # last confirmed closed candle
        body       = abs(c["close"] - c["open"])
        upper_wick = c["high"]  - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]

        if is_resistance:
            return upper_wick > body * 0.5
        else:
            return lower_wick > body * 0.5

    # ──────────────────────────────────────────────────────────────────
    # PER-TIMEFRAME ANALYSIS
    # ──────────────────────────────────────────────────────────────────

    def _analyze_tf(
        self,
        candles: list,
        levels: list,
        price: float,
        rsi_values: Optional[list[float]] = None,
    ) -> dict:
        """
        Full weighted scoring for one timeframe.
        Returns: {bias, score, breakdown}
        """
        if not candles or len(candles) < 6:
            return {"bias": "HOLD", "score": 0, "breakdown": {}}

        idx  = -2 if self.cfg.use_confirmed_close and len(candles) >= 3 else -1
        last = candles[idx]
        prev = candles[idx - 1]

        score     = 0
        breakdown = {}

        # 1. Candle body
        v = self.cfg.w_candle_body if last["close"] > last["open"] else -self.cfg.w_candle_body
        score += v; breakdown["candle_body"] = v

        # 2. Momentum
        v = self.cfg.w_momentum if last["close"] > prev["close"] else -self.cfg.w_momentum
        score += v; breakdown["momentum"] = v

        # 3. S/R range position
        supports    = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        near_sup    = supports[0]["price"]     if supports    else None
        near_res    = resistances[-1]["price"] if resistances else None
        v = 0
        if near_sup and near_res:
            mid = (near_sup + near_res) / 2
            v   = self.cfg.w_sr_position if price > mid else -self.cfg.w_sr_position
        score += v; breakdown["sr_position"] = v

        # 4. S/R proximity + level strength (with TF confluence and Fib bonus)
        prox = 0; strength = 0
        prox_thresh = self.cfg.sr_alert_pct * 2

        if near_sup and abs(price - near_sup) / price < prox_thresh:
            prox += self.cfg.w_sr_proximity
            matching = [l for l in supports if abs(l["price"] - near_sup) < 1]
            if matching:
                m = matching[0]
                if m["touches"] >= 3:
                    strength += self.cfg.w_level_strength
                if m.get("tf_count", 1) >= 2:           # D1 + H4 confluence
                    strength += self.cfg.w_fib_confluence
                if m.get("fib_confluence", False):
                    strength += self.cfg.w_fib_confluence

        if near_res and abs(price - near_res) / price < prox_thresh:
            prox -= self.cfg.w_sr_proximity
            matching = [l for l in resistances if abs(l["price"] - near_res) < 1]
            if matching:
                m = matching[0]
                if m["touches"] >= 3:
                    strength -= self.cfg.w_level_strength
                if m.get("tf_count", 1) >= 2:
                    strength -= self.cfg.w_fib_confluence
                if m.get("fib_confluence", False):
                    strength -= self.cfg.w_fib_confluence

        score += prox + strength
        breakdown["sr_proximity"]   = prox
        breakdown["level_strength"] = strength

        # 5. Candle pattern
        v = self._detect_candle_pattern(candles, levels, price)
        score += v; breakdown["candle_pattern"] = v

        # 6. Trend context
        v = self._score_trend_context(candles, price)
        score += v; breakdown["trend_context"] = v

        # 7. Session bonus
        v = self._score_session()
        score += v; breakdown["session"] = v

        # 8. RSI divergence
        v = self._detect_rsi_divergence(candles, rsi_values or [], price, levels)
        score += v; breakdown["rsi_divergence"] = v

        # 9. Consecutive candle momentum
        v = self._score_consecutive_candles(candles)
        score += v; breakdown["consecutive"] = v

        threshold = self.cfg.bias_threshold
        bias = "BUY" if score >= threshold else ("SELL" if score <= -threshold else "HOLD")
        return {"bias": bias, "score": score, "breakdown": breakdown}

    # ──────────────────────────────────────────────────────────────────
    # SIGNAL COMPOSITION HELPERS
    # ──────────────────────────────────────────────────────────────────

    def _apply_d1_veto(self, d1_bias: str, proposed_direction: str) -> bool:
        """
        [Improvement #1] D1 acts as an HTF filter.
        Returns True (vetoed) if proposed direction contradicts D1 bias.
        HOLD on D1 never vetoes — only a clear opposite signal does.
        """
        if d1_bias == "HOLD":
            return False
        return d1_bias != proposed_direction

    def _check_combined_score(
        self, tf_d1: dict, tf_h4: dict, tf_h1: dict, direction: str
    ) -> tuple[bool, int]:
        """
        [Improvement #2] Sum scores of TFs that agree with the direction.
        Returns (passes, total_score).
        """
        total = sum(
            tf["score"]
            for tf in (tf_d1, tf_h4, tf_h1)
            if tf["bias"] == direction
        )
        return total >= self.cfg.combined_score_min, total

    def _calc_entry_zone(
        self, direction: str, price: float, levels: list, atr: float
    ) -> tuple[float, float, str]:
        """
        [Improvement #5] Compute entry zone relative to nearest S/R.
        Returns (zone_low, zone_high, tag) where tag is IDEAL / FAIR / EXTENDED.
        """
        width    = atr * self.cfg.entry_zone_atr_mult
        supports = sorted([l for l in levels if l["price"] < price], key=lambda x: x["price"], reverse=True)
        resists  = sorted([l for l in levels if l["price"] > price], key=lambda x: x["price"])

        if direction == "BUY" and supports:
            sup        = supports[0]["price"]
            zone_low   = sup
            zone_high  = sup + width
            if zone_low <= price <= zone_high:
                tag = "IDEAL ✅"
            elif price < zone_low + width * 2:
                tag = "FAIR"
            else:
                tag = "EXTENDED ⚠️"
            return zone_low, zone_high, tag

        if direction == "SELL" and resists:
            res        = resists[0]["price"]
            zone_high  = res
            zone_low   = res - width
            if zone_low <= price <= zone_high:
                tag = "IDEAL ✅"
            elif price > zone_high - width * 2:
                tag = "FAIR"
            else:
                tag = "EXTENDED ⚠️"
            return zone_low, zone_high, tag

        return price, price, "FAIR"

    def _calc_dynamic_sl_tp(
        self, direction: str, entry: float, levels: list, atr: float
    ) -> tuple[float, float, float]:
        """
        [Improvements #14 + #15]
        SL: placed just beyond the nearest S/R level, not an arbitrary ATR multiple.
        TP1: 1:1 R:R (locks in profit on half position).
        TP2: configured R:R (let the rest run).
        Falls back to ATR-based SL if no nearby level found.
        """
        buffer      = atr * self.cfg.dynamic_sl_buffer_mult
        fallback_sl = atr * self.cfg.atr_sl_mult

        if direction == "BUY":
            sups     = sorted([l for l in levels if l["price"] < entry],
                               key=lambda x: x["price"], reverse=True)
            if sups and abs(entry - sups[0]["price"]) < atr * 2:
                sl = round(sups[0]["price"] - buffer, 2)
            else:
                sl = round(entry - fallback_sl, 2)
            dist = entry - sl
            tp1  = round(entry + dist * 1.0, 2)
            tp2  = round(entry + dist * self.cfg.tp_rr, 2)

        else:  # SELL
            ress = sorted([l for l in levels if l["price"] > entry],
                           key=lambda x: x["price"])
            if ress and abs(ress[0]["price"] - entry) < atr * 2:
                sl = round(ress[0]["price"] + buffer, 2)
            else:
                sl = round(entry + fallback_sl, 2)
            dist = sl - entry
            tp1  = round(entry - dist * 1.0, 2)
            tp2  = round(entry - dist * self.cfg.tp_rr, 2)

        return sl, tp1, tp2

    # ──────────────────────────────────────────────────────────────────
    # DAILY FLIP TRACKING [Improvement #13]
    # ──────────────────────────────────────────────────────────────────

    def _reset_daily_flips_if_needed(self):
        today = _utc_now().date()
        if self.last_flip_date != today:
            self.daily_flip_count = 0
            self.last_flip_date   = today

    def _flip_limit_reached(self) -> bool:
        return self.daily_flip_count >= self.cfg.daily_flip_limit

    # ──────────────────────────────────────────────────────────────────
    # FORMATTING HELPERS
    # ──────────────────────────────────────────────────────────────────

    def _breakdown_lines(self, breakdown: dict) -> str:
        labels = {
            "candle_body":    "Candle body    ",
            "momentum":       "Momentum       ",
            "sr_position":    "S/R position   ",
            "sr_proximity":   "S/R proximity  ",
            "level_strength": "Level strength ",
            "candle_pattern": "Pattern        ",
            "trend_context":  "Trend (SMA20)  ",
            "session":        "Session bonus  ",
            "rsi_divergence": "RSI divergence ",
            "consecutive":    "Consecutive    ",
        }
        lines = []
        for key, label in labels.items():
            v    = breakdown.get(key, 0)
            sign = "+" if v > 0 else ("−" if v < 0 else " ")
            bar  = "▓" * min(abs(v), 4) + "·" * max(0, 4 - abs(v))
            lines.append(f"  {label}  {sign}{abs(v)}  {bar}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: SIGNAL
    # ══════════════════════════════════════════════════════════════════

    async def get_signal(self) -> str:
        try:
            d1, h4, h1 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 100),
                self.fetcher.fetch_ohlcv("4h",   120),
                self.fetcher.fetch_ohlcv("1h",   100),
            )
            price = await self.fetcher.fetch_current_price()
            if not price or not d1:
                return "❌ Failed to fetch Gold data. Check API keys."

            price = float(price)

            # ── News / Session gates ──
            news_blocked, news_desc = _is_news_blackout(self.cfg.news_blackout_minutes)
            if news_blocked:
                return (
                    f"🚫 *Signal Suppressed — News Blackout*\n"
                    f"High-impact event: {news_desc}\n"
                    f"Signals resume {self.cfg.news_blackout_minutes}min after event.\n"
                    f"⏰ {_eat_now()}"
                )

            if _is_session_open_window(self.cfg.session_open_blackout_minutes):
                return (
                    f"⏸ *Signal Paused — Session Open Window*\n"
                    f"First {self.cfg.session_open_blackout_minutes}min after session open "
                    f"are excluded (spread/volatility spike).\n"
                    f"⏰ {_eat_now()}"
                )

            # ── Data preparation ──
            levels_d1  = self._find_sr_levels(d1, "D1")
            levels_h4  = self._find_sr_levels(h4, "H4") if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)

            fib_levels = self._calc_fibonacci_levels(d1)
            all_levels = self._enrich_levels_with_fib(all_levels, fib_levels)

            cur_atr, regime_atr = self._calc_atr(h4 if h4 else d1)
            regime_ok, regime_label = self._check_atr_regime(cur_atr, regime_atr)

            rsi_h4 = self._calc_rsi(h4 or [])
            rsi_h1 = self._calc_rsi(h1 or [])
            rsi_d1 = self._calc_rsi(d1)

            # ── TF analysis ──
            tf_d1 = self._analyze_tf(d1,       levels_d1,  price, rsi_d1)
            tf_h4 = self._analyze_tf(h4 or [], levels_h4,  price, rsi_h4)
            tf_h1 = self._analyze_tf(h1 or [], all_levels, price, rsi_h1)

            biases     = [tf_d1["bias"], tf_h4["bias"], tf_h1["bias"]]
            buy_count  = biases.count("BUY")
            sell_count = biases.count("SELL")

            if buy_count >= self.cfg.confluence_min_tfs:
                raw_direction = "BUY"
            elif sell_count >= self.cfg.confluence_min_tfs:
                raw_direction = "SELL"
            else:
                raw_direction = "HOLD"

            # ── Apply filters ──
            suppression_reason = None

            if not regime_ok:
                suppression_reason = f"🔇 ATR regime: {regime_label}"

            if raw_direction not in ("HOLD", None) and not suppression_reason:
                # [#1] D1 veto
                if self._apply_d1_veto(tf_d1["bias"], raw_direction):
                    suppression_reason = f"🚫 D1 veto — D1 says {tf_d1['bias']}, H4/H1 say {raw_direction}"

            if raw_direction not in ("HOLD", None) and not suppression_reason:
                # [#2] Combined score gate
                passes, total_score = self._check_combined_score(tf_d1, tf_h4, tf_h1, raw_direction)
                if not passes:
                    suppression_reason = f"⚠️ Combined score {total_score} < min {self.cfg.combined_score_min}"

            direction  = raw_direction if not suppression_reason else "HOLD"
            confluence = int((max(buy_count, sell_count) / 3) * 100) if direction != "HOLD" else 33

            # ── Trade levels ──
            entry = price
            if direction != "HOLD":
                sl, tp1, tp2 = self._calc_dynamic_sl_tp(direction, entry, all_levels, cur_atr)
                zone_low, zone_high, entry_tag = self._calc_entry_zone(
                    direction, entry, all_levels, cur_atr
                )
            else:
                dist = cur_atr * self.cfg.atr_sl_mult
                sl, tp1, tp2 = round(entry - dist, 2), round(entry + dist, 2), round(entry + dist, 2)
                entry_tag = "N/A"

            sl_dist = abs(entry - sl)

            # ── Nearest levels display ──
            near = sorted(all_levels, key=lambda x: abs(x["price"] - price))[:8]
            near = sorted(near, key=lambda x: x["price"], reverse=True)
            level_lines = []
            for l in near:
                arrow  = "🔴 R" if l["price"] > price else "🟢 S"
                heat   = " 🔥" * min(l["touches"] - 1, 3) if l["touches"] > 1 else ""
                fib    = " ◆Fib" if l.get("fib_confluence") else ""
                mtf    = " 🔗D1" if l.get("tf_count", 1) >= 2 else ""
                marker = " ◀ PRICE" if abs(l["price"] - price) / price < 0.001 else ""
                level_lines.append(f"  {arrow} `{l['price']:.2f}`{heat}{fib}{mtf}{marker}")

            dir_emoji  = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "🟡")
            conf_bar   = "█" * (confluence // 10) + "░" * (10 - confluence // 10)
            session    = _current_session()

            # Current RSI value for display
            cur_rsi_h4 = f"{rsi_h4[-1]:.1f}" if rsi_h4 else "—"

            msg = (
                f"🥇 *GOLD (XAU/USD)*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{dir_emoji} *Signal: {direction}*\n"
                f"📊 Confluence: `{confluence}%`  `{conf_bar}`\n"
                f"🕐 Session: `{session}`  |  RSI H4: `{cur_rsi_h4}`\n"
                f"📈 ATR Regime: `{regime_label}`\n"
            )

            if suppression_reason:
                msg += f"⚠️ Filter active: _{suppression_reason}_\n"

            msg += (
                f"\n💹 *Price:*        `{entry:.2f}`\n"
                f"📍 *Entry Zone:*   `{entry_tag}`  [`{zone_low:.2f}` – `{zone_high:.2f}`]\n"
            )

            if direction != "HOLD":
                msg += (
                    f"🛑 *Stop Loss:*    `{sl:.2f}`  (dist: `{sl_dist:.2f}`)\n"
                    f"🎯 *TP1 (1:1):*    `{tp1:.2f}`\n"
                    f"✅ *TP2 ({self.cfg.tp_rr:.1f}R):*   `{tp2:.2f}`\n"
                    f"📏 *ATR:*          `{cur_atr:.2f}`\n"
                )

            msg += (
                f"\n🕐 *Timeframe Scores*\n"
                f"  D1 → {tf_d1['bias']} ({tf_d1['score']:+d})  "
                f"H4 → {tf_h4['bias']} ({tf_h4['score']:+d})  "
                f"H1 → {tf_h1['bias']} ({tf_h1['score']:+d})\n\n"
                f"🔬 *Score Breakdown (H4)*\n"
                + self._breakdown_lines(tf_h4["breakdown"]) +
                f"\n\n🏗️ *Key S/R Levels*\n"
                + "\n".join(level_lines) +
                f"\n🔗 = D1+H4 confluence  ◆ = Fibonacci\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {_eat_now()}"
            )
            return msg

        except Exception as e:
            logger.error(f"get_signal error: {e}", exc_info=True)
            return f"❌ Signal error: {str(e)}"

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: LEVELS
    # ══════════════════════════════════════════════════════════════════

    async def get_levels(self) -> str:
        try:
            d1, h4 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 100),
                self.fetcher.fetch_ohlcv("4h",   120),
            )
            price = await self.fetcher.fetch_current_price()
            if not price or not d1:
                return "❌ Failed to fetch data."

            price      = float(price)
            levels_d1  = self._find_sr_levels(d1, "D1")
            levels_h4  = self._find_sr_levels(h4, "H4") if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)
            fib_levels = self._calc_fibonacci_levels(d1)
            all_levels = self._enrich_levels_with_fib(all_levels, fib_levels)
            cur_atr, _ = self._calc_atr(h4 if h4 else d1)

            above = sorted([l for l in all_levels if l["price"] > price],  key=lambda x: x["price"])[:6]
            below = sorted([l for l in all_levels if l["price"] <= price], key=lambda x: x["price"], reverse=True)[:6]

            def fmt(l):
                heat = " 🔥" * min(l["touches"] - 1, 3) if l["touches"] > 1 else ""
                fib  = " ◆Fib" if l.get("fib_confluence") else ""
                mtf  = " 🔗D1" if l.get("tf_count", 1) >= 2 else ""
                dist = abs(l["price"] - price)
                return f"  `{l['price']:.2f}`  ({dist:.1f}pt){heat}{fib}{mtf}"

            res_text = "\n".join([fmt(l) for l in above]) or "  —"
            sup_text = "\n".join([fmt(l) for l in below]) or "  —"

            # Fib levels for reference
            fib_lines = "  " + "  |  ".join([f"`{f:.2f}`" for f in fib_levels]) if fib_levels else "  —"

            return (
                f"🥇 *Gold Key S/R Levels*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💹 Price: `{price:.2f}`  |  ATR: `{cur_atr:.2f}`\n\n"
                f"🔴 *Resistance Above*\n{res_text}\n\n"
                f"🟢 *Support Below*\n{sup_text}\n\n"
                f"◆ *Fibonacci Levels*\n{fib_lines}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥=multi-touch  🔗D1=D1+H4 confluence  ◆=Fib match\n"
                f"⏰ {_eat_now()}"
            )

        except Exception as e:
            logger.error(f"get_levels error: {e}", exc_info=True)
            return f"❌ Levels error: {str(e)}"

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: ALERTS
    # ══════════════════════════════════════════════════════════════════

    async def check_alerts(self) -> list[str]:
        alerts = []
        try:
            # [#11] News blackout gate
            news_blocked, news_desc = _is_news_blackout(self.cfg.news_blackout_minutes)
            if news_blocked:
                logger.info(f"Alert scan suppressed — news blackout: {news_desc}")
                return alerts

            # [#12] Session open gate
            if _is_session_open_window(self.cfg.session_open_blackout_minutes):
                logger.info("Alert scan suppressed — session open window")
                return alerts

            # [#13] Reset daily flip count if new day
            self._reset_daily_flips_if_needed()

            d1, h4 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 80),
                self.fetcher.fetch_ohlcv("4h",   100),
            )
            price = await self.fetcher.fetch_current_price()
            if not price or not d1:
                return alerts

            price      = float(price)
            levels_d1  = self._find_sr_levels(d1, "D1")
            levels_h4  = self._find_sr_levels(h4, "H4") if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)
            fib_levels = self._calc_fibonacci_levels(d1)
            all_levels = self._enrich_levels_with_fib(all_levels, fib_levels)

            cur_atr, regime_atr = self._calc_atr(h4 if h4 else d1)
            regime_ok, _        = self._check_atr_regime(cur_atr, regime_atr)

            rsi_d1 = self._calc_rsi(d1)
            rsi_h4 = self._calc_rsi(h4 or [])

            tf_d1 = self._analyze_tf(d1,       levels_d1, price, rsi_d1)
            tf_h4 = self._analyze_tf(h4 or [], levels_h4, price, rsi_h4)
            biases = [tf_d1["bias"], tf_h4["bias"]]

            current_signal = (
                "BUY"  if biases.count("BUY")  >= 2 else
                "SELL" if biases.count("SELL") >= 2 else "HOLD"
            )

            # ── Signal flip check ──
            if (
                self.last_signal
                and self.last_signal != current_signal
                and current_signal != "HOLD"
                and regime_ok
                and not self._apply_d1_veto(tf_d1["bias"], current_signal)
                and not self._flip_limit_reached()
            ):
                passes, total_score = self._check_combined_score(
                    tf_d1, tf_h4, {"bias": "HOLD", "score": 0, "breakdown": {}},
                    current_signal,
                )
                if passes:
                    emoji   = "🟢📈" if current_signal == "BUY" else "🔴📉"
                    sl, tp1, tp2 = self._calc_dynamic_sl_tp(
                        current_signal, price, all_levels, cur_atr
                    )
                    _, _, entry_tag = self._calc_entry_zone(
                        current_signal, price, all_levels, cur_atr
                    )
                    session = _current_session()
                    self.daily_flip_count += 1

                    flips_left = self.cfg.daily_flip_limit - self.daily_flip_count
                    flip_warn  = f"\n⚠️ _Only {flips_left} more flip alerts today_" if flips_left <= 1 else ""

                    alerts.append(
                        f"{emoji} *GOLD Signal Flip!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"{self.last_signal}  →  *{current_signal}*\n\n"
                        f"💹 Price:       `{price:.2f}`\n"
                        f"📍 Entry Zone:  `{entry_tag}`\n"
                        f"🛑 SL:          `{sl:.2f}`\n"
                        f"🎯 TP1 (1:1):   `{tp1:.2f}`\n"
                        f"✅ TP2 ({self.cfg.tp_rr:.1f}R):  `{tp2:.2f}`\n"
                        f"📊 Score: D1 {tf_d1['score']:+d}  H4 {tf_h4['score']:+d}  "
                        f"Total: {total_score:+d}\n"
                        f"🕐 Session: `{session}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏰ {_eat_now()}"
                        f"{flip_warn}\n"
                        f"_/signal for full breakdown_"
                    )

            elif self._flip_limit_reached() and current_signal != self.last_signal:
                logger.info(f"Daily flip limit reached ({self.daily_flip_count}). Suppressing flip alert.")

            self.last_signal = current_signal

            # ── S/R proximity alerts ──
            triggered_now = set()
            for level in all_levels:
                proximity = abs(price - level["price"]) / price
                level_key = f"{level['price']:.0f}"

                if proximity <= self.cfg.sr_alert_pct:
                    triggered_now.add(level_key)
                    if level_key not in self.alerted_levels:
                        is_res = level["price"] > price

                        # [#6] Rejection candle gate
                        has_rejection = self._check_rejection_candle(
                            h4 or d1, level["price"], is_res
                        )
                        if not has_rejection:
                            logger.info(
                                f"S/R alert suppressed at {level['price']:.2f} — "
                                f"no rejection candle confirmed"
                            )
                            continue

                        zone_type = "🔴 RESISTANCE" if is_res else "🟢 SUPPORT"
                        heat      = " 🔥" * min(level["touches"] - 1, 3) if level["touches"] > 1 else ""
                        fib_tag   = "  ◆ Fibonacci confluence" if level.get("fib_confluence") else ""
                        mtf_tag   = "  🔗 D1+H4 confirmed"    if level.get("tf_count", 1) >= 2 else ""
                        bias_hint = "Rejection likely 🔽" if is_res else "Bounce likely 🔼"
                        strength  = ("Strong" if level["touches"] >= 3
                                     else ("Moderate" if level["touches"] == 2 else "Untested"))

                        alerts.append(
                            f"⚡ *Gold at Key Level!*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{zone_type}{heat}: `{level['price']:.2f}`\n"
                            f"💹 Price:    `{price:.2f}`\n"
                            f"📏 Distance: `{proximity * 100:.2f}%`\n"
                            f"💪 Strength: `{strength}` ({level['touches']} touches)\n"
                            f"{fib_tag}{mtf_tag}\n"
                            f"💡 {bias_hint}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_eat_now()}"
                        )
                        self.alerted_levels.add(level_key)

            # Cool-down: clear levels that price has moved well away from
            stale = {
                k for k in self.alerted_levels
                if k not in triggered_now
                and abs(float(k) - price) / price >
                    self.cfg.sr_alert_pct * self.cfg.sr_alert_cooldown_mult
            }
            self.alerted_levels -= stale

        except Exception as e:
            logger.error(f"check_alerts error: {e}", exc_info=True)

        return alerts
