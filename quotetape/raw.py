import json
import sqlite3

class Tape:
    def __init__(self, db):
        self.connection = sqlite3.connect(db)
        self.connection.execute('CREATE TABLE IF NOT EXISTS raw_events(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.connection.commit()

    def append(self, event):
        body = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.connection:
            cursor = self.connection.execute('INSERT INTO raw_events(body) VALUES (?)', (body,))
        return cursor.lastrowid

    def events(self):
        for key, body in self.connection.execute('SELECT id,body FROM raw_events ORDER BY id'):
            yield key, json.loads(body)

    def close(self):
        self.connection.close()
