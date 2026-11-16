from .books import BookStore

class QuoteReader:
    def __init__(self, db):
        self.books = BookStore(db)

    def quote(self, symbol):
        book = self.books.read(symbol)
        if book is None:
            return None
        bids = [p for side, p, qty in book['orders'].values() if side == 'B']
        asks = [p for side, p, qty in book['orders'].values() if side == 'S']
        return dict(symbol=symbol, epoch=book['epoch'], seq=book['seq'], bid=max(bids, default=None), ask=min(asks, default=None))

    def close(self):
        self.books.close()
