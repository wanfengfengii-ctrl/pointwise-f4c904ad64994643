import json
import tempfile
from pathlib import Path
from quotetape.ingest import FullCapture
from quotetape.quote import QuoteReader
from quotetape.raw import Tape
from quotetape.recovery import Receiver

with tempfile.TemporaryDirectory() as directory:
    db = str(Path(directory) / 'demo.sqlite')
    capture = FullCapture(db)
    reader = QuoteReader(db)
    try:
        capture.feed(dict(kind='full', symbol='A', epoch=1, seq=1, orders={'b1': ['B', 102, 5], 'a1': ['S', 107, 8]}))
        actual = reader.quote('A')
        assert actual == dict(symbol='A', epoch=1, seq=1, bid=102, ask=107)
        print(json.dumps(actual, ensure_ascii=False))
    finally:
        capture.close()
        reader.close()

with tempfile.TemporaryDirectory() as directory:
    db = str(Path(directory) / 'demo.sqlite')
    receiver = Receiver(db, ['A', 'B'])
    reader = QuoteReader(db)
    try:
        receiver.feed(dict(kind='epoch', epoch=1))
        for symbol in ('A', 'B'):
            receiver.feed(dict(kind='snapshot', epoch=1, symbol=symbol,
                               image=symbol + '-0', cut=0, part=0, parts=1,
                               orders=[]))
        print(json.dumps(dict(step='initial', A=reader.quote('A'),
                              B=reader.quote('B')), ensure_ascii=False))

        # Packet 2 arrives before packet 1: both symbols must wait.
        receiver.feed(dict(kind='delta', epoch=1, seq=2,
                           changes=[['A', 'put', 'a1', ['B', 100, 5]]]))
        print(json.dumps(dict(step='gap', A=reader.quote('A'),
                              B=reader.quote('B')), ensure_ascii=False))

        # A gets a complete image at cut=2; B is untouched and stays behind.
        receiver.feed(dict(kind='snapshot', epoch=1, symbol='A', image='A-2',
                           cut=2, part=0, parts=1, orders=[['a1', 'B', 100, 5]]))
        print(json.dumps(dict(step='A-image', A=reader.quote('A'),
                              B=reader.quote('B')), ensure_ascii=False))

        # Packet 1 closes B's gap from its own cut=0 image.
        before = receiver.work()['delta_entries_applied']
        receiver.feed(dict(kind='delta', epoch=1, seq=1,
                           changes=[['B', 'put', 'b1', ['S', 110, 3]]]))
        after = receiver.work()['delta_entries_applied']
        print(json.dumps(dict(step='B-caught-up', A=reader.quote('A'),
                              B=reader.quote('B'),
                              work_delta=after - before), ensure_ascii=False))

        # Sharded image: one shard must not replace the book.
        receiver.feed(dict(kind='delta', epoch=1, seq=3,
                           changes=[['B', 'put', 'b2', ['S', 108, 1]]]))
        receiver.feed(dict(kind='snapshot', epoch=1, symbol='B', image='B-4',
                           cut=4, part=0, parts=2,
                           orders=[['b2', 'S', 108, 1]]))
        print(json.dumps(dict(step='partial-image-B', B=reader.quote('B')),
                          ensure_ascii=False))
        receiver.feed(dict(kind='snapshot', epoch=1, symbol='B', image='B-4',
                           cut=4, part=1, parts=2,
                           orders=[['b3', 'B', 99, 2]]))
        print(json.dumps(dict(step='complete-image-B', B=reader.quote('B')),
                          ensure_ascii=False))

        return_to = receiver.work()['delta_entries_applied']
        receiver.close()
        receiver = Receiver(db, ['A', 'B'])
        print(json.dumps(dict(step='reopened', B=reader.quote('B'),
                              work_same=receiver.work()['delta_entries_applied'] == return_to),
                          ensure_ascii=False))

        # A new generation keeps the last confirmed books for provenance but
        # marks every symbol as recovering.
        receiver.feed(dict(kind='epoch', epoch=2))
        print(json.dumps(dict(step='epoch-2', A=reader.quote('A'),
                              B=reader.quote('B')), ensure_ascii=False))
        receiver.feed(dict(kind='snapshot', epoch=2, symbol='A', image='A2-0',
                           cut=0, part=0, parts=1, orders=[['a9', 'S', 120, 1]]))
        print(json.dumps(dict(step='epoch-2-A', A=reader.quote('A'),
                              B=reader.quote('B')), ensure_ascii=False))
        print(json.dumps(dict(step='raw-retained', events=len(list(Tape(db).events())),
                              work=receiver.work()), ensure_ascii=False))
    finally:
        receiver.close()
        reader.close()
