import json
import sqlite3


def checked_orders(orders):
    """Validate and copy an {order_id: [side, price, quantity]} mapping."""
    checked = {}
    for key, value in orders.items():
        if not isinstance(key, str) or not key or not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError('invalid order')
        side, price, quantity = value
        if side not in ('B', 'S') or type(price) is not int or price <= 0 or type(quantity) is not int or quantity <= 0:
            raise ValueError('invalid order value')
        checked[key] = [side, price, quantity]
    return checked


def encode_orders(orders):
    return json.dumps(orders, sort_keys=True, separators=(',', ':'))


class BookStore:
    def __init__(self, db):
        self.connection = sqlite3.connect(db)
        self.connection.execute('CREATE TABLE IF NOT EXISTS books(symbol TEXT PRIMARY KEY, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL)')
        self.connection.commit()

    def publish(self, symbol, epoch, cut, orders):
        if not isinstance(symbol, str) or not symbol or type(epoch) is not int or epoch <= 0 or type(cut) is not int or cut < 0:
            raise ValueError('invalid book identity')
        checked = checked_orders(orders)
        body = encode_orders(checked)
        with self.connection:
            self.connection.execute('INSERT INTO books VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET epoch=excluded.epoch,seq=excluded.seq,body=excluded.body', (symbol, epoch, cut, body))

    def _has_recovery(self):
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_symbols'"
        ).fetchone() is not None

    def _recovery_read(self, symbol):
        """Extended view for symbols registered by quotetape.recovery.Receiver.

        epoch/seq describe the last confirmed book; source_epoch is the
        current feed generation, and status is derived from the symbol's
        confirmed position against the newest observed package.  One SQL
        statement keeps the whole view on a single consistent snapshot.
        """
        row = self.connection.execute(
            'SELECT b.epoch,b.seq,b.body,r.image_epoch,r.position,'
            "(SELECT value FROM recovery_state WHERE key='epoch'),"
            "(SELECT value FROM recovery_state WHERE key='h') "
            'FROM recovery_symbols AS r LEFT JOIN books AS b ON b.symbol=r.symbol '
            'WHERE r.symbol=?', (symbol,)
        ).fetchone()
        if row is None:
            return None
        epoch, seq, body, image_epoch, position, source_epoch, watermark = row
        live = (image_epoch is not None and image_epoch == source_epoch
                and position is not None and position >= (watermark or 0))
        return dict(symbol=symbol, epoch=epoch, seq=seq,
                    orders={} if body is None else json.loads(body),
                    status='live' if live else 'recovering',
                    source_epoch=source_epoch)

    def read(self, symbol):
        if self._has_recovery():
            value = self._recovery_read(symbol)
            if value is not None:
                return value
        row = self.connection.execute('SELECT epoch,seq,body FROM books WHERE symbol=?', (symbol,)).fetchone()
        return None if row is None else dict(symbol=symbol, epoch=row[0], seq=row[1], orders=json.loads(row[2]))

    def close(self):
        self.connection.close()
