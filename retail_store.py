"""Versioned shared contract for rozniysa and zayavki. No Telegram side effects."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCHEMA_VERSION = 1
LOCK_ID = 823740199610


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class Transaction:
    def __init__(self, connection, sqlite):
        self.connection = connection
        self.sqlite = sqlite

    def execute(self, sql, args=()):
        return self.connection.execute(sql.replace("%s", "?") if self.sqlite else sql, args)

    def get(self, namespace, key, default=None):
        row = self.execute("SELECT value FROM retail_data WHERE namespace=%s AND key=%s",
                           (namespace, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, namespace, key, value):
        self.execute(
            "INSERT INTO retail_data(namespace,key,value,updated) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value,updated=excluded.updated",
            (namespace, str(key), dump(value), time.time()))

    def delete(self, namespace, key):
        self.execute("DELETE FROM retail_data WHERE namespace=%s AND key=%s", (namespace, str(key)))

    def scan(self, namespace):
        return [(key, json.loads(value)) for key, value in self.execute(
            "SELECT key,value FROM retail_data WHERE namespace=%s ORDER BY key", (namespace,)).fetchall()]

    def emit(self, kind, payload, event_id=None):
        key = event_id or uuid.uuid4().hex
        if self.get("outbox", key) is None:
            self.set("outbox", key, {"kind": kind, "payload": payload, "created": time.time(),
                                     "attempts": 0, "retry_at": 0})
        return key


class RetailStore:
    def __init__(self, url):
        if not url:
            raise ValueError("Укажи общий RETAIL_DATABASE_URL для обоих ботов")
        self.url = url
        self.sqlite = url.startswith("sqlite:///")
        self.path = url[len("sqlite:///"):] if self.sqlite else ""
        with self.transaction() as tx:
            tx.execute("CREATE TABLE IF NOT EXISTS retail_data ("
                       "namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,"
                       "updated DOUBLE PRECISION NOT NULL,PRIMARY KEY(namespace,key))")
            version = tx.get("system", "schema_version", SCHEMA_VERSION)
            if version != SCHEMA_VERSION:
                raise RuntimeError("Несовместимая версия общей базы: обнови оба бота")
            tx.set("system", "schema_version", SCHEMA_VERSION)

    def connect(self):
        if self.sqlite:
            connection = sqlite3.connect(self.path, timeout=20)
            connection.execute("PRAGMA busy_timeout=20000")
            return connection
        import psycopg
        return psycopg.connect(self.url, connect_timeout=10,
                               options="-c statement_timeout=20000 -c lock_timeout=20000")

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            if self.sqlite:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            yield Transaction(connection, self.sqlite)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, namespace, key, default=None):
        with self.transaction() as tx:
            return tx.get(namespace, key, default)

    def set(self, namespace, key, value):
        with self.transaction() as tx:
            tx.set(namespace, key, value)

    def scan(self, namespace):
        with self.transaction() as tx:
            return tx.scan(namespace)

    def put_catalog(self, products, *, confirmed, checked_at=None):
        """Atomically replace the visible selection, preserving removed-product tombstones.

        Product IDs do not contain the price. Already submitted orders are immutable
        snapshots and are deliberately not touched by this method.
        """
        now = time.time() if checked_at is None else checked_at
        current = {p["id"]: dict(p) for p in products}
        if len(current) != len(products):
            raise ValueError("Повторяющийся идентификатор позиции")
        with self.transaction() as tx:
            old = dict(tx.scan("catalog"))
            changed = set()
            for product_id, product in current.items():
                product["active"] = True
                product["revision"] = stable_id(dump([
                    product["title"], product["price"], product["currency"], True]))
                product["checked_at"] = now
                product["confirmed"] = bool(confirmed)
                if old.get(product_id, {}).get("revision") != product["revision"]:
                    changed.add(product_id)
                tx.set("catalog", product_id, product)
            for product_id, product in old.items():
                if product_id not in current and product.get("active"):
                    product.update(active=False, checked_at=now,
                                   revision=stable_id(product["revision"] + ":removed"))
                    tx.set("catalog", product_id, product)
                    changed.add(product_id)
            cancelled = 0
            if changed:
                for user_id, cart in tx.scan("carts"):
                    if any(line["product_id"] in changed for line in cart.get("items", [])):
                        clear_draft(tx, user_id, "price_changed")
                        cancelled += 1
            tx.set("system", "catalog", {"checked_at": now, "confirmed": bool(confirmed),
                                         "count": len(products), "version": SCHEMA_VERSION})
            return cancelled

    def mark_uncertain(self, reason):
        with self.transaction() as tx:
            meta = tx.get("system", "catalog", {})
            meta.update(confirmed=False, error=str(reason)[:300], attempted_at=time.time())
            tx.set("system", "catalog", meta)

    def housekeeping(self, *, draft_hours=24, archive_days=7, now=None):
        now = now or time.time()
        cleared = 0
        with self.transaction() as tx:
            for user_id, cart in tx.scan("carts"):
                if cart.get("updated", 0) <= now - draft_hours * 3600:
                    clear_draft(tx, user_id, "expired")
                    cleared += 1
            for order_id, order in tx.scan("orders"):
                expires = order.get("expires_at")
                if expires and expires <= now:
                    tx.emit("erase_order_messages", {"order_id": order_id,
                            "messages": order.get("messages", [])})
                    tx.delete("orders", order_id)
                    tx.delete("submissions", order.get("submission_key", ""))
            for key, action in tx.scan("actions"):
                if action.get("at", 0) < now - 2 * 86400:
                    tx.delete("actions", key)
            for key, event in tx.scan("outbox"):
                oid = event.get("payload", {}).get("order_id")
                if oid and event.get("kind") != "erase_order_messages" and not tx.get("orders", oid):
                    tx.delete("outbox", key)
            # Inactive profiles keep only the persistent greeting and Telegram ID.
            for user_id, profile in tx.scan("profiles"):
                if profile.get("updated", 0) < now - draft_hours * 3600 and not tx.get("carts", user_id):
                    keep = {k: v for k, v in profile.items() if k in {"welcome_id", "welcome_hash"}}
                    if profile.get("work_ids"):
                        tx.emit("cleanup", {"user_id": int(user_id), "ids": profile["work_ids"]})
                    if keep != profile:
                        tx.set("profiles", user_id, keep)
            return cleared


def clear_draft(tx, user_id, reason):
    profile = tx.get("profiles", str(user_id), {})
    ids = profile.pop("work_ids", [])
    keep = {k: v for k, v in profile.items() if k in {"welcome_id", "welcome_hash"}}
    keep.update(step="idle", updated=time.time())
    tx.set("profiles", str(user_id), keep)
    tx.delete("carts", str(user_id))
    tx.emit("cleanup", {"user_id": int(user_id), "ids": ids, "reason": reason})


class ProcessLease:
    """A dedicated connection owns the worker lock; loss must stop the worker."""
    def __init__(self, store, name):
        self.store, self.name, self.connection, self.handle = store, name, None, None
        self.key = int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)

    def acquire(self):
        if self.store.sqlite:
            import fcntl
            self.handle = open(self.store.path + "." + self.name + ".lock", "a+")
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                self.handle.close()
                self.handle = None
                return False
        self.connection = self.store.connect()
        self.connection.autocommit = True
        ok = self.connection.execute("SELECT pg_try_advisory_lock(%s)", (self.key,)).fetchone()[0]
        if not ok:
            self.connection.close()
            self.connection = None
        return bool(ok)

    def check(self):
        if not self.store.sqlite:
            if self.connection is None or self.connection.closed:
                raise RuntimeError("Потеряно соединение, удерживающее блокировку процесса")
            self.connection.execute("SELECT 1")

    def close(self):
        if self.connection is not None:
            self.connection.close()
        if self.handle is not None:
            self.handle.close()
