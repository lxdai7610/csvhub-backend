from collections import OrderedDict
import json
import threading
import time


class ResultCache:
    """Thread-safe LRU, bounded by encoded payload + key bytes; returns independent objects."""

    def __init__(self, max_bytes, ttl):
        self.max_bytes, self.ttl = max_bytes, ttl
        self.items = OrderedDict()
        self.bytes = self.hits = self.misses = 0
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.items.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires, payload, size = entry
            if expires <= time.monotonic():
                self.items.pop(key)
                self.bytes -= size
                self.misses += 1
                return None
            self.items.move_to_end(key)
            self.hits += 1
        return json.loads(payload)

    def put(self, key, value):
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
        # Conservative overhead allowance; does not include the transient query response.
        size = len(key.encode()) + len(payload) + 512
        if size > self.max_bytes:
            return
        with self.lock:
            old = self.items.pop(key, None)
            if old:
                self.bytes -= old[2]
            self.items[key] = (time.monotonic() + self.ttl, payload, size)
            self.bytes += size
            while self.bytes > self.max_bytes:
                _, (_, _, removed) = self.items.popitem(last=False)
                self.bytes -= removed

    def stats(self):
        with self.lock:
            return {"entries": len(self.items), "accounted_bytes": self.bytes,
                    "max_bytes": self.max_bytes, "hits": self.hits, "misses": self.misses}

    def invalidate_versions(self, versions):
        versions = set(versions)
        with self.lock:
            for key in list(self.items):
                try:
                    matches = json.loads(key).get("version") in versions
                except (ValueError, AttributeError, TypeError):
                    matches = False
                if matches:
                    self.bytes -= self.items.pop(key)[2]

