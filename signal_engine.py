import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from data_fetcher import DataFetcher

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────
SR_SWING_N = 5           # candles each side for swing high/low
SR_CLUSTER_PCT = 0.005   # 0.5% cluster threshold
SR_ALERT_PCT = 0.003     # 0.3% proximity to trigger S/R alert
ATR_PERIOD = 14
ATR_SL_MULT = 1.5        # SL = entry ± ATR × multiplier
TP_RR = 2.0              # Take Profit R:R ratio
# ────────────────────────────────────────────────────────────────────────


def _eat_now() -> str:
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y-%m-%d %H:%M EAT")


class SignalEngine:
    def __init__(self, td_key: str, fh_key: str):
        self.fetcher = DataFetcher(td_key, fh_key)
        self.last_signal: Optional[str] = None
        self.alerted_levels: set = set()  # level keys currently in alert range

    # ── S/R Detection ─────────────────────────────────────────────────

    def _find_sr_levels(self, candles: list, n: int = SR_SWING_N) -> list:
        """Identify swing high/low S/R levels."""
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        levels = []

        for i in range(n, len(candles) - n):
            # Swing high → resistance
            if all(highs[i] >= highs[i - j] for j in range(1, n + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, n + 1)):
                levels.append({"price": highs[i], "type": "resistance", "touches": 1})

            # Swing low → support
            if all(lows[i] <= lows[i - j] for j in range(1, n + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, n + 1)):
                levels.append({"price": lows[i], "type": "support", "touches": 1})

        return self._cluster_levels(levels)

    def _cluster_levels(self, levels: list, threshold: float = SR_CLUSTER_PCT) -> list:
        """Merge nearby S/R levels within threshold %."""
        if not levels:
            return []
        sorted_levels = sorted(levels, key=lambda x: x["price"])
        clustered = [sorted_levels[0].copy()]

        for lvl in sorted_levels[1:]:
            last = clustered[-1]
            if abs(lvl["price"] - last["price"]) / last["price"] < threshold:
                # Average price; keep the more common type; count touches
                last["price"] = round((last["price"] + lvl["price"]) / 2, 2)
                last["touches"] += lvl["touches"]
                # If mixed, prefer the type with more touches
            else:
                clustered.append(lvl.copy())

        return sorted(clustered, key=lambda x: x["price"], reverse=True)

    # ── ATR ────────────────────────────────────────────────────────────

    def _calc_atr(self, candles: list, period: int = ATR_PERIOD) -> float:
        trs = []
        for i in range(1, len(candles)):
            h = candles[i]["high"]
            l = candles[i]["low"]
            pc = candles[i - 1]["close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        if not trs:
            return 15.0
        recent = trs[-period:] if len(trs) >= period else trs
        return sum(recent) / len(recent)

    # ── Timeframe Analysis ─────────────────────────────────────────────

    def _analyze_tf(self, candles: list, levels: list, price: float) -> dict:
        """Score a single timeframe for bias."""
        if not candles or len(candles) < 6:
            return {"bias": "HOLD", "score": 0}

        last = candles[-1]
        prev = candles[-2]
        score = 0

        # Bullish/bearish candle body
        if last["close"] > last["open"]:
            score += 1
        else:
            score -= 1

        # Price momentum
        if last["close"] > prev["close"]:
            score += 1
        else:
            score -= 1

        # Position relative to S/R mid-range
        supports = [l for l in levels if l["price"] < price]
        resistances = [l for l in levels if l["price"] > price]
        nearest_sup = supports[0]["price"] if supports else None
        nearest_res = resistances[-1]["price"] if resistances else None

        if nearest_sup and nearest_res:
            mid = (nearest_sup + nearest_res) / 2
            score += 1 if price > mid else -1

        # Proximity to support (potential bounce)
        if nearest_sup and abs(price - nearest_sup) / price < SR_ALERT_PCT * 2:
            score += 2

        # Proximity to resistance (potential rejection)
        if nearest_res and abs(price - nearest_res) / price < SR_ALERT_PCT * 2:
            score -= 2

        if score >= 2:
            return {"bias": "BUY", "score": score}
        elif score <= -2:
            return {"bias": "SELL", "score": score}
        return {"bias": "HOLD", "score": score}

    # ── Signal Generation ──────────────────────────────────────────────

    async def get_signal(self) -> str:
        try:
            d1, h4, h1 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 80),
                self.fetcher.fetch_ohlcv("4h", 100),
                self.fetcher.fetch_ohlcv("1h", 100),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1:
                return "❌ Failed to fetch Gold data. Check API keys or try again."

            price = float(price)

            # S/R levels
            levels_d1 = self._find_sr_levels(d1)
            levels_h4 = self._find_sr_levels(h4) if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)

            # ATR (use H4 for trade-level sizing)
            atr = self._calc_atr(h4 if h4 else d1)

            # Multi-TF analysis
            tf_d1 = self._analyze_tf(d1, levels_d1, price)
            tf_h4 = self._analyze_tf(h4 or [], levels_h4, price)
            tf_h1 = self._analyze_tf(h1 or [], all_levels, price)

            biases = [tf_d1["bias"], tf_h4["bias"], tf_h1["bias"]]
            buy_count = biases.count("BUY")
            sell_count = biases.count("SELL")

            if buy_count >= 2:
                direction = "BUY"
                confluence = int((buy_count / 3) * 100)
            elif sell_count >= 2:
                direction = "SELL"
                confluence = int((sell_count / 3) * 100)
            else:
                direction = "HOLD"
                confluence = 33

            # Entry / SL / TP
            entry = price
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

            # Nearest levels display
            near = sorted(all_levels, key=lambda x: abs(x["price"] - price))[:8]
            near_sorted = sorted(near, key=lambda x: x["price"], reverse=True)
            level_lines = []
            for l in near_sorted:
                arrow = "🔴 R" if l["price"] > price else "🟢 S"
                heat = " 🔥" * min(l["touches"] - 1, 3) if l["touches"] > 1 else ""
                marker = " ◀ PRICE" if abs(l["price"] - price) / price < 0.002 else ""
                level_lines.append(f"  {arrow} `{l['price']:.2f}`{heat}{marker}")

            dir_emoji = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "🟡")
            conf_bar = "█" * (confluence // 10) + "░" * (10 - confluence // 10)

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
                f"  D1 → {tf_d1['bias']}  |  H4 → {tf_h4['bias']}  |  H1 → {tf_h1['bias']}\n\n"
                f"🏗️ *Key S/R Levels*\n"
                + "\n".join(level_lines) +
                f"\n━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {_eat_now()}"
            )

        except Exception as e:
            logger.error(f"get_signal error: {e}", exc_info=True)
            return f"❌ Signal error: {str(e)}"

    # ── Levels Display ─────────────────────────────────────────────────

    async def get_levels(self) -> str:
        try:
            d1, h4 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 80),
                self.fetcher.fetch_ohlcv("4h", 100),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1:
                return "❌ Failed to fetch data."

            price = float(price)
            levels_d1 = self._find_sr_levels(d1)
            levels_h4 = self._find_sr_levels(h4) if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)

            above = sorted([l for l in all_levels if l["price"] > price], key=lambda x: x["price"])[:6]
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

    # ── Alert Engine ───────────────────────────────────────────────────

    async def check_alerts(self) -> list[str]:
        """
        Returns a list of alert messages to push to Telegram.
        Triggers:
          1. Signal flip (BUY↔SELL)
          2. Price within SR_ALERT_PCT of a key S/R level
        """
        alerts = []
        try:
            d1, h4 = await asyncio.gather(
                self.fetcher.fetch_ohlcv("1day", 60),
                self.fetcher.fetch_ohlcv("4h", 80),
            )
            price = await self.fetcher.fetch_current_price()

            if not price or not d1:
                return alerts

            price = float(price)
            levels_d1 = self._find_sr_levels(d1)
            levels_h4 = self._find_sr_levels(h4) if h4 else []
            all_levels = self._cluster_levels(levels_d1 + levels_h4)

            # ── 1. Signal flip check ──
            tf_d1 = self._analyze_tf(d1, levels_d1, price)
            tf_h4 = self._analyze_tf(h4 or [], levels_h4, price)
            biases = [tf_d1["bias"], tf_h4["bias"]]

            if biases.count("BUY") >= 2:
                current_signal = "BUY"
            elif biases.count("SELL") >= 2:
                current_signal = "SELL"
            else:
                current_signal = "HOLD"

            if self.last_signal and self.last_signal != current_signal and current_signal != "HOLD":
                emoji = "🟢📈" if current_signal == "BUY" else "🔴📉"
                atr = self._calc_atr(h4 if h4 else d1)
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
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ {_eat_now()}\n"
                    f"_/signal for full analysis_"
                )

            self.last_signal = current_signal

            # ── 2. S/R proximity alerts ──
            triggered_now = set()
            for level in all_levels:
                proximity = abs(price - level["price"]) / price
                level_key = f"{level['price']:.0f}"

                if proximity <= SR_ALERT_PCT:
                    triggered_now.add(level_key)
                    if level_key not in self.alerted_levels:
                        zone_type = "🔴 RESISTANCE" if level["price"] > price else "🟢 SUPPORT"
                        heat = " 🔥" * min(level["touches"] - 1, 3) if level["touches"] > 1 else ""
                        bias_hint = "Watch for rejection 🔽" if level["price"] > price else "Watch for bounce 🔼"

                        alerts.append(
                            f"⚡ *Gold at Key Level!*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"{zone_type}{heat}: `{level['price']:.2f}`\n"
                            f"💹 Price: `{price:.2f}`\n"
                            f"📏 Distance: `{proximity * 100:.2f}%`\n"
                            f"💡 {bias_hint}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {_eat_now()}"
                        )
                        self.alerted_levels.add(level_key)

            # Reset levels no longer in proximity range
            self.alerted_levels = self.alerted_levels.intersection(triggered_now) | \
                                   (self.alerted_levels - {k for k in self.alerted_levels
                                                           if k not in triggered_now and
                                                           abs(float(k) - price) / price > SR_ALERT_PCT * 3})

        except Exception as e:
            logger.error(f"check_alerts error: {e}", exc_info=True)

        return alerts
