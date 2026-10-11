"""Repeatable bounded-memory row collections. No scoring logic lives here."""
import json
import sqlite3
import tempfile
from collections.abc import Sequence
from itertools import islice


class RowStream(Sequence):
    def __init__(self, factory, count=None):
        self.factory, self.count = factory, count

    def __iter__(self):
        return iter(self.factory())

    def __len__(self):
        if self.count is None:
            self.count = sum(1 for _ in self)
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return list(islice(self, start, stop, step))
        if index < 0:
            index += len(self)
        if index < 0:
            raise IndexError(index)
        try:
            return next(islice(self, index, index + 1))
        except StopIteration:
            raise IndexError(index) from None

    def where(self, predicate):
        return RowStream(lambda: (row for row in self if predicate(row)))

    def batches(self, size=300):
        if not 1 <= size <= 300:
            raise ValueError('Read batch size must be 1..300')
        iterator = iter(self)
        while batch := list(islice(iterator, size)):
            yield batch


class DiskRows(RowStream):
    """Spool a transformation once; subsequent aggregate passes read from disk."""
    def __init__(self, rows):
        self.directory = tempfile.TemporaryDirectory(prefix='htt-rows-')
        self.db = sqlite3.connect(self.directory.name + '/rows.db')
        self.db.execute('CREATE TABLE rows(data TEXT NOT NULL)')
        count = 0
        with self.db:
            for row in rows:
                self.db.execute('INSERT INTO rows VALUES(?)', (json.dumps(row, allow_nan=False, separators=(',', ':')),))
                count += 1
        connection = self.db
        super().__init__(lambda: (json.loads(data) for (data,) in connection.execute('SELECT data FROM rows')), count)

    def close(self):
        if getattr(self, 'db', None) is not None:
            self.db.close()
            self.db = None
        if hasattr(self, 'directory'):
            self.directory.cleanup()

    def __del__(self):
        self.close()


def selected(rows, predicate):
    return rows.where(predicate) if isinstance(rows, RowStream) else [r for r in rows if predicate(r)]
