import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from data_fetcher import DataFetcher

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
SR_SWING_N      = 5       # candles each side for swing detection
SR_CLUSTER_PCT  = 0.005   # 0.5% cluster merge threshold
SR_ALERT_PCT    = 0.003   # 0.3% proximity alert
ATR_PERIOD      = 14
ATR_AVG_PERIOD  = 20      # lookback for ATR average (volatility gate)
ATR_SL_MULT     = 1.5
TP_RR           = 2.0

# A timeframe score must reach this magnitude to become a BUY/SELL bias
SCORE_SIGNAL_THRESH = 4
# ─────────────────────────────────────────────────────────────────────────


def _utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def _eat_now() -> str:
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y-%m-%d %H:%M EAT")


def _session_label() -> str:
    h = _utc_hour()
    if 6  <= h < 12: return "🇬🇧 London"
    if 12 <= h < 20: return "🇺🇸 New York"
    return "🌏 Asian"


def _session_score() -> int:
    """London / New York (active) → +1  |  Asian (choppy) → -1."""
    h = _utc_hour()
    if 6 <= h < 20: return 1
    return -1


class SignalEngine:
    def __init__(self, td_key: str, fh_key: str):
        self.fetcher = DataFetcher(td_key, fh_key)
        self.last_signal: Optional[str] = None
        self.alerted_levels: set = set()

    # ────────────────────────────────────────────────────────────────────
    # S/R Detection
    # ────────────────────────────────────────────────────────────────────

    def _find_sr_levels(self, candles: list, n: int = SR_SWING_N) -> list:
        """Swing high/low detection → cluster → return sorted levels."""
        highs  = [c["high"] for c in candles]
        lows   = [c["low"]  for c in candles]
        levels = []

        for i in range(n, len(candles) - n):
            if all(highs[i] >= highs[i - j] for j in range(1, n + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, n + 1)):
                levels.append({"price": highs[i], "type": "resistance", "touches": 1})
            if all(lows[i]  <= lows[i  - j] for j in range(1, n + 1)) and \
               all(lows[i]  <= lows[i  + j] for j in range(1, n + 1)):
                levels.append({"price": lows[i],  "type": "support",    "touches": 1})

        return self._cluster_levels(levels)

    def _cluster_levels(self, levels: list, threshold: float = SR_CLUSTER_PCT) -> list:
        """Merge S/R levels within threshold % of each other."""
        if not levels:
            return []
        sorted_lvls = sorted(levels, key=lambda x: x["price"])
        clustered   = [sorted_lvls[0].copy()]

        for lvl in sorted_lvls[1:]:
            last = clustered[-1]
            if abs(lvl["price"] - last["price"]) / last["price"] < threshold:
                last["price"]    = round((last["price"] + lvl["price"]) / 2, 2)
                last["touches"] += lvl["touches"]
            else:
                clustered.append(lvl.copy())

        return sorted(clustered, key=lambda x: x["price"], reverse=True)

    # ────────────────────────────────────────────────────────────────────
    # ATR
    # ────────────────────────────────────────────────────────────────────

    def _calc_atr(self, candles: list, period: int = ATR_PERIOD) -> float:
        trs = []
        for i in range(1, len(candles)):
            h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if not trs:
            return 15.0
        recent = trs[-period:] if len(trs) >= period else trs
        return sum(recent) / len(recent)

    def _atr_volatility_score(self, candles: list) -> int:
        """
        Compare current ATR to its own rolling average.
        Market breathing  → +1  |  Dead / compressed → -1  |  Normal → 0
        """
        trs = []
        for i in range(1, len(candles)):
            h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

        needed = ATR_PERIOD + ATR_AVG_PERIOD
        if len(trs) < needed:
            return 0

        current_atr = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
        avg_atr     = sum(trs[-(ATR_PERIOD + ATR_AVG_PERIOD): -ATR_PERIOD]) / ATR_AVG_PERIOD

        if avg_atr == 0:
            return 0
        ratio = current_atr / avg_atr
        if ratio > 0.8: return  1   # active
        if ratio < 0.5: return -1   # compressed
        return 0

    # ────────────────────────────────────────────────────────────────────
    # Liquidity Grab  ← Gold-specific, highest-probability setup
    # ────────────────────────────────────────────────────────────────────

    def _liquidity_grab_score(self, candle: dict, levels: list, price: float) -> int:
        """
        Institutional stop-hunt pattern:
          Bullish grab  — wick pierced BELOW support,  closed ABOVE it → +3
          Bearish grab  — wick pierced ABOVE resistance, closed BELOW it → -3
        """
        for level in levels:
            lp = level["price"]
            if lp <= price and candle["low"] < lp and candle["close"] > lp:
                return 3   # bullish grab
            if lp >= price and candle["high"] > lp and candle["close"] < lp:
                return -3  # bearish grab
        return 0

    # ────────────────────────────────────────────────────────────────────
    # Displacement Candle  ← institutional footprint
    # ────────────────────────────────────────────────────────────────────

    def _displacement_score(self, candle: dict, atr: float) -> int:
        """
        Candle body > 1.5× ATR = large participant move.
        Bullish → +2  |  Bearish → -2
        """
        body = abs(candle["close"] - candle["open"])
        if body < 1.5 * atr:
            return 0
        return 2 if candle["close"] > candle["open"] else -2

    # ────────────────────────────────────────────────────────────────────
    # S/R Touch-Count Weighting
    # ────────────────────────────────────────────────────────────────────

    def _touch_weight_score(self, levels: list, price: float, raw_score: int) -> int:
        """
        Levels already track touches — now score them.
        Looks at the nearest level in the direction implied by raw_score.

        touches ≥ 4  → ±2  |  touches ≥ 2  → ±1  |  single touch → 0
        """
        if raw_score == 0:
            return 0
        direction = 1 if raw_score > 0 else -1

        candidates = (
            [l for l in levels if l["price"] <  price]  # support  for BUY
            if direction == 1 else
            [l for l in levels if l["price"] >  price]  # resistance for SELL
        )
        if not candidates:
            return 0

        nearest = min(candidates, key=lambda x: abs(x["price"] - price))
        t = nearest["touches"]
        if t >= 4: return 2 * direction
        if t >= 2: return 1 * direction
        return 0

    # ────────────────────────────────────────────────────────────────────
    # Per-Timeframe Scorer
    # ────────────────────────────────────────────────────────────────────

    def _analyze_tf(
        self,
        candles:  list,
        levels:   list,
        price:    float,
        atr:      float,
        htf_bias: Optional[str] = None,
    ) -> dict:
        """
        Score one timeframe. Returns {"bias": BUY/SELL/HOLD, "score": int}.

        htf_bias (cascade gate):
          If the higher timeframe has a clear BUY, any negative score here
          is zeroed out — no counter-trend signals can fire downstream.
        """
        if not candles or len(candles) < 6:
            return {"bias": "HOLD", "score": 0}

        last  = candles[-1]
        prev  = candles[-2]
        score = 0

        # 1. Candle body direction ─────────────────────────────────────
        score += 1 if last["close"] > last["open"] else -1

        # 2. Price momentum ───────────────────────────────────────────
        score += 1 if last["close"] > prev["close"] else -1

        # 3. Position inside S/R range ────────────────────────────────
        supports    = [l for l in levels if l["price"] <  price]
        resistances = [l for l in levels if l["price"] >  price]
        nearest_sup = supports[0]["price"]     if supports    else None
        nearest_res = resistances[-1]["price"] if resistances else None

        if nearest_sup and nearest_res:
            mid    = (nearest_sup + nearest_res) / 2
            score += 1 if price > mid else -1

        # 4. S/R proximity ────────────────────────────────────────────
        if nearest_sup and abs(price - nearest_sup) / price < SR_ALERT_PCT * 2:
            score += 2
        if nearest_res and abs(price - nearest_res) / price < SR_ALERT_PCT * 2:
            score -= 2

        # 5. Displacement candle ──────────────────────────────────────
        score += self._displacement_score(last, atr)

        # 6. Liquidity grab ───────────────────────────────────────────
        score += self._liquidity_grab_score(last, levels, price)

        # 7. S/R touch-count weight ───────────────────────────────────
        score += self._touch_weight_score(levels, price, score)

        # 8. Session filter ───────────────────────────────────────────
        score += _session_score()

        # 9. ATR volatility gate ──────────────────────────────────────
        score += self._atr_volatility_score(candles)

        # ── HTF Cascade Gate ──────────────────────────────────────────
        # Hard-block counter-trend scores when a higher TF has clear bias.
        if htf_bias == "BUY"  and score < 0: score = 0
        if htf_bias == "SELL" and score > 0: score = 0

        if score >=  SCORE_SIGNAL_THRESH:
            return {"bias": "BUY",  "score": score}
        elif score <= -SCORE_SIGNAL_THRESH:
            return {"bias": "SELL", "score": score}
        return {"bias": "HOLD", "score": score}

    # ────────────────────────────────────────────────────────────────────
    # Signal Generation  (D1 → H4 → H1 → M15 cascade)
    # ────────────────────────────────────────────────────────────────────

    async def get_signal(self) -> str:
        try:
            d1, h4, h1, m15 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day",   80),
                self.fetcher.fetch_ohlcv("4h",    100),
                self.fetcher.fetch_ohlcv("1h",    100),
                self.fetcher.fetch_ohlcv("15min", 100),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1:
                return "❌ Failed to fetch Gold data. Check API keys or try again."

            price = float(price)

            levels_d1  = self._find_sr_levels(d1)
            levels_h4  = self._find_sr_levels(h4)  if h4  else []
            levels_h1  = self._find_sr_levels(h1)  if h1  else []
            levels_m15 = self._find_sr_levels(m15) if m15 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)

            atr = self._calc_atr(h4 if h4 else d1)

            # Cascade: each TF gated by the one above
            tf_d1  = self._analyze_tf(d1,        levels_d1,  price, atr)
            tf_h4  = self._analyze_tf(h4  or [], levels_h4,  price, atr, htf_bias=tf_d1["bias"])
            tf_h1  = self._analyze_tf(h1  or [], levels_h1,  price, atr, htf_bias=tf_h4["bias"])
            tf_m15 = self._analyze_tf(m15 or [], levels_m15, price, atr, htf_bias=tf_h1["bias"])

            # D1 is gate only — H4 / H1 / M15 vote
            biases     = [tf_h4["bias"], tf_h1["bias"], tf_m15["bias"]]
            buy_count  = biases.count("BUY")
            sell_count = biases.count("SELL")

            if buy_count >= 2:
                direction  = "BUY"
                confluence = int((buy_count / 3) * 100)
            elif sell_count >= 2:
                direction  = "SELL"
                confluence = int((sell_count / 3) * 100)
            else:
                direction  = "HOLD"
                confluence = 33

            # Entry / SL / TP
            entry   = price
            sl_dist = atr * ATR_SL_MULT
            if direction == "BUY":
                sl = round(entry - sl_dist, 2)
                tp = round(entry + sl_dist * TP_RR, 2)
            elif direction == "SELL":
                sl = round(entry + sl_dist, 2)
                tp = round(entry - sl_dist * TP_RR, 2)
            else:
                sl = round(entry - sl_dist, 2)
                tp = round(entry + sl_dist, 2)

            # Key levels display
            near = sorted(all_levels, key=lambda x: abs(x["price"] - price))[:8]
            near_sorted = sorted(near, key=lambda x: x["price"], reverse=True)
            level_lines = []
            for l in near_sorted:
                arrow  = "🔴 R" if l["price"] > price else "🟢 S"
                heat   = " 🔥" * min(l["touches"] - 1, 3) if l["touches"] > 1 else ""
                marker = " ◀ PRICE" if abs(l["price"] - price) / price < 0.002 else ""
                level_lines.append(f"  {arrow} `{l['price']:.2f}`{heat}{marker}")

            dir_emoji = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "🟡")
            conf_bar  = "█" * (confluence // 10) + "░" * (10 - confluence // 10)
            scores    = (
                f"D1({tf_d1['score']:+d}) "
                f"H4({tf_h4['score']:+d}) "
                f"H1({tf_h1['score']:+d}) "
                f"M15({tf_m15['score']:+d})"
            )

            return (
                f"🥇 *GOLD (XAU/USD)*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{dir_emoji} *Signal: {direction}*\n"
                f"📊 Confluence: `{confluence}%`  `{conf_bar}`\n\n"
                f"💹 *Current Price:* `{price:.2f}`\n"
                f"🎯 *Entry:*        `{entry:.2f}`\n"
                f"🛑 *Stop Loss:*    `{sl:.2f}`\n"
                f"✅ *Take Profit:*  `{tp:.2f}`\n"
                f"📐 *R:R Ratio:*    1:{TP_RR:.1f}\n\n"
                f"🕐 *Timeframe Breakdown*\n"
                f"  D1  → {tf_d1['bias']}  |  H4  → {tf_h4['bias']}\n"
                f"  H1  → {tf_h1['bias']}  |  M15 → {tf_m15['bias']}\n"
                f"  `{scores}`\n\n"
                f"🏗️ *Key S/R Levels*\n"
                + "\n".join(level_lines)
                + f"\n━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Session: {_session_label()}\n"
                f"⏰ {_eat_now()}"
            )

        except Exception as e:
            logger.error(f"get_signal error: {e}", exc_info=True)
            return f"❌ Signal error: {str(e)}"

    # ────────────────────────────────────────────────────────────────────
    # Levels Display
    # ────────────────────────────────────────────────────────────────────

    async def get_levels(self) -> str:
        try:
            d1, h4 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 80),
                self.fetcher.fetch_ohlcv("4h",  100),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1:
                return "❌ Failed to fetch data."

            price      = float(price)
            levels_d1  = self._find_sr_levels(d1)
            levels_h4  = self._find_sr_levels(h4) if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)

            above = sorted([l for l in all_levels if l["price"] >  price], key=lambda x: x["price"])[:6]
            below = sorted([l for l in all_levels if l["price"] <= price], key=lambda x: x["price"], reverse=True)[:6]

            def fmt(l):
                heat = " 🔥" * min(l["touches"] - 1, 3) if l["touches"] > 1 else ""
                return f"  `{l['price']:.2f}`{heat}"

            res_text = "\n".join([fmt(l) for l in above]) or "  —"
            sup_text = "\n".join([fmt(l) for l in below]) or "  —"

            return (
                f"🥇 *Gold Key S/R Levels*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💹 Price: `{price:.2f}`\n\n"
                f"🔴 *Resistance Above*\n{res_text}\n\n"
                f"🟢 *Support Below*\n{sup_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 = Tested multiple times (stronger)\n"
                f"⏰ {_eat_now()}"
            )

        except Exception as e:
            logger.error(f"get_levels error: {e}", exc_info=True)
            return f"❌ Levels error: {str(e)}"

    # ────────────────────────────────────────────────────────────────────
    # Alert Engine
    # ────────────────────────────────────────────────────────────────────

    async def check_alerts(self) -> list[str]:
        """
        Triggers:
          1. Signal direction flip (BUY ↔ SELL) using full 4-TF cascade
          2. Price within SR_ALERT_PCT of a key S/R level
        """
        alerts = []
        try:
            d1, h4, h1, m15 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day",   60),
                self.fetcher.fetch_ohlcv("4h",     80),
                self.fetcher.fetch_ohlcv("1h",     80),
                self.fetcher.fetch_ohlcv("15min",  80),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1:
                return alerts

            price      = float(price)
            levels_d1  = self._find_sr_levels(d1)
            levels_h4  = self._find_sr_levels(h4)  if h4  else []
            levels_h1  = self._find_sr_levels(h1)  if h1  else []
            levels_m15 = self._find_sr_levels(m15) if m15 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)
            atr        = self._calc_atr(h4 if h4 else d1)

            # Full cascade
            tf_d1  = self._analyze_tf(d1,        levels_d1,  price, atr)
            tf_h4  = self._analyze_tf(h4  or [], levels_h4,  price, atr, htf_bias=tf_d1["bias"])
            tf_h1  = self._analyze_tf(h1  or [], levels_h1,  price, atr, htf_bias=tf_h4["bias"])
            tf_m15 = self._analyze_tf(m15 or [], levels_m15, price, atr, htf_bias=tf_h1["bias"])

            biases     = [tf_h4["bias"], tf_h1["bias"], tf_m15["bias"]]
            buy_count  = biases.count("BUY")
            sell_count = biases.count("SELL")

            if buy_count >= 2:
                current_signal = "BUY"
            elif sell_count >= 2:
                current_signal = "SELL"
            else:
                current_signal = "HOLD"

            # ── 1. Signal flip ────────────────────────────────────────
            if (self.last_signal
                    and self.last_signal != current_signal
                    and current_signal   != "HOLD"):

                emoji   = "🟢📈" if current_signal == "BUY" else "🔴📉"
                sl_dist = atr * ATR_SL_MULT
                if current_signal == "BUY":
                    sl = round(price - sl_dist, 2)
                    tp = round(price + sl_dist * TP_RR, 2)
                else:
                    sl = round(price + sl_dist, 2)
                    tp = round(price - sl_dist * TP_RR, 2)

                alerts.append(
                    f"{emoji} *GOLD Signal Flip!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{self.last_signal}  →  *{current_signal}*\n\n"
                    f"💹 Price:  `{price:.2f}`\n"
                    f"🎯 Entry: `{price:.2f}`\n"
                    f"🛑 SL:    `{sl:.2f}`\n"
                    f"✅ TP:    `{tp:.2f}`\n"
                    f"📍 Session: {_session_label()}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_eat_now()}\n"
                    f"_/signal for full analysis_"
                )

            self.last_signal = current_signal

            # ── 2. S/R proximity alerts ───────────────────────────────
            triggered_now = set()
            for level in all_levels:
                proximity = abs(price - level["price"]) / price
                level_key = f"{level['price']:.0f}"

                if proximity <= SR_ALERT_PCT:
                    triggered_now.add(level_key)
                    if level_key not in self.alerted_levels:
                        zone_type = "🔴 RESISTANCE" if level["price"] > price else "🟢 SUPPORT"
                        heat      = " 🔥" * min(level["touches"] - 1, 3) if level["touches"] > 1 else ""
                        bias_hint = "Watch for rejection 🔽" if level["price"] > price else "Watch for bounce 🔼"

                        alerts.append(
                            f"⚡ *Gold at Key Level!*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{zone_type}{heat}: `{level['price']:.2f}`\n"
                            f"💹 Price: `{price:.2f}`\n"
                            f"📏 Distance: `{proximity * 100:.2f}%`\n"
                            f"💡 {bias_hint}\n"
                            f"📍 Session: {_session_label()}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_eat_now()}"
                        )
                        self.alerted_levels.add(level_key)

            # Only keep levels still in proximity range
            self.alerted_levels = triggered_now

        except Exception as e:
            logger.error(f"check_alerts error: {e}", exc_info=True)

        return alerts
