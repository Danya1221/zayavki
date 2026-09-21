"""Private customer checkout and administrator CRM over Telegram Bot API."""
import asyncio
import hashlib
import logging
import time
import sqlite3
import psycopg
from contextlib import suppress
from decimal import Decimal

from orders import UserError, DEFAULT_STATUSES
from telegram_api import TelegramError, AmbiguousSend
from views import e, cash, button as b, keyboard as kb, items_text, fields_text, order_text, chunks, FIELD_NAMES, date

log = logging.getLogger(__name__)
OPTIONAL = {"apartment", "delivery_note", "order_note"}
PROMPTS = {
    "name": "Как зовут получателя?",
    "phone": "Укажи российский телефон +7 или нажми «Отправить мой номер».",
    "city": "Укажи город или населённый пункт доставки.",
    "street": "Укажи улицу или территорию. Если улицы нет — напиши название посёлка / СНТ.",
    "house": "Укажи дом, при необходимости корпус или строение.",
    "apartment": "Укажи квартиру / офис. Для частного дома можно пропустить.",
    "delivery_note": "Примечание к получению: удобное время, подъезд, ориентир или пункт выдачи.",
    "order_note": "Добавить общее примечание к заказу?",
}


class RequestBot:
    def __init__(self, service, settings, api):
        self.service, self.settings, self.api = service, settings, api
        self.stop = asyncio.Event()
        self.username = ""
        self.bot_id = 0
        self.lock = asyncio.Lock()

    async def db(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def send(self, chat_id, text, markup=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup is not None:
            payload["reply_markup"] = markup
        return await self.api.call("sendMessage", **payload)

    async def erase(self, chat_id, message_id):
        try:
            await self.api.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            text = str(exc).lower()
            if "not found" in text:
                return
            if "can't be deleted" in text or "cannot be deleted" in text:
                # Telegram limits deletion of old messages. Remove sensitive text
                # from editable bot messages; user input is removed immediately.
                with suppress(TelegramError):
                    await self.api.call("editMessageText", chat_id=chat_id, message_id=message_id,
                                        text="Эта карточка завершена.", reply_markup=kb([]))
                return
            raise

    async def work(self, user_id, text, markup=None, *, role=None, order_id=None):
        profile = await self.db(self.service.profile, user_id)
        old = profile.get("work_ids", [])
        for message_id in old:
            await self.erase(user_id, message_id)
        await self.db(self.service.update_profile, user_id, {"work_ids": []})
        ids = []
        pages = chunks(text)
        for index, page in enumerate(pages):
            message = await self.send(user_id, page, markup if index == len(pages) - 1 else None)
            ids.append(message["message_id"])
            await self.db(self.service.update_profile, user_id, {"work_ids": ids})
            if order_id:
                await self.db(self.service.remember_message, order_id, user_id, message["message_id"], role or "admin", index)
        return ids

    async def catalog_url(self):
        return self.settings.catalog_url or await self.db(self.service.store.get, "system", "catalog_url", "")

    async def welcome(self, user_id):
        default = (f"Добро пожаловать в {self.settings.shop_name}!\n\n"
                   "Выбери устройство в прайсе и перейди по его названию сюда.\n"
                   "Здесь можно собрать корзину и отправить заявку менеджеру.\n"
                   "Доставка платная, стоимость согласуем отдельно.")
        text = await self.db(self.service.store.get, "settings", "welcome", default)
        profile = await self.db(self.service.profile, user_id)
        rows = [[b("🛒 Моя корзина", "cart"), b("Мои заявки", "mine:0")]]
        url = await self.catalog_url()
        if url:
            rows.insert(0, [b("📦 Открыть прайс", url=url)])
        if user_id in self.service.admins:
            rows.append([b("🗂 Управление заявками", "a:menu")])
        payload_hash = hashlib.sha256((text + str(rows)).encode()).hexdigest()
        welcome_id = profile.get("welcome_id")
        if welcome_id:
            try:
                await self.api.call("editMessageText", chat_id=user_id, message_id=welcome_id,
                                    text=e(text), reply_markup=kb(rows), parse_mode="HTML")
            except TelegramError as exc:
                if "not modified" not in str(exc).lower():
                    if "not found" in str(exc).lower():
                        welcome_id = None
                    else:
                        raise
        if not welcome_id:
            welcome_id = (await self.send(user_id, e(text), kb(rows)))["message_id"]
        await self.db(self.service.update_profile, user_id,
                      {"welcome_id": welcome_id, "welcome_hash": payload_hash})

    async def show_cart(self, user_id, page=0):
        cart = await self.db(self.service.cart, user_id)
        if not cart.get("items"):
            await self.work(user_id, "Корзина пуста. Выбери устройство в прайсе.", kb([[b("📦 Прайс", url=await self.catalog_url())]]) if await self.catalog_url() else None)
            return
        total = sum(Decimal(i["price"]) * i["qty"] for i in cart["items"])
        text = "<b>Твоя корзина</b>\n\n" + items_text(cart["items"])
        text += "\n\n<b>Товары: " + cash(total, cart["items"][0]["currency"]) + "</b>\nДоставка оплачивается отдельно."
        rows = []
        token = cart["token"]
        page = max(0, min(page, (len(cart["items"]) - 1) // 8))
        for index, item in enumerate(cart["items"][page*8:(page+1)*8], page*8+1):
            pid = item["product_id"]
            rows.append([b(f"− {index}", f"qty:{token}:{pid}:-1"), b(f"{index}: {item['qty']} шт.", "noop"),
                         b(f"+ {index}", f"qty:{token}:{pid}:1"),
                         b("🗑", f"remove:{token}:{pid}")])
        paging = []
        if page:
            paging.append(b("← Товары", "cartpage:" + str(page-1)))
        if (page+1)*8 < len(cart["items"]):
            paging.append(b("Товары →", "cartpage:" + str(page+1)))
        if paging:
            rows.append(paging)
        rows += [[b("💬 Примечание к корзине", "note:cart")],
                 [b("Оформить заявку →", "checkout")],
                 [b("Очистить", "clear")]]
        url = await self.catalog_url()
        if url:
            rows.append([b("Добавить ещё товар", url=url)])
        await self.db(self.service.update_profile, user_id, {"step": "cart"})
        await self.work(user_id, text, kb(rows))

    async def ask(self, user_id, field):
        await self.db(self.service.update_profile, user_id, {"step": field})
        if field == "cart":
            return await self.show_cart(user_id)
        if field == "delivery":
            text = ("<b>Как получить заказ?</b>\n\nДоставка платная. Менеджер рассчитает стоимость, "
                    "а ты подтвердишь итоговую сумму перед отправкой.")
            rows = [[b("🚚 Курьер", "delivery:courier"), b("📦 Транспортная компания", "delivery:shipping")]]
            rows.append([b("🏬 Самовывоз", "delivery:pickup")])
            text += "\n\nМесто и время самовывоза согласует менеджер."
            rows += [[b("💬 Примечание", "note:delivery"), b("← Назад", "back")]]
            return await self.work(user_id, text, kb(rows))
        if field == "review":
            return await self.review(user_id)
        rows = [[b("💬 Примечание", "note:" + field), b("← Назад", "back")]]
        if field in OPTIONAL:
            rows.insert(0, [b("Пропустить", "skip")])
        cart = await self.db(self.service.cart, user_id)
        current = cart.get("fields", {}).get(field, "")
        text = PROMPTS[field] + ("\n\nСейчас: " + e(current) if current else "")
        if field == "phone":
            markup = {"keyboard": [[{"text": "📱 Отправить мой номер", "request_contact": True}]],
                      "resize_keyboard": True, "one_time_keyboard": True}
            # The main prompt carries inline navigation; the contact keyboard is
            # attached to a tracked temporary companion message.
            await self.work(user_id, text, kb(rows))
            msg = await self.send(user_id, "Можно ввести номер сообщением.", markup)
            profile = await self.db(self.service.profile, user_id)
            await self.db(self.service.update_profile, user_id,
                          {"work_ids": profile.get("work_ids", []) + [msg["message_id"]]})
            return
        await self.work(user_id, text, kb(rows))

    def steps(self, cart):
        address = [] if cart.get("delivery") == "pickup" else ["city", "street", "house", "apartment"]
        return ["cart", "name", "phone", "delivery", *address, "delivery_note", "order_note", "review"]

    async def advance(self, user_id, current, delta=1):
        cart = await self.db(self.service.cart, user_id)
        steps = self.steps(cart)
        index = steps.index(current) if current in steps else 0
        await self.ask(user_id, steps[max(0, min(len(steps) - 1, index + delta))])

    async def review(self, user_id):
        cart = await self.db(self.service.cart, user_id)
        await self.db(self.service.validate_checkout, cart)
        text = "<b>Проверь заявку перед отправкой</b>\n\n" + items_text(cart["items"]) + "\n\n"
        text += fields_text(cart.get("fields", {}), cart.get("notes", {}))
        total = sum(Decimal(i["price"]) * i["qty"] for i in cart["items"])
        text += "\n\nТовары: <b>" + cash(total, cart["items"][0]["currency"]) + "</b>"
        if cart["delivery"] == "pickup":
            text += "\nСамовывоз: место и время согласует менеджер."
        else:
            text += "\nДоставка платная. Её стоимость менеджер пришлёт на согласование отдельно."
        text += "\n\nНаличие подтверждает менеджер. Заявки принимаются круглосуточно."
        await self.work(user_id, text, kb([[b("✅ Отправить заявку", "submit:" + cart["token"])],
            [b("Исправить данные", "edit"), b("💬 Примечание", "note:order_note")],
            [b("← В корзину", "cart")]]))

    async def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        user = (callback or message).get("from", {})
        chat = message.get("chat", {})
        actor = user.get("id")
        if not actor or chat.get("type") != "private" or int(chat.get("id", 0)) != int(actor):
            if callback:
                with suppress(TelegramError):
                    await self.api.call("answerCallbackQuery", callback_query_id=callback["id"],
                                        text="Открой личный чат с ботом", show_alert=True)
            return
        actor = int(actor)
        await self.db(self.service.statuses)
        if callback:
            with suppress(TelegramError):
                await self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
        try:
            if callback:
                await self.callback(actor, user, callback["data"], callback["id"])
            else:
                await self.message(actor, user, message, str(update.get("update_id", message.get("message_id"))))
        except (UserError, PermissionError) as exc:
            await self.work(actor, e(str(exc)), kb([[b("🛒 Корзина", "cart")]]))
        finally:
            if not callback and message.get("message_id"):
                # Incoming contact/address/code messages should not pile up.
                with suppress(TelegramError):
                    await self.erase(actor, message["message_id"])

    async def callback(self, actor, user, data, action_id):
        if data.startswith("a:"):
            self.service.require_admin(actor)
            return await self.admin_callback(actor, data)
        if data == "noop":
            return
        if data == "cart":
            return await self.show_cart(actor)
        if data.startswith("cartpage:"):
            return await self.show_cart(actor, int(data.split(":")[1]))
        if data.startswith("mine:"):
            orders = await self.db(self.service.customer_orders, actor)
            page = max(0, min(int(data.split(":")[1]), max(0, (len(orders)-1)//8)))
            rows = [[b(o["id"] + " · " + self.service.status_label(o["status"]), "own:" + o["id"])]
                    for o in orders[page*8:(page+1)*8]]
            nav = []
            if page:
                nav.append(b("←", "mine:" + str(page-1)))
            if (page+1)*8 < len(orders):
                nav.append(b("→", "mine:" + str(page+1)))
            if nav:
                rows.append(nav)
            rows.append([b("🛒 Корзина", "cart")])
            return await self.work(actor, "<b>Мои заявки</b>\n" + ("Выбери заявку." if orders else "Заявок пока нет."), kb(rows))
        if data.startswith("own:"):
            order = await self.db(self.service.get_order, actor, data.split(":")[1])
            if order["user_id"] != actor:
                raise UserError("Заявка не найдена.")
            return await self.work(actor, order_text(order, self.service.status_label(order["status"])),
                                   kb([[b("← Мои заявки", "mine:0")]]), role="client", order_id=order["id"])
        if data == "clear":
            await self.db(self.service.cancel_cart, actor)
            return await self.work(actor, "Незавершённое оформление удалено.")
        if data.startswith("qty:") or data.startswith("remove:"):
            parts = data.split(":")
            cart = await self.db(self.service.cart, actor)
            if cart.get("token") != parts[1]:
                raise UserError("Эта карточка устарела. Открой корзину.")
            if parts[0] == "remove":
                await self.db(self.service.remove_item, actor, parts[2], action_id)
                return await self.show_cart(actor)
            if parts[-1] not in {"-1", "1"}:
                raise UserError("Неизвестное действие.")
            await self.db(self.service.change_qty, actor, parts[2], int(parts[3]), action_id)
            return await self.show_cart(actor)
        if data in {"checkout", "begin", "edit"}:
            # "begin" is retained for keyboards sent by the previous version.
            if not (await self.db(self.service.cart, actor)).get("items"):
                raise UserError("Сначала добавь устройство в корзину.")
            return await self.ask(actor, "name")
        if data == "back":
            profile = await self.db(self.service.profile, actor)
            return await self.advance(actor, profile.get("step", "cart"), -1)
        if data == "skip":
            profile = await self.db(self.service.profile, actor)
            step = profile.get("step")
            if step == "note":
                return await self.ask(actor, profile.get("return_step", "cart"))
            if step not in OPTIONAL:
                raise UserError("Это поле нужно заполнить.")
            await self.db(self.service.field, actor, step, "")
            return await self.advance(actor, step)
        if data.startswith("delivery:"):
            value = data.split(":", 1)[1]
            await self.db(self.service.field, actor, "delivery", value)
            return await self.advance(actor, "delivery")
        if data.startswith("note:"):
            context = data.split(":", 1)[1]
            profile = await self.db(self.service.profile, actor)
            return await self.start_note(actor, context, profile.get("step", "cart"))
        if data.startswith("submit:"):
            order = await self.db(self.service.submit, user, data[7:])
            await self.work(actor, "✅ Заявка принята. Менеджер проверит наличие и свяжется с тобой.\n\n" +
                            order_text(order, self.service.status_label(order["status"])),
                            role="client", order_id=order["id"])
            return
        if data.startswith("agree:"):
            _, order_id, revision = data.split(":")
            order = await self.db(self.service.accept_delivery, actor, order_id, int(revision))
            await self.work(actor, "✅ Стоимость доставки согласована.\n\n" +
                            order_text(order, self.service.status_label(order["status"])),
                            role="client", order_id=order["id"])
            return
        raise UserError("Кнопка устарела. Открой /start.")

    async def start_note(self, actor, context, return_step):
        await self.db(self.service.update_profile, actor,
                      {"step": "note", "note_context": context, "return_step": return_step})
        await self.work(actor, "Напиши примечание — менеджер увидит его в заявке.",
                        kb([[b("Пропустить", "skip")]]))

    async def message(self, actor, user, message, update_id):
        text = (message.get("text") or "").strip()
        if text.startswith("/start"):
            await self.welcome(actor)
            argument = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            if argument.startswith("p_"):
                await self.db(self.service.add, actor, argument[2:],
                              action_id=f"start:{self.bot_id}:{update_id}")
                return await self.show_cart(actor)
            if actor in self.service.admins:
                return await self.admin_menu(actor)
            return
        if text.split("@")[0] == "/admin":
            self.service.require_admin(actor)
            return await self.admin_menu(actor)
        if text in {"/cart", "/корзина"}:
            return await self.show_cart(actor)
        if text == "/cancel":
            await self.db(self.service.cancel_cart, actor)
            await self.welcome(actor)
            return
        profile = await self.db(self.service.profile, actor)
        step = profile.get("step", "")
        if step.startswith("admin_"):
            self.service.require_admin(actor)
            return await self.admin_input(actor, step, text)
        if step == "note":
            await self.db(self.service.note, actor, profile.get("note_context", "order_note"), text)
            return await self.ask(actor, profile.get("return_step", "cart"))
        if step in PROMPTS:
            contact = message.get("contact")
            if step == "phone" and contact:
                if contact.get("user_id") != actor:
                    raise UserError("Отправь свой контакт или введи номер получателя вручную.")
                text = contact.get("phone_number", "")
            await self.db(self.service.field, actor, step, text)
            if step == "phone":
                message_out = await self.send(actor, "Телефон сохранён.", {"remove_keyboard": True})
                await self.erase(actor, message_out["message_id"])
            return await self.advance(actor, step)
        await self.welcome(actor)
        await self.work(actor, "Выбери товар в прайсе или открой корзину.", kb([[b("🛒 Корзина", "cart")]]))

    async def admin_menu(self, actor):
        self.service.require_admin(actor)
        await self.db(self.service.update_profile, actor, {"step": "idle"})
        active = await self.db(self.service.list_orders, actor)
        await self.work(actor, f"<b>Заявки магазина</b>\nАктивных: {len(active)}", kb([
            [b("📥 Активные", "a:list:0"), b("🗄 Архив · 7 дней", "a:archive:0")],
            [b("🔎 Найти заявку", "a:search"), b("⚙️ Статусы", "a:statuses")],
            [b("📝 Приветствие", "a:welcome"), b("🛒 Режим покупателя", "cart")],
        ]))

    async def admin_list(self, actor, page=0, archive=False, query=""):
        orders = await self.db(self.service.list_orders, actor, archive=archive, query=query)
        if query:
            orders += await self.db(self.service.list_orders, actor, archive=not archive, query=query)
        page = max(0, min(page, max(0, (len(orders)-1)//8)))
        rows = [[b(f'{o["id"]} · {o["fields"]["name"][:15]} · {self.service.status_label(o["status"])}',
                   "a:o:" + o["id"])] for o in orders[page*8:(page+1)*8]]
        prefix = "a:archive:" if archive else "a:list:"
        nav = []
        if page:
            nav.append(b("←", prefix + str(page-1)))
        if (page+1)*8 < len(orders):
            nav.append(b("→", prefix + str(page+1)))
        if nav:
            rows.append(nav)
        rows.append([b("← Управление", "a:menu")])
        await self.work(actor, ("<b>Архив</b>" if archive else "<b>Активные заявки</b>") +
                        f"\nНайдено: {len(orders)}", kb(rows))

    def admin_order_keyboard(self, order):
        oid = order["id"]
        return kb([[b("🔄 Статус", "a:change:" + oid), b("🚚 Цена доставки", "a:delivery:" + oid)],
                   [b("🔒 Заметка", "a:note:" + oid), b("История", "a:history:" + oid)],
                   [b("← Заявки", "a:list:0")]])

    async def admin_order(self, actor, order_id):
        order = await self.db(self.service.get_order, actor, order_id)
        self.service.require_admin(actor)
        await self.db(self.service.update_profile, actor, {"step": "idle"})
        await self.work(actor, order_text(order, self.service.status_label(order["status"]), admin=True),
                        self.admin_order_keyboard(order), role="admin", order_id=order_id)

    async def admin_callback(self, actor, data):
        parts = data.split(":")
        action = parts[1]
        if action == "menu":
            return await self.admin_menu(actor)
        if action in {"list", "archive"}:
            return await self.admin_list(actor, int(parts[2]), action == "archive")
        if action == "o":
            return await self.admin_order(actor, parts[2])
        if action == "change":
            order = await self.db(self.service.get_order, actor, parts[2])
            rows = [[b(("✅ " if s["id"] == order["status"] else "") + s["label"],
                       f'a:set:{order["id"]}:{s["id"]}')] for s in self.service.statuses() if not s.get("hidden")]
            rows.append([b("← Заявка", "a:o:" + order["id"])])
            return await self.work(actor, "Выбери статус заявки " + order["id"], kb(rows))
        if action == "set":
            await self.db(self.service.set_status, actor, parts[2], parts[3])
            return await self.admin_order(actor, parts[2])
        if action in {"delivery", "note"}:
            order = await self.db(self.service.get_order, actor, parts[2])
            await self.db(self.service.update_profile, actor, {"step": f"admin_{action}:{order['id']}"})
            prompt = ("Укажи платную доставку числом в валюте заявки. Клиент получит итог на подтверждение."
                      if action == "delivery" else "Внутренняя заметка. Клиент её не увидит.")
            return await self.work(actor, prompt, kb([[b("← Заявка", "a:o:" + order["id"])]]))
        if action == "history":
            order = await self.db(self.service.get_order, actor, parts[2])
            lines = [f'{date(h["at"])} · {h["actor"]} · {e(h["action"])}'
                     + (" → " + e(self.service.status_label(h["to"])) if h.get("to") else "")
                     for h in order["history"]]
            return await self.work(actor, "<b>История " + order["id"] + "</b>\n" + "\n".join(lines),
                                   kb([[b("← Заявка", "a:o:" + order["id"])]]))
        if action in {"search", "welcome"}:
            await self.db(self.service.update_profile, actor, {"step": "admin_" + action})
            return await self.work(actor, "Напиши номер заявки, телефон, имя или username." if action == "search"
                                   else "Отправь новый текст постоянного приветствия (до 2500 символов).",
                                   kb([[b("← Управление", "a:menu")]]))
        if action == "statuses":
            rows = [[b(s["label"] + (" · завершает" if s["terminal"] else ""), "a:status:" + s["id"])]
                    for s in self.service.statuses()]
            rows += [[b("＋ Новый статус", "a:addstatus")], [b("← Управление", "a:menu")]]
            return await self.work(actor, "Настройка статусов", kb(rows))
        if action == "status":
            status = next((s for s in self.service.statuses() if s["id"] == parts[2]), None)
            if not status:
                raise UserError("Статус не найден.")
            sid = status["id"]
            return await self.work(actor, e(status["label"]) + ("\nЗавершает заявку" if status["terminal"] else "\nАктивная заявка"),
                kb([[b("Переименовать", "a:rename:" + sid), b("Завершение вкл/выкл", "a:terminal:" + sid)],
                    [b("↑ Выше", "a:move:" + sid + ":-1"), b("↓ Ниже", "a:move:" + sid + ":1")],
                    [b("← Статусы", "a:statuses")]]))
        if action in {"rename", "addstatus"}:
            await self.db(self.service.update_profile, actor,
                          {"step": "admin_status:" + (parts[2] if action == "rename" else "")})
            return await self.work(actor, "Напиши название статуса (до 35 символов).", kb([[b("← Статусы", "a:statuses")]]))
        if action == "terminal":
            status = next(s for s in self.service.statuses() if s["id"] == parts[2])
            await self.db(self.service.save_status, actor, status["id"], status["label"], not status["terminal"])
            return await self.admin_callback(actor, "a:statuses")
        if action == "move":
            if parts[3] not in {"-1", "1"}:
                raise UserError("Неизвестное действие")
            await self.db(self.service.move_status, actor, parts[2], int(parts[3]))
            return await self.admin_callback(actor, "a:statuses")
        raise UserError("Неизвестное действие.")

    async def admin_input(self, actor, step, text):
        self.service.require_admin(actor)
        action, _, oid = step.partition(":")
        if action == "admin_delivery":
            await self.db(self.service.delivery_price, actor, oid, text)
            return await self.admin_order(actor, oid)
        if action == "admin_note":
            await self.db(self.service.internal_note, actor, oid, text)
            return await self.admin_order(actor, oid)
        if action == "admin_status":
            current = next((s for s in self.service.statuses() if s["id"] == oid), {})
            await self.db(self.service.save_status, actor, oid, text, current.get("terminal", False))
            await self.db(self.service.update_profile, actor, {"step": "idle"})
            return await self.admin_callback(actor, "a:statuses")
        if action == "admin_welcome":
            if not 1 <= len(text) <= 2500:
                raise UserError("Приветствие: от 1 до 2500 символов.")
            await self.db(self.service.store.set, "settings", "welcome", text)
            await self.welcome(actor)
            return await self.admin_menu(actor)
        if action == "admin_search":
            await self.db(self.service.update_profile, actor, {"step": "idle"})
            return await self.admin_list(actor, query=text)

    async def dispatch_event(self, event_id, event):
        kind, payload = event["kind"], event["payload"]
        if kind in {"cleanup", "erase_order_messages"}:
            records = payload.get("messages") or [{"chat_id": payload.get("user_id"), "id": mid}
                                                  for mid in payload.get("ids", [])]
            for record in records:
                await self.erase(record["chat_id"], record["id"])
            return
        oid = payload["order_id"]
        order = await self.db(self.service.store.get, "orders", oid)
        if not order:
            return
        if kind == "order_changed":
            for record in list(order.get("messages", [])):
                if record.get("role") == "quote" and not order.get("completed_at"):
                    continue
                if order.get("completed_at"):
                    await self.erase(record["chat_id"], record["id"])
                    continue
                pages = chunks(order_text(order, self.service.status_label(order["status"]),
                                          admin=record["role"] == "admin"))
                page = record.get("page", 0)
                if page >= len(pages):
                    await self.erase(record["chat_id"], record["id"])
                    continue
                markup = self.admin_order_keyboard(order) if record["role"] == "admin" else kb([])
                try:
                    await self.api.call("editMessageText", chat_id=record["chat_id"], message_id=record["id"],
                                        text=pages[page], parse_mode="HTML",
                                        reply_markup=markup if page == len(pages)-1 else kb([]))
                except TelegramError as exc:
                    if not any(x in str(exc).lower() for x in ("not modified", "not found", "can't be edited")):
                        raise
            return
        if order.get("completed_at"):
            return
        if kind == "order_admin":
            target = int(payload["admin_id"])
            if target not in self.service.admins:
                return
            text = "📥 Новая заявка\n\n" + order_text(order, self.service.status_label(order["status"]), admin=True)
            markup = self.admin_order_keyboard(order)
            role = "admin"
        elif kind == "delivery_quote":
            if order["delivery_revision"] != payload["revision"] or order["delivery_accepted"]:
                return
            target = order["user_id"]
            total = Decimal(order["subtotal"]) + Decimal(order["delivery_price"])
            text = (f'<b>Доставка для заявки №{oid}</b>\n\n'
                    f'Товары: {cash(order["subtotal"], order["currency"])}\n'
                    f'Доставка: {cash(order["delivery_price"], order["currency"])}\n'
                    f'<b>Итого: {cash(total, order["currency"])}</b>\n\nПодтверди стоимость доставки.')
            markup = kb([[b("✅ Согласен с итоговой суммой", f'agree:{oid}:{order["delivery_revision"]}')]])
            role = "quote"
        else:
            return
        sent = event.get("sent", [])
        pages = chunks(text)
        for index, page in enumerate(pages):
            if index < len(sent):
                continue
            message = await self.send(target, page, markup if index == len(pages)-1 else None)
            sent.append(message["message_id"])
            event["sent"] = sent
            await self.db(self.service.store.set, "outbox", event_id, event)
            if role != "quote":
                await self.db(self.service.remember_message, oid, target, message["message_id"], role, index)
            else:
                # Quote messages are removed with the order but must retain their
                # revision-specific acceptance button until the buyer responds.
                await self.db(self.service.remember_message, oid, target, message["message_id"], role, index)

    async def maintenance_once(self):
        await self.db(self.service.store.housekeeping, draft_hours=self.settings.draft_hours,
                      archive_days=self.settings.archive_days)
        await self.db(self.service.statuses)
        events = await self.db(self.service.store.scan, "outbox")
        events.sort(key=lambda pair: pair[1].get("created", 0))
        eligible = [(key, event) for key, event in events if not event.get("uncertain")
                    and event.get("retry_at", 0) <= time.time()]
        for event_id, event in eligible[:40]:
            try:
                await self.dispatch_event(event_id, event)
            except AmbiguousSend:
                # Do not duplicate an order notification after an ambiguous write.
                # The persisted order is always available in the admin list.
                event["uncertain"] = True
                event["error"] = "Telegram не подтвердил отправку; проверь список заявок"
                await self.db(self.service.store.set, "outbox", event_id, event)
                log.warning("Не подтверждена отправка уведомления; заявка сохранена")
                continue
            except TelegramError as exc:
                if "bot was blocked" in str(exc).lower() or "user is deactivated" in str(exc).lower():
                    await self.db(self.delete_event, event_id)
                    continue
                event["attempts"] = event.get("attempts", 0) + 1
                event["retry_at"] = time.time() + min(1800, 10 * 2 ** min(event["attempts"], 7))
                await self.db(self.service.store.set, "outbox", event_id, event)
                continue
            await self.db(self.delete_event, event_id)

    def delete_event(self, event_id):
        with self.service.store.transaction() as tx:
            tx.delete("outbox", event_id)

    async def run(self):
        me = await self.api.call("getMe")
        self.username, self.bot_id = me["username"], me["id"]
        await self.api.call("deleteWebhook", drop_pending_updates=False)
        await self.db(self.service.statuses)
        offset = await self.db(self.service.store.get, "system", "updates:" + str(self.bot_id), 0)
        last_cleanup = 0
        while not self.stop.is_set():
            try:
                updates = await self.api.call("getUpdates", offset=offset, timeout=15,
                                             allowed_updates=["message", "callback_query"])
                for update in updates:
                    async with self.lock:
                        await self.handle(update)
                    offset = update["update_id"] + 1
                    await self.db(self.service.store.set, "system", "updates:" + str(self.bot_id), offset)
                if time.monotonic() - last_cleanup > 5:
                    async with self.lock:
                        await self.maintenance_once()
                    last_cleanup = time.monotonic()
            except asyncio.CancelledError:
                raise
            except AmbiguousSend:
                # Core writes are transactional/idempotent; acknowledge the input
                # to avoid repeating a successful action after a lost UI response.
                if "update" in locals():
                    offset = max(offset, update["update_id"] + 1)
                    await self.db(self.service.store.set, "system", "updates:" + str(self.bot_id), offset)
                log.warning("Ответ интерфейса не подтверждён Telegram")
                await asyncio.sleep(2)
            except (TelegramError, OSError, sqlite3.Error, psycopg.Error):
                log.warning("Временная ошибка Telegram/базы; повтор через 3 секунды")
                await asyncio.sleep(3)
            except Exception:
                log.exception("Ошибка обработки: заявка и история остаются в базе")
                # Skip a malformed update so it cannot block all subsequent users.
                if "update" in locals():
                    offset = max(offset, update["update_id"] + 1)
                    await self.db(self.service.store.set, "system", "updates:" + str(self.bot_id), offset)
                await asyncio.sleep(3)
