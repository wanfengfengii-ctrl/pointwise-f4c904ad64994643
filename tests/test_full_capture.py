import tempfile
import unittest
from pathlib import Path
from quotetape.raw import Tape
from quotetape.books import BookStore
from quotetape.ingest import FullCapture
from quotetape.quote import QuoteReader

class FullCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = str(Path(self.temporary.name) / 'capture.sqlite')

    def open_resource(self, klass):
        value = klass(self.db)
        self.addCleanup(value.close)
        return value

    def test_raw_round_trip_and_order(self):
        tape = self.open_resource(Tape)
        events = [{'name': '证券甲', 'value': None}, {'name': 'B', 'value': [3, 4]}]
        self.assertEqual([tape.append(e) for e in events], [1, 2])
        self.assertEqual(list(tape.events()), list(enumerate(events, 1)))

    def test_independent_reader_observes_full_replacement(self):
        capture = self.open_resource(FullCapture)
        reader = self.open_resource(QuoteReader)
        self.assertIsNone(reader.quote('A'))
        capture.feed(dict(kind='full', symbol='A', epoch=1, seq=7, orders={'b': ['B', 100, 3], 'a': ['S', 105, 4]}))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=7, bid=100, ask=105))
        capture.feed(dict(kind='full', symbol='A', epoch=1, seq=8, orders={'x': ['S', 108, 2]}))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=8, bid=None, ask=108))

    def test_empty_book_and_symbol_isolation(self):
        books = self.open_resource(BookStore)
        books.publish('A', 2, 0, {})
        books.publish('B', 1, 4, {'a': ['B', 5, 1]})
        self.assertEqual(books.read('A'), dict(symbol='A', epoch=2, seq=0, orders={}))
        self.assertEqual(books.read('B')['orders'], {'a': ['B', 5, 1]})

    def test_validation_does_not_replace_existing(self):
        books = self.open_resource(BookStore)
        books.publish('A', 1, 1, {'x': ['B', 5, 2]})
        with self.assertRaises(ValueError):
            books.publish('A', 1, 2, {'bad': ['B', -1, 2]})
        self.assertEqual(books.read('A')['seq'], 1)

    def test_no_mutable_alias_and_reopen(self):
        books = BookStore(self.db)
        orders = {'x': ['B', 5, 2]}
        books.publish('A', 1, 1, orders)
        orders['x'][1] = 9
        books.read('A')['orders']['x'][1] = 12
        books.close()
        reopened = self.open_resource(BookStore)
        self.assertEqual(reopened.read('A')['orders'], {'x': ['B', 5, 2]})

if __name__ == '__main__':
    unittest.main()
