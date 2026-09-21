import unittest
from html.parser import HTMLParser

from catalog_api import ingest
import test_catalog_api as api_fixtures
from test_catalog_api import snapshot, PRODUCT
import test_orders as order_fixtures
from views import chunks, visible_units


class ApiRegressionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp, asyncTearDown = api_fixtures.ApiTests.asyncSetUp, api_fixtures.ApiTests.asyncTearDown

    async def test_malformed_optional_url_does_not_reject_prices(self):
        response = await self.client.post('/api/catalog/sync',
            json=snapshot(catalog_url='https://['), headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(self.store.get('catalog', PRODUCT['id']))

    async def test_validation_requires_key_and_does_not_mutate_catalog_or_receipt(self):
        ingest(self.store, snapshot())
        self.store.set('carts', '200', {'items': [{'product_id': PRODUCT['id']}]})
        before = {ns: self.store.scan(ns) for ns in ('catalog', 'system', 'carts', 'orders')}
        changed = snapshot(2, products=[dict(PRODUCT, price='1')])
        denied = await self.client.post('/api/catalog/validate', json=changed)
        self.assertEqual(denied.status, 401)
        accepted = await self.client.post('/api/catalog/validate', json=changed, headers=self.headers)
        self.assertEqual(accepted.status, 200)
        self.assertEqual((await accepted.json())['products'], 1)
        after = {ns: self.store.scan(ns) for ns in before}
        self.assertEqual(before, after)


class CheckoutRegressionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp, asyncTearDown, input = order_fixtures.BotFlowTests.asyncSetUp, order_fixtures.BotFlowTests.asyncTearDown, order_fixtures.BotFlowTests.input

    async def test_long_valid_catalog_title_can_be_opened_in_cart(self):
        title = 'iPhone ' + '🟠' * 2200 + ' & <test>'
        ingest(self.store, snapshot(products=[dict(PRODUCT, title=title)]))
        await self.input('/start p_' + PRODUCT['id'])
        texts = [p['text'] for method, p in self.api.calls if method == 'sendMessage']
        self.assertEqual(sum(t.count('🟠') for t in texts), 2200)
        self.assertTrue(all(visible_units(t) <= 3500 for t in texts))
        self.assertEqual(len(self.service.cart(200)['items']), 1)


class MessageSplitTests(unittest.TestCase):
    def test_long_html_line_preserves_all_text_and_balanced_links(self):
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text, self.tags = [], []
            def handle_data(self, text): self.text.append(text)
            def handle_starttag(self, tag, attrs): self.tags.append(tag)
            def handle_endtag(self, tag):
                if not self.tags or self.tags.pop() != tag:
                    raise AssertionError('Broken formatting')
        raw = '🟠 & <буква>' * 650
        from html import escape
        pages = chunks('<b><a href="https://t.me/test">' + escape(raw) + '</a></b>')
        recovered = []
        for page in pages:
            parser = Parser()
            parser.feed(page)
            self.assertFalse(parser.tags)
            recovered.extend(parser.text)
            self.assertLessEqual(visible_units(page), 3500)
        self.assertEqual(''.join(recovered), raw)
