"""Authenticated catalog ingestion. Never exposes customer or order records."""
import asyncio
import hashlib
import hmac
import logging
import math
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from aiohttp import web

from retail_store import dump

log = logging.getLogger(__name__)
MAX_BODY = 20 * 1024 * 1024
MAX_PRODUCTS = 30_000
MAX_TITLE = 3500
MAX_LABEL = 200
CHECKOUT_BUILD = "checkout-2026.09.21-audit"


class BadCatalog(ValueError):
    pass


class StaleCatalog(ValueError):
    pass


def telegram_link(value):
    if not isinstance(value, str) or len(value) > 500:
        raise BadCatalog("Invalid catalog URL")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise BadCatalog("Invalid catalog URL") from None
    if parsed.scheme != "https" or parsed.netloc != "t.me" or not parsed.path.strip("/"):
        raise BadCatalog("Catalog URL must be a Telegram HTTPS link")
    return value


def validate_payload(data):
    if not isinstance(data, dict) or data.get("protocol") != 1:
        raise BadCatalog("Unsupported protocol")
    version = data.get("revision")
    if type(version) is not int or not 0 < version <= int((time.time() + 300) * 1_000_000):
        raise BadCatalog("Invalid revision or sender clock")
    operation = data.get("operation")
    if operation not in {"snapshot", "uncertain", "catalog_url"}:
        raise BadCatalog("Unknown operation")

    result = {"protocol": 1, "revision": version, "operation": operation}
    if operation == "uncertain":
        reason = data.get("reason")
        if not isinstance(reason, str) or len(reason) > 300:
            raise BadCatalog("Invalid reason")
        result["reason"] = reason
        return result

    if operation == "catalog_url":
        result["url"] = telegram_link(data.get("url"))
        return result

    products = data.get("products")
    if not isinstance(products, list) or len(products) > MAX_PRODUCTS:
        raise BadCatalog("Invalid product list")
    if type(data.get("confirmed")) is not bool:
        raise BadCatalog("Invalid availability flag")

    checked = data.get("checked_at")
    if type(checked) not in {int, float} or not math.isfinite(checked) or not 0 <= checked <= time.time() + 300:
        raise BadCatalog("Invalid supplier timestamp")

    clean = []
    positions = {}
    for index, p in enumerate(products, start=1):
        if not isinstance(p, dict):
            raise BadCatalog(f"Item {index}: product must be an object")

        pid = p.get("id")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{24}", pid):
            raise BadCatalog(f"Item {index}: invalid product ID")

        row = {"id": pid}
        for field, maximum in (("title", MAX_TITLE), ("brand", MAX_LABEL), ("section", MAX_LABEL)):
            value = p.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise BadCatalog(f"Item {index}: invalid product {field}")
            row[field] = value

        raw_price = p.get("price")
        if not isinstance(raw_price, str) or len(raw_price) > 32:
            raise BadCatalog(f"Item {index}: invalid price")
        try:
            price = Decimal(raw_price)
            if not price.is_finite() or not 0 < price <= 10_000_000 or price != price.quantize(Decimal("0.01")):
                raise BadCatalog(f"Item {index}: invalid price")
        except InvalidOperation:
            raise BadCatalog(f"Item {index}: invalid price") from None

        currency = p.get("currency")
        if currency not in {"RUB", "USD", "EUR"}:
            raise BadCatalog(f"Item {index}: invalid currency")
        row.update(price=raw_price, currency=currency)

        for field in ("model", "sim", "condition"):
            if field in p:
                if not isinstance(p[field], str) or len(p[field]) > MAX_LABEL:
                    raise BadCatalog(f"Item {index}: invalid product variant {field}")
                row[field] = p[field]

        if "storage_rank" in p:
            if type(p["storage_rank"]) is not int or not 0 <= p["storage_rank"] <= 1_000_000_000:
                raise BadCatalog(f"Item {index}: invalid storage rank")
            row["storage_rank"] = p["storage_rank"]

        if pid in positions:
            current_index = positions[pid]
            current = clean[current_index]
            if current["currency"] != row["currency"]:
                raise BadCatalog(f"Item {index}: conflicting duplicate product ID {pid}")
            if Decimal(row["price"]) < Decimal(current["price"]):
                clean[current_index] = row
            continue

        positions[pid] = len(clean)
        clean.append(row)

    result.update(products=clean, confirmed=data["confirmed"], checked_at=checked)
    if data.get("catalog_url"):
        try:
            result["catalog_url"] = telegram_link(data["catalog_url"])
        except BadCatalog:
            log.warning("Игнорирую некорректную необязательную ссылку каталога в snapshot")
    return result

def ingest(store, payload):
    data = validate_payload(payload)
    signature = hashlib.sha256(dump(data).encode()).hexdigest()
    with store.transaction() as tx:
        previous = tx.get("system", "catalog_api_receipt", {})
        if data["revision"] <= previous.get("revision", 0):
            if data["revision"] == previous["revision"] and signature == previous["signature"]:
                return {**previous["result"], "duplicate": True}
            raise StaleCatalog("A newer catalog update is already applied")
        cancelled = 0
        if data["operation"] == "snapshot":
            cancelled = store.put_catalog(data["products"], confirmed=data["confirmed"],
                                          checked_at=data["checked_at"], _tx=tx)
            if data.get("catalog_url"):
                tx.set("system", "catalog_url", data["catalog_url"])
        elif data["operation"] == "uncertain":
            store.mark_uncertain(data["reason"], _tx=tx)
        else:
            tx.set("system", "catalog_url", data["url"])
        result = {"ok": True, "protocol": 1, "revision": data["revision"], "cancelled_drafts": cancelled}
        tx.set("system", "catalog_api_receipt", {"revision": data["revision"], "signature": signature,
                                                 "received_at": time.time(), "result": result})
        return result


def create_app(store, key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key or ""):
        raise ValueError("SYNC_API_KEY: нужен секрет из 32–128 латинских букв, цифр, _ или -")
    expected = ("Bearer " + key).encode()

    @web.middleware
    async def authorize(request, handler):
        if request.path != "/health":
            supplied = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(supplied, expected):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(request):
        return web.json_response({"ok": True, "service": "zayavki", "protocol": 1})

    async def validation(request):
        """Validate a catalog snapshot without mutating orders or catalog state."""
        try:
            payload = await request.json()
            data = validate_payload(payload)
            if data.get("operation") != "snapshot":
                raise BadCatalog("Validation endpoint accepts snapshot only")
            return web.json_response({
                "ok": True,
                "protocol": 1,
                "revision": data["revision"],
                "products": len(data["products"]),
                "build": CHECKOUT_BUILD,
            })
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "catalog_too_large"}, status=413)
        except BadCatalog as exc:
            return web.json_response({"error": "invalid_catalog", "detail": str(exc)[:500]}, status=400)
        except (ValueError, TypeError, UnicodeError):
            return web.json_response({"error": "invalid_catalog", "detail": "Malformed JSON or catalog payload"}, status=400)
        except Exception as exc:
            log.warning("Тест каталога не принят: %s", type(exc).__name__)
            return web.json_response({"error": "temporarily_unavailable"}, status=503)

    async def sync(request):
        try:
            payload = await request.json()
            result = await asyncio.to_thread(ingest, store, payload)
            return web.json_response(result)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "catalog_too_large"}, status=413)
        except StaleCatalog:
            return web.json_response({"error": "stale_revision"}, status=409)
        except BadCatalog as exc:
            return web.json_response({"error": "invalid_catalog", "detail": str(exc)[:500]}, status=400)
        except (ValueError, TypeError, UnicodeError):
            return web.json_response({"error": "invalid_catalog", "detail": "Malformed JSON or catalog payload"}, status=400)
        except Exception as exc:
            log.warning("Каталог не принят: %s", type(exc).__name__)
            return web.json_response({"error": "temporarily_unavailable"}, status=503)

    app = web.Application(middlewares=[authorize], client_max_size=MAX_BODY)
    app.router.add_get("/health", health)
    app.router.add_post("/api/catalog/validate", validation)
    app.router.add_post("/api/catalog/sync", sync)
    return app


async def start_server(store, key, port, host="0.0.0.0"):
    runner = web.AppRunner(create_app(store, key), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info("API каталога запущен на порту %s · сборка %s", port, CHECKOUT_BUILD)
    return runner
