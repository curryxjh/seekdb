"""Framed commit log and rebuildable, disk-resident open-addressed hash indexes.

Only a complete, checksummed frame followed by its commit trailer is replayed.
The append log is authoritative; all hash indexes and transaction files are scratch.
"""
import copy
import json
import mmap
import os
from pathlib import Path
import shutil
import struct
import tempfile
import zlib

from .sql import DBError

U32 = struct.Struct('<I')
PAIR = struct.Struct('<qQ')
ROW_HEADER = struct.Struct('<Qq')
FRAME = struct.Struct('<8sQI')
MAGIC = b'MVTX0001'
TRAILER = b'COMMIT01'
CHUNK = 1024 * 1024


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def hash_key(key):
    # SplitMix64 prevents sequential/adversarially aligned BIGINTs clustering.
    mask = (1 << 64) - 1
    value = (key + 0x9e3779b97f4a7c15) & mask
    value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & mask
    value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & mask
    return value ^ (value >> 31)


class DiskIndex:
    def __init__(self, path, capacity=1024):
        self.path, self.capacity, self.used = Path(path), capacity, 0
        self.file = open(self.path, 'w+b', buffering=0)
        self.file.truncate(capacity * PAIR.size)
        self.map = mmap.mmap(self.file.fileno(), 0)

    def slot(self, key):
        slot = hash_key(key) & (self.capacity - 1)
        while True:
            stored, value = PAIR.unpack_from(self.map, slot * PAIR.size)
            if value == 0 or stored == key:
                return slot, value
            slot = (slot + 1) & (self.capacity - 1)

    def get(self, key):
        _, value = self.slot(key)
        return None if value == 0 else value - 2

    def set(self, key, offset):
        slot, value = self.slot(key)
        if value == 0 and (self.used + 1) * 10 > self.capacity * 7:
            self.grow()
            slot, value = self.slot(key)
        if value == 0:
            self.used += 1
        PAIR.pack_into(self.map, slot * PAIR.size, key, offset + 2)

    def items(self):
        for slot in range(self.capacity):
            key, value = PAIR.unpack_from(self.map, slot * PAIR.size)
            if value:
                yield key, value - 2

    def grow(self):
        larger = DiskIndex(str(self.path) + '.grow', self.capacity * 2)
        for key, offset in self.items():
            larger.set(key, offset)
        old_path = self.path
        self.close()
        larger.map.close()
        larger.file.close()
        os.replace(larger.path, old_path)
        self.capacity, self.used = larger.capacity, larger.used
        self.file = open(old_path, 'r+b', buffering=0)
        self.map = mmap.mmap(self.file.fileno(), 0)

    def close(self):
        self.map.close()
        self.file.close()


class VectorFile:
    """Rebuildable exact flat vector index with a disk hash of live positions."""
    def __init__(self, parent, tid, dimension):
        self.file = open(parent / f'vector-{tid}.flat', 'w+b', buffering=0)
        self.index = DiskIndex(parent / f'vector-{tid}.idx')
        self.codec = struct.Struct(f'<{dimension}f')

    def set(self, key, vector):
        self.file.seek(0, os.SEEK_END)
        offset = self.file.tell()
        write_all(self.file, self.codec.pack(*vector))
        self.index.set(key, offset)

    def rows(self):
        for key, offset in self.index.items():
            if offset >= 0:
                self.file.seek(offset)
                yield key, self.codec.unpack(self.file.read(self.codec.size))

    def close(self):
        self.index.close()
        self.file.close()


class Layer:
    def __init__(self, parent, catalog):
        self.path = Path(tempfile.mkdtemp(prefix='stage-', dir=parent))
        self.file = open(self.path / 'records', 'w+b', buffering=0)
        self.indexes = {}
        self.catalog = copy.deepcopy(catalog)

    def index(self, tid):
        if tid not in self.indexes:
            self.indexes[tid] = DiskIndex(self.path / f'{tid}.idx')
        return self.indexes[tid]

    def append(self, payload):
        self.file.seek(0, os.SEEK_END)
        offset = self.file.tell() + U32.size
        write_all(self.file, U32.pack(len(payload)))
        write_all(self.file, payload)
        if payload[:1] in (b'R', b'D'):
            tid, key = ROW_HEADER.unpack_from(payload, 1)
            self.index(tid).set(key, -1 if payload[:1] == b'D' else offset)
        return offset

    def merge(self, other):
        other.file.seek(0, os.SEEK_END)
        size = other.file.tell()
        for _, payload in records(other.file, 0, size):
            self.append(payload)
        self.catalog = other.catalog

    def close(self):
        for index in self.indexes.values():
            index.close()
        self.file.close()
        shutil.rmtree(self.path)


def write_all(file, data):
    view = memoryview(data)
    while view:
        size = file.write(view)
        if not size:
            raise OSError('short write')
        view = view[size:]


def records(file, start, end):
    offset = start
    while offset < end:
        file.seek(offset)
        raw = file.read(U32.size)
        if len(raw) != U32.size:
            raise OSError('truncated record length')
        length = U32.unpack(raw)[0]
        if length < 1 or offset + U32.size + length > end:
            raise OSError('invalid record length')
        payload = file.read(length)
        if len(payload) != length:
            raise OSError('truncated record')
        yield offset + U32.size, payload
        offset += U32.size + length
    if offset != end:
        raise OSError('invalid frame boundary')


def read_payload(file, offset):
    file.seek(offset - U32.size)
    raw = file.read(U32.size)
    if len(raw) != U32.size:
        raise OSError('missing record')
    length = U32.unpack(raw)[0]
    payload = file.read(length)
    if len(payload) != length:
        raise OSError('truncated record')
    return payload


def encode_row(schema, row):
    out = bytearray()
    for column, value in zip(schema['columns'], row):
        kind = column['kind']
        if kind == 'BIGINT':
            out.extend(struct.pack('<q', value))
        elif kind == 'VARCHAR':
            raw = value.encode('utf-8')
            out.extend(U32.pack(len(raw)))
            out.extend(raw)
        else:
            out.extend(struct.pack(f'<{len(value)}f', *value))
    return bytes(out)


def decode_row(schema, payload):
    offset, row = 1 + ROW_HEADER.size, []
    for column in schema['columns']:
        kind = column['kind']
        if kind == 'BIGINT':
            row.append(struct.unpack_from('<q', payload, offset)[0])
            offset += 8
        elif kind == 'VARCHAR':
            size = U32.unpack_from(payload, offset)[0]
            offset += U32.size
            row.append(payload[offset:offset+size].decode('utf-8'))
            offset += size
        else:
            size = column['size']
            row.append(list(struct.unpack_from(f'<{size}f', payload, offset)))
            offset += 4 * size
    if offset != len(payload):
        raise OSError('row/schema mismatch')
    return row


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.directory / 'LOCK', 'a+b')
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DBError('database_locked', 'data directory is already in use') from exc
        # A fixed private scratch subtree is discardable even after interrupted recovery.
        self.scratch = self.directory / 'scratch'
        if self.scratch.exists():
            if self.scratch.is_symlink():
                raise OSError('scratch directory must not be a symlink')
            shutil.rmtree(self.scratch)
        self.scratch.mkdir()
        self.catalog, self.indexes, self.transaction = {}, {}, None
        self.vector_files, self.by_tid = {}, {}
        self.wal = open(self.directory / 'commit.log', 'a+b', buffering=0)
        self.recover()
        if os.name == 'posix':
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @property
    def visible_catalog(self):
        return self.transaction.catalog if self.transaction is not None else self.catalog

    def index(self, tid):
        if tid not in self.indexes:
            self.indexes[tid] = DiskIndex(self.scratch / f'base-{tid}.idx')
        return self.indexes[tid]

    def apply(self, offset, payload):
        tag = payload[:1]
        if tag == b'C':
            schema = json.loads(payload[1:])
            self.catalog[schema['name']] = schema
            self.by_tid[schema['tid']] = schema
        elif tag == b'I':
            change = json.loads(payload[1:])
            schema = self.catalog[change['table']]
            schema['indexes'][change['name']] = change['column']
            tid = schema['tid']
            if tid not in self.vector_files:
                col = next(i for i, c in enumerate(schema['columns']) if c['kind'] == 'VECTOR')
                primary = next(i for i, c in enumerate(schema['columns']) if c['name'] == schema['primary'])
                flat = VectorFile(self.scratch, tid, schema['columns'][col]['size'])
                self.vector_files[tid] = flat
                for row in self.rows(schema):
                    flat.set(row[primary], row[col])
        elif tag in (b'R', b'D'):
            tid, key = ROW_HEADER.unpack_from(payload, 1)
            self.index(tid).set(key, -1 if tag == b'D' else offset)
            if tid in self.vector_files:
                flat = self.vector_files[tid]
                if tag == b'D':
                    flat.index.set(key, -1)
                else:
                    schema = self.by_tid[tid]
                    col = next(i for i, c in enumerate(schema['columns']) if c['kind'] == 'VECTOR')
                    flat.set(key, decode_row(schema, payload)[col])
        else:
            raise OSError('unknown log operation')

    def recover(self):
        size, position = os.fstat(self.wal.fileno()).st_size, 0
        while position + FRAME.size <= size:
            self.wal.seek(position)
            magic, length, expected = FRAME.unpack(self.wal.read(FRAME.size))
            end = position + FRAME.size + length
            if magic != MAGIC or end + len(TRAILER) > size:
                break
            remaining, crc = length, 0
            while remaining:
                chunk = self.wal.read(min(CHUNK, remaining))
                if not chunk:
                    raise OSError('unexpected EOF')
                crc = zlib.crc32(chunk, crc)
                remaining -= len(chunk)
            if crc != expected or self.wal.read(len(TRAILER)) != TRAILER:
                break
            for offset, payload in records(self.wal, position + FRAME.size, end):
                self.apply(offset, payload)
            position = end + len(TRAILER)
        # No persistent derived state has been modified; another kill here is safe.
        if position != size:
            self.wal.truncate(position)
            os.fsync(self.wal.fileno())
        self.wal.seek(0, os.SEEK_END)

    def new_layer(self):
        return Layer(self.scratch, self.visible_catalog)

    def begin(self):
        if self.transaction is not None:
            raise DBError('transaction_error', 'nested transactions are unsupported')
        self.transaction = self.new_layer()

    def rollback(self):
        if self.transaction is not None:
            self.transaction.close()
            self.transaction = None

    def commit_layer(self, layer):
        layer.file.seek(0, os.SEEK_END)
        length = layer.file.tell()
        if not length:
            return
        layer.file.seek(0)
        crc = 0
        while chunk := layer.file.read(CHUNK):
            crc = zlib.crc32(chunk, crc)
        self.wal.seek(0, os.SEEK_END)
        start = self.wal.tell()
        write_all(self.wal, FRAME.pack(MAGIC, length, crc))
        layer.file.seek(0)
        while chunk := layer.file.read(CHUNK):
            write_all(self.wal, chunk)
        write_all(self.wal, TRAILER)
        os.fsync(self.wal.fileno())
        for offset, payload in records(self.wal, start + FRAME.size, start + FRAME.size + length):
            self.apply(offset, payload)

    def commit(self):
        if self.transaction is not None:
            self.commit_layer(self.transaction)
            self.rollback()

    def accept(self, layer):
        if self.transaction is not None:
            self.transaction.merge(layer)
        else:
            self.commit_layer(layer)

    def locate(self, tid, key, statement=None):
        for layer in (statement, self.transaction):
            if layer is not None and tid in layer.indexes:
                offset = layer.indexes[tid].get(key)
                if offset is not None:
                    return None if offset < 0 else (layer.file, offset)
        offset = self.index(tid).get(key)
        return None if offset is None or offset < 0 else (self.wal, offset)

    def rows(self, schema, key=None):
        tid = schema['tid']
        if key is not None:
            location = self.locate(tid, key)
            if location is not None:
                yield decode_row(schema, read_payload(*location))
            return
        overlay = None if self.transaction is None else self.transaction.indexes.get(tid)
        for pk, offset in self.index(tid).items():
            if offset >= 0 and (overlay is None or overlay.get(pk) is None):
                yield decode_row(schema, read_payload(self.wal, offset))
        if overlay is not None:
            for _, offset in overlay.items():
                if offset >= 0:
                    yield decode_row(schema, read_payload(self.transaction.file, offset))

    def vectors(self, schema):
        """Read the current transaction's exact vector view without document text."""
        tid = schema['tid']
        col = next(i for i, c in enumerate(schema['columns']) if c['kind'] == 'VECTOR')
        primary = next(i for i, c in enumerate(schema['columns']) if c['name'] == schema['primary'])
        if tid not in self.vector_files:
            for row in self.rows(schema):
                yield row[primary], row[col]
            return
        overlay = None if self.transaction is None else self.transaction.indexes.get(tid)
        for key, vector in self.vector_files[tid].rows():
            if overlay is None or overlay.get(key) is None:
                yield key, vector
        if overlay is not None:
            for key, offset in overlay.items():
                if offset >= 0:
                    row = decode_row(schema, read_payload(self.transaction.file, offset))
                    yield key, row[col]

    def close(self):
        self.rollback()
        for index in self.indexes.values():
            index.close()
        for flat in self.vector_files.values():
            flat.close()
        self.wal.close()
        self.lock_file.close()
        shutil.rmtree(self.scratch)
