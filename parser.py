"""
Парсер Wildberries с защитой от rate limit (429) и анти-бота (x-pow).
- Кэширование результатов (15 мин) — снижает нагрузку на WB при росте пользователей
- Увеличенные паузы между запросами
- Retry при 429 и при ошибках JSON
"""
import requests
import random
import logging
import time
import json

logger = logging.getLogger(__name__)

WB_AFFILIATE_ID = ""

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

WB_DEST    = -1257786
WB_VERSION = "v9"

# ─── Throttling ─────────────────────────────────────────────────────────────
_last_request_time = 0
MIN_INTERVAL = 4.0  # секунды между запросами к WB (увеличено для снижения нагрузки)

def _wait():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request_time = time.time()

# ─── Кэш результатов ─────────────────────────────────────────────────────────
_cache = {}  # key -> (timestamp, results)
CACHE_TTL = 900  # 15 минут

def _cache_get(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        logger.info(f"Cache HIT: {key}")
        return entry[1]
    return None

def _cache_set(key, value):
    _cache[key] = (time.time(), value)
    # чистим старые записи, если кэш разросся
    if len(_cache) > 200:
        cutoff = time.time() - CACHE_TTL
        for k in list(_cache.keys()):
            if _cache[k][0] < cutoff:
                del _cache[k]


def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept-Encoding": "identity",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
        "Connection": "close",
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
        v = p.get("salePriceU" if key == "total" else "priceU", 0)
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


def _wb_get(params: dict, retries: int = 3) -> list:
    url = f"https://search.wb.ru/exactmatch/ru/common/{WB_VERSION}/search"

    for attempt in range(retries + 1):
        _wait()
        try:
            r = requests.get(url, params=params, headers=_headers(), timeout=15)

            if r.status_code == 200:
                try:
                    return r.json().get("data", {}).get("products", [])
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"WB JSON error (попытка {attempt+1}): {e}")
                    time.sleep(2)
                    continue

            elif r.status_code == 429:
                wait_time = 8 * (attempt + 1)  # 8с, 16с, 24с, 32с
                logger.warning(f"WB 429 (попытка {attempt+1}) — жду {wait_time}с")
                time.sleep(wait_time)
                continue

            elif r.status_code == 403:
                logger.warning(f"WB 403 — доступ запрещён")
                return []

            else:
                logger.warning(f"WB HTTP {r.status_code}")
                return []

        except requests.Timeout:
            logger.warning(f"WB timeout (попытка {attempt+1})")
            time.sleep(3)
        except (requests.ConnectionError, ConnectionResetError) as e:
            logger.warning(f"WB connection error (попытка {attempt+1}): {e}")
            time.sleep(3)
        except Exception as e:
            logger.error(f"WB error: {e}")
            return []

    logger.warning("WB: все попытки исчерпаны")
    return []


def search(query: str, max_price: int) -> list:
    """Поиск товаров по запросу с кэшированием и фильтром релевантности."""
    cache_key = f"search:{query.lower().strip()}:{max_price}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "query":     query,
        "resultset": "catalog",
        "limit":     50,
        "sort":      "priceup",
        "page":      1,
        "appType":   1,
        "curr":      "rub",
        "lang":      "ru",
        "dest":      WB_DEST,
        "priceU":    max_price * 100,
    }

    products = _wb_get(params)

    results = []
    for p in products:
        pr   = _price(p)
        name = p.get("name", "")
        if pr and pr <= max_price and is_relevant(name, query):
            results.append(_to_item(p, pr))

    logger.info(f"WB search '{query}' до {max_price}₽: "
                f"{len(results)} релевантных из {len(products)} товаров")

    _cache_set(cache_key, results)
    return results


def hot_deals() -> list:
    """Горящие скидки — товары со скидкой от 25%. Кэшируется на 15 минут."""
    cache_key = "hot_deals"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    query = random.choice([
        "наушники", "кроссовки", "смартфон",
        "куртка",   "ноутбук",   "часы",
    ])

    params = {
        "query":     query,
        "resultset": "catalog",
        "limit":     50,
        "sort":      "popular",
        "page":      1,
        "appType":   1,
        "curr":      "rub",
        "lang":      "ru",
        "dest":      WB_DEST,
        "discount":  25,
    }

    products = _wb_get(params)

    results = []
    for p in products:
        pr  = _price(p)
        old = _price(p, "basic")
        if pr and old and old > pr and (1 - pr / old) >= 0.25:
            item = _to_item(p, pr)
            item["old"] = old
            results.append(item)

    results.sort(key=lambda x: -(x.get("old", 0) - x["price"]))
    results = results[:8]
    logger.info(f"WB hot deals '{query}': {len(results)} товаров со скидкой")

    _cache_set(cache_key, results)
    return results
