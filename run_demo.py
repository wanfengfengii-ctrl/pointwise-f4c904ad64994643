import json
import tempfile
from pathlib import Path
from quotetape.ingest import FullCapture
from quotetape.quote import QuoteReader

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
