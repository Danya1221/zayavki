import asyncio
import aiohttp


class TelegramError(RuntimeError):
    pass


class AmbiguousSend(TelegramError):
    """Telegram may have accepted the write: never blindly retry it."""


class TelegramAPI:
    def __init__(self, token):
        if not token:
            raise ValueError("BOT_TOKEN обязателен")
        self.base = "https://api.telegram.org/bot" + token
        self.http = None

    async def call(self, method, **payload):
        safe = method not in {"sendMessage", "sendPhoto", "sendDocument"}
        for attempt in range(4):
            if self.http is None or self.http.closed:
                self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
            try:
                async with self.http.post(self.base + "/" + method, json=payload) as response:
                    body = await response.json(content_type=None)
                    status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                await self.close()
                if not safe and not isinstance(exc, aiohttp.ClientConnectorError):
                    raise AmbiguousSend("Ответ Telegram потерян; результат отправки неизвестен") from None
                if attempt == 3:
                    raise TelegramError("Telegram временно недоступен: " + type(exc).__name__) from None
                await asyncio.sleep(1 + attempt)
                continue
            if body.get("ok"):
                return body.get("result")
            if status == 429 and attempt < 3:
                await asyncio.sleep(min(60, max(1, int(body.get("parameters", {}).get("retry_after", 2)))))
                continue
            if status >= 500:
                if not safe:
                    raise AmbiguousSend("Сбой Telegram при отправке; результат неизвестен")
                if attempt < 3:
                    await asyncio.sleep(1 + attempt)
                    continue
            raise TelegramError(str(body.get("description", "Ошибка Telegram"))[:500])
        raise TelegramError("Telegram временно недоступен")

    async def close(self):
        if self.http is not None and not self.http.closed:
            await self.http.close()
        self.http = None
