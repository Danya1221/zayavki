import asyncio
import copy
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from catalog_api import create_app, ingest, BadCatalog, StaleCatalog
from retail_store import RetailStore, Transaction, stable_id
from orders import OrderService
from settings import Settings

KEY = 'a_test_only_catalog_key_0123456789abcdef'
PRODUCT = {'id':stable_id('phone'), 'title':'iPhone 17 256 Black (1Sim+eSim)', 'brand':'Apple',
           'section':'iPhone 17', 'price':'80000', 'currency':'RUB'}


def snapshot(revision=1, **values):
    return {'protocol':1, 'revision':revision, 'operation':'snapshot', 'products':[PRODUCT],
            'confirmed':True, 'checked_at':time.time(), **values}


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RetailStore(os.getenv('TEST_DATABASE_URL') or 'sqlite:///'+self.tmp.name+'/data.db')
        with self.store.transaction() as tx:
            tx.execute('DELETE FROM retail_data')

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_and_receipt_commit_together_and_repeat_is_safe(self):
        data = snapshot()
        first = ingest(self.store, data)
        second = ingest(self.store, data)
        self.assertTrue(first['ok'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(len(self.store.scan('catalog')), 1)

    def test_catalog_and_receipt_rollback_together(self):
        ingest(self.store, snapshot())
        before = self.store.get('catalog', PRODUCT['id'])
        original = Transaction.set
        def fail_receipt(tx, namespace, key, value):
            if key=='catalog_api_receipt':
                raise RuntimeError('test failure at commit boundary')
            return original(tx,namespace,key,value)
        with patch.object(Transaction,'set',fail_receipt):
            with self.assertRaises(RuntimeError):
                ingest(self.store,snapshot(2,products=[dict(PRODUCT,price='90000')]))
        self.assertEqual(self.store.get('catalog',PRODUCT['id']),before)
        self.assertEqual(self.store.get('system','catalog_api_receipt')['revision'],1)

    def test_old_snapshot_cannot_reactivate_stale_prices(self):
        old = snapshot()
        ingest(self.store, old)
        ingest(self.store, {'protocol':1,'revision':2,'operation':'uncertain','reason':'closed'})
        with self.assertRaises(StaleCatalog):
            ingest(self.store,old)
        self.assertFalse(self.store.get('system','catalog')['confirmed'])

    def test_same_revision_with_different_content_rejected(self):
        ingest(self.store,snapshot())
        with self.assertRaises(StaleCatalog):
            ingest(self.store,snapshot(products=[dict(PRODUCT,price='999')]))

    def test_snapshot_invalidates_draft_but_freezes_submitted_order(self):
        ingest(self.store,snapshot())
        service = OrderService(self.store,[1])
        service.add(200,PRODUCT['id'])
        for field,value in {'name':'Иван Петров','phone':'+79123456789','delivery':'pickup'}.items():
            service.field(200,field,value)
        order = service.submit({'id':200},service.cart(200)['token'])
        service.add(200,PRODUCT['id'])
        changed = snapshot(2,products=[dict(PRODUCT,price='85000')])
        result = ingest(self.store,changed)
        self.assertEqual(result['cancelled_drafts'],1)
        self.assertFalse(service.cart(200))
        self.assertEqual(service.get_order(200,order['id'])['subtotal'],'80000.00')
        self.assertTrue(ingest(self.store,changed)['duplicate'])

    def test_explicit_empty_snapshot_deactivates_removed_products(self):
        ingest(self.store,snapshot())
        ingest(self.store,snapshot(2,products=[]))
        self.assertFalse(self.store.get('catalog',PRODUCT['id'])['active'])

    def test_retry_does_not_refresh_supplier_timestamp(self):
        data = snapshot(checked_at=time.time()-86400)
        ingest(self.store,data)
        ingest(self.store,data)
        self.assertEqual(self.store.get('system','catalog')['checked_at'],data['checked_at'])

    def test_invalid_snapshots_never_replace_catalog(self):
        ingest(self.store,snapshot())
        for value in (None, {}, snapshot(2,products=[PRODUCT,PRODUCT]), snapshot(2,confirmed='true'),
                      snapshot(2,checked_at=float('nan')), snapshot(2,products=[dict(PRODUCT,price='NaN')]),
                      snapshot(2,products=[dict(PRODUCT,price='-1')]), snapshot(2,products=[dict(PRODUCT,price='1.234')]),
                      snapshot(2,products=[dict(PRODUCT,id='invalid')]), snapshot(2,products=[dict(PRODUCT,currency='XYZ')]),
                      snapshot(2,catalog_url='https://evil.test/'), snapshot(2,checked_at=time.time()+3600)):
            with self.subTest(value=value):
                with self.assertRaises(BadCatalog):
                    ingest(self.store,value)
                self.assertEqual(self.store.get('catalog',PRODUCT['id'])['price'],'80000')

    def test_whitelisted_fields_prevent_remote_order_writes(self):
        ingest(self.store,snapshot(products=[dict(PRODUCT,active=False,confirmed=False,revision='hack',orders={'bad':1})]))
        product = self.store.get('catalog',PRODUCT['id'])
        self.assertTrue(product['active'])
        self.assertNotIn('orders',product)
        self.assertFalse(self.store.scan('orders'))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RetailStore('sqlite:///'+self.tmp.name+'/http.db')
        self.server = TestServer(create_app(self.store,KEY))
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.headers = {'Authorization':'Bearer '+KEY}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_unauthorized_upload_does_not_modify_database(self):
        for headers in ({},{'Authorization':'Bearer wrong'}):
            response = await self.client.post('/api/catalog/sync',json=snapshot(),headers=headers)
            self.assertEqual(response.status,401)
        self.assertFalse(self.store.scan('catalog'))

    async def test_health_contains_no_customer_information(self):
        response=await self.client.get('/health')
        self.assertEqual(await response.json(),{'ok':True,'service':'zayavki','protocol':1})
        response=await self.client.get('/orders',headers=self.headers)
        self.assertEqual(response.status,404)

    async def test_http_roundtrip_and_stale_response(self):
        data=snapshot()
        response=await self.client.post('/api/catalog/sync',json=data,headers=self.headers)
        self.assertEqual(response.status,200)
        response=await self.client.post('/api/catalog/sync',json=data,headers=self.headers)
        self.assertTrue((await response.json())['duplicate'])
        response=await self.client.post('/api/catalog/sync',json=snapshot(2,products=[]),headers=self.headers)
        self.assertEqual(response.status,200)
        response=await self.client.post('/api/catalog/sync',json=data,headers=self.headers)
        self.assertEqual(response.status,409)

    async def test_invalid_catalog_returns_actionable_detail(self):
        duplicate = snapshot(products=[PRODUCT, PRODUCT])
        response = await self.client.post('/api/catalog/sync', json=duplicate, headers=self.headers)
        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertEqual(body['error'], 'invalid_catalog')
        self.assertIn('duplicate product ID', body['detail'])
        self.assertIn(PRODUCT['id'], body['detail'])

    async def test_malformed_json_rejected_without_server_failure(self):
        response=await self.client.post('/api/catalog/sync',data='{broken',headers=self.headers)
        self.assertEqual(response.status,400)


class ConfigurationTests(unittest.TestCase):
    def test_own_database_and_no_store_document_variables_required(self):
        with patch.dict(os.environ,{'BOT_TOKEN':'test','ADMIN_IDS':'1','DATABASE_URL':'sqlite:///own.db',
                                    'SYNC_API_KEY':KEY},clear=True):
            settings=Settings.from_env()
        self.assertEqual(settings.database_url,'sqlite:///own.db')
        self.assertFalse(hasattr(settings,'seller_info'))
        self.assertFalse(hasattr(settings,'pickup_address'))

    def test_own_database_takes_priority_over_old_shared_url(self):
        with patch.dict(os.environ,{'BOT_TOKEN':'test','ADMIN_IDS':'1','DATABASE_URL':'sqlite:///own.db',
                                    'RETAIL_DATABASE_URL':'sqlite:///old.db','SYNC_API_KEY':KEY},clear=True):
            self.assertEqual(Settings.from_env().database_url,'sqlite:///own.db')

    def test_missing_sync_key_rejected(self):
        with patch.dict(os.environ,{'BOT_TOKEN':'test','ADMIN_IDS':'1','DATABASE_URL':'sqlite:///own.db'},clear=True):
            with self.assertRaisesRegex(ValueError,'SYNC_API_KEY'):
                Settings.from_env()
