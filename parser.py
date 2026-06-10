"""
Модуль парсинга Wildberries.
Использует несколько методов — если один заблокирован, пробует следующий.
"""
import requests
import random
import logging
import time

logger = logging.getLogger(__name__)

WB_AFFILIATE_ID = ""  # задаётся из bot.py

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Android 14; Mobile; rv:125.0) Gecko/125.0 Firefox/125.0",
]

DESTS = [-1257786, -1059500, -2133464, -1275551]

def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "Connection": "keep-alive",
    }

def wb_link(article_id):
    base = f"https://www.wildberries.ru/catalog/{article_id}/detail.aspx"
    return f"{base}?utm_source=affiliate&utm_campaign={WB_AFFILIATE_ID}" if WB_AFFILIATE_ID else base

def _price(p, key="total"):
    try:
        sizes = p.get("sizes", [])
        if sizes:
            v = sizes[0].get("price", {}).get(key, 0)
            if v: return v // 100
        fallback = "salePriceU" if key == "total" else "priceU"
        v = p.get(fallback, 0)
        return v // 100 if v else None
    except:
        return None

def _to_item(p, price):
    return {
        "id":     f"wb_{p.get('id', 0)}",
        "name":   p.get("name", ""),
        "brand":  p.get("brand", ""),
        "price":  price,
        "old":    _price(p, "basic"),
        "rating": round(float(p.get("reviewRating", 0) or 0), 1),
        "fb":     int(p.get("feedbacks", 0) or 0),
        "url":    wb_link(p.get("id", 0)),
    }

def is_relevant(name: str, query: str) -> bool:
    n = name.lower()
    words = [w for w in query.lower().split() if len(w) > 2]
    if not words: return True
    matches = sum(1 for w in words if w in n)
    return matches >= max(1, len(words) // 2)


def search(query: str, max_price: int) -> list[dict]:
    """Поиск товаров — перебираем все методы пока не найдём."""

    # Метод 1: exactmatch search API (основной)
    for version in ("v9", "v7", "v5", "v4"):
        for dest in DESTS:
            try:
                url = f"https://search.wb.ru/exactmatch/ru/common/{version}/search"
                params = {
                    "query": query, "resultset": "catalog",
                    "limit": 50, "sort": "priceup",
                    "page": 1, "appType": 1,
                    "curr": "rub", "lang": "ru",
                    "dest": dest, "priceU": max_price * 100,
                }
                r = requests.get(url, params=params, headers=_headers(), timeout=15)
                if r.status_code == 200:
                    products = r.json().get("data", {}).get("products", [])
                    if products:
                        results = []
                        for p in products:
                            pr = _price(p)
                            if pr and pr <= max_price and is_relevant(p.get("name",""), query):
                                results.append(_to_item(p, pr))
                        logger.info(f"WB search OK [{version}/dest={dest}]: '{query}' → {len(results)} релевантных из {len(products)}")
                        return results
                elif r.status_code != 403:
                    logger.warning(f"WB {version} dest={dest}: HTTP {r.status_code}")
                time.sleep(0.3)
            except requests.Timeout:
                logger.warning(f"WB timeout {version}")
            except Exception as e:
                logger.warning(f"WB error {version}: {e}")

    # Метод 2: catalog search (резервный)
    try:
        url2 = "https://catalog.wb.ru/catalog/electronic14/v2/catalog"
        params2 = {
            "appType": 1, "curr": "rub", "dest": -1257786,
            "sort": "priceup", "spp": 30, "limit": 30,
        }
        r2 = requests.get(url2, params=params2, headers=_headers(), timeout=12)
        if r2.status_code == 200:
            products = r2.json().get("data", {}).get("products", [])
            results = []
            for p in products:
                pr = _price(p)
                if pr and pr <= max_price and is_relevant(p.get("name",""), query):
                    results.append(_to_item(p, pr))
            if results:
                logger.info(f"WB catalog OK: {len(results)} товаров")
                return results
    except Exception as e:
        logger.warning(f"WB catalog error: {e}")

    logger.warning(f"WB: все методы не дали результат для '{query}'")
    return []


def hot_deals() -> list[dict]:
    """Горящие скидки."""
    queries = ["наушники", "кроссовки", "смартфон", "куртка", "ноутбук", "часы"]
    query   = random.choice(queries)
    results = []

    for version in ("v9", "v7", "v5"):
        for dest in DESTS[:2]:
            try:
                url = f"https://search.wb.ru/exactmatch/ru/common/{version}/search"
                params = {
                    "query": query, "resultset": "catalog",
                    "limit": 50, "sort": "popular",
                    "page": 1, "appType": 1,
                    "curr": "rub", "lang": "ru",
                    "dest": dest, "discount": 25,
                }
                r = requests.get(url, params=params, headers=_headers(), timeout=15)
                if r.status_code == 200:
                    products = r.json().get("data", {}).get("products", [])
                    for p in products:
                        pr  = _price(p)
                        old = _price(p, "basic")
                        if pr and old and old > pr and (1 - pr/old) >= 0.25:
                            item = _to_item(p, pr)
                            item["old"] = old
                            results.append(item)
                    if results:
                        results.sort(key=lambda x: -(x.get("old",0) - x["price"]))
                        logger.info(f"WB hot deals OK [{version}]: {len(results)} товаров")
                        return results[:8]
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"WB hot {version}: {e}")

    return []
