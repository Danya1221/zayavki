import asyncio
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

from orders import OrderService, UserError, phone_number, text_field
from retail_store import RetailStore, stable_id
from settings import Settings
from bot import RequestBot
from views import chunks, order_text

USER = {"id": 200, "username": "buyer", "first_name": "Иван"}
PRODUCT = {"id": stable_id("iphone17"), "title": "iPhone 17 256 Black (SIM + eSIM)",
           "price": "79800", "currency": "RUB", "brand": "Apple", "section": "iPhone 17"}


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.url = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + self.tmp.name + "/store.db"
        self.store = RetailStore(self.url)
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM retail_data")
        self.service = OrderService(self.store, [1, 2])
        self.store.put_catalog([PRODUCT], confirmed=True)

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self, user=USER, delivery="pickup"):
        self.service.add(user["id"], PRODUCT["id"])
        for field, value in {"name": "Иван Петров", "phone": "+79123456789"}.items():
            self.service.field(user["id"], field, value)
        self.service.field(user["id"], "delivery", delivery)
        if delivery != "pickup":
            for field, value in {"city": "Москва", "street": "Тверская", "house": "12 корпус 2"}.items():
                self.service.field(user["id"], field, value)
        return self.service.cart(user["id"])["token"]

    def test_russian_phone_normalization(self):
        self.assertEqual(phone_number("8 (912) 345-67-89"), "+79123456789")
        self.assertEqual(phone_number("9123456789"), "+79123456789")
        for value in ("123", "+358401234567", "+77011234567", "abc", "+70000000000"):
            with self.assertRaises(UserError):
                phone_number(value)

    def test_addresses_accept_buildings_but_reject_garbage(self):
        self.assertEqual(text_field("house", "12А/3"), "12А/3")
        self.assertEqual(text_field("street", "СНТ Берёзка"), "СНТ Берёзка")
        for field, value in (("house", "где-то"), ("city", "123"), ("street", "http://spam.test")):
            with self.assertRaises(UserError):
                text_field(field, value)

    def test_checkout_requires_contact_fields(self):
        cart = self.service.add(200, PRODUCT["id"])
        with self.assertRaises(UserError):
            self.service.submit(USER, cart["token"])
        self.assertFalse(self.store.scan("orders"))

    def test_cart_multiple_items_and_quantities(self):
        second = dict(PRODUCT, id=stable_id("watch"), title="Apple Watch S11", price="30000")
        self.store.put_catalog([PRODUCT, second], confirmed=True)
        token = self.fill()
        self.service.add(200, second["id"])
        self.service.change_qty(200, PRODUCT["id"], 1)
        order = self.service.submit(USER, token)
        self.assertEqual(order["subtotal"], "189600.00")
        self.assertEqual(len(order["items"]), 2)

    def test_repeated_add_callback_is_idempotent(self):
        self.service.add(200, PRODUCT["id"], action_id="same")
        self.service.add(200, PRODUCT["id"], action_id="same")
        self.assertEqual(self.service.cart(200)["items"][0]["qty"], 1)

    def test_repeated_submit_returns_same_order(self):
        token = self.fill()
        first = self.service.submit(USER, token)
        second = self.service.submit(USER, token)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.scan("orders")), 1)

    def test_concurrent_submit_creates_one_order(self):
        token = self.fill()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.service.submit(USER, token), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(self.store.scan("orders")), 1)

    def test_price_change_removes_draft_and_not_order(self):
        token = self.fill()
        order = self.service.submit(USER, token)
        self.fill()
        changed = self.store.put_catalog([dict(PRODUCT, price="81000")], confirmed=True)
        self.assertEqual(changed, 1)
        self.assertFalse(self.service.cart(200))
        self.assertEqual(self.service.get_order(200, order["id"])["subtotal"], "79800.00")

    def test_item_removal_invalidates_draft_and_link(self):
        self.fill()
        self.store.put_catalog([], confirmed=True)
        self.assertFalse(self.service.cart(200))
        with self.assertRaises(UserError):
            self.service.add(200, PRODUCT["id"])

    def test_unchanged_catalog_keeps_cart(self):
        token = self.fill()
        self.store.put_catalog([PRODUCT], confirmed=True)
        self.assertEqual(self.service.cart(200)["token"], token)

    def test_closed_or_stale_supplier_still_accepts_request(self):
        token = self.fill()
        self.store.mark_uncertain("Поставщик закрыт")
        order = self.service.submit(USER, token)
        self.assertTrue(order["requires_confirmation"])
        self.assertEqual(order["status"], "new")

    def test_customer_notes_are_preserved(self):
        token = self.fill()
        self.service.note(200, "item_" + PRODUCT["id"], "Нужна белая упаковка")
        self.service.note(200, "phone", "Звонить после 12")
        self.service.field(200, "order_note", "Подарок")
        order = self.service.submit(USER, token)
        self.assertEqual(order["items"][0]["note"], "Нужна белая упаковка")
        self.assertEqual(order["notes"]["phone"], "Звонить после 12")
        self.assertEqual(order["fields"]["order_note"], "Подарок")

    def test_customer_cannot_read_or_change_other_order(self):
        order = self.service.submit(USER, self.fill())
        with self.assertRaises(UserError):
            self.service.get_order(201, order["id"])
        with self.assertRaises(PermissionError):
            self.service.set_status(200, order["id"], "completed")
        with self.assertRaises(PermissionError):
            self.service.list_orders(200)

    def test_paid_delivery_requires_buyer_acceptance(self):
        order = self.service.submit(USER, self.fill(delivery="courier"))
        self.assertIsNone(order["delivery_price"])
        with self.assertRaises(UserError):
            self.service.set_status(1, order["id"], "courier")
        order = self.service.delivery_price(1, order["id"], "700")
        with self.assertRaises(UserError):
            self.service.accept_delivery(201, order["id"], order["delivery_revision"])
        self.service.accept_delivery(200, order["id"], order["delivery_revision"])
        self.assertEqual(self.service.set_status(1, order["id"], "courier")["status"], "courier")

    def test_old_delivery_quote_cannot_be_accepted(self):
        order = self.service.submit(USER, self.fill(delivery="courier"))
        self.service.delivery_price(1, order["id"], "700")
        self.service.delivery_price(1, order["id"], "900")
        with self.assertRaises(UserError):
            self.service.accept_delivery(200, order["id"], 1)
        self.assertFalse(self.service.get_order(200, order["id"])["delivery_accepted"])

    def test_completed_order_archives_for_seven_days(self):
        order = self.service.submit(USER, self.fill())
        finished = self.service.set_status(1, order["id"], "completed")
        self.assertFalse(self.service.list_orders(1))
        self.assertEqual(len(self.service.list_orders(1, archive=True)), 1)
        self.assertAlmostEqual(finished["expires_at"] - finished["completed_at"], 7*86400)
        self.store.housekeeping(now=finished["expires_at"] + 1)
        self.assertIsNone(self.store.get("orders", order["id"]))

    def test_reopened_defect_is_not_purged(self):
        order = self.service.submit(USER, self.fill())
        completed = self.service.set_status(1, order["id"], "completed")
        self.service.set_status(1, order["id"], "defect")
        self.store.housekeeping(now=completed["expires_at"] + 1)
        self.assertEqual(self.service.get_order(1, order["id"])["status"], "defect")

    def test_draft_cleanup_preserves_welcome(self):
        self.fill()
        self.service.update_profile(200, {"welcome_id": 10, "work_ids": [11,12]})
        self.store.housekeeping(now=time.time() + 25*3600)
        self.assertFalse(self.service.cart(200))
        self.assertEqual(self.service.profile(200)["welcome_id"], 10)
        jobs = [v for _, v in self.store.scan("outbox") if v["kind"] == "cleanup"]
        self.assertEqual(jobs[0]["payload"]["ids"], [11,12])

    def test_admin_status_settings_and_history(self):
        statuses = self.service.save_status(1, "", "Ожидает поставку")
        custom = statuses[-1]["id"]
        order = self.service.submit(USER, self.fill())
        order = self.service.set_status(2, order["id"], custom)
        self.assertEqual(order["history"][-1]["actor"], 2)
        self.service.move_status(1, custom, -1)
        with self.assertRaises(PermissionError):
            self.service.save_status(200, "", "Взлом")

    def test_private_notes_not_in_customer_view(self):
        order = self.service.submit(USER, self.fill())
        self.service.internal_note(1, order["id"], "Закупка 75000")
        order = self.service.get_order(1, order["id"])
        self.assertIn("Закупка 75000", order_text(order, "Новая", admin=True))
        self.assertNotIn("Закупка 75000", order_text(order, "Новая", admin=False))


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.counter = 100
    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.counter += 1
            return {"message_id": self.counter}
        return True


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RetailStore("sqlite:///" + self.tmp.name + "/bot.db")
        self.store.put_catalog([PRODUCT], confirmed=True)
        self.service = OrderService(self.store, [1])
        self.api = FakeAPI()
        self.settings = Settings("fake", self.store.url, (1,), privacy_url="https://example.test/privacy",
                                 terms_url="https://example.test/terms", seller_info="Тестовый продавец",
                                 pickup_address="Москва, пункт выдачи")
        self.bot = RequestBot(self.service, self.settings, self.api)
        self.seq = 0

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def input(self, text="", callback=None, actor=200):
        self.seq += 1
        user = dict(USER, id=actor)
        msg = {"from": user, "chat": {"id": actor, "type": "private"},
               "message_id": self.seq, "text": text}
        update = {"update_id": self.seq, "message": msg}
        if callback:
            update = {"update_id": self.seq, "callback_query": {"id": str(self.seq),
                      "from": user, "data": callback, "message": msg}}
        await self.bot.handle(update)

    async def test_complete_customer_flow(self):
        await self.input("/start p_" + PRODUCT["id"])
        await self.input(callback="checkout")
        await self.input(callback="begin")
        await self.input("Иван Петров")
        await self.input("89123456789")
        await self.input(callback="delivery:courier")
        await self.input("Москва")
        await self.input("Тверская")
        await self.input("12А")
        await self.input(callback="skip")
        await self.input("Позвонить за час")
        await self.input("Подарочная упаковка")
        token = self.service.cart(200)["token"]
        await self.input(callback="submit:" + token)
        self.assertEqual(len(self.store.scan("orders")), 1)
        await self.bot.maintenance_once()
        admin_messages = [p for m,p in self.api.calls if m == "sendMessage" and p["chat_id"] == 1]
        self.assertTrue(admin_messages)
        self.assertTrue(any("iPhone 17" in p["text"] and "79 800" in p["text"] for p in admin_messages))
        self.assertEqual(self.store.scan("orders")[0][1]["fields"]["delivery_note"], "Позвонить за час")

    async def test_forged_admin_button_rejected(self):
        await self.input(callback="a:list:0")
        self.assertTrue(any("Нет доступа" in p.get("text", "") for _,p in self.api.calls))
        self.assertFalse(any("Активные заявки" in p.get("text", "") for _,p in self.api.calls))

    async def test_group_messages_never_expose_customer_data(self):
        await self.bot.handle({"message": {"from": USER, "chat": {"id": -100123, "type": "supergroup"},
                                          "text": "/start", "message_id": 1}})
        self.assertFalse(self.api.calls)

    async def test_welcome_is_not_duplicated(self):
        await self.input("/start")
        await self.input("/start")
        greeting = [p for m,p in self.api.calls if m == "sendMessage" and "Добро пожаловать" in p["text"]]
        self.assertEqual(len(greeting), 1)

    async def test_previous_cart_token_cannot_change_new_cart(self):
        await self.input("/start p_" + PRODUCT["id"])
        old = self.service.cart(200)["token"]
        self.service.cancel_cart(200)
        await self.input("/start p_" + PRODUCT["id"])
        await self.input(callback=f"qty:{old}:{PRODUCT['id']}:1")
        self.assertEqual(self.service.cart(200)["items"][0]["qty"], 1)


if __name__ == "__main__":
    unittest.main()
