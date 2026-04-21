import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

TWELVEDATA_BASE = "https://api.twelvedata.com"
FINNHUB_BASE    = "https://finnhub.io/api/v1"
SYMBOL          = "XAU/USD"

# Valid TwelveData intervals used by this bot
# "15min" | "1h" | "4h" | "1day"


class DataFetcher:
    def __init__(self, twelvedata_key: str, finnhub_key: str):
        self.td_key = twelvedata_key
        self.fh_key = finnhub_key

    async def fetch_ohlcv(self, interval: str, outputsize: int = 100) -> Optional[list]:
        """
        Fetch OHLCV candles from Twelve Data (returned oldest → newest).
        Supported intervals: 15min, 1h, 4h, 1day
        """
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
                        logger.warning(f"Twelve Data returned no values for [{interval}]")
                        return None

                    return [
                        {
                            "open":     float(v["open"]),
                            "high":     float(v["high"]),
                            "low":      float(v["low"]),
                            "close":    float(v["close"]),
                            "datetime": v["datetime"],
                        }
                        for v in reversed(values)   # oldest first
                    ]

        except Exception as e:
            logger.error(f"fetch_ohlcv [{interval}] failed: {e}")
            return None

    async def fetch_current_price(self) -> Optional[float]:
        """
        Live Gold price — Finnhub first, falls back to Twelve Data.
        """
        # ── Primary: Finnhub ──────────────────────────────────────────
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

        # ── Fallback: Twelve Data price endpoint ──────────────────────
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
