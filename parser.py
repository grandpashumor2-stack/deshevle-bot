import asyncio
import logging
import os
import random
import aiohttp

logger = logging.getLogger(__name__)

# Партнёрская ссылка WB (замени на свою после регистрации на affiliate.wildberries.ru)
WB_AFFILIATE_ID = os.environ.get("WB_AFFILIATE_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}

HOT_QUERIES = [
    "смартфон", "наушники", "кроссовки", "куртка",
    "ноутбук", "часы", "рюкзак", "кофемашина",
    "планшет", "платье",
]


class WBParser:

    def make_url(self, article_id: int) -> str:
        base = f"https://www.wildberries.ru/catalog/{article_id}/detail.aspx"
        if WB_AFFILIATE_ID:
            return (
                f"{base}?targetUrl=WA&utm_source=affiliate"
                f"&utm_medium=cpa&utm_campaign={WB_AFFILIATE_ID}"
            )
        return base

    async def search(self, query: str, max_price: int) -> list[dict]:
        """Поиск товаров на WB по запросу с фильтром по цене."""
        # Пробуем два эндпоинта — v9 основной, v5 запасной
        for version in ("v9", "v7", "v5"):
            url = f"https://search.wb.ru/exactmatch/ru/common/{version}/search"
            params = {
                "query": query,
                "resultset": "catalog",
                "limit": 20,
                "sort": "priceup",
                "page": 1,
                "appType": 1,
                "curr": "rub",
                "lang": "ru",
                "dest": -1257786,
                "priceU": max_price * 100,
            }
            results = await self._fetch_products(url, params, max_price=max_price)
            if results is not None:
                return results
            await asyncio.sleep(0.5)

        return []

    async def get_hot_deals(self) -> list[dict]:
        """Подборка товаров со скидкой 25%+."""
        query = random.choice(HOT_QUERIES)
        for version in ("v9", "v7", "v5"):
            url = f"https://search.wb.ru/exactmatch/ru/common/{version}/search"
            params = {
                "query": query,
                "resultset": "catalog",
                "limit": 30,
                "sort": "popular",
                "page": 1,
                "appType": 1,
                "curr": "rub",
                "lang": "ru",
                "dest": -1257786,
                "discount": 25,
            }
            products_raw = await self._fetch_raw(url, params)
            if products_raw is not None:
                results = []
                for p in products_raw:
                    price = self._extract_price(p)
                    old_price = self._extract_old_price(p)
                    if price and old_price and old_price > price:
                        discount = (1 - price / old_price) * 100
                        if discount >= 25:
                            item = self._parse_product(p, price)
                            item["old_price"] = old_price
                            results.append(item)
                results.sort(key=lambda x: -(x.get("old_price", 0) - x["price"]))
                return results[:8]
            await asyncio.sleep(0.5)

        return []

    async def _fetch_products(self, url: str, params: dict, max_price: int) -> list[dict] | None:
        raw = await self._fetch_raw(url, params)
        if raw is None:
            return None
        results = []
        for p in raw:
            price = self._extract_price(p)
            if price and price <= max_price:
                results.append(self._parse_product(p, price))
        return results

    async def _fetch_raw(self, url: str, params: dict) -> list | None:
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", {}).get("products", [])
                    logger.warning(f"WB {url} returned {resp.status}")
                    return None
        except asyncio.TimeoutError:
            logger.warning("WB timeout")
            return None
        except Exception as e:
            logger.error(f"WB fetch error: {e}")
            return None

    def _extract_price(self, p: dict) -> int | None:
        try:
            sizes = p.get("sizes", [])
            if sizes:
                price_u = sizes[0].get("price", {}).get("total", 0)
                if price_u:
                    return price_u // 100
            sale = p.get("salePriceU", 0)
            if sale:
                return sale // 100
        except Exception:
            pass
        return None

    def _extract_old_price(self, p: dict) -> int | None:
        try:
            sizes = p.get("sizes", [])
            if sizes:
                price_u = sizes[0].get("price", {}).get("basic", 0)
                if price_u:
                    return price_u // 100
            basic = p.get("priceU", 0)
            if basic:
                return basic // 100
        except Exception:
            pass
        return None

    def _parse_product(self, p: dict, price: int) -> dict:
        article = p.get("id", 0)
        return {
            "id": str(article),
            "name": p.get("name", "Товар"),
            "brand": p.get("brand", ""),
            "price": price,
            "old_price": self._extract_old_price(p),
            "rating": p.get("reviewRating", 0),
            "feedbacks": p.get("feedbacks", 0),
            "url": self.make_url(article),
        }
