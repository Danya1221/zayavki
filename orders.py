"""Transactional shopping cart and order workflow. Every admin action checks its actor."""
import re
import time
import uuid
from decimal import Decimal, InvalidOperation

from retail_store import clear_draft, stable_id

DEFAULT_STATUSES = [
    {"id": "new", "label": "Новая", "terminal": False},
    {"id": "working", "label": "В работе", "terminal": False},
    {"id": "assembly", "label": "Сборка", "terminal": False},
    {"id": "courier", "label": "У курьера", "terminal": False},
    {"id": "pickup", "label": "Готов к выдаче", "terminal": False},
    {"id": "completed", "label": "Завершено", "terminal": True},
    {"id": "cancelled", "label": "Отменено", "terminal": True},
    {"id": "return", "label": "Возврат", "terminal": False},
    {"id": "defect", "label": "Брак", "terminal": False},
]
FIELDS = ("name", "phone", "city", "street", "house", "apartment", "delivery_note", "order_note")


class UserError(ValueError):
    pass


def money(value, *, positive=False):
    try:
        number = Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise UserError("Укажи сумму числом, например 700") from None
    if not number.is_finite() or number < 0 or number > 10_000_000 or (positive and number <= 0):
        raise UserError("Сумма должна быть больше нуля и не превышать 10 000 000")
    if number != number.quantize(Decimal("0.01")):
        raise UserError("У суммы может быть не больше двух знаков после запятой")
    return str(number.quantize(Decimal("0.01")))


def phone_number(value):
    import phonenumbers
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("8"):
        text = "+7" + digits[1:]
    elif len(digits) == 10:
        text = "+7" + digits
    try:
        parsed = phonenumbers.parse(text, "RU")
    except phonenumbers.NumberParseException:
        raise UserError("Укажи российский номер, например +7 912 345-67-89") from None
    if not phonenumbers.is_valid_number(parsed) or phonenumbers.region_code_for_number(parsed) != "RU":
        raise UserError("Нужен корректный российский номер +7")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def text_field(field, value):
    value = re.sub(r"\s+", " ", str(value)).strip()
    limits = {"name": 100, "city": 120, "street": 160, "house": 40, "apartment": 60}
    if len(value) > limits.get(field, 500):
        raise UserError("Слишком длинное значение. Сократи текст.")
    if field == "phone":
        return phone_number(value)
    if field in {"name", "city", "street"}:
        if sum(c.isalpha() for c in value) < 2 or re.search(r"https?://|@|[<>{}]", value):
            raise UserError("Укажи понятное название буквами, без ссылок.")
    if field == "house" and not re.fullmatch(r"(?=.*\d)[\w\s/.,№#-]{1,40}", value, re.UNICODE):
        raise UserError("Укажи номер дома, например 12, 12А или 12 корпус 2.")
    return value


class OrderService:
    def __init__(self, store, admins, *, archive_days=7, draft_hours=24, fresh_seconds=1800):
        self._statuses = None
        self.store = store
        self.admins = frozenset(int(i) for i in admins)
        if not self.admins:
            raise ValueError("ADMIN_IDS обязателен: заявки должны получать назначенные администраторы")
        self.archive_days, self.draft_hours, self.fresh_seconds = archive_days, draft_hours, fresh_seconds

    def require_admin(self, actor):
        if int(actor) not in self.admins:
            raise PermissionError("Нет доступа")

    def statuses(self):
        if self._statuses is None:
            self._statuses = self.store.get("settings", "statuses", DEFAULT_STATUSES)
        return self._statuses

    def status_label(self, status):
        return next((s["label"] for s in self.statuses() if s["id"] == status), status)

    def catalog(self):
        return [p for _, p in self.store.scan("catalog") if p.get("active")]

    def profile(self, user_id):
        return self.store.get("profiles", str(user_id), {})

    def update_profile(self, user_id, values):
        with self.store.transaction() as tx:
            profile = tx.get("profiles", str(user_id), {})
            profile.update(values, updated=time.time())
            tx.set("profiles", str(user_id), profile)
            return profile

    def cart(self, user_id):
        return self.store.get("carts", str(user_id), {})

    def add(self, user_id, product_id, *, action_id=None):
        with self.store.transaction() as tx:
            if action_id and tx.get("actions", action_id):
                return tx.get("carts", str(user_id), {})
            product = tx.get("catalog", product_id)
            if not product or not product.get("active"):
                raise UserError("Эта позиция больше не доступна. Открой актуальный прайс.")
            cart = tx.get("carts", str(user_id), {})
            if not cart:
                cart = {"token": uuid.uuid4().hex[:16], "items": [], "notes": {}, "fields": {}, "delivery": ""}
            if cart["items"] and cart["items"][0]["currency"] != product["currency"]:
                raise UserError("Позиции в разных валютах нужно оформлять отдельными заказами.")
            if len(cart["items"]) >= 30 and not any(i["product_id"] == product_id for i in cart["items"]):
                raise UserError("В одной заявке можно оформить до 30 разных позиций.")
            line = next((i for i in cart["items"] if i["product_id"] == product_id), None)
            if line:
                if line["qty"] >= 20:
                    raise UserError("Для количества больше 20 свяжись с менеджером.")
                line["qty"] += 1
            else:
                cart["items"].append({"product_id": product_id, "title": product["title"],
                    "price": money(product["price"], positive=True), "currency": product["currency"],
                    "revision": product["revision"], "qty": 1, "note": ""})
            cart.update(updated=time.time())
            tx.set("carts", str(user_id), cart)
            if action_id:
                tx.set("actions", action_id, {"at": time.time()})
            return cart

    def change_qty(self, user_id, product_id, delta, action_id=None):
        with self.store.transaction() as tx:
            cart = tx.get("carts", str(user_id), {})
            if action_id and tx.get("actions", action_id):
                return cart
            for line in list(cart.get("items", [])):
                if line["product_id"] == product_id:
                    line["qty"] = min(20, line["qty"] + int(delta))
                    if line["qty"] <= 0:
                        cart["items"].remove(line)
            cart["updated"] = time.time()
            tx.set("carts", str(user_id), cart)
            if action_id:
                tx.set("actions", action_id, {"at": time.time()})
            return cart

    def remove_item(self, user_id, product_id, action_id=None):
        """Remove one cart line completely, regardless of its quantity."""
        with self.store.transaction() as tx:
            cart = tx.get("carts", str(user_id), {})
            if action_id and tx.get("actions", action_id):
                return cart
            cart["items"] = [
                line for line in cart.get("items", [])
                if line["product_id"] != product_id
            ]
            cart["updated"] = time.time()
            tx.set("carts", str(user_id), cart)
            if action_id:
                tx.set("actions", action_id, {"at": time.time()})
            return cart

    def field(self, user_id, field, value):
        if field not in FIELDS and field != "delivery":
            raise UserError("Неизвестное поле")
        if field == "delivery":
            if value not in {"courier", "pickup", "shipping"}:
                raise UserError("Выбери способ получения кнопкой.")
        else:
            value = text_field(field, value)
        with self.store.transaction() as tx:
            cart = tx.get("carts", str(user_id), {})
            if not cart.get("items"):
                raise UserError("Корзина пуста или устарела. Выбери товар в прайсе.")
            if field == "delivery":
                cart["delivery"] = value
            else:
                cart.setdefault("fields", {})[field] = value
            cart["updated"] = time.time()
            tx.set("carts", str(user_id), cart)

    def note(self, user_id, context, value):
        value = text_field("note", value)
        with self.store.transaction() as tx:
            cart = tx.get("carts", str(user_id), {})
            if not cart.get("items"):
                raise UserError("Сначала добавь товар в корзину.")
            if context.startswith("item_"):
                product_id = context[5:]
                line = next((i for i in cart["items"] if i["product_id"] == product_id), None)
                if not line:
                    raise UserError("Позиция больше не находится в корзине.")
                line["note"] = value
            else:
                cart.setdefault("notes", {})[context] = value
            cart["updated"] = time.time()
            tx.set("carts", str(user_id), cart)

    def cancel_cart(self, user_id):
        with self.store.transaction() as tx:
            clear_draft(tx, str(user_id), "cancelled")

    def validate_checkout(self, cart):
        if not cart.get("items"):
            raise UserError("Корзина пуста или устарела. Выбери товар в прайсе.")
        for field in ("name", "phone"):
            text_field(field, cart.get("fields", {}).get(field, ""))
        if cart.get("delivery") not in {"courier", "pickup", "shipping"}:
            raise UserError("Выбери способ получения.")
        if cart["delivery"] != "pickup":
            for field in ("city", "street", "house"):
                text_field(field, cart.get("fields", {}).get(field, ""))

    def submit(self, user, token):
        user_id = int(user["id"])
        submission_key = f"{user_id}:{token}"
        with self.store.transaction() as tx:
            previous = tx.get("submissions", submission_key)
            if previous:
                order = tx.get("orders", previous)
                if order:
                    return order
            cart = tx.get("carts", str(user_id), {})
            if cart.get("token") != token:
                raise UserError("Эта карточка устарела. Открой текущую корзину.")
            self.validate_checkout(cart)
            now = time.time()
            meta = tx.get("system", "catalog", {})
            confirmed = bool(meta.get("confirmed") and now - meta.get("checked_at", 0) < self.fresh_seconds)
            total = Decimal("0")
            for line in cart["items"]:
                product = tx.get("catalog", line["product_id"])
                if not product or not product.get("active") or product["revision"] != line["revision"]:
                    raise UserError("Цена или наличие изменились. Выбери товар заново в прайсе.")
                total += Decimal(line["price"]) * line["qty"]
                confirmed = confirmed and product.get("confirmed", False)
            order_id = uuid.uuid4().hex[:12].upper()
            order = {"id": order_id, "user_id": user_id, "username": user.get("username", ""),
                "telegram_name": " ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
                "items": cart["items"], "fields": cart.get("fields", {}), "notes": cart.get("notes", {}),
                "delivery": cart["delivery"], "subtotal": money(total),
                "currency": cart["items"][0]["currency"],
                "delivery_price": "0.00" if cart["delivery"] == "pickup" else None,
                "delivery_revision": 0, "delivery_accepted": cart["delivery"] == "pickup",
                "requires_confirmation": not confirmed, "status": "new", "created_at": now,
                "completed_at": None, "expires_at": None, "submission_key": submission_key,
                "internal_notes": [], "history": [{"at": now, "actor": user_id, "action": "created"}],
                "messages": []}
            tx.set("orders", order_id, order)
            tx.set("submissions", submission_key, order_id)
            clear_draft(tx, str(user_id), "submitted")
            for admin_id in self.admins:
                tx.emit("order_admin", {"order_id": order_id, "admin_id": admin_id},
                        event_id=f"new:{order_id}:{admin_id}")
            return order

    def get_order(self, actor, order_id):
        order = self.store.get("orders", order_id)
        if not order or (int(actor) not in self.admins and int(actor) != order["user_id"]):
            raise UserError("Заявка не найдена.")
        if order.get("expires_at") and order["expires_at"] <= time.time():
            raise UserError("Заявка больше не находится в оперативном архиве.")
        return order

    def customer_orders(self, actor):
        now = time.time()
        return sorted([o for _, o in self.store.scan("orders") if o["user_id"] == int(actor)
                       and (not o.get("expires_at") or o["expires_at"] > now)],
                      key=lambda o: o["created_at"], reverse=True)

    def list_orders(self, actor, *, archive=False, status=None, query=""):
        self.require_admin(actor)
        now = time.time()
        orders = [o for _, o in self.store.scan("orders")
                  if (not o.get("expires_at") or o["expires_at"] > now)
                  and bool(o.get("completed_at")) == bool(archive)]
        if status:
            orders = [o for o in orders if o["status"] == status]
        if query:
            query = query.casefold().strip()
            orders = [o for o in orders if query in (o["id"] + " " + o.get("username", "") + " " +
                      o["fields"].get("phone", "") + " " + o["fields"].get("name", "")).casefold()]
        return sorted(orders, key=lambda o: o["created_at"], reverse=True)

    def set_status(self, actor, order_id, status):
        self.require_admin(actor)
        with self.store.transaction() as tx:
            order = tx.get("orders", order_id)
            if not order or (order.get("expires_at") and order["expires_at"] <= time.time()):
                raise UserError("Заявка не найдена.")
            settings = tx.get("settings", "statuses", DEFAULT_STATUSES)
            definition = next((s for s in settings if s["id"] == status and not s.get("hidden")), None)
            if not definition:
                raise UserError("Статус больше не доступен.")
            if status in {"courier", "completed"} and order["delivery"] != "pickup" and not order["delivery_accepted"]:
                raise UserError("Сначала укажи стоимость доставки и получи подтверждение клиента.")
            if order["status"] == status:
                return order
            now = time.time()
            order["history"].append({"at": now, "actor": int(actor),
                                     "action": "status", "from": order["status"], "to": status})
            order["status"] = status
            if definition.get("terminal"):
                order["completed_at"] = now
                order["expires_at"] = now + self.archive_days * 86400
            else:
                order["completed_at"] = order["expires_at"] = None
            tx.set("orders", order_id, order)
            tx.emit("order_changed", {"order_id": order_id})
            return order

    def delivery_price(self, actor, order_id, value):
        self.require_admin(actor)
        price = money(value, positive=True)
        with self.store.transaction() as tx:
            order = tx.get("orders", order_id)
            if not order or order.get("completed_at"):
                raise UserError("Активная заявка не найдена.")
            if order["delivery"] == "pickup":
                raise UserError("Для самовывоза доставка не начисляется.")
            order.update(delivery_price=price, delivery_accepted=False,
                         delivery_revision=order["delivery_revision"] + 1)
            order["history"].append({"at": time.time(), "actor": int(actor), "action": "delivery_price", "value": price})
            tx.set("orders", order_id, order)
            tx.emit("delivery_quote", {"order_id": order_id, "revision": order["delivery_revision"]})
            tx.emit("order_changed", {"order_id": order_id})
            return order

    def accept_delivery(self, actor, order_id, revision):
        with self.store.transaction() as tx:
            order = tx.get("orders", order_id)
            if not order or order["user_id"] != int(actor):
                raise UserError("Заявка не найдена.")
            if order.get("completed_at"):
                raise UserError("Заявка уже завершена.")
            if order["delivery_price"] is None or order["delivery_revision"] != int(revision):
                raise UserError("Стоимость доставки обновилась. Используй последнюю карточку.")
            if not order["delivery_accepted"]:
                order["delivery_accepted"] = True
                order["history"].append({"at": time.time(), "actor": int(actor), "action": "delivery_accepted"})
                tx.set("orders", order_id, order)
                tx.emit("order_changed", {"order_id": order_id})
            return order

    def internal_note(self, actor, order_id, text):
        self.require_admin(actor)
        text = text_field("note", text)
        if not text:
            raise UserError("Заметка не может быть пустой.")
        with self.store.transaction() as tx:
            order = tx.get("orders", order_id)
            if not order or (order.get("expires_at") and order["expires_at"] <= time.time()):
                raise UserError("Заявка не найдена.")
            order["internal_notes"].append({"actor": int(actor), "at": time.time(), "text": text})
            tx.set("orders", order_id, order)
            tx.emit("order_changed", {"order_id": order_id})

    def save_status(self, actor, status_id, label, terminal=False):
        self.require_admin(actor)
        label = text_field("status", label)
        if not 1 <= len(label) <= 35:
            raise UserError("Название статуса: от 1 до 35 символов.")
        with self.store.transaction() as tx:
            statuses = tx.get("settings", "statuses", DEFAULT_STATUSES)
            if status_id:
                target = next((s for s in statuses if s["id"] == status_id), None)
                if not target:
                    raise UserError("Статус не найден.")
                if status_id == "new" and terminal:
                    raise UserError("Начальный статус не может завершать заявку.")
                # Do not silently archive active orders when editing a definition.
                if target["terminal"] != bool(terminal) and any(
                        o["status"] == status_id for _, o in tx.scan("orders")):
                    raise UserError("Сначала переведи заявки из этого статуса в другой.")
                target.update(label=label, terminal=bool(terminal))
            else:
                if len(statuses) >= 20:
                    raise UserError("Можно настроить до 20 статусов.")
                statuses.append({"id": "s" + uuid.uuid4().hex[:8], "label": label, "terminal": bool(terminal)})
            tx.set("settings", "statuses", statuses)
            self._statuses = None
            return statuses

    def move_status(self, actor, status_id, delta):
        self.require_admin(actor)
        with self.store.transaction() as tx:
            statuses = tx.get("settings", "statuses", DEFAULT_STATUSES)
            index = next((i for i, s in enumerate(statuses) if s["id"] == status_id), None)
            if index is None:
                raise UserError("Статус не найден.")
            target = max(0, min(len(statuses) - 1, index + int(delta)))
            statuses.insert(target, statuses.pop(index))
            tx.set("settings", "statuses", statuses)
            self._statuses = None

    def remember_message(self, order_id, chat_id, message_id, role, page=0):
        with self.store.transaction() as tx:
            order = tx.get("orders", order_id)
            if not order:
                return
            entry = {"chat_id": int(chat_id), "id": int(message_id), "role": role, "page": int(page)}
            if entry not in order["messages"]:
                order["messages"].append(entry)
                tx.set("orders", order_id, order)
