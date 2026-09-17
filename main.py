import asyncio
import logging
import signal
from contextlib import suppress

from catalog_api import start_server
from bot import RequestBot
from orders import OrderService
from retail_store import RetailStore, ProcessLease
from settings import Settings
from telegram_api import TelegramAPI


async def main():
    settings = Settings.from_env()
    store = await asyncio.to_thread(RetailStore, settings.database_url)
    api = TelegramAPI(settings.bot_token)
    service = OrderService(store, settings.admin_ids, archive_days=settings.archive_days,
                           draft_hours=settings.draft_hours, fresh_seconds=settings.fresh_seconds)
    bot = RequestBot(service, settings, api)
    lease = ProcessLease(store, "zayavki-" + settings.bot_token.split(":", 1)[0])
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, bot.stop.set)
    while not bot.stop.is_set():
        if await asyncio.to_thread(lease.acquire):
            break
        logging.info("Ожидаю завершения предыдущего процесса оформления")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(bot.stop.wait(), 3)
    if bot.stop.is_set():
        lease.close()
        await api.close()
        return

    async def watch_lock():
        while not bot.stop.is_set():
            await asyncio.to_thread(lease.check)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(bot.stop.wait(), 5)

    tasks, runner = [], None
    try:
        runner = await start_server(store, settings.sync_api_key, settings.port)
        tasks = [asyncio.create_task(bot.run()), asyncio.create_task(watch_lock()),
                 asyncio.create_task(bot.stop.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        bot.stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await api.close()
        lease.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
