import json
import sqlite3

class BookStore:
    def __init__(self, db):
        self.connection = sqlite3.connect(db)
        self.connection.execute('CREATE TABLE IF NOT EXISTS books(symbol TEXT PRIMARY KEY, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL)')
        self.connection.commit()

    def publish(self, symbol, epoch, cut, orders):
        if not isinstance(symbol, str) or not symbol or type(epoch) is not int or epoch <= 0 or type(cut) is not int or cut < 0:
            raise ValueError('invalid book identity')
        checked = {}
        for key, value in orders.items():
            if not isinstance(key, str) or not key or not isinstance(value, (list, tuple)) or len(value) != 3:
                raise ValueError('invalid order')
            side, price, quantity = value
            if side not in ('B', 'S') or type(price) is not int or price <= 0 or type(quantity) is not int or quantity <= 0:
                raise ValueError('invalid order value')
            checked[key] = [side, price, quantity]
        body = json.dumps(checked, sort_keys=True, separators=(',', ':'))
        with self.connection:
            self.connection.execute('INSERT INTO books VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET epoch=excluded.epoch,seq=excluded.seq,body=excluded.body', (symbol, epoch, cut, body))

    def read(self, symbol):
        row = self.connection.execute('SELECT epoch,seq,body FROM books WHERE symbol=?', (symbol,)).fetchone()
        return None if row is None else dict(symbol=symbol, epoch=row[0], seq=row[1], orders=json.loads(row[2]))

    def close(self):
        self.connection.close()
