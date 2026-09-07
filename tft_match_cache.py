"""Persistent exact compact fight results, independent of composition search.

The caller supplies a conservative combat-input namespace and exact input keys.
IEEE doubles are stored unchanged; this cache never predicts another fight.
It is optional derived data: corrupt entries are rejected and computed afresh.
"""
from collections import Counter
import hashlib
import math
from pathlib import Path
import sqlite3
import struct
import warnings


FIELDS = ("duration", "allyHpFraction", "enemyHpFraction", "damage", "damageDps", "frontlineTime")
OUTCOMES = ("win", "loss", "draw", "timeout")
RECORD = struct.Struct("<B6d")
HEADER = struct.Struct("<BH")
FORMAT = 1
QUERY_BATCH = 256
SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def encode(key, results):
    if not 1 <= len(results) <= 65535:
        raise ValueError("score cache requires a nonempty bounded fight sequence")
    payload = bytearray(HEADER.pack(FORMAT, len(results)))
    for result in results:
        values = [result[field] for field in FIELDS]
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
            raise ValueError("score cache cannot store nonfinite fight values")
        payload.extend(RECORD.pack(OUTCOMES.index(result["outcome"]), *values))
    return bytes(payload) + hashlib.blake2b(key + payload, digest_size=16).digest()


def decode(key, blob):
    if not isinstance(blob, bytes) or len(blob) < HEADER.size + RECORD.size + 16:
        raise ValueError("invalid cached fight sequence")
    payload, checksum = blob[:-16], blob[-16:]
    if hashlib.blake2b(key + payload, digest_size=16).digest() != checksum:
        raise ValueError("cached fight checksum mismatch")
    version, count = HEADER.unpack_from(payload)
    if version != FORMAT or count == 0 or len(payload) != HEADER.size + count * RECORD.size:
        raise ValueError("cached fight format mismatch")
    results = []
    for offset in range(HEADER.size, len(payload), RECORD.size):
        outcome, *values = RECORD.unpack_from(payload, offset)
        if outcome >= len(OUTCOMES) or not all(math.isfinite(value) for value in values):
            raise ValueError("invalid cached fight outcome or value")
        duration, ally_hp, enemy_hp, damage, dps, frontline = values
        if (not 0 <= duration <= 120 or not 0 <= ally_hp <= 1 or not 0 <= enemy_hp <= 1
                or damage < 0 or dps < 0 or not 0 <= frontline <= duration + 1e-8):
            raise ValueError("cached fight value outside its domain")
        results.append(dict(zip(FIELDS, values), outcome=OUTCOMES[outcome]))
    return results


class ScoreCache:
    """One WAL database per combat namespace, shared by bounded warm workers."""
    def __init__(self, directory, namespace):
        self.stats = Counter()
        self.connection = None
        self.error = None
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            # Namespace is hashed here too so it can never escape the cache directory.
            name = hashlib.sha256(namespace.encode()).hexdigest()
            self.path = directory / (name + ".sqlite3")
            self.connection = sqlite3.connect(self.path, timeout=30)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS scores (signature BLOB PRIMARY KEY, result BLOB NOT NULL) WITHOUT ROWID")
            self.connection.commit()
        except (OSError, sqlite3.Error) as error:
            self._disable(error)

    def _disable(self, error):
        self.error = str(error)
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        warnings.warn(f"TFT score cache unavailable; fights will be computed normally: {error}", RuntimeWarning)

    def get_many(self, keys):
        if self.connection is None:
            return {}
        keys = list(dict.fromkeys(keys))
        found, corrupt = {}, []
        try:
            for start in range(0, len(keys), QUERY_BATCH):
                block = keys[start:start + QUERY_BATCH]
                marks = ",".join("?" for _ in block)
                for key, blob in self.connection.execute(f"SELECT signature,result FROM scores WHERE signature IN ({marks})", block):
                    try:
                        found[key] = decode(key, blob)
                    except (ValueError, struct.error):
                        corrupt.append(key)
            if corrupt:
                with self.connection:
                    self.connection.executemany("DELETE FROM scores WHERE signature=?", [(key,) for key in corrupt])
            self.stats["scoreCacheHits"] += len(found)
            self.stats["scoreCacheMisses"] += len(keys) - len(found)
            self.stats["scoreCacheCorruptRows"] += len(corrupt)
            return found
        except sqlite3.Error as error:
            self._disable(error)
            return {}

    def put_many(self, entries):
        if self.connection is None:
            return
        # Validate/encode before beginning a transaction: partial or invalid
        # evaluator output must never enter an apparently successful cache.
        rows = [(key, encode(key, result)) for key, result in entries]
        if not rows:
            return
        try:
            with self.connection:
                self.connection.executemany("INSERT OR IGNORE INTO scores(signature,result) VALUES (?,?)", rows)
            self.stats["scoreCacheWrites"] += len(rows)
        except sqlite3.Error as error:
            self._disable(error)

    def discard(self, key):
        if self.connection is None:
            return
        try:
            with self.connection:
                self.connection.execute("DELETE FROM scores WHERE signature=?", (key,))
            self.stats["scoreCacheCorruptRows"] += 1
        except sqlite3.Error as error:
            self._disable(error)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __del__(self):
        self.close()
