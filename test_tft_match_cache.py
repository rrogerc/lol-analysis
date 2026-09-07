"""Exact binary cache records, corruption recovery and concurrent writers."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
import unittest

from tft_match_cache import ScoreCache, encode, decode


def result(outcome="win", duration=3.25):
    return {"outcome": outcome, "duration": duration, "allyHpFraction": .123456789,
            "enemyHpFraction": -0.0, "damage": 9876.54321, "damageDps": 3038.936372307692,
            "frontlineTime": 3.0}


class TestExactScoreCache(unittest.TestCase):
    def test_binary_roundtrip_preserves_float_bits_signed_zero_and_outcome_order(self):
        key = hashlib.sha256(b"fight inputs").digest()
        rows = [result(outcome, 3.25 + index) for index, outcome in enumerate(("win", "loss", "draw", "timeout"))]
        restored = decode(key, encode(key, rows))
        for before, after in zip(rows, restored):
            for field, value in before.items():
                if isinstance(value, float):
                    self.assertEqual(struct.pack("<d", value), struct.pack("<d", after[field]))
                else:
                    self.assertEqual(value, after[field])
        with self.assertRaisesRegex(ValueError, "checksum"):
            decode(b"different inputs", encode(key, rows))

    def test_multiple_connections_share_committed_records_and_namespaces_do_not(self):
        with TemporaryDirectory() as directory:
            first = ScoreCache(directory, "same combat")
            first.put_many([(b"one", [result()])])
            second = ScoreCache(directory, "same combat")
            other = ScoreCache(directory, "different combat")
            self.assertEqual(second.get_many([b"one", b"missing"]), {b"one": [result()]})
            self.assertEqual(other.get_many([b"one"]), {})
            self.assertEqual(second.stats["scoreCacheHits"], 1)
            self.assertEqual(second.stats["scoreCacheMisses"], 1)
            for cache in (first, second, other):
                cache.close()

    def test_corrupt_entry_is_deleted_and_can_be_recomputed(self):
        with TemporaryDirectory() as directory:
            cache = ScoreCache(directory, "combat")
            cache.put_many([(b"one", [result()])])
            with cache.connection:
                cache.connection.execute("UPDATE scores SET result=? WHERE signature=?", (b"corrupt", b"one"))
            self.assertEqual(cache.get_many([b"one"]), {})
            self.assertEqual(cache.stats["scoreCacheCorruptRows"], 1)
            cache.put_many([(b"one", [result()])])
            self.assertEqual(cache.get_many([b"one"]), {b"one": [result()]})
            cache.close()

    def test_invalid_output_does_not_write_a_partial_batch(self):
        with TemporaryDirectory() as directory:
            cache = ScoreCache(directory, "combat")
            with self.assertRaises(ValueError):
                cache.put_many([(b"good", [result()]), (b"bad", [dict(result(), damage=float("nan"))])])
            self.assertEqual(cache.get_many([b"good"]), {})
            cache.close()

    def test_concurrent_connections_preserve_all_unique_records(self):
        with TemporaryDirectory() as directory:
            ScoreCache(directory, "combat").close()
            def write(index):
                cache = ScoreCache(directory, "combat")
                self.assertIsNone(cache.error)
                rows = [(f"{index}-{i}".encode(), [result(duration=3.0 + i)]) for i in range(40)]
                cache.put_many(rows)
                self.assertEqual(cache.get_many([key for key, _ in rows]), dict(rows))
                cache.close()
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(8)))
            cache = ScoreCache(directory, "combat")
            self.assertEqual(len(cache.get_many([f"{index}-{i}".encode() for index in range(8) for i in range(40)])), 320)
            cache.close()

    def test_unavailable_derived_cache_falls_back_with_an_explicit_warning(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, "file")
            path.write_text("not a directory")
            with self.assertWarnsRegex(RuntimeWarning, "computed normally"):
                cache = ScoreCache(path, "combat")
            self.assertEqual(cache.get_many([b"one"]), {})
            cache.put_many([(b"one", [result()])])
            self.assertIsNotNone(cache.error)


if __name__ == "__main__":
    unittest.main()
