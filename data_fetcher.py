import time
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

TWELVEDATA_BASE = "https://api.twelvedata.com"
FINNHUB_BASE    = "https://finnhub.io/api/v1"
SYMBOL          = "XAU/USD"

# ── Cache TTLs (seconds) ──────────────────────────────────────────────────
# Candles only change when a new candle closes, so we never need to
# re-fetch more often than the timeframe itself.
# This keeps daily TwelveData usage at ~127 credits vs 1,152+ without cache.
CACHE_TTL = {
    "1day":  86400,   # D1  — refresh once per day    →   1 credit/day
    "4h":    14400,   # H4  — refresh every 4 hours   →   6 credits/day
    "1h":     3600,   # H1  — refresh every hour      →  24 credits/day
    "15min":   900,   # M15 — refresh every 15 min    →  96 credits/day
}                     #                          Total → 127 credits/day
# ─────────────────────────────────────────────────────────────────────────


class DataFetcher:
    def __init__(self, twelvedata_key: str, finnhub_key: str):
        self.td_key = twelvedata_key
        self.fh_key = finnhub_key

        # { "interval:outputsize" : {"data": [...], "fetched_at": float} }
        self._cache: dict = {}

    # ── Internal cache helpers ────────────────────────────────────────────

    def _cache_key(self, interval: str, outputsize: int) -> str:
        return f"{interval}:{outputsize}"

    def _get_cached(self, interval: str, outputsize: int) -> Optional[list]:
        key = self._cache_key(interval, outputsize)
        entry = self._cache.get(key)
        if not entry:
            return None
        ttl = CACHE_TTL.get(interval, 60)
        age = time.time() - entry["fetched_at"]
        if age < ttl:
            logger.debug(f"Cache HIT [{interval}] age={age:.0f}s ttl={ttl}s")
            return entry["data"]
        logger.debug(f"Cache EXPIRED [{interval}] age={age:.0f}s ttl={ttl}s")
        return None

    def _set_cache(self, interval: str, outputsize: int, data: list):
        key = self._cache_key(interval, outputsize)
        self._cache[key] = {"data": data, "fetched_at": time.time()}
        logger.debug(f"Cache SET [{interval}] candles={len(data)}")

    # ── OHLCV ─────────────────────────────────────────────────────────────

    async def fetch_ohlcv(self, interval: str, outputsize: int = 100) -> Optional[list]:
        """
        Fetch OHLCV candles from Twelve Data (oldest → newest).
        Results are cached per interval/outputsize for the candle's natural TTL,
        so the bot never re-fetches data that hasn't changed yet.

        Supported intervals: 15min | 1h | 4h | 1day
        Daily credit cost with caching:
            D1=1  H4=6  H1=24  M15=96  → ~127 total (free plan limit: 800)
        """
        # Return from cache if still fresh
        cached = self._get_cached(interval, outputsize)
        if cached is not None:
            return cached

        # Cache miss — hit the API
        url    = f"{TWELVEDATA_BASE}/time_series"
        params = {
            "symbol":     SYMBOL,
            "interval":   interval,
            "outputsize": outputsize,
            "apikey":     self.td_key,
            "format":     "JSON",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()

                    if data.get("status") == "error":
                        logger.error(
                            f"Twelve Data error [{interval}]: {data.get('message')}"
                        )
                        return None

                    values = data.get("values", [])
                    if not values:
                        logger.warning(
                            f"Twelve Data returned no values for [{interval}]"
                        )
                        return None

                    candles = [
                        {
                            "open":     float(v["open"]),
                            "high":     float(v["high"]),
                            "low":      float(v["low"]),
                            "close":    float(v["close"]),
                            "datetime": v["datetime"],
                        }
                        for v in reversed(values)   # oldest first
                    ]

                    self._set_cache(interval, outputsize, candles)
                    logger.info(f"Fetched [{interval}] {len(candles)} candles from API")
                    return candles

        except Exception as e:
            logger.error(f"fetch_ohlcv [{interval}] failed: {e}")
            return None

    # ── Live price ────────────────────────────────────────────────────────

    async def fetch_current_price(self) -> Optional[float]:
        """
        Live Gold price — Finnhub first (free, no quota impact),
        falls back to Twelve Data /price endpoint.
        """
        # Primary: Finnhub (free tier, no daily limit for quotes)
        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{FINNHUB_BASE}/quote"
                params = {"symbol": "OANDA:XAU_USD", "token": self.fh_key}
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    if data.get("c") and data["c"] > 0:
                        return float(data["c"])
        except Exception as e:
            logger.warning(f"Finnhub price fetch failed: {e}")

        # Fallback: Twelve Data /price (costs 1 credit, used only if Finnhub fails)
        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{TWELVEDATA_BASE}/price"
                params = {"symbol": SYMBOL, "apikey": self.td_key}
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    return float(data["price"])
        except Exception as e:
            logger.error(f"Twelve Data price fallback failed: {e}")
            return None

    # ── Full Finnhub quote ────────────────────────────────────────────────

    async def fetch_finnhub_quote(self) -> Optional[dict]:
        """Full Finnhub quote — open / high / low / prev close / current."""
        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{FINNHUB_BASE}/quote"
                params = {"symbol": "OANDA:XAU_USD", "token": self.fh_key}
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    if data.get("c") and data["c"] > 0:
                        return {
                            "current":    float(data["c"]),
                            "open":       float(data["o"]),
                            "high":       float(data["h"]),
                            "low":        float(data["l"]),
                            "prev_close": float(data["pc"]),
                        }
        except Exception as e:
            logger.error(f"fetch_finnhub_quote failed: {e}")
        return None
