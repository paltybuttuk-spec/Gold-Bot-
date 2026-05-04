"""
signal_engine.py — Gold Signal Engine v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Prediction + Confirmation architecture — signals fire AS the move begins.

Three-phase alert system:
  Phase 0 PREDICT  — velocity toward a level, alerts before price arrives
  Phase 1 APPROACH — price within 0.8% and heading toward the level
  Phase 2 ENTER    — M15 candle closes with rejection/breakout pattern

H4/D1 = structural context (WHERE levels are, what bias is)
M15   = entry timing (WHEN to enter — 16x faster than waiting for H4 close)

All 15 win-rate improvements from v2 are retained.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
    sr_cluster_pct: float = 0.005
    sr_alert_pct: float = 0.003        # 0.3% = at the level
    sr_alert_cooldown_mult: float = 3.0
    fib_confluence_pct: float = 0.002

    # ── Prediction — Phase 0 ──────────────────────────────────────────
    approach_warn_pct: float = 0.008
    # Distance to fire "APPROACHING" alert. 0.8% ≈ $24 at $3000.
    # This fires BEFORE price reaches the level.

    velocity_window: int = 6
    # M15 candles used to calculate speed and direction of price movement.

    approach_eta_candles: int = 8
    # Max M15 candles lookahead for a prediction to fire (~2 hours).

    # ── M15 Entry Confirmation — Phase 2 ─────────────────────────────
    m15_rejection_wick_ratio: float = 0.40
    # Wick must be ≥ 40% of M15 candle range to confirm rejection.
    # Lower = more signals, higher = stricter. Range: 0.30–0.60

    m15_breakout_body_ratio: float = 0.60
    # Body must be ≥ 60% of M15 candle range to confirm breakout momentum.

    m15_require_h4_alignment: bool = True
    # Only fire M15 entry if H4 bias agrees with the direction.
    # Prevents catching counter-trend M15 moves.

    # ── Compression Detection ─────────────────────────────────────────
    compression_swing_n: int = 4
    # Minimum number of higher lows (or lower highs) needed to flag compression.

    # ── ATR / Trade Sizing ─────────────────────────────────────────────
    atr_period: int = 14
    atr_sl_mult: float = 1.5           # fallback SL if no nearby S/R
    tp_rr: float = 2.0
    dynamic_sl_buffer_mult: float = 0.3

    # ── Scoring Thresholds ─────────────────────────────────────────────
    bias_threshold: int = 3
    confluence_min_tfs: int = 2
    combined_score_min: int = 6

    # ── Scoring Weights ────────────────────────────────────────────────
    w_candle_body: int = 1
    w_momentum: int = 1
    w_sr_position: int = 1
    w_sr_proximity: int = 2
    w_level_strength: int = 1
    w_candle_pattern: int = 2
    w_trend_context: int = 1
    w_session: int = 1
    w_divergence: int = 3
    w_consecutive: int = 1
    w_fib_confluence: int = 1
    consecutive_candle_cap: int = 2

    # ── ATR Regime Filter ─────────────────────────────────────────────
    atr_regime_threshold: float = 0.6
    atr_regime_lookback: int = 20

    # ── Candle Close Confirmation (H4-level scoring) ──────────────────
    use_confirmed_close: bool = True
    # Note: M15 entry signals always use the last closed M15 candle.

    # ── Entry Zone ─────────────────────────────────────────────────────
    entry_zone_atr_mult: float = 0.3

    # ── News / Session Filters ─────────────────────────────────────────
    news_blackout_minutes: int = 30
    session_open_blackout_minutes: int = 15

    # ── Daily Flip Limit ──────────────────────────────────────────────
    daily_flip_limit: int = 3

    # ── Check Interval ────────────────────────────────────────────────
    check_interval_minutes: int = 5


# ── FOMC dates (UTC 18:00) ─────────────────────────────────────────────
_FOMC_DATES = [
    (2025,1,29),(2025,3,19),(2025,5,7),(2025,6,18),
    (2025,7,30),(2025,9,17),(2025,10,29),(2025,12,10),
    (2026,1,28),(2026,3,18),(2026,4,29),(2026,6,17),
    (2026,7,29),(2026,9,16),(2026,10,28),(2026,12,9),
]
_FOMC_HOUR_UTC = 18

CFG = Config()


# ══════════════════════════════════════════════════════════════════════
# MODULE UTILITIES
# ══════════════════════════════════════════════════════════════════════

def _eat_now() -> str:
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M EAT")

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _current_session() -> str:
    h = _utc_now().hour
    if 7  <= h < 16: return "LONDON"
    if 13 <= h < 22: return "NEW_YORK"
    if 0  <= h < 9:  return "ASIAN"
    return "OFF"

def _is_session_open_window(minutes: int) -> bool:
    h, m   = _utc_now().hour, _utc_now().minute
    total  = h * 60 + m
    for open_min in (7 * 60, 13 * 60):
        if 0 <= total - open_min < minutes:
            return True
    return False

def _first_friday(year: int, month: int) -> datetime:
    first_wd     = calendar.weekday(year, month, 1)
    days_to_fri  = (4 - first_wd) % 7
    return datetime(year, month, 1 + days_to_fri, 13, 30, tzinfo=timezone.utc)

def _get_news_events() -> list:
    now    = _utc_now()
    events = []
    for dm in range(-1, 3):
        yr = now.year + (now.month + dm - 1) // 12
        mo = (now.month + dm - 1) % 12 + 1
        try:
            events.append(_first_friday(yr, mo))
            fwd = calendar.weekday(yr, mo, 1)
            dw  = (2 - fwd) % 7
            sw  = 1 + dw + 7
            if sw <= calendar.monthrange(yr, mo)[1]:
                events.append(datetime(yr, mo, sw, 13, 30, tzinfo=timezone.utc))
        except ValueError:
            pass
    for y, mo, d in _FOMC_DATES:
        events.append(datetime(y, mo, d, _FOMC_HOUR_UTC, 0, tzinfo=timezone.utc))
    return events

def _is_news_blackout(minutes: int) -> tuple:
    now    = _utc_now()
    window = timedelta(minutes=minutes)
    for ev in _get_news_events():
        diff = abs(now - ev)
        if diff <= window:
            direction = "in" if ev > now else "ago"
            return True, f"{int(diff.total_seconds()//60)}min {direction}"
    return False, ""


# ══════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════

class SignalEngine:
    def __init__(self, td_key: str, fh_key: str, config: Config = CFG):
        self.fetcher  = DataFetcher(td_key, fh_key)
        self.cfg      = config

        # Alert state
        self.last_signal:       Optional[str]  = None
        self.alerted_levels:    set            = set()   # at-level alerts (Phase 1)
        self.approach_alerted:  set            = set()   # approaching alerts (Phase 0)
        self.entry_alerted:     set            = set()   # entry fired (Phase 2) — clears when price moves away

        # Daily flip tracking
        self.daily_flip_count:  int            = 0
        self.last_flip_date:    Optional[date] = None

    # ──────────────────────────────────────────────────────────────────
    # S/R DETECTION
    # ──────────────────────────────────────────────────────────────────

    def _find_sr_levels(self, candles: list, tf_label: str = "H4") -> list:
        # Strip malformed/zero-price candles before any processing
        candles = [c for c in candles if c.get("high", 0) > 0 and c.get("low", 0) > 0]
        n      = self.cfg.sr_swing_n
        if len(candles) < n * 2 + 1:
            return []
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
        if not levels:
            return []
        sorted_lvls = sorted(levels, key=lambda x: x["price"])
        clustered   = [{**sorted_lvls[0], "tfs": set(sorted_lvls[0].get("tfs", set()))}]
        for lvl in sorted_lvls[1:]:
            last = clustered[-1]
            if last["price"] > 0 and abs(lvl["price"] - last["price"]) / last["price"] < self.cfg.sr_cluster_pct:
                last["price"]   = round((last["price"] + lvl["price"]) / 2, 2)
                last["touches"] += lvl.get("touches", 1)
                merged           = last["tfs"] | lvl.get("tfs", set())
                last["tfs"]      = merged
                last["tf_count"] = len(merged)
            else:
                clustered.append({**lvl, "tfs": set(lvl.get("tfs", set()))})
        return sorted(clustered, key=lambda x: x["price"], reverse=True)

    # ──────────────────────────────────────────────────────────────────
    # FIBONACCI
    # ──────────────────────────────────────────────────────────────────

    def _calc_fibonacci_levels(self, candles: list) -> list:
        if len(candles) < 20:
            return []
        recent = candles[-50:] if len(candles) >= 50 else candles
        high   = max(c["high"] for c in recent)
        low    = min(c["low"]  for c in recent)
        diff   = high - low
        if diff < 1:
            return []
        return [round(high - r * diff, 2) for r in (0.236, 0.382, 0.500, 0.618, 0.786)]

    def _enrich_levels_with_fib(self, levels: list, fib_levels: list) -> list:
        for lvl in levels:
            lvl.setdefault("fib_confluence", False)
            for fib in fib_levels:
                if fib > 0 and abs(lvl["price"] - fib) / fib < self.cfg.fib_confluence_pct:
                    lvl["fib_confluence"] = True
                    lvl["touches"]        += 1
                    break
        return levels

    # ──────────────────────────────────────────────────────────────────
    # INDICATORS
    # ──────────────────────────────────────────────────────────────────

    def _calc_atr(self, candles: list) -> tuple:
        """Returns (current_atr, regime_avg_atr)."""
        trs = [
            max(candles[i]["high"] - candles[i]["low"],
                abs(candles[i]["high"] - candles[i-1]["close"]),
                abs(candles[i]["low"]  - candles[i-1]["close"]))
            for i in range(1, len(candles))
        ]
        if not trs:
            return 15.0, 15.0
        cur_trs   = trs[-self.cfg.atr_period:]
        reg_trs   = trs[-self.cfg.atr_regime_lookback:]
        return sum(cur_trs) / len(cur_trs), sum(reg_trs) / len(reg_trs)

    def _calc_rsi(self, candles: list, period: int = 14) -> list:
        closes = [c["close"] for c in candles]
        if len(closes) < period + 1:
            return []
        deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains    = [max(d, 0) for d in deltas]
        losses   = [abs(min(d, 0)) for d in deltas]
        avg_gain = sum(gains[:period])  / period
        avg_loss = sum(losses[:period]) / period
        rsi = []
        for i in range(period, len(deltas)):
            rsi.append(100.0 if avg_loss == 0 else round(100 - 100/(1 + avg_gain/avg_loss), 2))
            avg_gain = (avg_gain * (period-1) + gains[i])  / period
            avg_loss = (avg_loss * (period-1) + losses[i]) / period
        return rsi

    # ──────────────────────────────────────────────────────────────────
    # PREDICTION ENGINE — Phase 0
    # ──────────────────────────────────────────────────────────────────

    def _calc_m15_velocity(self, candles_m15: list) -> tuple:
        """
        Calculate price velocity from recent M15 candles.
        Returns (points_per_candle: float, label: str).
        Positive = moving up, negative = moving down.
        """
        n = min(self.cfg.velocity_window, len(candles_m15))
        if n < 2:
            return 0.0, "FLAT →"
        recent = candles_m15[-n:]
        change = recent[-1]["close"] - recent[0]["close"]
        ppc    = change / (n - 1)        # points per M15 candle

        if   ppc >  8: label = "STRONG UP ↑↑↑"
        elif ppc >  3: label = "UP ↑↑"
        elif ppc >  0.5: label = "DRIFTING UP ↑"
        elif ppc < -8: label = "STRONG DOWN ↓↓↓"
        elif ppc < -3: label = "DOWN ↓↓"
        elif ppc < -0.5: label = "DRIFTING DOWN ↓"
        else:          label = "FLAT →"
        return round(ppc, 2), label

    def _predict_approaches(self, price: float, velocity: float, levels: list) -> list:
        """
        Phase 0: Which levels is price currently heading toward?
        Returns list of {level, eta_candles, eta_minutes, distance_pct}.
        Only includes levels within approach_eta_candles M15 candles.
        """
        if abs(velocity) < 0.3:
            return []
        predictions = []
        for level in levels:
            lp = level["price"]
            moving_toward = (velocity > 0 and lp > price) or (velocity < 0 and lp < price)
            if not moving_toward:
                continue
            dist       = abs(lp - price)
            eta_c      = dist / abs(velocity)
            if eta_c <= self.cfg.approach_eta_candles:
                predictions.append({
                    "level":        level,
                    "eta_candles":  round(eta_c, 1),
                    "eta_minutes":  int(eta_c * 15),
                    "distance_pct": round(dist / price * 100, 3),
                    "distance_pts": round(dist, 2),
                })
        return sorted(predictions, key=lambda x: x["eta_candles"])

    def _detect_compression(self, candles: list, levels: list, price: float) -> dict:
        """
        Detect price compression (higher lows into resistance, or lower highs into support).
        This is a PREDICTIVE signal — a coiling spring before a breakout.
        Returns {detected, direction, target_level, description}.
        """
        if len(candles) < 10:
            return {"detected": False}

        n         = min(20, len(candles))
        recent    = candles[-n:]
        lows      = [c["low"]  for c in recent]
        highs     = [c["high"] for c in recent]
        closes    = [c["close"] for c in recent]
        min_swings = self.cfg.compression_swing_n

        # Find swing lows
        swing_lows = [
            lows[i] for i in range(1, len(lows)-1)
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]
        ]
        # Find swing highs
        swing_highs = [
            highs[i] for i in range(1, len(highs)-1)
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]
        ]

        resistances = [l for l in levels if l["price"] > price]
        supports    = [l for l in levels if l["price"] < price]

        # Bullish compression: higher lows converging toward resistance
        if (len(swing_lows) >= min_swings and resistances
                and all(swing_lows[i] < swing_lows[i+1]
                        for i in range(len(swing_lows)-min_swings, len(swing_lows)-1))):
            target = resistances[-1]
            return {
                "detected":     True,
                "direction":    "BULLISH",
                "target_level": target["price"],
                "description":  (
                    f"Higher lows compressing toward resistance `{target['price']:.2f}`. "
                    f"Breakout likely upward."
                ),
            }

        # Bearish compression: lower highs converging toward support
        if (len(swing_highs) >= min_swings and supports
                and all(swing_highs[i] > swing_highs[i+1]
                        for i in range(len(swing_highs)-min_swings, len(swing_highs)-1))):
            target = supports[0]
            return {
                "detected":     True,
                "direction":    "BEARISH",
                "target_level": target["price"],
                "description":  (
                    f"Lower highs compressing toward support `{target['price']:.2f}`. "
                    f"Breakdown likely."
                ),
            }

        return {"detected": False}

    # ──────────────────────────────────────────────────────────────────
    # M15 ENTRY CONFIRMATION — Phase 2
    # ──────────────────────────────────────────────────────────────────

    def _analyze_m15_at_level(
        self, candles_m15: list, level_price: float, price: float
    ) -> dict:
        """
        Analyze the last closed M15 candle for entry pattern at a key level.
        Returns {signal, pattern_type, reason, confidence, entry_data}.

        Patterns:
          REJECTION — wick at level, body away = fade the level
          BREAKOUT  — strong body closes through the level = follow the break
          NONE      — no actionable pattern yet
        """
        if len(candles_m15) < 3:
            return {"signal": "NONE", "pattern_type": "NONE", "reason": "insufficient M15 data"}

        is_resistance = level_price > price
        c  = candles_m15[-2]    # last confirmed closed M15 candle
        pc = candles_m15[-3]    # the one before

        rng        = c["high"] - c["low"]
        if rng < 0.5:
            return {"signal": "NONE", "pattern_type": "NONE", "reason": "flat M15 candle"}

        body       = abs(c["close"] - c["open"])
        upper_wick = c["high"]  - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]
        body_ratio = body / rng
        uw_ratio   = upper_wick / rng
        lw_ratio   = lower_wick / rng

        rej_thresh      = self.cfg.m15_rejection_wick_ratio
        breakout_thresh = self.cfg.m15_breakout_body_ratio

        if is_resistance:
            # ── Rejection at resistance (SELL setup) ──
            if uw_ratio >= rej_thresh and c["close"] < c["open"]:
                confidence = "HIGH" if uw_ratio > 0.55 else "MEDIUM"
                return {
                    "signal":       "SELL",
                    "pattern_type": "REJECTION",
                    "reason":       f"M15 bearish rejection — upper wick {uw_ratio:.0%} of range",
                    "confidence":   confidence,
                }
            # ── Bearish engulfing at resistance ──
            if (c["close"] < c["open"] and pc["close"] > pc["open"]
                    and body > abs(pc["close"] - pc["open"]) * 0.8
                    and c["high"] >= level_price * 0.999):
                return {
                    "signal":       "SELL",
                    "pattern_type": "ENGULFING",
                    "reason":       "M15 bearish engulfing at resistance",
                    "confidence":   "HIGH",
                }
            # ── Breakout above resistance (BUY) ──
            if (c["close"] > level_price and c["open"] < level_price
                    and body_ratio >= breakout_thresh and c["close"] > c["open"]):
                return {
                    "signal":       "BUY",
                    "pattern_type": "BREAKOUT",
                    "reason":       f"M15 breakout above resistance — body {body_ratio:.0%}",
                    "confidence":   "HIGH" if body_ratio > 0.75 else "MEDIUM",
                }

        else:  # support
            # ── Rejection at support (BUY setup) ──
            if lw_ratio >= rej_thresh and c["close"] > c["open"]:
                confidence = "HIGH" if lw_ratio > 0.55 else "MEDIUM"
                return {
                    "signal":       "BUY",
                    "pattern_type": "REJECTION",
                    "reason":       f"M15 bullish rejection — lower wick {lw_ratio:.0%} of range",
                    "confidence":   confidence,
                }
            # ── Bullish engulfing at support ──
            if (c["close"] > c["open"] and pc["close"] < pc["open"]
                    and body > abs(pc["close"] - pc["open"]) * 0.8
                    and c["low"] <= level_price * 1.001):
                return {
                    "signal":       "BUY",
                    "pattern_type": "ENGULFING",
                    "reason":       "M15 bullish engulfing at support",
                    "confidence":   "HIGH",
                }
            # ── Breakdown below support (SELL) ──
            if (c["close"] < level_price and c["open"] > level_price
                    and body_ratio >= breakout_thresh and c["close"] < c["open"]):
                return {
                    "signal":       "SELL",
                    "pattern_type": "BREAKDOWN",
                    "reason":       f"M15 breakdown below support — body {body_ratio:.0%}",
                    "confidence":   "HIGH" if body_ratio > 0.75 else "MEDIUM",
                }

        return {"signal": "NONE", "pattern_type": "NONE", "reason": "no M15 pattern yet — watching"}

    # ──────────────────────────────────────────────────────────────────
    # SCORING COMPONENTS (H4/D1 structural bias)
    # ──────────────────────────────────────────────────────────────────

    def _detect_candle_pattern(self, candles: list, levels: list, price: float) -> int:
        if len(candles) < 2:
            return 0
        idx  = -2 if self.cfg.use_confirmed_close and len(candles) >= 3 else -1
        cur  = candles[idx]; prev = candles[idx-1]
        body_cur   = abs(cur["close"]  - cur["open"])
        body_prev  = abs(prev["close"] - prev["open"])
        lower_wick = min(cur["open"], cur["close"]) - cur["low"]
        upper_wick = cur["high"] - max(cur["open"], cur["close"])
        supports    = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        near_sup    = supports[0]["price"]     if supports    else None
        near_res    = resistances[-1]["price"] if resistances else None
        t           = self.cfg.sr_alert_pct * 4
        score = 0
        if cur["close"] > cur["open"] and prev["close"] < prev["open"] and body_cur > body_prev \
                and near_sup and abs(price - near_sup)/price < t:
            score += self.cfg.w_candle_pattern
        if cur["close"] < cur["open"] and prev["close"] > prev["open"] and body_cur > body_prev \
                and near_res and abs(price - near_res)/price < t:
            score -= self.cfg.w_candle_pattern
        if body_cur > 0 and lower_wick > 2*body_cur and near_sup and abs(price-near_sup)/price < t:
            score += self.cfg.w_candle_pattern
        if body_cur > 0 and upper_wick > 2*body_cur and near_res and abs(price-near_res)/price < t:
            score -= self.cfg.w_candle_pattern
        return score

    def _detect_rsi_divergence(self, candles: list, rsi: list, price: float, levels: list) -> int:
        if len(rsi) < 10 or len(candles) < 10:
            return 0
        supports    = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        near_sup    = supports[0]["price"]     if supports    else None
        near_res    = resistances[-1]["price"] if resistances else None
        t           = self.cfg.sr_alert_pct * 4
        near_s = near_sup and abs(price - near_sup)/price < t
        near_r = near_res and abs(price - near_res)/price < t
        if not near_s and not near_r:
            return 0
        lb = min(20, len(candles)-1, len(rsi)-1)
        rc = [c["close"] for c in candles[-lb:]]
        rr = rsi[-lb:]
        lows  = [(i,rc[i],rr[i]) for i in range(1,len(rc)-1) if rc[i]<rc[i-1] and rc[i]<rc[i+1]]
        highs = [(i,rc[i],rr[i]) for i in range(1,len(rc)-1) if rc[i]>rc[i-1] and rc[i]>rc[i+1]]
        if near_s and len(lows) >= 2 and lows[-1][1] < lows[-2][1] and lows[-1][2] > lows[-2][2]:
            return self.cfg.w_divergence
        if near_r and len(highs) >= 2 and highs[-1][1] > highs[-2][1] and highs[-1][2] < highs[-2][2]:
            return -self.cfg.w_divergence
        return 0

    def _score_consecutive_candles(self, candles: list) -> int:
        if len(candles) < 4:
            return 0
        idx  = -2 if self.cfg.use_confirmed_close and len(candles) >= 3 else -1
        ref  = candles[idx]; bull = ref["close"] > ref["open"]; count = 0
        for i in range(idx, max(idx-5, -len(candles))-1, -1):
            c = candles[i]
            if (bull and c["close"] > c["open"]) or (not bull and c["close"] < c["open"]):
                count += 1
            else:
                break
        bonus = min(count-1, self.cfg.consecutive_candle_cap) * self.cfg.w_consecutive
        return bonus if bull else -bonus

    def _check_atr_regime(self, cur: float, avg: float) -> tuple:
        if avg == 0:
            return True, "NORMAL"
        r = cur / avg
        if r < self.cfg.atr_regime_threshold:
            return False, f"CONSOLIDATING ({r:.0%})"
        return True, f"{'VOLATILE' if r > 1.5 else 'NORMAL'} ({r:.0%})"

    def _score_trend_context(self, candles: list, price: float) -> int:
        w   = min(20, len(candles))
        if w < 5: return 0
        sma = sum(c["close"] for c in candles[-w:]) / w
        if price > sma * 1.001: return  self.cfg.w_trend_context
        if price < sma * 0.999: return -self.cfg.w_trend_context
        return 0

    def _score_session(self) -> int:
        return self.cfg.w_session if _current_session() in ("LONDON", "NEW_YORK") else 0

    def _analyze_tf(self, candles: list, levels: list, price: float, rsi: list = None) -> dict:
        if not candles or len(candles) < 6:
            return {"bias": "HOLD", "score": 0, "breakdown": {}}
        idx = -2 if self.cfg.use_confirmed_close and len(candles) >= 3 else -1
        last = candles[idx]; prev = candles[idx-1]
        score = 0; bd = {}
        v = self.cfg.w_candle_body if last["close"] > last["open"] else -self.cfg.w_candle_body
        score += v; bd["candle_body"] = v
        v = self.cfg.w_momentum if last["close"] > prev["close"] else -self.cfg.w_momentum
        score += v; bd["momentum"] = v
        supports    = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        near_sup    = supports[0]["price"]     if supports    else None
        near_res    = resistances[-1]["price"] if resistances else None
        v = 0
        if near_sup and near_res:
            v = self.cfg.w_sr_position if price > (near_sup+near_res)/2 else -self.cfg.w_sr_position
        score += v; bd["sr_position"] = v
        prox = 0; strength = 0; pt = self.cfg.sr_alert_pct * 2
        if near_sup and abs(price-near_sup)/price < pt:
            prox += self.cfg.w_sr_proximity
            m = [l for l in supports if abs(l["price"]-near_sup) < 1]
            if m and m[0]["touches"] >= 3: strength += self.cfg.w_level_strength
            if m and m[0].get("tf_count",1) >= 2: strength += self.cfg.w_fib_confluence
            if m and m[0].get("fib_confluence"): strength += self.cfg.w_fib_confluence
        if near_res and abs(price-near_res)/price < pt:
            prox -= self.cfg.w_sr_proximity
            m = [l for l in resistances if abs(l["price"]-near_res) < 1]
            if m and m[0]["touches"] >= 3: strength -= self.cfg.w_level_strength
            if m and m[0].get("tf_count",1) >= 2: strength -= self.cfg.w_fib_confluence
            if m and m[0].get("fib_confluence"): strength -= self.cfg.w_fib_confluence
        score += prox + strength; bd["sr_proximity"] = prox; bd["level_strength"] = strength
        v = self._detect_candle_pattern(candles, levels, price)
        score += v; bd["candle_pattern"] = v
        v = self._score_trend_context(candles, price)
        score += v; bd["trend_context"] = v
        v = self._score_session()
        score += v; bd["session"] = v
        v = self._detect_rsi_divergence(candles, rsi or [], price, levels)
        score += v; bd["rsi_divergence"] = v
        v = self._score_consecutive_candles(candles)
        score += v; bd["consecutive"] = v
        t = self.cfg.bias_threshold
        return {"bias": "BUY" if score >= t else ("SELL" if score <= -t else "HOLD"),
                "score": score, "breakdown": bd}

    # ──────────────────────────────────────────────────────────────────
    # SIGNAL COMPOSITION
    # ──────────────────────────────────────────────────────────────────

    def _apply_d1_veto(self, d1_bias: str, direction: str) -> bool:
        return d1_bias != "HOLD" and d1_bias != direction

    def _check_combined_score(self, tf_d1: dict, tf_h4: dict, tf_h1: dict, direction: str) -> tuple:
        total = sum(tf["score"] for tf in (tf_d1, tf_h4, tf_h1) if tf["bias"] == direction)
        return total >= self.cfg.combined_score_min, total

    def _calc_entry_zone(self, direction: str, price: float, levels: list, atr: float) -> tuple:
        width    = atr * self.cfg.entry_zone_atr_mult
        supports = sorted([l for l in levels if l["price"] < price], key=lambda x: x["price"], reverse=True)
        resists  = sorted([l for l in levels if l["price"] > price], key=lambda x: x["price"])
        if direction == "BUY" and supports:
            s = supports[0]["price"]
            zl, zh = s, s + width
            tag = "IDEAL ✅" if zl <= price <= zh else ("FAIR" if price < zl + width*2 else "EXTENDED ⚠️")
            return zl, zh, tag
        if direction == "SELL" and resists:
            r = resists[0]["price"]
            zl, zh = r - width, r
            tag = "IDEAL ✅" if zl <= price <= zh else ("FAIR" if price > zh - width*2 else "EXTENDED ⚠️")
            return zl, zh, tag
        return price, price, "FAIR"

    def _calc_dynamic_sl_tp(self, direction: str, entry: float, levels: list, atr: float) -> tuple:
        buf = atr * self.cfg.dynamic_sl_buffer_mult
        fb  = atr * self.cfg.atr_sl_mult
        if direction == "BUY":
            sups = sorted([l for l in levels if l["price"] < entry], key=lambda x: x["price"], reverse=True)
            sl   = round(sups[0]["price"] - buf, 2) if sups and abs(entry-sups[0]["price"]) < atr*2 else round(entry-fb, 2)
            dist = entry - sl
            return sl, round(entry + dist, 2), round(entry + dist * self.cfg.tp_rr, 2)
        ress = sorted([l for l in levels if l["price"] > entry], key=lambda x: x["price"])
        sl   = round(ress[0]["price"] + buf, 2) if ress and abs(ress[0]["price"]-entry) < atr*2 else round(entry+fb, 2)
        dist = sl - entry
        return sl, round(entry - dist, 2), round(entry - dist * self.cfg.tp_rr, 2)

    # ──────────────────────────────────────────────────────────────────
    # DAILY FLIP TRACKING
    # ──────────────────────────────────────────────────────────────────

    def _reset_daily_flips_if_needed(self):
        today = _utc_now().date()
        if self.last_flip_date != today:
            self.daily_flip_count = 0
            self.last_flip_date   = today

    def _flip_limit_reached(self) -> bool:
        return self.daily_flip_count >= self.cfg.daily_flip_limit

    # ──────────────────────────────────────────────────────────────────
    # FORMATTING
    # ──────────────────────────────────────────────────────────────────

    def _breakdown_lines(self, bd: dict) -> str:
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
            v = bd.get(key, 0)
            sign = "+" if v > 0 else ("−" if v < 0 else " ")
            bar  = "▓" * min(abs(v), 4) + "·" * max(0, 4-abs(v))
            lines.append(f"  {label}  {sign}{abs(v)}  {bar}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: GET SIGNAL
    # ══════════════════════════════════════════════════════════════════

    async def get_signal(self) -> str:
        try:
            d1, h4, h1, m15 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 100),
                self.fetcher.fetch_ohlcv("4h",   120),
                self.fetcher.fetch_ohlcv("1h",   100),
                self.fetcher.fetch_ohlcv("15min", 50),
            )
            price = await self.fetcher.fetch_current_price()
            if not price or not d1:
                return "❌ Failed to fetch Gold data."

            price = float(price)

            # Gates
            nb, nd = _is_news_blackout(self.cfg.news_blackout_minutes)
            if nb:
                return f"🚫 *News Blackout* — {nd}\nSignals resume {self.cfg.news_blackout_minutes}min after event.\n⏰ {_eat_now()}"
            if _is_session_open_window(self.cfg.session_open_blackout_minutes):
                return f"⏸ *Session Open Window* — paused {self.cfg.session_open_blackout_minutes}min\n⏰ {_eat_now()}"

            # Levels + Fib
            levels_d1  = self._find_sr_levels(d1, "D1")
            levels_h4  = self._find_sr_levels(h4, "H4") if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)
            fib_levels = self._calc_fibonacci_levels(d1)
            all_levels = self._enrich_levels_with_fib(all_levels, fib_levels)

            cur_atr, reg_atr = self._calc_atr(h4 if h4 else d1)
            regime_ok, regime_label = self._check_atr_regime(cur_atr, reg_atr)

            rsi_d1 = self._calc_rsi(d1)
            rsi_h4 = self._calc_rsi(h4 or [])
            rsi_h1 = self._calc_rsi(h1 or [])

            tf_d1 = self._analyze_tf(d1,       levels_d1,  price, rsi_d1)
            tf_h4 = self._analyze_tf(h4 or [], levels_h4,  price, rsi_h4)
            tf_h1 = self._analyze_tf(h1 or [], all_levels, price, rsi_h1)

            biases = [tf_d1["bias"], tf_h4["bias"], tf_h1["bias"]]
            bc, sc = biases.count("BUY"), biases.count("SELL")
            raw_dir = "BUY" if bc >= self.cfg.confluence_min_tfs else \
                      "SELL" if sc >= self.cfg.confluence_min_tfs else "HOLD"

            suppression = None
            if not regime_ok:
                suppression = f"ATR: {regime_label}"
            if raw_dir != "HOLD" and not suppression and self._apply_d1_veto(tf_d1["bias"], raw_dir):
                suppression = f"D1 veto — D1={tf_d1['bias']}"
            if raw_dir != "HOLD" and not suppression:
                passes, tot = self._check_combined_score(tf_d1, tf_h4, tf_h1, raw_dir)
                if not passes:
                    suppression = f"Score {tot} < min {self.cfg.combined_score_min}"

            direction  = raw_dir if not suppression else "HOLD"
            confluence = int((max(bc, sc) / 3) * 100) if direction != "HOLD" else 33

            entry = price
            if direction != "HOLD":
                sl, tp1, tp2 = self._calc_dynamic_sl_tp(direction, entry, all_levels, cur_atr)
                _, _, entry_tag = self._calc_entry_zone(direction, entry, all_levels, cur_atr)
            else:
                d = cur_atr * self.cfg.atr_sl_mult
                sl, tp1, tp2 = round(entry-d,2), round(entry+d,2), round(entry+d,2)
                entry_tag = "N/A"

            # M15 analysis
            velocity, vel_label = self._calc_m15_velocity(m15 or [])
            predictions = self._predict_approaches(price, velocity, all_levels)
            compression = self._detect_compression(h4 or [], all_levels, price)

            # M15 entry at nearest level
            near = sorted(all_levels, key=lambda x: abs(x["price"] - price))
            m15_entry = {"signal": "NONE", "pattern_type": "NONE", "reason": "—"}
            if near and m15:
                m15_entry = self._analyze_m15_at_level(m15, near[0]["price"], price)

            # Level display
            disp = sorted(all_levels, key=lambda x: abs(x["price"] - price))[:8]
            disp = sorted(disp, key=lambda x: x["price"], reverse=True)
            level_lines = []
            for l in disp:
                arrow  = "🔴 R" if l["price"] > price else "🟢 S"
                heat   = " 🔥" * min(l["touches"]-1, 3) if l["touches"] > 1 else ""
                fib    = " ◆" if l.get("fib_confluence") else ""
                mtf    = " 🔗" if l.get("tf_count",1) >= 2 else ""
                mark   = " ◀" if abs(l["price"]-price)/price < 0.001 else ""
                level_lines.append(f"  {arrow} `{l['price']:.2f}`{heat}{fib}{mtf}{mark}")

            dir_emoji  = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "🟡")
            conf_bar   = "█" * (confluence // 10) + "░" * (10 - confluence // 10)
            cur_rsi_h4 = f"{rsi_h4[-1]:.1f}" if rsi_h4 else "—"

            m15_emoji = {"BUY": "🟢", "SELL": "🔴"}.get(m15_entry["signal"], "⬜")
            m15_conf  = m15_entry.get("confidence", "")
            m15_conf_badge = f" [{m15_conf}]" if m15_conf else ""

            pred_lines = ""
            if predictions:
                p = predictions[0]
                pred_lines = (
                    f"\n🎯 *Approaching:* `{p['level']['price']:.2f}` "
                    f"in ~{p['eta_minutes']}min ({p['eta_candles']} M15 candles)\n"
                )

            comp_line = ""
            if compression.get("detected"):
                dir_c = "🟢" if compression["direction"] == "BULLISH" else "🔴"
                comp_line = f"\n🌀 *Compression:* {dir_c} {compression['description']}\n"

            msg = (
                f"🥇 *GOLD (XAU/USD)*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{dir_emoji} *H4 Signal: {direction}*\n"
                f"📊 Confluence: `{confluence}%`  `{conf_bar}`\n"
                f"🕐 Session: `{_current_session()}`  |  RSI H4: `{cur_rsi_h4}`\n"
                f"📈 ATR Regime: `{regime_label}`\n"
            )
            if suppression:
                msg += f"⚠️ _Filtered: {suppression}_\n"

            msg += (
                f"\n💹 *Price:*     `{entry:.2f}`\n"
                f"📍 *Entry Zone:* `{entry_tag}`\n"
            )
            if direction != "HOLD":
                msg += (
                    f"🛑 *SL:*        `{sl:.2f}`  (dist `{abs(entry-sl):.2f}`)\n"
                    f"🎯 *TP1 (1:1):* `{tp1:.2f}`\n"
                    f"✅ *TP2 ({self.cfg.tp_rr:.1f}R):*`{tp2:.2f}`\n"
                )

            msg += (
                f"\n⚡ *M15 Momentum*\n"
                f"  Velocity:  `{velocity:+.1f} pts/candle`  {vel_label}\n"
                f"  Pattern:   {m15_emoji} `{m15_entry['pattern_type']}`{m15_conf_badge}\n"
                f"  Detail:    _{m15_entry['reason']}_\n"
                + pred_lines + comp_line +
                f"\n🕐 *Timeframe Bias*\n"
                f"  D1 → {tf_d1['bias']} ({tf_d1['score']:+d})  "
                f"H4 → {tf_h4['bias']} ({tf_h4['score']:+d})  "
                f"H1 → {tf_h1['bias']} ({tf_h1['score']:+d})\n\n"
                f"🔬 *H4 Score Breakdown*\n"
                + self._breakdown_lines(tf_h4["breakdown"]) +
                f"\n\n🏗️ *Key S/R Levels*\n"
                + "\n".join(level_lines) +
                f"\n🔗D1+H4  ◆Fib  🔥multi-touch\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {_eat_now()}"
            )
            return msg

        except Exception as e:
            logger.error(f"get_signal error: {e}", exc_info=True)
            return f"❌ Signal error: {str(e)}"

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: GET LEVELS
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
                heat = " 🔥" * min(l["touches"]-1, 3) if l["touches"] > 1 else ""
                fib  = " ◆Fib" if l.get("fib_confluence") else ""
                mtf  = " 🔗D1" if l.get("tf_count",1) >= 2 else ""
                return f"  `{l['price']:.2f}`  ({abs(l['price']-price):.1f}pt){heat}{fib}{mtf}"

            fib_str = "  " + "  |  ".join(f"`{f:.2f}`" for f in fib_levels) if fib_levels else "  —"

            return (
                f"🥇 *Gold S/R Levels*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💹 `{price:.2f}`  |  ATR `{cur_atr:.2f}`\n\n"
                f"🔴 *Resistance*\n" + "\n".join(fmt(l) for l in above) + f"\n\n"
                f"🟢 *Support*\n"    + "\n".join(fmt(l) for l in below) + f"\n\n"
                f"◆ *Fibonacci*\n{fib_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥multi-touch  🔗D1+H4  ◆Fib  pt=distance\n"
                f"⏰ {_eat_now()}"
            )

        except Exception as e:
            logger.error(f"get_levels error: {e}", exc_info=True)
            return f"❌ Levels error: {str(e)}"

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: CHECK ALERTS — Three-Phase System
    # ══════════════════════════════════════════════════════════════════

    async def check_alerts(self) -> list:
        alerts = []
        try:
            nb, nd = _is_news_blackout(self.cfg.news_blackout_minutes)
            if nb:
                logger.info(f"Alerts suppressed — news blackout: {nd}")
                return alerts
            if _is_session_open_window(self.cfg.session_open_blackout_minutes):
                logger.info("Alerts suppressed — session open window")
                return alerts

            self._reset_daily_flips_if_needed()

            # Fetch all timeframes including M15
            d1, h4, m15 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day",  80),
                self.fetcher.fetch_ohlcv("4h",    100),
                self.fetcher.fetch_ohlcv("15min", 60),
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

            cur_atr, reg_atr = self._calc_atr(h4 if h4 else d1)
            regime_ok, _     = self._check_atr_regime(cur_atr, reg_atr)

            rsi_d1 = self._calc_rsi(d1)
            rsi_h4 = self._calc_rsi(h4 or [])

            tf_d1 = self._analyze_tf(d1,       levels_d1, price, rsi_d1)
            tf_h4 = self._analyze_tf(h4 or [], levels_h4, price, rsi_h4)

            velocity, vel_label = self._calc_m15_velocity(m15 or [])
            session = _current_session()

            # ── Phase 0: PREDICTION — approaching level ───────────────
            if regime_ok:
                predictions = self._predict_approaches(price, velocity, all_levels)
                for pred in predictions[:2]:   # max 2 approach alerts at a time
                    level     = pred["level"]
                    level_key = f"approach:{level['price']:.0f}"
                    if level_key not in self.approach_alerted:
                        is_res    = level["price"] > price
                        zone_type = "RESISTANCE 🔴" if is_res else "SUPPORT 🟢"
                        heat      = " 🔥" * min(level["touches"]-1, 3) if level["touches"] > 1 else ""
                        fib_tag   = "  ◆ Fib level" if level.get("fib_confluence") else ""
                        mtf_tag   = "  🔗 D1+H4"    if level.get("tf_count",1) >= 2 else ""
                        bias_note = (
                            "Expect rejection or breakout at this level." if is_res
                            else "Expect bounce or breakdown at this level."
                        )
                        alerts.append(
                            f"👀 *GOLD — Level Approaching*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{zone_type}{heat}: `{level['price']:.2f}`\n"
                            f"💹 Price now: `{price:.2f}`\n"
                            f"📏 Distance:  `{pred['distance_pts']:.1f}pt` ({pred['distance_pct']:.2f}%)\n"
                            f"⏱ ETA:       ~`{pred['eta_minutes']}min` ({pred['eta_candles']} M15 candles)\n"
                            f"🚀 Velocity: `{velocity:+.1f} pts/candle`  {vel_label}\n"
                            f"{fib_tag}{mtf_tag}\n"
                            f"💡 {bias_note}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🕐 Session: `{session}`\n"
                            f"⏰ {_eat_now()}\n"
                            f"_Watch for M15 confirmation at the level_"
                        )
                        self.approach_alerted.add(level_key)

            # ── Phase 1: SIGNAL FLIP (H4-structural) ──────────────────
            biases = [tf_d1["bias"], tf_h4["bias"]]
            cur_sig = ("BUY"  if biases.count("BUY")  >= 2 else
                       "SELL" if biases.count("SELL") >= 2 else "HOLD")

            if (self.last_signal and self.last_signal != cur_sig
                    and cur_sig != "HOLD" and regime_ok
                    and not self._apply_d1_veto(tf_d1["bias"], cur_sig)
                    and not self._flip_limit_reached()):
                passes, total = self._check_combined_score(
                    tf_d1, tf_h4, {"bias":"HOLD","score":0,"breakdown":{}}, cur_sig)
                if passes:
                    sl, tp1, tp2 = self._calc_dynamic_sl_tp(cur_sig, price, all_levels, cur_atr)
                    _, _, entry_tag = self._calc_entry_zone(cur_sig, price, all_levels, cur_atr)
                    emoji  = "🟢📈" if cur_sig == "BUY" else "🔴📉"
                    fleft  = self.cfg.daily_flip_limit - self.daily_flip_count - 1
                    fwarn  = f"\n⚠️ _{fleft} flip alert{'s' if fleft!=1 else ''} left today_" if fleft <= 1 else ""
                    self.daily_flip_count += 1
                    alerts.append(
                        f"{emoji} *H4 Signal Flip!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"*{self.last_signal}  →  {cur_sig}*\n\n"
                        f"💹 Price:       `{price:.2f}`\n"
                        f"📍 Entry Zone:  `{entry_tag}`\n"
                        f"🛑 SL:          `{sl:.2f}`\n"
                        f"🎯 TP1 (1:1):   `{tp1:.2f}`\n"
                        f"✅ TP2 ({self.cfg.tp_rr:.1f}R):  `{tp2:.2f}`\n"
                        f"📊 Score: D1 {tf_d1['score']:+d}  H4 {tf_h4['score']:+d}  Σ{total:+d}\n"
                        f"🕐 Session: `{session}`{fwarn}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏰ {_eat_now()}\n"
                        f"_/signal for full breakdown_"
                    )

            self.last_signal = cur_sig

            # ── Phase 2: M15 ENTRY — fires immediately at move start ──
            triggered_now  = set()
            entry_now      = set()
            approach_clear = set()

            for level in all_levels:
                proximity = abs(price - level["price"]) / price
                level_key = f"{level['price']:.0f}"
                approach_key = f"approach:{level['price']:.0f}"

                # Clear approach alert once price arrives at the level
                if proximity <= self.cfg.sr_alert_pct * 1.5:
                    approach_clear.add(approach_key)

                # ── At-level: M15 entry analysis ──
                if proximity <= self.cfg.sr_alert_pct and m15:
                    is_res    = level["price"] > price
                    triggered_now.add(level_key)
                    m15_result = self._analyze_m15_at_level(m15, level["price"], price)

                    entry_key = f"entry:{level_key}:{m15_result['pattern_type']}"

                    if m15_result["signal"] != "NONE" and entry_key not in self.entry_alerted:
                        direction = m15_result["signal"]

                        # H4 alignment check
                        if self.cfg.m15_require_h4_alignment and tf_h4["bias"] not in (direction, "HOLD"):
                            logger.info(
                                f"M15 entry at {level['price']:.2f} suppressed — "
                                f"H4={tf_h4['bias']} misaligns with M15={direction}"
                            )
                        else:
                            sl, tp1, tp2 = self._calc_dynamic_sl_tp(direction, price, all_levels, cur_atr)
                            sl_dist      = abs(price - sl)
                            heat         = " 🔥" * min(level["touches"]-1, 3) if level["touches"] > 1 else ""
                            fib_tag      = "◆Fib" if level.get("fib_confluence") else ""
                            mtf_tag      = "🔗D1+H4" if level.get("tf_count",1) >= 2 else ""
                            badges       = " ".join(filter(None, [fib_tag, mtf_tag]))
                            pattern_type = m15_result["pattern_type"]
                            confidence   = m15_result.get("confidence", "MEDIUM")
                            conf_emoji   = "🔥" if confidence == "HIGH" else "✔️"
                            dir_emoji_e  = "🟢" if direction == "BUY" else "🔴"
                            zone_type    = "RESISTANCE" if is_res else "SUPPORT"
                            _, _, entry_tag = self._calc_entry_zone(direction, price, all_levels, cur_atr)

                            alerts.append(
                                f"{dir_emoji_e} *GOLD ENTRY — {direction} NOW*\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"{conf_emoji} `{pattern_type}` at {zone_type}{heat}\n"
                                f"Level: `{level['price']:.2f}`  {badges}\n\n"
                                f"💹 Price:       `{price:.2f}`\n"
                                f"📍 Entry Zone:  `{entry_tag}`\n"
                                f"🛑 SL:          `{sl:.2f}`  (dist `{sl_dist:.2f}`)\n"
                                f"🎯 TP1 (1:1):   `{tp1:.2f}`\n"
                                f"✅ TP2 ({self.cfg.tp_rr:.1f}R):  `{tp2:.2f}`\n"
                                f"📊 H4: {tf_h4['bias']} ({tf_h4['score']:+d})  "
                                f"D1: {tf_d1['bias']} ({tf_d1['score']:+d})\n"
                                f"🕐 Session: `{session}`\n"
                                f"_{m15_result['reason']}_\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"⏰ {_eat_now()}"
                            )
                            entry_now.add(entry_key)
                            self.entry_alerted.add(entry_key)

            # State cleanup
            self.approach_alerted -= approach_clear
            stale_approach = {
                k for k in self.approach_alerted
                if abs(float(k.split(":")[1]) - price) / price >
                   self.cfg.sr_alert_pct * self.cfg.sr_alert_cooldown_mult * 2
            }
            self.approach_alerted -= stale_approach

            stale_entry = {
                k for k in self.entry_alerted
                if abs(float(k.split(":")[1]) - price) / price >
                   self.cfg.sr_alert_pct * self.cfg.sr_alert_cooldown_mult
            }
            self.entry_alerted -= stale_entry

            stale_at = {
                k for k in self.alerted_levels
                if k not in triggered_now
                and abs(float(k) - price)/price >
                   self.cfg.sr_alert_pct * self.cfg.sr_alert_cooldown_mult
            }
            self.alerted_levels -= stale_at

        except Exception as e:
            logger.error(f"check_alerts error: {e}", exc_info=True)

        return alerts


    # ══════════════════════════════════════════════════════════════════
    # PUBLIC: GET CHART
    # Returns a BytesIO PNG — pass directly to bot.send_photo()
    # ══════════════════════════════════════════════════════════════════

    async def get_chart(self, tf: str = "4h") -> tuple:
        """
        Fetch candles, compute levels, generate and return chart.

        Args:
            tf: "4h" (default) or "1h"

        Returns:
            (buf: BytesIO, caption: str)
            buf    — PNG chart ready for bot.send_photo()
            caption — short text summary for the Telegram caption
        """
        from chart_generator import generate_chart

        tf_label   = "H4" if tf == "4h" else "H1"
        outputsize = 80  if tf == "4h" else 100

        try:
            d1, tf_candles = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 100),
                self.fetcher.fetch_ohlcv(tf, outputsize),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1 or not tf_candles:
                return None, "❌ Failed to fetch chart data."

            price = float(price)

            # Levels
            levels_d1  = self._find_sr_levels(d1, "D1")
            levels_tf  = self._find_sr_levels(tf_candles, tf_label)
            all_levels = self._cluster_levels(levels_d1 + levels_tf)
            fib_levels = self._calc_fibonacci_levels(d1)
            all_levels = self._enrich_levels_with_fib(all_levels, fib_levels)

            # Current signal direction for arrow
            rsi_d1 = self._calc_rsi(d1)
            rsi_tf = self._calc_rsi(tf_candles)
            tf_d1  = self._analyze_tf(d1, levels_d1, price, rsi_d1)
            tf_main = self._analyze_tf(tf_candles, levels_tf, price, rsi_tf)

            biases = [tf_d1["bias"], tf_main["bias"]]
            if biases.count("BUY") >= 2:
                direction = "BUY"
            elif biases.count("SELL") >= 2:
                direction = "SELL"
            else:
                direction = "HOLD"

            scores = {
                "d1": f"{tf_d1['bias']} ({tf_d1['score']:+d})",
                "h4": f"{tf_main['bias']} ({tf_main['score']:+d})",
                "h1": "—",
            }

            # Generate chart
            buf = generate_chart(
                candles          = tf_candles,
                levels           = all_levels,
                fib_levels       = fib_levels,
                price            = price,
                signal_direction = direction,
                tf_label         = tf_label,
                scores           = scores,
            )

            # Caption
            n_res = len([l for l in all_levels if l["price"] > price])
            n_sup = len([l for l in all_levels if l["price"] <= price])
            dir_emoji = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "🟡")
            caption = (
                f"🥇 XAU/USD {tf_label}  |  {dir_emoji} {direction}\n"
                f"💹 Price: {price:.2f}\n"
                f"🔴 {n_res} resistance  🟢 {n_sup} support  ◆ {len(fib_levels)} fib\n"
                f"⏰ {_eat_now()}"
            )
            return buf, caption

        except Exception as e:
            logger.error(f"get_chart error: {e}", exc_info=True)
            return None, f"❌ Chart error: {str(e)}"
