import time
import unittest
from unittest.mock import AsyncMock

import test_orders as fixtures
from test_orders import PRODUCT, USER
from retail_store import stable_id
from orders import UserError
from telegram_api import TelegramError
from views import chunks, visible_units, order_text


class ReliabilityTests(unittest.TestCase):
    setUp, tearDown, fill = fixtures.DomainTests.setUp, fixtures.DomainTests.tearDown, fixtures.DomainTests.fill

    def test_order_needs_no_seller_documents_or_terms_confirmation(self):
        order = self.service.submit(USER, self.fill())
        self.assertEqual(order['status'], 'new')
        self.assertNotIn('terms', order)

    def test_customer_order_list_is_scoped_to_owner(self):
        self.service.submit(USER,self.fill())
        self.assertEqual(len(self.service.customer_orders(200)),1)
        self.assertFalse(self.service.customer_orders(201))

    def test_oversized_notes_split_without_losing_fields(self):
        token = self.fill()
        for field in ['name','phone','city','street','house','apartment','delivery','order_note']:
            self.service.note(200,field,'📝' * 500)
        order = self.service.submit(USER,token)
        text = order_text(order,'Новая',admin=True)
        pages = chunks(text)
        self.assertGreater(len(pages),1)
        self.assertTrue(all(visible_units(page)<=3500 for page in pages))
        self.assertEqual(sum(page.count('📝') for page in pages),4000)

    def test_expired_status_callback_cannot_reopen_purged_order(self):
        order = self.service.submit(USER,self.fill())
        order = self.service.set_status(1,order['id'],'completed')
        order['expires_at'] = time.time()-1
        self.store.set('orders',order['id'],order)
        with self.assertRaises(UserError):
            self.service.set_status(1,order['id'],'defect')

    def test_idempotency_receipts_expire(self):
        self.store.set('actions','old',{'at':time.time()-3*86400})
        self.store.set('actions','recent',{'at':time.time()})
        self.store.housekeeping()
        self.assertIsNone(self.store.get('actions','old'))
        self.assertIsNotNone(self.store.get('actions','recent'))

    def test_html_entity_length_is_counted_as_visible_text(self):
        self.assertEqual(len(chunks('&amp;'*2000)),1)


class BotReliabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp, asyncTearDown, input = fixtures.BotFlowTests.asyncSetUp, fixtures.BotFlowTests.asyncTearDown, fixtures.BotFlowTests.input

    async def test_uncertain_notifications_do_not_starve_cleanup(self):
        for i in range(45):
            self.store.set('outbox',str(i),{'kind':'order_admin','payload':{'order_id':'existing'},
                'created':0,'uncertain':True})
        # Keep uncertain records in this test; real housekeeping removes records for missing orders.
        self.store.housekeeping = lambda **kwargs: 0
        self.store.set('outbox','cleanup',{'kind':'cleanup','payload':{'user_id':200,'ids':[700]},'created':time.time()})
        await self.bot.maintenance_once()
        self.assertTrue(any(m=='deleteMessage' and p['message_id']==700 for m,p in self.api.calls))

    async def test_thirty_item_cart_keyboard_stays_within_limits(self):
        products = [dict(PRODUCT,id=stable_id(str(i)),title='Телефон '+str(i)) for i in range(30)]
        self.store.put_catalog(products,confirmed=True)
        for p in products:
            self.service.add(200,p['id'])
        await self.bot.show_cart(200)
        markup = [p['reply_markup'] for m,p in self.api.calls if m=='sendMessage' and p.get('reply_markup')][-1]
        self.assertLess(sum(len(row) for row in markup['inline_keyboard']),100)
        self.assertTrue(any(b.get('callback_data')=='cartpage:1' for row in markup['inline_keyboard'] for b in row))

    async def test_cart_has_no_per_item_comment_buttons(self):
        self.store.put_catalog([PRODUCT], confirmed=True)
        self.service.add(200, PRODUCT['id'])
        await self.bot.show_cart(200)
        markup = [p['reply_markup'] for m,p in self.api.calls if m=='sendMessage' and p.get('reply_markup')][-1]
        callbacks = [
            button.get('callback_data', '')
            for row in markup['inline_keyboard']
            for button in row
        ]
        self.assertFalse(any(value.startswith('itemnote:') for value in callbacks))
        self.assertTrue(any(value == 'note:cart' for value in callbacks))

    async def test_cart_has_full_remove_button_and_separate_checkout_clear_rows(self):
        self.store.put_catalog([PRODUCT], confirmed=True)
        self.service.add(200, PRODUCT['id'])
        self.service.add(200, PRODUCT['id'])
        await self.bot.show_cart(200)
        markup = [p['reply_markup'] for m,p in self.api.calls if m=='sendMessage' and p.get('reply_markup')][-1]
        rows = markup['inline_keyboard']

        item_row = rows[0]
        self.assertEqual(len(item_row), 4)
        self.assertEqual(item_row[-1]['text'], '🗑')
        remove_callback = item_row[-1]['callback_data']
        self.assertTrue(remove_callback.startswith('remove:'))

        checkout_rows = [row for row in rows if any(b.get('callback_data') == 'checkout' for b in row)]
        clear_rows = [row for row in rows if any(b.get('callback_data') == 'clear' for b in row)]
        self.assertEqual(len(checkout_rows), 1)
        self.assertEqual(len(clear_rows), 1)
        self.assertEqual(len(checkout_rows[0]), 1)
        self.assertEqual(len(clear_rows[0]), 1)

        token = self.service.cart(200)['token']
        self.service.remove_item(200, PRODUCT['id'])
        self.assertEqual(self.service.cart(200)['items'], [])

    async def test_old_item_notes_are_not_shown_in_cart_text(self):
        from views import items_text
        item = dict(PRODUCT, qty=1, note='старый комментарий')
        text = items_text([item])
        self.assertNotIn('К товару', text)
        self.assertNotIn('старый комментарий', text)

    async def test_order_older_than_48_hours_is_redacted(self):
        original = self.api.call
        async def api(method,**payload):
            if method=='deleteMessage':
                raise TelegramError("message can't be deleted")
            return await original(method,**payload)
        self.api.call=api
        await self.bot.erase(200,77)
        self.assertTrue(any(m=='editMessageText' and p['text']=='Эта карточка завершена.' for m,p in self.api.calls))

    async def test_customer_cannot_open_other_customers_card(self):
        token = fixtures.DomainTests.fill(self)
        order = self.service.submit(USER,token)
        await self.input(callback='own:'+order['id'],actor=201)
        sent = [p['text'] for m,p in self.api.calls if m=='sendMessage']
        self.assertTrue(any('не найдена' in t for t in sent))
        self.assertFalse(any('+79123456789' in t for t in sent))

    async def test_deleted_persistent_greeting_is_restored(self):
        await self.input('/start')
        original = self.api.call
        async def api(method,**payload):
            if method=='editMessageText':
                raise TelegramError('message to edit not found')
            return await original(method,**payload)
        self.api.call=api
        await self.input('/start')
        self.assertEqual(sum(m=='sendMessage' and 'Добро пожаловать' in p['text'] for m,p in self.api.calls),2)
