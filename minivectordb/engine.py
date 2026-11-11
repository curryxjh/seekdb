"""Type checking, transactional mutations, streaming scans and bounded sorting."""
from dataclasses import dataclass
import heapq
import json
import math
import operator
import os
import pickle
import struct
import sys
import tempfile

from .sql import DBError, Parser, fail
from .storage import Store, ROW_HEADER, encode_row, json_bytes

try:
    import numpy as np
except ImportError:
    np = None

COMPARE = {'=': operator.eq, '<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge}
CACHE_BUDGET = 48 * 1024 * 1024
SORT_BUDGET = 8 * 1024 * 1024


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = item
        return result
    def reject(value):
        raise ValueError('non-finite JSON constant')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject(value)
        return number
    return json.loads(value, object_pairs_hook=pairs, parse_constant=reject, parse_float=finite_float)


def convert(column, value):
    kind = column['kind']
    if kind == 'BIGINT':
        if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
            fail('BIGINT requires a signed 64-bit integer', 'type_error')
    elif kind == 'VARCHAR':
        if not isinstance(value, str) or len(value) > column['size']:
            fail('VARCHAR type or Unicode character length mismatch', 'type_error')
        try:
            value.encode('utf-8')
        except UnicodeError:
            fail('invalid Unicode string', 'type_error')
    else:
        if isinstance(value, str):
            try:
                value = strict_json(value)
            except (ValueError, TypeError):
                fail('invalid vector literal', 'invalid_vector')
        if not isinstance(value, list) or len(value) != column['size']:
            fail('vector dimension mismatch', 'invalid_vector')
        rounded = []
        for item in value:
            if type(item) not in (float, int):
                fail('vector elements must be finite numeric values', 'invalid_vector')
            try:
                number = float(item)
                if not math.isfinite(number):
                    raise ValueError()
                stored = struct.unpack('<f', struct.pack('<f', number))[0]
                if not math.isfinite(stored):
                    raise ValueError()
            except (ValueError, OverflowError, struct.error):
                fail('vector element cannot be represented as finite float32', 'invalid_vector')
            rounded.append(stored)
        value = rounded
    return value


@dataclass(frozen=True)
class Descending:
    value: object

    def __lt__(self, other):
        return self.value > other.value


def external_sorted(items, directory):
    """Sort with bounded run buffers; the merge opens at most 32 runs at a time."""
    with tempfile.TemporaryDirectory(prefix='sort-', dir=directory) as folder:
        run_paths, pending, size, serial = [], [], 0, 0
        def write_run(values):
            nonlocal serial
            path = os.path.join(folder, str(serial))
            serial += 1
            with open(path, 'wb') as file:
                for value in values:
                    pickle.dump(value, file, protocol=5)
            return path
        def read_run(path):
            with open(path, 'rb') as file:
                while True:
                    try:
                        yield pickle.load(file)
                    except EOFError:
                        return
        for item in items:
            pending.append(item)
            size += len(pickle.dumps(item, protocol=5)) + 256
            if size >= SORT_BUDGET:
                pending.sort(key=lambda pair: pair[0])
                run_paths.append(write_run(pending))
                pending, size = [], 0
        if not run_paths:
            yield from sorted(pending, key=lambda pair: pair[0])
            return
        if pending:
            pending.sort(key=lambda pair: pair[0])
            run_paths.append(write_run(pending))
        while len(run_paths) > 32:
            merged = []
            for start in range(0, len(run_paths), 32):
                group = run_paths[start:start+32]
                merged.append(write_run(heapq.merge(*(read_run(p) for p in group), key=lambda pair: pair[0])))
                for path in group:
                    os.unlink(path)
            run_paths = merged
        yield from heapq.merge(*(read_run(p) for p in run_paths), key=lambda pair: pair[0])


class VectorCache:
    """One bounded table cache; disk state is always the source of truth.

    Optional self-built median-partition / bounding-box index. Every candidate
    distance is exact; ANN requests may use this stronger exact result.
    """
    def __init__(self, store, schema):
        self.schema, self.columns, self.boxes = schema, [], None
        tid = schema['tid']
        estimate = store.index(tid).used
        if store.transaction is not None and tid in store.transaction.indexes:
            estimate += store.transaction.indexes[tid].used
        # float64 represents stored float32 exactly and avoids subtraction overflow.
        row_size = sum(c['size'] * 8 if c['kind'] == 'VECTOR' else 8 if c['kind'] == 'BIGINT' else 72 for c in schema['columns'])
        if estimate * (row_size + 16) > CACHE_BUDGET:
            raise MemoryError('cache budget')
        allocated = estimate * row_size
        for col in schema['columns']:
            if col['kind'] == 'VECTOR':
                self.columns.append(np.empty((estimate, col['size']), dtype=np.float64))
            elif col['kind'] == 'BIGINT':
                self.columns.append(np.empty(estimate, dtype=np.int64))
            else:
                self.columns.append([])
        count = 0
        for row in store.rows(schema):
            for i, (col, value) in enumerate(zip(schema['columns'], row)):
                if col['kind'] == 'VARCHAR':
                    allocated += sys.getsizeof(value) + 8
                    if allocated > CACHE_BUDGET:
                        raise MemoryError('cache budget')
                    self.columns[i].append(value)
                else:
                    self.columns[i][count] = value
            count += 1
        self.count = count
        for i, col in enumerate(schema['columns']):
            if col['kind'] == 'VARCHAR':
                self.columns[i] = np.asarray(self.columns[i], dtype=object)
            else:
                self.columns[i] = self.columns[i][:count]
        if schema['indexes'] and count:
            self.build_index()

    def build_index(self):
        col = next(i for i, c in enumerate(self.schema['columns']) if c['kind'] == 'VECTOR')
        vectors = self.columns[col]
        order = np.arange(self.count)
        leaves = []
        def partition(start, stop):
            subset = vectors[order[start:stop]]
            if stop - start <= 128:
                leaves.append((start, stop, subset.min(axis=0), subset.max(axis=0)))
                return
            dimension = int(np.argmax(np.ptp(subset, axis=0)))
            middle = (stop - start) // 2
            perm = np.argpartition(subset[:, dimension], middle)
            order[start:stop] = order[start:stop][perm]
            del subset, perm
            partition(start, start + middle)
            partition(start + middle, stop)
        partition(0, self.count)
        self.columns = [column[order] for column in self.columns]
        self.boxes = leaves

    def row(self, index):
        result = []
        for col, values in zip(self.schema['columns'], self.columns):
            value = values[index]
            result.append(value.tolist() if col['kind'] == 'VECTOR' else int(value) if col['kind'] == 'BIGINT' else value)
        return result

    def topk(self, conditions, vector_col, query, k, primary):
        mask = np.ones(self.count, dtype=bool)
        for index, op, value in conditions:
            mask &= COMPARE[op](self.columns[index], value)
        candidates = np.flatnonzero(mask)
        if not len(candidates) or k == 0:
            return []
        k = min(k, len(candidates))
        vectors = self.columns[vector_col]
        q = np.asarray(query, dtype=np.float64)
        if self.boxes is not None and len(self.boxes) > 1:
            # Bounding boxes give admissible lower bounds, including filtered data.
            # A tolerance prevents roundoff at a boundary from discarding a tie.
            bounds = []
            for start, stop, low, high in self.boxes:
                gap = np.maximum(np.maximum(low - q, q - high), 0.0)
                bounds.append((float(np.dot(gap, gap)), start, stop))
            best, threshold = [], math.inf
            for bound, start, stop in sorted(bounds):
                if bound > threshold + max(1e-12, threshold * 1e-12):
                    break
                local = np.flatnonzero(mask[start:stop]) + start
                if not len(local):
                    continue
                delta = vectors[local] - q
                distances = np.einsum('ij,ij->i', delta, delta)
                for index, distance in zip(local, distances):
                    key = (-float(distance), -int(self.columns[primary][index]), int(index))
                    if len(best) < k:
                        heapq.heappush(best, key)
                    elif key > best[0]:
                        heapq.heapreplace(best, key)
                if len(best) == k:
                    threshold = -best[0][0]
            return [(index, math.sqrt(-negative)) for negative, _, index in sorted(best, key=lambda x: (-x[0], -x[1]))]
        delta = vectors[candidates] - q
        distances = np.einsum('ij,ij->i', delta, delta)
        if len(candidates) > k:
            threshold = np.partition(distances, k-1)[k-1]
            keep = distances <= threshold
            candidates, distances = candidates[keep], distances[keep]
        ordered = np.lexsort((self.columns[primary][candidates], distances))[:k]
        return [(int(candidates[i]), math.sqrt(float(distances[i]))) for i in ordered]


class Database:
    def __init__(self, directory):
        self.store = Store(directory)
        self.cache = None
        self.cache_disabled = set()

    def invalidate(self):
        self.cache = None
        self.cache_disabled.clear()

    def schema(self, table):
        schema = self.store.visible_catalog.get(table)
        if schema is None:
            fail(f'unknown table: {table}', 'unknown_table')
        return schema

    @staticmethod
    def column(schema, name):
        for i, col in enumerate(schema['columns']):
            if col['name'] == name:
                return i
        fail(f'unknown column: {name}', 'unknown_column')

    def conditions(self, schema, conditions):
        result = []
        for name, op, value in conditions:
            index = self.column(schema, name)
            col = schema['columns'][index]
            if col['kind'] == 'VECTOR':
                fail('vector comparisons are unsupported', 'unsupported_sql')
            # Comparison string literals need not obey the column's storage length.
            if col['kind'] == 'VARCHAR':
                if not isinstance(value, str):
                    fail('string comparison requires a string', 'type_error')
            else:
                value = convert(col, value)
            result.append((index, op, value))
        return result

    @staticmethod
    def matches(row, conditions):
        return all(COMPARE[op](row[index], value) for index, op, value in conditions)

    def scan(self, schema, conditions):
        primary = self.column(schema, schema['primary'])
        key = next((value for i, op, value in conditions if i == primary and op == '='), None)
        return (row for row in self.store.rows(schema, key) if self.matches(row, conditions))

    def execute(self, sql, mode='exact'):
        if mode not in ('exact', 'ann'):
            fail('search_mode must be exact or ann', 'invalid_request')
        query = Parser(sql).parse()
        op = query['op']
        if op in ('begin', 'commit', 'rollback'):
            getattr(self.store, op)()
            self.invalidate()
            return {}
        if op == 'select':
            return self.select(query)
        layer = self.store.new_layer()
        try:
            result = self.mutate(query, layer)
            self.store.accept(layer)
            self.invalidate()
            return result
        finally:
            layer.close()

    def mutate(self, query, layer):
        op, table = query['op'], query['table']
        if op == 'create':
            if table in layer.catalog:
                fail('table already exists', 'table_exists')
            columns = query['columns']
            names = [col['name'] for col in columns]
            if len(set(names)) != len(names):
                fail('duplicate column name', 'duplicate_column')
            primary = query['primary']
            if primary not in names or columns[names.index(primary)]['kind'] != 'BIGINT':
                fail('exactly one BIGINT primary key is required', 'type_error')
            if sum(col['kind'] == 'VECTOR' for col in columns) > 1:
                fail('at most one vector column is supported', 'type_error')
            tid = max((s['tid'] for s in layer.catalog.values()), default=0) + 1
            schema = dict(name=table, tid=tid, columns=columns, primary=primary, indexes={})
            layer.catalog[table] = schema
            layer.append(b'C' + json_bytes(schema))
            return {}
        schema = self.schema(table)
        if op == 'index':
            index = self.column(schema, query['column'])
            if schema['columns'][index]['kind'] != 'VECTOR':
                fail('vector index requires a vector column', 'type_error')
            if any(query['name'] in s['indexes'] for s in layer.catalog.values()):
                fail('index already exists', 'index_exists')
            layer.catalog[table]['indexes'][query['name']] = query['column']
            layer.append(b'I' + json_bytes(dict(table=table, name=query['name'], column=query['column'])))
            return {}
        primary = self.column(schema, schema['primary'])
        count = 0
        if op == 'insert':
            names = query['columns'] or [c['name'] for c in schema['columns']]
            positions = [self.column(schema, name) for name in names]
            if len(set(positions)) != len(schema['columns']) or len(positions) != len(schema['columns']):
                fail('INSERT must specify every column exactly once', 'type_error')
            for values in query['rows']:
                if len(values) != len(positions):
                    fail('column/value count mismatch', 'type_error')
                row = [None] * len(positions)
                for index, value in zip(positions, values):
                    row[index] = convert(schema['columns'][index], value)
                key = row[primary]
                if self.store.locate(schema['tid'], key, layer) is not None:
                    fail('duplicate primary key', 'duplicate_key')
                layer.append(b'R' + ROW_HEADER.pack(schema['tid'], key) + encode_row(schema, row))
                count += 1
        else:
            conditions = self.conditions(schema, query['where'])
            assignments = []
            if op == 'update':
                seen = set()
                for name, value in query['assignments']:
                    index = self.column(schema, name)
                    if index == primary:
                        fail('primary key updates are unsupported', 'unsupported_sql')
                    if index in seen:
                        fail('duplicate assignment')
                    seen.add(index)
                    assignments.append((index, convert(schema['columns'][index], value)))
            for row in self.scan(schema, conditions):
                key = row[primary]
                if op == 'delete':
                    payload = b'D' + ROW_HEADER.pack(schema['tid'], key)
                else:
                    for index, value in assignments:
                        row[index] = value
                    payload = b'R' + ROW_HEADER.pack(schema['tid'], key) + encode_row(schema, row)
                layer.append(payload)
                count += 1
        return {'affected_rows': count}

    def compile_expr(self, schema, expression):
        index = self.column(schema, expression[1])
        if expression[0] == 'column':
            return ('column', index)
        col = schema['columns'][index]
        if col['kind'] != 'VECTOR':
            fail('L2_DISTANCE requires a vector column', 'type_error')
        return ('distance', index, convert(col, expression[2]))

    @staticmethod
    def evaluate(expression, row):
        if expression[0] == 'column':
            return row[expression[1]]
        return math.sqrt(math.fsum((a-b) ** 2 for a, b in zip(row[expression[1]], expression[2])))

    def select(self, query):
        schema = self.schema(query['table'])
        primary = self.column(schema, schema['primary'])
        conditions = self.conditions(schema, query['where'])
        projection = query['projection']
        if projection is None:
            projection = [(('column', col['name']), None) for col in schema['columns']]
        names, expressions, aliases = [], [], {}
        for expression, alias in projection:
            compiled = self.compile_expr(schema, expression)
            name = alias or (expression[1] if expression[0] == 'column' else expression[3])
            names.append(name)
            expressions.append(compiled)
            if alias:
                if alias in aliases:
                    fail('ambiguous projection alias')
                aliases[alias] = compiled
        ordering = []
        for expression, desc in query['ordering']:
            compiled = aliases.get(expression[1]) if expression[0] == 'column' else None
            compiled = compiled or self.compile_expr(schema, expression)
            if compiled[0] == 'column' and schema['columns'][compiled[1]]['kind'] == 'VECTOR':
                fail('vector ordering is unsupported', 'unsupported_sql')
            ordering.append((compiled, desc))
        limit = query['limit']
        if limit == 0:
            return dict(columns=names, rows=iter(()))
        if np is not None and limit is not None and len(ordering) == 1 and not ordering[0][1] and ordering[0][0][0] == 'distance':
            tid = schema['tid']
            if tid not in self.cache_disabled:
                if self.cache is None or self.cache.schema['tid'] != tid:
                    self.cache = None
                    try:
                        self.cache = VectorCache(self.store, schema)
                    except MemoryError:
                        self.cache_disabled.add(tid)
                if self.cache is not None:
                    distance = ordering[0][0]
                    selected = self.cache.topk(conditions, distance[1], distance[2], limit, primary)
                    def cached_rows():
                        for index, value in selected:
                            row = self.cache.row(index)
                            yield [value if expr == distance else self.evaluate(expr, row) for expr in expressions]
                    return dict(columns=names, rows=cached_rows())
        if limit is not None and limit <= 4096 and len(ordering) == 1 and not ordering[0][1] and ordering[0][0][0] == 'distance' and schema['indexes']:
            distance = ordering[0][0]
            def vector_candidates():
                for key, vector in self.store.vectors(schema):
                    if conditions:
                        row = next(self.store.rows(schema, key))
                        if not self.matches(row, conditions):
                            continue
                    value = math.sqrt(math.fsum((a-b) ** 2 for a,b in zip(vector, distance[2])))
                    yield value, key
            chosen = heapq.nsmallest(limit, vector_candidates())
            def indexed_rows():
                for value, key in chosen:
                    row = next(self.store.rows(schema, key))
                    yield [value if expr == distance else self.evaluate(expr, row) for expr in expressions]
            return dict(columns=names, rows=indexed_rows())
        def items():
            for row in self.scan(schema, conditions):
                key = tuple(Descending(self.evaluate(expr, row)) if desc else self.evaluate(expr, row) for expr, desc in ordering)
                yield (key + (row[primary],), row)
        if ordering:
            # nsmallest is bounded by K; large K uses disk merge sorting instead.
            row_bound = sum(32 * c['size'] + 64 if c['kind'] == 'VECTOR' else 4 * c['size'] + 64 if c['kind'] == 'VARCHAR' else 32 for c in schema['columns'])
            if limit is not None and limit <= 4096 and limit * (row_bound + 256) <= SORT_BUDGET:
                selected = heapq.nsmallest(limit, items(), key=lambda pair: pair[0])
            else:
                selected = external_sorted(items(), self.store.scratch)
        else:
            selected = items()
        def output_rows():
            for count, (_, row) in enumerate(selected):
                if limit is not None and count >= limit:
                    break
                yield [self.evaluate(expr, row) for expr in expressions]
        return dict(columns=names, rows=output_rows())

    def close(self):
        self.invalidate()
        self.store.close()
