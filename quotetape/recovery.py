import json
import sqlite3

from .books import checked_orders, encode_orders

# Tables owned by the receiver.  ``raw_events`` and ``books`` are the same
# tables used by quotetape.raw.Tape and quotetape.books.BookStore, so the
# original retention and publication paths stay compatible.
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS raw_events(id INTEGER PRIMARY KEY, body TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS books('
    'symbol TEXT PRIMARY KEY, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, '
    'body TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS recovery_state(key TEXT PRIMARY KEY, value INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS recovery_symbols('
    'symbol TEXT PRIMARY KEY, image_epoch INTEGER, position INTEGER)',
    'CREATE TABLE IF NOT EXISTS recovery_seen('
    'epoch INTEGER NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(epoch, seq))',
    'CREATE TABLE IF NOT EXISTS recovery_pending('
    'epoch INTEGER NOT NULL, seq INTEGER NOT NULL, symbol TEXT NOT NULL, '
    'body TEXT NOT NULL, PRIMARY KEY(epoch, seq, symbol))',
    'CREATE TABLE IF NOT EXISTS recovery_images('
    'epoch INTEGER NOT NULL, symbol TEXT NOT NULL, image TEXT NOT NULL, '
    'cut INTEGER NOT NULL, parts INTEGER NOT NULL, '
    'PRIMARY KEY(epoch, symbol, image))',
    'CREATE TABLE IF NOT EXISTS recovery_image_parts('
    'epoch INTEGER NOT NULL, symbol TEXT NOT NULL, image TEXT NOT NULL, '
    'part INTEGER NOT NULL, body TEXT NOT NULL, '
    'PRIMARY KEY(epoch, symbol, image, part))',
)


class Receiver:
    """Incremental feed receiver with sharded snapshots and recovery state.

    Everything needed across a close/reopen lives in SQLite: raw events,
    per-symbol confirmed positions, the packages awaiting a gap, and the
    shards of incomplete images.  The object itself keeps no feed history,
    so resident memory is independent of how much has been confirmed.
    """

    def __init__(self, db, symbols):
        if (not isinstance(symbols, (list, tuple)) or not symbols
                or any(not isinstance(symbol, str) or not symbol for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError('invalid symbols')
        self.symbols = tuple(symbols)
        self.closed = False
        self.connection = sqlite3.connect(db)
        for statement in SCHEMA:
            self.connection.execute(statement)
        self.connection.commit()
        with self.connection:
            for symbol in self.symbols:
                self.connection.execute(
                    'INSERT OR IGNORE INTO recovery_symbols'
                    '(symbol, image_epoch, position) VALUES (?,?,?)',
                    (symbol, None, None),
                )

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        if not self.closed:
            self.connection.close()
            self.closed = True

    # -- small state helpers ----------------------------------------------
    def _state(self, key):
        row = self.connection.execute(
            'SELECT value FROM recovery_state WHERE key=?', (key,)
        ).fetchone()
        return None if row is None else row[0]

    def _set_state(self, key, value):
        self.connection.execute(
            'INSERT INTO recovery_state(key,value) VALUES (?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, value),
        )

    def _retain(self, event):
        body = json.dumps(event, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))
        self.connection.execute(
            'INSERT INTO raw_events(body) VALUES (?)', (body,)
        )

    def _observe(self, seq):
        watermark = self._state('h') or 0
        if seq > watermark:
            self._set_state('h', seq)

    def _add_work(self, amount):
        if amount:
            self.connection.execute(
                'INSERT INTO recovery_state(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=value+excluded.value',
                ('work', amount),
            )

    def work(self):
        value = self._state('work')
        return {'delta_entries_applied': 0 if value is None else value}

    def _publish(self, symbol, epoch, seq, orders):
        self.connection.execute(
            'INSERT INTO books VALUES (?,?,?,?) '
            'ON CONFLICT(symbol) DO UPDATE SET '
            'epoch=excluded.epoch,seq=excluded.seq,body=excluded.body',
            (symbol, epoch, seq, encode_orders(orders)),
        )

    def _identity(self, epoch, symbol):
        """Return the symbol's confirmed position for ``epoch`` or None."""
        row = self.connection.execute(
            'SELECT image_epoch,position FROM recovery_symbols WHERE symbol=?',
            (symbol,),
        ).fetchone()
        if row is None or row[0] != epoch:
            return None
        return row[1]

    # -- ingestion ---------------------------------------------------------
    def feed(self, event):
        if self.closed:
            raise ValueError('receiver is closed')
        kind = event.get('kind')
        if kind == 'epoch':
            self._feed_epoch(event)
        elif kind == 'delta':
            self._feed_delta(event)
        elif kind == 'snapshot':
            self._feed_snapshot(event)
        else:
            raise ValueError('unknown event kind')

    def _feed_epoch(self, event):
        epoch = event.get('epoch')
        if type(epoch) is not int or epoch <= 0:
            raise ValueError('invalid epoch')
        current = self._state('epoch')
        if current is not None and epoch <= current:
            # Replaying the current epoch header is harmless; an older epoch
            # and all of its messages are ignored by the state machine.
            with self.connection:
                self._retain(event)
            return
        with self.connection:
            self._retain(event)
            self._set_state('epoch', epoch)
            self._set_state('h', 0)
            self.connection.execute('DELETE FROM recovery_seen')
            self.connection.execute('DELETE FROM recovery_pending')
            self.connection.execute('DELETE FROM recovery_images')
            self.connection.execute('DELETE FROM recovery_image_parts')
            self.connection.execute(
                'UPDATE recovery_symbols SET image_epoch=NULL, position=NULL'
            )

    def _feed_delta(self, event):
        epoch = event.get('epoch')
        seq = event.get('seq')
        changes = event.get('changes')
        if (type(epoch) is not int or type(seq) is not int or seq < 1
                or not isinstance(changes, list)):
            raise ValueError('invalid delta')
        current = self._state('epoch')
        if current is None:
            raise ValueError('delta before epoch')
        if epoch < current:
            with self.connection:
                self._retain(event)
            return
        if epoch > current:
            raise ValueError('delta before epoch event')
        grouped = self._group_changes(changes)
        with self.connection:
            self._retain(event)
            inserted = self.connection.execute(
                'INSERT OR IGNORE INTO recovery_seen(epoch,seq) VALUES (?,?)',
                (epoch, seq),
            ).rowcount
            if inserted:
                self._observe(seq)
                self._store_pending(epoch, seq, grouped)
                for symbol in self.symbols:
                    self._advance(epoch, symbol)
                self._prune(epoch)

    def _group_changes(self, changes):
        grouped = {}
        for item in changes:
            if not isinstance(item, (list, tuple)) or len(item) != 4:
                raise ValueError('invalid change')
            symbol, operation, order_id, value = item
            if symbol not in self.symbols:
                raise ValueError('unknown symbol')
            if operation == 'put':
                checked = checked_orders({order_id: value})
                grouped.setdefault(symbol, []).append(
                    ['put', order_id, checked[order_id]]
                )
            elif operation == 'del':
                if value is not None or not isinstance(order_id, str) or not order_id:
                    raise ValueError('invalid delete')
                grouped.setdefault(symbol, []).append(['del', order_id, None])
            else:
                raise ValueError('invalid operation')
        return grouped

    def _store_pending(self, epoch, seq, grouped):
        """Keep a package for a symbol until a confirmed image covers it.

        A package is kept whenever the symbol has not confirmed a position at
        or past it.  This includes symbols with no image yet: a future image
        may end before this package, and then only the stored suffix can be
        replayed onto the new image.
        """
        for symbol, operations in grouped.items():
            position = self._identity(epoch, symbol)
            if position is not None and seq <= position:
                continue
            self.connection.execute(
                'INSERT OR IGNORE INTO recovery_pending(epoch,seq,symbol,body) '
                'VALUES (?,?,?,?)',
                (epoch, seq, symbol,
                 json.dumps(operations, separators=(',', ':'))),
            )

    # -- contiguous suffix application ------------------------------------
    def _advance(self, epoch, symbol):
        position = self._identity(epoch, symbol)
        if position is None:
            # Without a complete image of this epoch there is nothing that a
            # delta may legitimately modify.
            return
        if self.connection.execute(
                'SELECT 1 FROM recovery_seen WHERE epoch=? AND seq=?',
                (epoch, position + 1),
        ).fetchone() is None:
            return
        row = self.connection.execute(
            'SELECT body FROM books WHERE symbol=?', (symbol,)
        ).fetchone()
        orders = {} if row is None else json.loads(row[0])
        applied = 0
        advanced = False
        while self.connection.execute(
                'SELECT 1 FROM recovery_seen WHERE epoch=? AND seq=?',
                (epoch, position + 1),
        ).fetchone() is not None:
            position += 1
            advanced = True
            pending = self.connection.execute(
                'SELECT body FROM recovery_pending '
                'WHERE epoch=? AND seq=? AND symbol=?',
                (epoch, position, symbol),
            ).fetchone()
            if pending is not None:
                operations = json.loads(pending[0])
                for operation, order_id, value in operations:
                    if operation == 'put':
                        orders[order_id] = list(value)
                    else:
                        orders.pop(order_id, None)
                applied += len(operations)
                self.connection.execute(
                    'DELETE FROM recovery_pending '
                    'WHERE epoch=? AND seq=? AND symbol=?',
                    (epoch, position, symbol),
                )
        if advanced:
            self._publish(symbol, epoch, position, orders)
            self.connection.execute(
                'UPDATE recovery_symbols SET position=? WHERE symbol=?',
                (position, symbol),
            )
            self._add_work(applied)
            # Images ending at or before the confirmed position can never be
            # installed any more; drop any remaining shards of them.
            self._discard_images(epoch, symbol, position)

    def _prune(self, epoch):
        """Forget package state that no managed symbol can still need."""
        # Per-symbol pending entries below the confirmed position are part of
        # the confirmed book already.
        for symbol in self.symbols:
            position = self._identity(epoch, symbol)
            if position is not None:
                self.connection.execute(
                    'DELETE FROM recovery_pending '
                    'WHERE epoch=? AND symbol=? AND seq<=?',
                    (epoch, symbol, position),
                )
        marks = ','.join('?' for _ in self.symbols)
        rows = self.connection.execute(
            'SELECT image_epoch,position FROM recovery_symbols '
            'WHERE symbol IN (%s)' % marks, self.symbols,
        ).fetchall()
        if len(rows) != len(self.symbols):
            return
        if any(row[0] != epoch or row[1] is None for row in rows):
            # A symbol still waits for its first image of this epoch, so even
            # the oldest package number may yet be needed to replay a suffix
            # onto that image.
            return
        minimum = min(row[1] for row in rows)
        self.connection.execute(
            'DELETE FROM recovery_seen WHERE epoch=? AND seq<=?',
            (epoch, minimum),
        )

    # -- sharded snapshots -------------------------------------------------
    def _feed_snapshot(self, event):
        epoch = event.get('epoch')
        symbol = event.get('symbol')
        image = event.get('image')
        cut = event.get('cut')
        part = event.get('part')
        parts = event.get('parts')
        orders = event.get('orders')
        if (type(epoch) is not int or epoch <= 0
                or symbol not in self.symbols
                or not isinstance(image, str) or not image
                or type(cut) is not int or cut < 0
                or type(part) is not int or type(parts) is not int
                or parts < 1 or not 0 <= part < parts
                or not isinstance(orders, list)):
            raise ValueError('invalid snapshot')
        shard = {}
        for item in orders:
            if not isinstance(item, (list, tuple)) or len(item) != 4:
                raise ValueError('invalid snapshot order')
            order_id, side, price, quantity = item
            checked = checked_orders({order_id: [side, price, quantity]})
            shard[order_id] = checked[order_id]

        current = self._state('epoch')
        if current is None:
            raise ValueError('snapshot before epoch')
        if epoch < current:
            with self.connection:
                self._retain(event)
            return
        if epoch > current:
            raise ValueError('snapshot before epoch event')

        with self.connection:
            self._retain(event)
            position = self._identity(epoch, symbol)
            if position is not None and cut <= position:
                # An image that cannot advance the confirmed position must
                # neither replace the book nor consume shard storage.
                return
            known = self.connection.execute(
                'SELECT cut,parts FROM recovery_images '
                'WHERE epoch=? AND symbol=? AND image=?',
                (epoch, symbol, image),
            ).fetchone()
            if known is not None:
                if known != (cut, parts):
                    raise ValueError('inconsistent image metadata')
            else:
                self.connection.execute(
                    'INSERT INTO recovery_images(epoch,symbol,image,cut,parts) '
                    'VALUES (?,?,?,?,?)',
                    (epoch, symbol, image, cut, parts),
                )
            self.connection.execute(
                'INSERT OR IGNORE INTO recovery_image_parts'
                '(epoch,symbol,image,part,body) VALUES (?,?,?,?,?)',
                (epoch, symbol, image, part,
                 json.dumps(shard, separators=(',', ':'))),
            )
            present = self.connection.execute(
                'SELECT COUNT(*) FROM recovery_image_parts '
                'WHERE epoch=? AND symbol=? AND image=?',
                (epoch, symbol, image),
            ).fetchone()[0]
            if present == parts:
                self._install(epoch, symbol, image, cut)
                self._prune(epoch)

    def _discard_images(self, epoch, symbol, ceiling):
        """Drop images for ``symbol`` that can no longer advance its book."""
        self.connection.execute(
            'DELETE FROM recovery_image_parts WHERE epoch=? AND symbol=? AND image IN '
            '(SELECT image FROM recovery_images WHERE epoch=? AND symbol=? AND cut<=?)',
            (epoch, symbol, epoch, symbol, ceiling),
        )
        self.connection.execute(
            'DELETE FROM recovery_images WHERE epoch=? AND symbol=? AND cut<=?',
            (epoch, symbol, ceiling),
        )

    def _install(self, epoch, symbol, image, cut):
        merged = {}
        for body, in self.connection.execute(
                'SELECT body FROM recovery_image_parts '
                'WHERE epoch=? AND symbol=? AND image=?',
                (epoch, symbol, image),
        ):
            merged.update(json.loads(body))
        self._publish(symbol, epoch, cut, merged)
        self.connection.execute(
            'UPDATE recovery_symbols SET image_epoch=?, position=? WHERE symbol=?',
            (epoch, cut, symbol),
        )
        # The image already reflects every package through its cut.
        self.connection.execute(
            'DELETE FROM recovery_pending WHERE epoch=? AND symbol=? AND seq<=?',
            (epoch, symbol, cut),
        )
        self._discard_images(epoch, symbol, cut)
        # Only the contiguous suffix above the cut may be replayed.  A hole
        # leaves the symbol recovering at the new position.
        self._advance(epoch, symbol)
