"""In-memory ring buffers for short-term trends (about an hour by default)."""
import math
import threading
from array import array

NAN = float("nan")


class Ring(object):
    """Fixed-capacity time series table: one timestamp column and named float columns."""

    def __init__(self, columns, capacity=1800):
        self.columns = list(columns)
        self.capacity = capacity
        self.times = array("d")
        self.data = dict((name, array("f")) for name in self.columns)
        self.lock = threading.Lock()

    def append(self, stamp, values):
        with self.lock:
            if len(self.times) >= self.capacity:
                self.times.pop(0)
                for column in self.data.values():
                    column.pop(0)
            self.times.append(stamp)
            for name in self.columns:
                value = values.get(name)
                self.data[name].append(NAN if value is None else float(value))

    def query(self, since=None, points=240, columns=None):
        """Return {"t": [...], "<column>": [...]} with at most `points` rows; gaps are None.

        When there are more rows than `points`, consecutive rows are averaged into buckets.
        """
        with self.lock:
            times = list(self.times)
            names = [name for name in (columns or self.columns) if name in self.data]
            data = dict((name, list(self.data[name])) for name in names)
        start = 0
        if since is not None:
            while start < len(times) and times[start] < since:
                start += 1
        count = len(times) - start
        result = {"t": []}
        for name in names:
            result[name] = []
        if count <= 0:
            return result
        size = max(1, int(math.ceil(count / float(points))))
        for begin in range(start, len(times), size):
            end = min(len(times), begin + size)
            result["t"].append(round(times[end - 1], 1))
            for name in names:
                values = [value for value in data[name][begin:end] if value == value]
                result[name].append(round(sum(values) / len(values), 4) if values else None)
        return result

    def mean(self, since, columns=None):
        """Mean of each column over the rows at or after `since` (None without data), plus the seconds they span."""
        with self.lock:
            times = list(self.times)
            names = [name for name in (columns or self.columns) if name in self.data]
            data = dict((name, list(self.data[name])) for name in names)
        start = 0
        while start < len(times) and times[start] < since:
            start += 1
        result = {"span": times[-1] - times[start] if start < len(times) else 0.0, "rows": len(times) - start}
        for name in names:
            values = [value for value in data[name][start:] if value == value]
            result[name] = sum(values) / len(values) if values else None
        return result

    def __len__(self):
        return len(self.times)
