"""SQLite storage: the household's own ledger. One connection shared across threads behind a lock."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS members (
    tg_id        INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    ingest_token TEXT NOT NULL UNIQUE,
    joined_at    TEXT NOT NULL
);

-- One row per spend. status: pending | shared | personal; kind once shared: split | full | settle
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payer        INTEGER NOT NULL,
    amount       INTEGER NOT NULL,     -- paise; the shared part when only part was shared
    total        INTEGER,              -- the full payment when only part was shared, else NULL
    description  TEXT NOT NULL,
    merchant     TEXT,                 -- as parsed from the SMS, for rules
    source       TEXT NOT NULL,        -- sms | manual | photo
    raw          TEXT,                 -- SMS text, only for parsed spends
    dedup        TEXT UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',
    kind         TEXT,
    counterparty INTEGER,              -- for full / settle
    by_rule      INTEGER NOT NULL DEFAULT 0,
    message_id   INTEGER,              -- the payer's Telegram message showing this item
    ask_message_id INTEGER,            -- the "how much was shared?" question for this item
    order_id     INTEGER,              -- order email attached to this payment
    view         TEXT,                 -- NULL: the usual prompt; 'pick': the tick list
    picked       TEXT,                 -- names of the ticked order lines, once split by ticking
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_message ON items(payer, message_id);
CREATE INDEX IF NOT EXISTS items_status ON items(status);

-- Written when an item becomes shared, so later membership changes do not rewrite history.
CREATE TABLE IF NOT EXISTS shares (
    item_id INTEGER NOT NULL REFERENCES items(id),
    tg_id   INTEGER NOT NULL,
    owed    INTEGER NOT NULL,
    PRIMARY KEY (item_id, tg_id)
);

-- The DM each other member got about a shared item, so it can be edited later.
CREATE TABLE IF NOT EXISTS notices (
    item_id    INTEGER NOT NULL,
    tg_id      INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (item_id, tg_id)
);

CREATE TABLE IF NOT EXISTS rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id    INTEGER NOT NULL,
    key      TEXT NOT NULL,
    merchant TEXT NOT NULL,
    action   TEXT NOT NULL,            -- split | personal
    UNIQUE (tg_id, key)
);

-- Bank SMS waiting to be read. The text is cleared once finished; the hash stays for a week
-- so phone retries are still recognised as duplicates.
CREATE TABLE IF NOT EXISTS inbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payer      INTEGER NOT NULL,
    hash       TEXT NOT NULL UNIQUE,
    text       TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    due_at     TEXT NOT NULL,
    done_at    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_due ON inbox(done_at, due_at);

-- One IMAP mailbox per member, read only for order emails. The password is stored as given.
CREATE TABLE IF NOT EXISTS mail_accounts (
    tg_id       INTEGER PRIMARY KEY,
    address     TEXT NOT NULL,
    password    TEXT NOT NULL,
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL,
    uidvalidity INTEGER,
    last_uid    INTEGER,
    failures    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Orders read from emails. item_id is NULL while waiting for a matching payment.
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payer       INTEGER NOT NULL,
    merchant    TEXT NOT NULL,
    total       INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    item_id     INTEGER
);
CREATE INDEX IF NOT EXISTS orders_waiting ON orders(payer, total, item_id);

CREATE TABLE IF NOT EXISTS order_lines (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    pos      INTEGER NOT NULL,
    name     TEXT NOT NULL,
    amount   INTEGER NOT NULL,
    ticked   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS order_lines_order ON order_lines(order_id, pos);
"""

_ITEM_FIELDS = {"description", "amount", "message_id", "ask_message_id", "view"}

# Columns added after the first release; CREATE TABLE IF NOT EXISTS leaves older tables as they were.
_ADDED_ITEM_COLUMNS = {"total": "INTEGER", "ask_message_id": "INTEGER", "order_id": "INTEGER",
                       "view": "TEXT", "picked": "TEXT"}

_FMT = "%Y-%m-%dT%H:%M:%SZ"


class _Rollback(Exception):
    pass


class DB:
    def __init__(self, path: str, clock: Callable[[], datetime] | None = None):
        # Every stored timestamp and every time window goes through `clock` (tests replace it).
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(items)")}
            for name, sql_type in _ADDED_ITEM_COLUMNS.items():
                if name not in have:
                    self._conn.execute(f"ALTER TABLE items ADD COLUMN {name} {sql_type}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def stamp(self, offset: timedelta = timedelta()) -> str:
        """UTC ISO timestamp for now + offset, in the format stored everywhere (sorts as text)."""
        return (self.clock() + offset).astimezone(timezone.utc).strftime(_FMT)

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # --- settings
    def get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self._run("INSERT INTO kv(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def set_default(self, key: str, value: str) -> str:
        """Store `value` unless the key exists; return whichever is stored."""
        with self._lock:
            self._run("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (key, value))
            return self.get(key)

    # --- members
    def add_member(self, tg_id: int, name: str, token: str) -> bool:
        cur = self._run("INSERT OR IGNORE INTO members(tg_id, name, ingest_token, joined_at) "
                        "VALUES(?, ?, ?, ?)", (tg_id, name, token, self.stamp()))
        return cur.rowcount == 1

    def member(self, tg_id: int):
        return self._one("SELECT * FROM members WHERE tg_id=?", (tg_id,))

    def member_by_token(self, token: str):
        return self._one("SELECT * FROM members WHERE ingest_token=?", (token,))

    def members(self) -> list:
        return self._all("SELECT * FROM members ORDER BY joined_at, tg_id")

    def rename_member(self, tg_id: int, name: str) -> None:
        self._run("UPDATE members SET name=? WHERE tg_id=?", (name, tg_id))

    # --- items
    def add_item(self, payer: int, amount: int, description: str, source: str, *,
                 merchant: str | None = None, raw: str | None = None, dedup: str | None = None) -> int | None:
        """Returns the new id, or None if this spend was already recorded."""
        ts = self.stamp()
        try:
            cur = self._run(
                "INSERT INTO items(payer, amount, description, merchant, source, raw, dedup, "
                "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (payer, amount, description, merchant, source, raw, dedup, ts, ts))
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid

    def item(self, item_id: int):
        return self._one("SELECT * FROM items WHERE id=?", (item_id,))

    def item_by_dedup(self, dedup: str):
        return self._one("SELECT * FROM items WHERE dedup=?", (dedup,))

    def item_by_message(self, payer: int, message_id: int):
        return self._one("SELECT * FROM items WHERE payer=? AND message_id=?", (payer, message_id))

    def item_by_ask(self, payer: int, message_id: int):
        return self._one("SELECT * FROM items WHERE payer=? AND ask_message_id=?", (payer, message_id))

    def unsent_items(self, created_before: str) -> list:
        """Items that should be on the payer's screen but never got there (send failed)."""
        return self._all("SELECT * FROM items WHERE message_id IS NULL AND status IN ('pending', 'shared') "
                         "AND created_at <= ? ORDER BY id", (created_before,))

    def update_item(self, item_id: int, **fields) -> None:
        unknown = set(fields) - _ITEM_FIELDS
        if unknown:
            raise ValueError(f"unknown item fields: {unknown}")
        sets = ", ".join(f"{name}=?" for name in fields)
        self._run(f"UPDATE items SET {sets}, updated_at=? WHERE id=?", (*fields.values(), self.stamp(), item_id))

    def share(self, item_id: int, kind: str, shares: dict[int, int], *,
              counterparty: int | None = None, by_rule: bool = False) -> bool:
        """Atomically pending -> shared with its share rows; False if it was not pending."""
        with self._tx() as c:
            cur = c.execute("UPDATE items SET status='shared', kind=?, counterparty=?, by_rule=?, view=NULL, "
                            "updated_at=? WHERE id=? AND status='pending'",
                            (kind, counterparty, int(by_rule), self.stamp(), item_id))
            if cur.rowcount != 1:
                return False
            c.executemany("INSERT INTO shares(item_id, tg_id, owed) VALUES(?, ?, ?)",
                          [(item_id, tg, owed) for tg, owed in shares.items()])
            return True

    def share_part(self, item_id: int, part: int, shares: dict[int, int], picked: str | None = None) -> bool:
        """Atomically pending -> split of `part` of the payment, keeping the full payment in `total`.

        False if the item was not pending or `part` exceeds its amount. All SET expressions see the
        old row, so `total` gets the original amount; sharing all of it leaves `total` NULL.
        `picked` names the ticked order lines when the part came from the tick list.
        """
        with self._tx() as c:
            cur = c.execute(
                "UPDATE items SET total=CASE WHEN ? < amount THEN amount END, amount=?, status='shared', "
                "kind='split', counterparty=NULL, by_rule=0, view=NULL, picked=?, updated_at=? "
                "WHERE id=? AND status='pending' AND ? BETWEEN 1 AND amount",
                (part, part, picked, self.stamp(), item_id, part))
            if cur.rowcount != 1:
                return False
            c.executemany("INSERT INTO shares(item_id, tg_id, owed) VALUES(?, ?, ?)",
                          [(item_id, tg, owed) for tg, owed in shares.items()])
            return True

    def mark_personal(self, item_id: int, *, by_rule: bool = False) -> bool:
        cur = self._run("UPDATE items SET status='personal', by_rule=?, view=NULL, updated_at=? "
                        "WHERE id=? AND status='pending'", (int(by_rule), self.stamp(), item_id))
        return cur.rowcount == 1

    def unshare(self, item_id: int) -> bool:
        """Atomically shared/personal -> pending, dropping its shares and restoring a part-shared
        item to its full amount; False if already pending. Order-line ticks are kept."""
        with self._tx() as c:
            cur = c.execute("UPDATE items SET status='pending', kind=NULL, counterparty=NULL, by_rule=0, "
                            "amount=COALESCE(total, amount), total=NULL, picked=NULL, updated_at=? "
                            "WHERE id=? AND status IN ('shared', 'personal')", (self.stamp(), item_id))
            if cur.rowcount != 1:
                return False
            c.execute("DELETE FROM shares WHERE item_id=?", (item_id,))
            return True

    def edit_item(self, item_id: int, amount: int, description: str,
                  shares: dict[int, int] | None = None) -> None:
        """Change amount/description; replace the shares too if the item is still shared."""
        with self._tx() as c:
            c.execute("UPDATE items SET amount=?, description=?, updated_at=? WHERE id=?",
                      (amount, description, self.stamp(), item_id))
            if shares is None:
                return
            status = c.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if status and status["status"] == "shared":
                c.execute("DELETE FROM shares WHERE item_id=?", (item_id,))
                c.executemany("INSERT INTO shares(item_id, tg_id, owed) VALUES(?, ?, ?)",
                              [(item_id, tg, owed) for tg, owed in shares.items()])

    def shares(self, item_id: int) -> dict[int, int]:
        rows = self._all("SELECT tg_id, owed FROM shares WHERE item_id=? ORDER BY tg_id", (item_id,))
        return {r["tg_id"]: r["owed"] for r in rows}

    def debts(self) -> list[tuple[int, int, int]]:
        """(debtor, creditor, amount) totals across all shared items."""
        rows = self._all(
            "SELECT s.tg_id AS debtor, i.payer AS creditor, SUM(s.owed) AS owed "
            "FROM shares s JOIN items i ON i.id = s.item_id "
            "WHERE i.status='shared' AND s.tg_id != i.payer GROUP BY s.tg_id, i.payer")
        return [(r["debtor"], r["creditor"], r["owed"]) for r in rows]

    def shared_items(self, limit: int | None = None, newest_first: bool = False) -> list:
        order = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM items WHERE status='shared' ORDER BY created_at {order}, id {order}"
        if limit is not None:
            return self._all(sql + " LIMIT ?", (limit,))
        return self._all(sql)

    def all_shares(self) -> dict[int, dict[int, int]]:
        out: dict[int, dict[int, int]] = {}
        for r in self._all("SELECT item_id, tg_id, owed FROM shares"):
            out.setdefault(r["item_id"], {})[r["tg_id"]] = r["owed"]
        return out

    # --- notices to other members
    def set_notice(self, item_id: int, tg_id: int, message_id: int) -> None:
        self._run("INSERT INTO notices(item_id, tg_id, message_id) VALUES(?, ?, ?) "
                  "ON CONFLICT(item_id, tg_id) DO UPDATE SET message_id=excluded.message_id",
                  (item_id, tg_id, message_id))

    def notices(self, item_id: int) -> dict[int, int]:
        rows = self._all("SELECT tg_id, message_id FROM notices WHERE item_id=?", (item_id,))
        return {r["tg_id"]: r["message_id"] for r in rows}

    def delete_notices(self, item_id: int) -> None:
        self._run("DELETE FROM notices WHERE item_id=?", (item_id,))

    # --- per-merchant rules
    def set_rule(self, tg_id: int, key: str, merchant: str, action: str) -> None:
        self._run("INSERT INTO rules(tg_id, key, merchant, action) VALUES(?, ?, ?, ?) "
                  "ON CONFLICT(tg_id, key) DO UPDATE SET action=excluded.action, merchant=excluded.merchant",
                  (tg_id, key, merchant, action))

    def get_rule(self, tg_id: int, key: str) -> str | None:
        row = self._one("SELECT action FROM rules WHERE tg_id=? AND key=?", (tg_id, key))
        return row["action"] if row else None

    def rules(self, tg_id: int) -> list:
        return self._all("SELECT * FROM rules WHERE tg_id=? ORDER BY merchant", (tg_id,))

    def delete_rule(self, rule_id: int, tg_id: int) -> bool:
        return self._run("DELETE FROM rules WHERE id=? AND tg_id=?", (rule_id, tg_id)).rowcount == 1

    # --- SMS inbox
    def enqueue_sms(self, payer: int, hash_: str, text: str) -> bool:
        """False if this SMS (same payer and text) is already queued or was handled this week."""
        ts = self.stamp()
        cur = self._run("INSERT OR IGNORE INTO inbox(payer, hash, text, due_at, created_at) VALUES(?, ?, ?, ?, ?)",
                        (payer, hash_, text, ts, ts))
        return cur.rowcount == 1

    def due_sms(self, limit: int = 20) -> list:
        return self._all("SELECT * FROM inbox WHERE done_at IS NULL AND due_at <= ? ORDER BY id LIMIT ?",
                         (self.stamp(), limit))

    def retry_sms(self, inbox_id: int, attempts: int, delay: timedelta) -> None:
        self._run("UPDATE inbox SET attempts=?, due_at=? WHERE id=?", (attempts, self.stamp(delay), inbox_id))

    def finish_sms(self, inbox_id: int) -> None:
        self._run("UPDATE inbox SET text=NULL, done_at=? WHERE id=?", (self.stamp(), inbox_id))

    def inbox_row(self, inbox_id: int):
        return self._one("SELECT * FROM inbox WHERE id=?", (inbox_id,))

    def purge(self) -> None:
        """Forget finished SMS after a week and orders that never matched a payment after two days."""
        with self._tx() as c:
            c.execute("DELETE FROM inbox WHERE done_at IS NOT NULL AND done_at < ?", (self.stamp(-timedelta(days=7)),))
            old = "SELECT id FROM orders WHERE item_id IS NULL AND received_at < ?"
            cutoff = self.stamp(-timedelta(days=2))
            c.execute(f"DELETE FROM order_lines WHERE order_id IN ({old})", (cutoff,))
            c.execute("DELETE FROM orders WHERE item_id IS NULL AND received_at < ?", (cutoff,))

    # --- mailboxes
    def set_mail_account(self, tg_id: int, address: str, password: str, host: str, port: int) -> None:
        """One mailbox per member; replacing it starts the UID state afresh."""
        self._run("INSERT OR REPLACE INTO mail_accounts(tg_id, address, password, host, port, created_at) "
                  "VALUES(?, ?, ?, ?, ?, ?)", (tg_id, address, password, host, port, self.stamp()))

    def mail_account(self, tg_id: int):
        return self._one("SELECT * FROM mail_accounts WHERE tg_id=?", (tg_id,))

    def mail_accounts(self) -> list:
        return self._all("SELECT * FROM mail_accounts ORDER BY tg_id")

    def delete_mail_account(self, tg_id: int) -> bool:
        return self._run("DELETE FROM mail_accounts WHERE tg_id=?", (tg_id,)).rowcount == 1

    def mail_fetched(self, tg_id: int, uidvalidity: int, last_uid: int) -> None:
        self._run("UPDATE mail_accounts SET uidvalidity=?, last_uid=?, failures=0 WHERE tg_id=?",
                  (uidvalidity, last_uid, tg_id))

    def mail_failed(self, tg_id: int) -> int:
        """Count one more failed pass; returns the consecutive failure count."""
        with self._lock:
            self._run("UPDATE mail_accounts SET failures=failures+1 WHERE tg_id=?", (tg_id,))
            row = self._one("SELECT failures FROM mail_accounts WHERE tg_id=?", (tg_id,))
            return row["failures"] if row else 0

    # --- orders from emails
    def add_order(self, payer: int, merchant: str, total: int, lines: Iterable[tuple[str, int]]) -> int:
        with self._tx() as c:
            order_id = c.execute("INSERT INTO orders(payer, merchant, total, received_at) VALUES(?, ?, ?, ?)",
                                 (payer, merchant, total, self.stamp())).lastrowid
            c.executemany("INSERT INTO order_lines(order_id, pos, name, amount) VALUES(?, ?, ?, ?)",
                          [(order_id, pos, name, amount) for pos, (name, amount) in enumerate(lines)])
            return order_id

    def order(self, order_id: int):
        return self._one("SELECT * FROM orders WHERE id=?", (order_id,))

    def order_lines(self, order_id: int) -> list:
        return self._all("SELECT * FROM order_lines WHERE order_id=? ORDER BY pos", (order_id,))

    def delete_order(self, order_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM order_lines WHERE order_id=?", (order_id,))
            c.execute("DELETE FROM orders WHERE id=?", (order_id,))

    def item_for_order(self, payer: int, full: int, window: timedelta, *, attached: bool):
        """The payer's most recent item of exactly this full payment created within ±window of now."""
        has = "IS NOT NULL" if attached else "IS NULL"
        return self._one(
            f"SELECT * FROM items WHERE payer=? AND COALESCE(total, amount)=? AND order_id {has} "
            "AND created_at BETWEEN ? AND ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (payer, full, self.stamp(-window), self.stamp(window)))

    def waiting_orders(self, payer: int, total: int, window: timedelta) -> list:
        """Unattached orders of this payer and total received within ±window of now, newest first."""
        return self._all("SELECT * FROM orders WHERE payer=? AND total=? AND item_id IS NULL "
                         "AND received_at BETWEEN ? AND ? ORDER BY received_at DESC, id DESC",
                         (payer, total, self.stamp(-window), self.stamp(window)))

    def attach_order(self, order_id: int, item_id: int) -> bool:
        """Atomically attach; False if the item already has an order or the order is taken."""
        try:
            with self._tx() as c:
                cur = c.execute("UPDATE items SET order_id=?, updated_at=? WHERE id=? AND order_id IS NULL",
                                (order_id, self.stamp(), item_id))
                if cur.rowcount != 1:
                    return False
                if c.execute("UPDATE orders SET item_id=? WHERE id=? AND item_id IS NULL",
                             (item_id, order_id)).rowcount != 1:
                    raise _Rollback
                return True
        except _Rollback:
            return False

    def toggle_line(self, order_id: int, line_id: int) -> bool:
        cur = self._run("UPDATE order_lines SET ticked=1-ticked WHERE id=? AND order_id=?", (line_id, order_id))
        return cur.rowcount == 1
