# swarm/history.py
import csv, os, threading, time

class RollingHistory:
    """A time-sampled rolling series persisted to CSV as `underlying,ts,value`.

    The window is defined in TIME, not in rows: an observation is only written once
    `sample_every_s` has elapsed since the last one, so polling faster does not shrink
    the lookback. Agents may poll every 60s while the window still spans hours.
    """
    def __init__(self, path, sample_every_s=900, window=30):
        self.path, self.sample_every_s, self.window = path, sample_every_s, window
        self._lock = threading.Lock()
        self._series = None                       # {underlying: [(ts, value), ...]}

    def _load(self):
        if self._series is not None:
            return self._series
        series = {}
        if os.path.exists(self.path):
            with open(self.path, newline="") as f:
                for r in csv.reader(f):
                    if len(r) != 3:               # legacy 2-column rows: ignore
                        continue
                    try:
                        series.setdefault(r[0], []).append((float(r[1]), float(r[2])))
                    except ValueError:
                        continue
        self._series = series
        return series

    def observe(self, underlying, value, now=None):
        """Record `value` if the sample interval has elapsed.

        Returns the window of PRIOR observations, so the caller always compares the
        current value against history that excludes it.
        """
        now = time.time() if now is None else now
        with self._lock:
            rows = self._load().setdefault(underlying, [])
            window = [v for _, v in rows[-self.window:]]
            if not rows or now - rows[-1][0] >= self.sample_every_s:
                rows.append((now, value))
                with open(self.path, "a", newline="") as f:
                    csv.writer(f).writerow([underlying, now, value])
            return window

def percentile(window, value):
    """Fraction of prior observations at or below `value`; 0.5 with no history."""
    return sum(1 for v in window if v <= value) / len(window) if window else 0.5
