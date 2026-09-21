import html
import re
from html.parser import HTMLParser
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

DELIVERY = {"courier": "Курьер", "pickup": "Самовывоз", "shipping": "Транспортная компания"}
FIELD_NAMES = {
    "name": "Имя", "phone": "Телефон", "city": "Город / населённый пункт",
    "street": "Улица / территория", "house": "Дом / корпус", "apartment": "Квартира / офис",
    "delivery_note": "Примечание к доставке", "order_note": "Примечание к заказу",
    "contact": "Контакты", "delivery": "Получение", "cart": "Корзина",
}


def e(value):
    return html.escape(str(value or ""))


def cash(value, currency="RUB"):
    if value is None:
        return "рассчитывается менеджером"
    number = Decimal(str(value))
    text = f"{number:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
    return text + " " + {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)


def date(value):
    return datetime.fromtimestamp(value, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")


def button(text, data=None, url=None):
    return {"text": text, **({"url": url} if url else {"callback_data": data})}


def keyboard(rows):
    return {"inline_keyboard": rows}


def items_text(items):
    rows = []
    for index, item in enumerate(items, 1):
        rows.append(f'{index}. <b>{e(item["title"])}</b>\n'
                    f'{item["qty"]} × {cash(item["price"], item["currency"])} = '
                    f'{cash(Decimal(item["price"]) * item["qty"], item["currency"])}')
    return "\n\n".join(rows)


def fields_text(fields, notes):
    parts = [f"{e(FIELD_NAMES.get(k, k))}: {e(v)}" for k, v in fields.items() if v]
    for context, note in notes.items():
        if note:
            parts.append(f'💬 {e(FIELD_NAMES.get(context, context))}: {e(note)}')
    return "\n".join(parts)


def order_text(order, label, *, admin=False):
    currency = order["currency"]
    text = f'<b>Заявка №{order["id"]}</b> · {date(order["created_at"])}\nСтатус: <b>{e(label)}</b>\n\n'
    text += items_text(order["items"]) + "\n\n"
    text += "Товары: <b>" + cash(order["subtotal"], currency) + "</b>\n"
    text += "Получение: " + DELIVERY[order["delivery"]] + "\n"
    text += "Доставка: " + cash(order["delivery_price"], currency) + "\n"
    if order["delivery_price"] is not None:
        total = Decimal(order["subtotal"]) + Decimal(order["delivery_price"])
        text += "<b>Итого: " + cash(total, currency) + "</b>\n"
        if order["delivery"] != "pickup":
            text += ("✅ Стоимость доставки подтверждена" if order["delivery_accepted"]
                     else "⏳ Ожидает согласования стоимости доставки") + "\n"
    text += "\n" + fields_text(order["fields"], order.get("notes", {}))
    if admin:
        user_id = order["user_id"]
        username = "@" + order["username"] if order.get("username") else "username не указан"
        text += f'\n\nTelegram: <a href="tg://user?id={user_id}">{e(username)}</a>\nID: {user_id}'
        if order["requires_confirmation"]:
            text += "\n⚠️ Наличие и цену нужно подтвердить у поставщика."
        for note in order.get("internal_notes", [])[-5:]:
            text += "\n🔒 " + e(note["text"])
    return text


def visible_units(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


class _LineSplitter(HTMLParser):
    """Split a generated HTML line while closing/reopening formatting tags."""
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.pages, self.parts, self.tags = [], [], []
        self.size = 0

    def flush(self):
        if self.size:
            self.pages.append(''.join(self.parts) + ''.join('</' + tag + '>' for tag, _ in reversed(self.tags)))
        self.parts = [raw for _, raw in self.tags]
        self.size = 0

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        self.parts.append(raw)
        self.tags.append((tag, raw))

    def handle_endtag(self, tag):
        self.parts.append('</' + tag + '>')
        if self.tags and self.tags[-1][0] == tag:
            self.tags.pop()

    def handle_data(self, data):
        for char in data:
            size = 2 if ord(char) > 0xffff else 1
            if self.size + size > self.limit:
                self.flush()
            self.parts.append(html.escape(char))
            self.size += size


def chunks(text, limit=3500):
    # Prefer complete lines. Long product titles remain intact across messages,
    # including emoji and HTML entities, with valid markup in every message.
    pages, current = [], ""
    for line in text.split("\n"):
        if visible_units(line) > limit:
            if current:
                pages.append(current.rstrip())
                current = ""
            splitter = _LineSplitter(limit)
            splitter.feed(line)
            splitter.close()
            splitter.flush()
            pages.extend(splitter.pages[:-1])
            current = splitter.pages[-1]
            continue
        candidate = (current + "\n" + line).strip("\n")
        if visible_units(candidate) > limit and current:
            pages.append(current.rstrip())
            current = line
        else:
            current = candidate
    if current:
        pages.append(current.rstrip())
    return pages
