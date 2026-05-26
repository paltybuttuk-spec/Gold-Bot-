import time
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

TWELVEDATA_BASE = "https://api.twelvedata.com"
FINNHUB_BASE    = "https://finnhub.io/api/v1"
SYMBOL          = "XAU/USD"

CACHE_TTL = {
    "1day":  86400,
    "4h":    14400,
    "1h":     3600,
    "15min":   900,
}


class DataFetcher:
    def __init__(self, twelvedata_key: str, finnhub_key: str):
        self.td_key = twelvedata_key
        self.fh_key = finnhub_key
        self._cache: dict = {}

    def _cache_key(self, interval: str, outputsize: int, symbol: str = "XAU/USD") -> str:
        return f"{symbol}:{interval}:{outputsize}"

    def _get_cached(self, interval: str, outputsize: int, symbol: str = "XAU/USD") -> Optional[list]:
        key = self._cache_key(interval, outputsize, symbol)
        entry = self._cache.get(key)
        if not entry:
            return None
        ttl = CACHE_TTL.get(interval, 60)
        age = time.time() - entry["fetched_at"]
        if age < ttl:
            logger.debug(f"Cache HIT [{symbol}:{interval}] age={age:.0f}s")
            return entry["data"]
        return None

    def _set_cache(self, interval: str, outputsize: int, data: list, symbol: str = "XAU/USD"):
        key = self._cache_key(interval, outputsize, symbol)
        self._cache[key] = {"data": data, "fetched_at": time.time()}

    async def fetch_ohlcv(self, interval: str, outputsize: int = 100,
                          symbol: str = "XAU/USD") -> Optional[list]:
        cached = self._get_cached(interval, outputsize, symbol)
        if cached is not None:
            return cached

        url    = f"{TWELVEDATA_BASE}/time_series"
        params = {
            "symbol":     symbol,
            "interval":   interval,
            "outputsize": outputsize,
            "apikey":     self.td_key,
            "format":     "JSON",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                    if data.get("status") == "error":
                        logger.error(f"Twelve Data error [{symbol}:{interval}]: {data.get('message')}")
                        return None
                    values = data.get("values", [])
                    if not values:
                        return None
                    candles = [
                        {"open": float(v["open"]), "high": float(v["high"]),
                         "low": float(v["low"]),  "close": float(v["close"]),
                         "datetime": v["datetime"]}
                        for v in reversed(values)
                    ]
                    self._set_cache(interval, outputsize, candles, symbol)
                    logger.info(f"Fetched [{symbol}:{interval}] {len(candles)} candles")
                    return candles
        except Exception as e:
            logger.error(f"fetch_ohlcv [{symbol}:{interval}] failed: {e}")
            return None

    # --- NEW: DXY convenience wrapper ---
    async def fetch_dxy_ohlcv(self, interval: str, outputsize: int = 100) -> Optional[list]:
        """Fetch DXY (US Dollar Index) candles. Same format as XAU/USD candles."""
        return await self.fetch_ohlcv(interval, outputsize, symbol="DXY")

    async def fetch_current_price(self) -> Optional[float]:
        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{FINNHUB_BASE}/quote"
                params = {"symbol": "OANDA:XAU_USD", "token": self.fh_key}
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("c") and data["c"] > 0:
                        return float(data["c"])
        except Exception as e:
            logger.warning(f"Finnhub price fetch failed: {e}")

        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{TWELVEDATA_BASE}/price"
                params = {"symbol": SYMBOL, "apikey": self.td_key}
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    return float(data["price"])
        except Exception as e:
            logger.error(f"Twelve Data price fallback failed: {e}")
            return None

    # --- NEW: DXY spot price ---
    async def fetch_dxy_price(self) -> Optional[float]:
        """Fetch live DXY price from Twelve Data /price endpoint."""
        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{TWELVEDATA_BASE}/price"
                params = {"symbol": "DXY", "apikey": self.td_key}
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    p = float(data.get("price", 0))
                    return p if p > 0 else None
        except Exception as e:
            logger.error(f"fetch_dxy_price failed: {e}")
            return None

    async def fetch_finnhub_quote(self) -> Optional[dict]:
        try:
            async with aiohttp.ClientSession() as session:
                url    = f"{FINNHUB_BASE}/quote"
                params = {"symbol": "OANDA:XAU_USD", "token": self.fh_key}
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("c") and data["c"] > 0:
                        return {"current": float(data["c"]), "open": float(data["o"]),
                                "high": float(data["h"]), "low": float(data["l"]),
                                "prev_close": float(data["pc"])}
        except Exception as e:
            logger.error(f"fetch_finnhub_quote failed: {e}")
        return None
