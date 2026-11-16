import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from quotetape.books import BookStore
from quotetape.quote import QuoteReader
from quotetape.raw import Tape
from quotetape.recovery import Receiver

ROOT = str(Path(__file__).resolve().parent.parent)


def delta(epoch, seq, changes):
    return dict(kind='delta', epoch=epoch, seq=seq, changes=changes)


def snapshot(epoch, symbol, image, cut, part, parts, orders):
    return dict(kind='snapshot', epoch=epoch, symbol=symbol, image=image,
                cut=cut, part=part, parts=parts, orders=orders)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = str(Path(self.temporary.name) / 'recovery.sqlite')

    def open_receiver(self, symbols=('A', 'B')):
        value = Receiver(self.db, list(symbols))
        self.addCleanup(value.close)
        return value

    def reader(self):
        value = QuoteReader(self.db)
        self.addCleanup(value.close)
        return value

    def books(self):
        value = BookStore(self.db)
        self.addCleanup(value.close)
        return value

    def test_contract_example_gap_then_high_cut_image(self):
        receiver = self.open_receiver()
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        for symbol in ('A', 'B'):
            receiver.feed(snapshot(1, symbol, symbol + '-0', 0, 0, 1, []))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=0, bid=None,
                                                ask=None, status='live', source_epoch=1))
        receiver.feed(delta(1, 2, [['A', 'put', 'a1', ['B', 100, 1]]]))
        self.assertEqual(reader.quote('A')['status'], 'recovering')
        self.assertEqual(reader.quote('A')['seq'], 0)
        self.assertEqual(reader.quote('B')['status'], 'recovering')
        self.assertEqual(reader.quote('B')['seq'], 0)
        receiver.feed(snapshot(1, 'A', 'a-2', 2, 0, 1, [['a1', 'B', 100, 1]]))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=2, bid=100,
                                                ask=None, status='live', source_epoch=1))
        self.assertEqual(reader.quote('B')['status'], 'recovering')
        receiver.feed(delta(1, 1, [['B', 'put', 'b1', ['S', 110, 2]]]))
        self.assertEqual(reader.quote('B'), dict(symbol='B', epoch=1, seq=2, bid=None,
                                                ask=110, status='live', source_epoch=1))
        self.assertEqual(reader.quote('A')['seq'], 2)
        self.assertEqual(reader.quote('A')['bid'], 100)

    def test_shards_do_not_replace_until_complete_and_lower_complete_wins(self):
        receiver = self.open_receiver(('A',))
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'base', 0, 0, 1, [['old', 'B', 50, 1]]))
        self.assertEqual(reader.quote('A')['seq'], 0)
        # A future image is cut=5 but only one of two shards has arrived.
        receiver.feed(snapshot(1, 'A', 'future', 5, 0, 2, [['f', 'B', 60, 1]]))
        self.assertEqual(reader.quote('A')['seq'], 0)
        self.assertEqual(self.books().read('A')['orders'], {'old': ['B', 50, 1]})
        # A complete lower-cut image must be adopted rather than waiting.
        receiver.feed(snapshot(1, 'A', 'three', 3, 0, 1, [['x', 'B', 70, 2]]))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=3, bid=70,
                                                ask=None, status='live', source_epoch=1))
        self.assertEqual(self.books().read('A')['orders'], {'x': ['B', 70, 2]})
        # Completing the higher image legitimately switches again.
        receiver.feed(snapshot(1, 'A', 'future', 5, 1, 2, [['g', 'S', 80, 3]]))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=5, bid=60,
                                                ask=80, status='live', source_epoch=1))
        self.assertEqual(self.books().read('A')['orders'],
                         {'f': ['B', 60, 1], 'g': ['S', 80, 3]})

    def test_old_image_does_not_roll_back(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'new', 4, 0, 1, [['x', 'B', 10, 1]]))
        receiver.feed(snapshot(1, 'A', 'old', 2, 0, 1, [['y', 'S', 20, 1]]))
        quote = self.reader().quote('A')
        self.assertEqual((quote['seq'], quote['bid'], quote['ask']), (4, 10, None))
        receiver.feed(delta(1, 3, [['A', 'put', 'z', ['B', 9, 1]]]))
        self.assertEqual(self.reader().quote('A')['seq'], 4)

    def test_delta_before_image_is_not_applied_to_empty_book(self):
        receiver = self.open_receiver(('A',))
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(delta(1, 1, [['A', 'put', 'a1', ['B', 100, 1]]]))
        state = self.books().read('A')
        self.assertEqual(state, dict(symbol='A', epoch=None, seq=None, orders={},
                                     status='recovering', source_epoch=1))
        self.assertEqual(reader.quote('A')['bid'], None)
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        # The image at cut=1 already contains packet 1; nothing is replayed.
        receiver.feed(snapshot(1, 'A', 'i1', 1, 0, 1, [['a1', 'B', 100, 1]]))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=1, bid=100,
                                                ask=None, status='live', source_epoch=1))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})

    def test_image_suffix_replay_only_after_cut(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'base', 0, 0, 1, []))
        receiver.feed(delta(1, 3, [['A', 'put', 'c', ['B', 13, 1]]]))
        receiver.feed(delta(1, 2, [['A', 'put', 'b', ['B', 12, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        receiver.feed(snapshot(1, 'A', 'two', 2, 0, 1, [['a', 'B', 11, 1],
                                                       ['b', 'B', 12, 1]]))
        self.assertEqual(self.reader().quote('A')['seq'], 3)
        self.assertEqual(receiver.work(), {'delta_entries_applied': 1})
        self.assertEqual(self.books().read('A')['orders'],
                         {'a': ['B', 11, 1], 'b': ['B', 12, 1], 'c': ['B', 13, 1]})

    def test_packet_touching_two_symbols_is_atomic(self):
        receiver = self.open_receiver()
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        for symbol in ('A', 'B'):
            receiver.feed(snapshot(1, symbol, symbol, 0, 0, 1, []))
        receiver.feed(delta(1, 1, [['A', 'put', 'a1', ['B', 10, 1]],
                                   ['B', 'put', 'b1', ['S', 20, 1]]]))
        self.assertEqual((reader.quote('A')['seq'], reader.quote('B')['seq']), (1, 1))
        self.assertEqual((reader.quote('A')['bid'], reader.quote('B')['ask']), (10, 20))
        self.assertEqual(reader.quote('A')['status'], 'live')
        self.assertEqual(reader.quote('B')['status'], 'live')

    def test_higher_packet_missing_keeps_symbol_recovering(self):
        receiver = self.open_receiver(('A',))
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, []))
        receiver.feed(delta(1, 2, [['A', 'put', 'x', ['B', 5, 1]]]))
        self.assertEqual(reader.quote('A')['status'], 'recovering')
        receiver.feed(delta(1, 1, []))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=2, bid=5,
                                                ask=None, status='live', source_epoch=1))
        receiver.feed(delta(1, 1, []))
        self.assertEqual(self.reader().quote('A')['seq'], 2)
        self.assertEqual(receiver.work(), {'delta_entries_applied': 1})

    def test_epoch_change_recovers_and_retains_previous_book(self):
        receiver = self.open_receiver(('A',))
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, [['x', 'B', 7, 2]]))
        self.assertEqual(reader.quote('A')['status'], 'live')
        receiver.feed(dict(kind='epoch', epoch=2))
        state = self.books().read('A')
        self.assertEqual(state, dict(symbol='A', epoch=1, seq=0,
                                     orders={'x': ['B', 7, 2]},
                                     status='recovering', source_epoch=2))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=0, bid=7,
                                                ask=None, status='recovering',
                                                source_epoch=2))
        # Old generation data never moves the new generation.
        receiver.feed(delta(1, 1, [['A', 'put', 'y', ['B', 9, 1]]]))
        receiver.feed(snapshot(1, 'A', 'old', 9, 0, 1, [['y', 'B', 9, 1]]))
        self.assertEqual(self.books().read('A')['orders'], {'x': ['B', 7, 2]})
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        receiver.feed(snapshot(2, 'A', 'n', 0, 0, 1, [['z', 'S', 3, 1]]))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=2, seq=0, bid=None,
                                                ask=3, status='live', source_epoch=2))

    def test_reopen_continues_and_redelivery_is_not_reapplied(self):
        receiver = Receiver(self.db, ['A', 'B'])
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'ia', 0, 0, 1, []))
        receiver.feed(snapshot(1, 'B', 'ib', 0, 0, 1, []))
        receiver.feed(delta(1, 1, [['A', 'put', 'a1', ['B', 10, 1]]]))
        receiver.feed(delta(1, 2, [['B', 'put', 'b1', ['S', 20, 1]]]))
        work_before = receiver.work()
        a_before = receiver.connection.execute(
            'SELECT epoch,seq,body FROM books WHERE symbol=?', ('A',)
        ).fetchone()
        receiver.close()

        reopened = self.open_receiver()
        self.assertEqual(reopened.work(), work_before)
        self.assertEqual(self.reader().quote('A')['seq'], 2)
        # Redeliver the whole history; nothing may be applied twice.
        reopened.feed(dict(kind='epoch', epoch=1))
        reopened.feed(snapshot(1, 'A', 'ia', 0, 0, 1, []))
        reopened.feed(snapshot(1, 'B', 'ib', 0, 0, 1, []))
        reopened.feed(delta(1, 1, [['A', 'put', 'a1', ['B', 10, 1]]]))
        reopened.feed(delta(1, 2, [['B', 'put', 'b1', ['S', 20, 1]]]))
        self.assertEqual(reopened.work(), work_before)
        self.assertEqual(self.reader().quote('A')['seq'], 2)
        self.assertEqual(self.reader().quote('B')['seq'], 2)
        raw = list(Tape(self.db).events())
        self.assertEqual(len(raw), 10)
        self.assertEqual(raw[5][1], dict(kind='epoch', epoch=1))
        self.assertEqual(raw[-1][1], delta(1, 2, [['B', 'put', 'b1', ['S', 20, 1]]]))
        self.assertEqual(a_before[2], reopened.connection.execute(
            'SELECT body FROM books WHERE symbol=?', ('A',)).fetchone()[0])

    def test_duplicate_and_missing_packets_do_not_reapply_prefix(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, []))
        receiver.feed(delta(1, 1, [['A', 'put', 'a', ['B', 1, 1]]]))
        receiver.feed(delta(1, 2, [['A', 'put', 'b', ['B', 2, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 2})
        for _ in range(4):
            receiver.feed(delta(1, 1, [['A', 'put', 'a', ['B', 1, 1]]]))
            receiver.feed(delta(1, 2, [['A', 'put', 'b', ['B', 2, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 2})
        self.assertEqual(self.books().read('A')['orders'],
                         {'a': ['B', 1, 1], 'b': ['B', 2, 1]})
        # Packets 4..6 wait for 3, then the whole suffix applies once.
        for seq in (4, 5, 6):
            receiver.feed(delta(1, seq, [['A', 'put', 'o%d' % seq, ['S', seq, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 2})
        self.assertEqual(receiver.work()['delta_entries_applied'], 2)
        receiver.feed(delta(1, 3, [['A', 'put', 'o3', ['S', 3, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 6})
        self.assertEqual(self.reader().quote('A')['seq'], 6)

    def test_reads_return_independent_values(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, [['x', 'B', 5, 2]]))
        state = self.books().read('A')
        state['orders']['x'][1] = 99
        self.assertEqual(self.books().read('A')['orders']['x'],
                         ['B', 5, 2])
        self.assertEqual(self.reader().quote('A')['bid'], 5)

    def test_unmanaged_symbol_keeps_old_shape(self):
        books = self.books()
        books.publish('Z', 3, 0, {'x': ['B', 4, 1]})
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        self.assertEqual(books.read('Z'), dict(symbol='Z', epoch=3, seq=0,
                                              orders={'x': ['B', 4, 1]}))
        self.assertIsNone(books.read('Q'))

    def test_reopen_replays_only_the_suffix_onto_first_image(self):
        receiver = Receiver(self.db, ['A'])
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(delta(1, 3, [['A', 'put', 'c', ['B', 13, 1]]]))
        receiver.feed(delta(1, 4, [['A', 'put', 'd', ['B', 14, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        receiver.close()

        reopened = self.open_receiver(('A',))
        reopened.feed(delta(1, 5, [['A', 'put', 'e', ['B', 15, 1]]]))
        reopened.feed(snapshot(1, 'A', 'two', 2, 0, 1, [['a', 'B', 11, 1],
                                                       ['b', 'B', 12, 1]]))
        quote = self.reader().quote('A')
        self.assertEqual((quote['epoch'], quote['seq'], quote['bid']), (1, 5, 15))
        self.assertEqual(self.books().read('A')['orders'],
                         {'a': ['B', 11, 1], 'b': ['B', 12, 1],
                          'c': ['B', 13, 1], 'd': ['B', 14, 1],
                          'e': ['B', 15, 1]})
        self.assertEqual(reopened.work(), {'delta_entries_applied': 3})

    def test_partial_image_does_not_lend_status_to_other_symbol(self):
        receiver = self.open_receiver()
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'B', 'b0', 0, 0, 1, []))
        # A only has one shard of a cut=1 image.
        receiver.feed(snapshot(1, 'A', 'a1', 1, 0, 2, [['a1', 'B', 1, 1]]))
        receiver.feed(delta(1, 1, [['B', 'put', 'b1', ['S', 2, 1]]]))
        # B catches up by delta replay alone; A must wait for its own shards.
        self.assertEqual(reader.quote('B'), dict(symbol='B', epoch=1, seq=1,
                                                bid=None, ask=2, status='live',
                                                source_epoch=1))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=None, seq=None,
                                                bid=None, ask=None,
                                                status='recovering',
                                                source_epoch=1))
        receiver.feed(snapshot(1, 'A', 'a1', 1, 1, 2, []))
        self.assertEqual(reader.quote('A'), dict(symbol='A', epoch=1, seq=1, bid=1,
                                                ask=None, status='live',
                                                source_epoch=1))

    def test_empty_packet_advances_all_symbols(self):
        receiver = self.open_receiver()
        reader = self.reader()
        receiver.feed(dict(kind='epoch', epoch=1))
        for symbol in ('A', 'B'):
            receiver.feed(snapshot(1, symbol, symbol, 0, 0, 1, []))
        receiver.feed(delta(1, 1, []))
        self.assertEqual(reader.quote('A')['seq'], 1)
        self.assertEqual(reader.quote('B')['seq'], 1)
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})

    def test_del_of_absent_order_is_noop(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, [['x', 'B', 5, 1]]))
        receiver.feed(delta(1, 1, [['A', 'del', 'missing', None],
                                   ['A', 'del', 'x', None]]))
        self.assertEqual(self.books().read('A')['orders'], {})
        self.assertEqual(receiver.work(), {'delta_entries_applied': 2})

    def test_work_counts_real_application_only(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, [['keep', 'B', 1, 1]]))
        # Loading an image is not work; a packet for another symbol is not
        # work; storing a packet waiting on a gap is not work.
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        receiver.feed(delta(1, 2, [['A', 'put', 'p', ['B', 2, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        receiver.feed(delta(1, 1, [['A', 'put', 'q', ['S', 3, 1]]]))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 2})

    def test_independent_process_reader_sees_successful_feed(self):
        receiver = Receiver(self.db, ['A'])
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(delta(1, 1, [['A', 'put', 'a', ['B', 10, 1]]]))
        receiver.feed(snapshot(1, 'A', 'i', 1, 0, 1, [['a', 'B', 10, 1]]))
        receiver.close()
        script = textwrap.dedent('''
            import json, sys
            sys.path.insert(0, sys.argv[1])
            from quotetape.quote import QuoteReader
            reader = QuoteReader(sys.argv[2])
            print(json.dumps(reader.quote('A')))
            reader.close()
        ''')
        output = subprocess.run(
            [sys.executable, '-I', '-B', '-c', script, ROOT, self.db],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(json.loads(output),
                         dict(symbol='A', epoch=1, seq=1, bid=10, ask=None,
                              status='live', source_epoch=1))

    def test_crash_between_feeds_keeps_accepted_state_after_reopen(self):
        script = textwrap.dedent('''
            import os, sys
            sys.path.insert(0, sys.argv[1])
            from quotetape.recovery import Receiver
            receiver = Receiver(sys.argv[2], ['A'])
            receiver.feed(dict(kind='epoch', epoch=1))
            receiver.feed(dict(kind='snapshot', epoch=1, symbol='A', image='i',
                               cut=0, part=0, parts=1, orders=[]))
            receiver.feed(dict(kind='delta', epoch=1, seq=1,
                               changes=[['A', 'put', 'a', ['B', 7, 1]]]))
            receiver.feed(dict(kind='delta', epoch=1, seq=3,
                               changes=[['A', 'put', 'c', ['S', 9, 1]]]))
            sys.stdout.write(str(receiver.work()['delta_entries_applied']))
            sys.stdout.flush()
            os._exit(0)
        ''')
        output = subprocess.run(
            [sys.executable, '-I', '-B', '-c', script, ROOT, self.db],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(output.stdout, '1')
        receiver = self.open_receiver(('A',))
        self.assertEqual(self.reader().quote('A'), dict(
            symbol='A', epoch=1, seq=1, bid=7, ask=None,
            status='recovering', source_epoch=1))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 1})
        receiver.feed(delta(1, 2, [['A', 'put', 'b', ['S', 8, 1]]]))
        self.assertEqual(self.reader().quote('A'), dict(
            symbol='A', epoch=1, seq=3, bid=7, ask=8,
            status='live', source_epoch=1))
        self.assertEqual(receiver.work(), {'delta_entries_applied': 3})
        # Packets 1 and 3 must not be re-applied after the reopen.
        self.assertEqual(self.books().read('A')['orders'],
                         {'a': ['B', 7, 1], 'b': ['S', 8, 1], 'c': ['S', 9, 1]})

    def test_ignored_stale_images_do_not_accumulate(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'base', 5, 0, 1, [['x', 'B', 10, 1]]))
        for index in range(20):
            receiver.feed(snapshot(1, 'A', 'stale%d' % index, 3, 0, 2, []))
            receiver.feed(snapshot(1, 'A', 'base', 5, 0, 1, [['x', 'B', 10, 1]]))
        self.assertEqual(receiver.connection.execute(
            'SELECT COUNT(*) FROM recovery_images').fetchone()[0], 0)
        self.assertEqual(receiver.connection.execute(
            'SELECT COUNT(*) FROM recovery_image_parts').fetchone()[0], 0)
        self.assertEqual(receiver.work(), {'delta_entries_applied': 0})
        self.assertEqual(self.reader().quote('A')['seq'], 5)

    def test_resident_state_does_not_grow_with_confirmed_history(self):
        receiver = self.open_receiver(('A',))
        receiver.feed(dict(kind='epoch', epoch=1))
        receiver.feed(snapshot(1, 'A', 'i', 0, 0, 1, []))
        for seq in range(1, 301):
            receiver.feed(delta(1, seq, [
                ['A', 'put', 'o%d' % (seq % 5), ['B', seq, 1]]]))
        counts = {}
        for table in ('recovery_seen', 'recovery_pending',
                      'recovery_images', 'recovery_image_parts'):
            counts[table] = receiver.connection.execute(
                'SELECT COUNT(*) FROM %s' % table).fetchone()[0]
        self.assertEqual(counts, {'recovery_seen': 0, 'recovery_pending': 0,
                                  'recovery_images': 0,
                                  'recovery_image_parts': 0})
        self.assertEqual(receiver.work(), {'delta_entries_applied': 300})
        # Raw retention stays complete: every accepted event remains on disk.
        self.assertEqual(len(list(Tape(self.db).events())), 302)


if __name__ == '__main__':
    unittest.main()
