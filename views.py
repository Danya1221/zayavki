import html
import re
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
        if item.get("note"):
            rows.append("💬 К товару: " + e(item["note"]))
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


def chunks(text, limit=3500):
    # All generated tags close on their line; never split an entity or tag.
    pages, current = [], ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line).strip("\n")
        if visible_units(candidate) > limit and current:
            pages.append(current.rstrip())
            current = line
        else:
            current = candidate
        if visible_units(current) > limit:
            raise ValueError("Одна строка превышает лимит Telegram")
    if current:
        pages.append(current.rstrip())
    return pages
