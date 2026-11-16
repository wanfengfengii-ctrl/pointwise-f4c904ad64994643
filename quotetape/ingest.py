from .raw import Tape
from .books import BookStore

class FullCapture:
    def __init__(self, db):
        self.raw = Tape(db)
        self.books = BookStore(db)

    def feed(self, event):
        if event.get('kind') != 'full':
            raise ValueError('expected full book event')
        self.books.publish(event['symbol'], event['epoch'], event['seq'], event['orders'])
        return self.raw.append(event)

    def close(self):
        self.raw.close()
        self.books.close()
