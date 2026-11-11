"""Strict tokenizer and recursive-descent parser for the competition SQL subset."""
from dataclasses import dataclass
import re


class DBError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(message, code='syntax_error'):
    raise DBError(code, message)


@dataclass
class Token:
    kind: str
    value: object
    start: int
    end: int


NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', re.ASCII)
IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')


def tokenize(sql):
    tokens, i = [], 0
    while i < len(sql):
        start, char = i, sql[i]
        if char.isspace():
            i += 1
            continue
        if sql.startswith('--', i) or char == '#':
            end = sql.find('\n', i)
            i = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith('/*', i):
            end = sql.find('*/', i + 2)
            if end < 0:
                fail('unterminated comment')
            i = end + 2
            continue
        if char in "'\"`":
            quote, value = char, []
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        value.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                # SQL uses doubled quotes; backslashes remain literal.
                value.append(sql[i])
                i += 1
            else:
                fail('unterminated quoted literal')
            kind = 'ident' if quote == '`' else 'string'
            tokens.append(Token(kind, ''.join(value), start, i))
            continue
        match = NUMBER.match(sql, i)
        if match:
            value = match.group()
            i = match.end()
            tokens.append(Token('number', value, start, i))
            continue
        match = IDENT.match(sql, i)
        if match:
            i = match.end()
            tokens.append(Token('ident', match.group(), start, i))
            continue
        if sql[i:i+2] in ('<=', '>=', '!=', '<>'):
            i += 2
        elif char in '(),;*=<>[]':
            i += 1
        else:
            fail(f'unsupported character at position {i}')
        tokens.append(Token('symbol', sql[start:i], start, i))
    tokens.append(Token('eof', '', i, i))
    return tokens


class Parser:
    def __init__(self, sql):
        if not isinstance(sql, str):
            fail('sql must be a string')
        self.sql, self.tokens, self.pos = sql, tokenize(sql), 0

    def at(self, value):
        token = self.tokens[self.pos]
        return token.kind in ('symbol', 'ident') and str(token.value).upper() == value

    def take(self, value):
        if self.at(value):
            self.pos += 1
            return True
        return False

    def need(self, value):
        if not self.take(value):
            fail(f'expected {value}')

    def ident(self):
        token = self.tokens[self.pos]
        if token.kind != 'ident':
            fail('expected identifier')
        self.pos += 1
        return str(token.value).lower()

    def integer(self):
        token = self.tokens[self.pos]
        if token.kind != 'number' or not re.fullmatch(r'[+-]?\d+', token.value, re.ASCII):
            fail('expected integer')
        self.pos += 1
        return int(token.value)

    def literal(self):
        token = self.tokens[self.pos]
        if token.kind == 'string':
            self.pos += 1
            return token.value
        if token.kind == 'number':
            self.pos += 1
            return int(token.value) if re.fullmatch(r'[+-]?\d+', token.value, re.ASCII) else float(token.value)
        if self.take('['):
            result = []
            if not self.take(']'):
                while True:
                    result.append(self.literal())
                    if not self.take(','):
                        break
                self.need(']')
            return result
        fail('expected non-null literal')

    def expr(self):
        start = self.tokens[self.pos].start
        column = self.ident()
        if column == 'l2_distance' and self.take('('):
            vector_column = self.ident()
            self.need(',')
            vector = self.literal()
            self.need(')')
            return ('distance', vector_column, vector, self.sql[start:self.tokens[self.pos-1].end])
        return ('column', column)

    def where(self):
        if not self.take('WHERE'):
            return []
        def term():
            if self.take('('):
                result = conjunction()
                self.need(')')
                return result
            column = self.ident()
            op = self.tokens[self.pos].value
            if op not in ('=', '<', '<=', '>', '>='):
                fail('unsupported comparison', 'unsupported_sql')
            self.pos += 1
            return [(column, op, self.literal())]
        def conjunction():
            result = term()
            while self.take('AND'):
                result.extend(term())
            return result
        return conjunction()

    def parse(self):
        if self.take('CREATE'):
            if self.take('VECTOR'):
                self.need('INDEX')
                name = self.ident()
                self.need('ON')
                table = self.ident()
                self.need('(')
                column = self.ident()
                self.need(')')
                result = dict(op='index', table=table, name=name, column=column)
            else:
                self.need('TABLE')
                table, columns, primary = self.ident(), [], None
                self.need('(')
                while True:
                    if self.take('PRIMARY'):
                        self.need('KEY')
                        self.need('(')
                        name = self.ident()
                        self.need(')')
                        if primary is not None:
                            fail('multiple primary keys')
                        primary = name
                    else:
                        name, kind = self.ident(), self.ident().upper()
                        if kind not in ('BIGINT', 'VARCHAR', 'VECTOR'):
                            fail('unsupported data type', 'unsupported_sql')
                        size = None
                        if kind != 'BIGINT':
                            self.need('(')
                            size = self.integer()
                            self.need(')')
                            if size <= 0:
                                fail('type size must be positive', 'type_error')
                        if self.take('PRIMARY'):
                            self.need('KEY')
                            if primary is not None:
                                fail('multiple primary keys')
                            primary = name
                        columns.append(dict(name=name, kind=kind, size=size))
                    if not self.take(','):
                        break
                self.need(')')
                result = dict(op='create', table=table, columns=columns, primary=primary)
        elif self.take('INSERT'):
            self.need('INTO')
            table, columns, rows = self.ident(), None, []
            if self.take('('):
                columns = [self.ident()]
                while self.take(','):
                    columns.append(self.ident())
                self.need(')')
            self.need('VALUES')
            while True:
                self.need('(')
                row = [self.literal()]
                while self.take(','):
                    row.append(self.literal())
                self.need(')')
                rows.append(row)
                if not self.take(','):
                    break
            result = dict(op='insert', table=table, columns=columns, rows=rows)
        elif self.take('UPDATE'):
            table, assignments = self.ident(), []
            self.need('SET')
            while True:
                column = self.ident()
                self.need('=')
                assignments.append((column, self.literal()))
                if not self.take(','):
                    break
            result = dict(op='update', table=table, assignments=assignments, where=self.where())
        elif self.take('DELETE'):
            self.need('FROM')
            result = dict(op='delete', table=self.ident(), where=self.where())
        elif self.take('SELECT'):
            projection = []
            if self.take('*'):
                projection = None
            else:
                while True:
                    expression = self.expr()
                    alias = self.ident() if self.take('AS') else None
                    projection.append((expression, alias))
                    if not self.take(','):
                        break
            self.need('FROM')
            table = self.ident()
            conditions = self.where()
            ordering = []
            if self.take('ORDER'):
                self.need('BY')
                while True:
                    expression = self.expr()
                    desc = self.take('DESC')
                    if not desc:
                        self.take('ASC')
                    ordering.append((expression, desc))
                    if not self.take(','):
                        break
            limit = self.integer() if self.take('LIMIT') else None
            if limit is not None and limit < 0:
                fail('negative limit')
            result = dict(op='select', table=table, projection=projection, where=conditions, ordering=ordering, limit=limit)
        elif self.take('BEGIN'):
            self.take('TRANSACTION')
            result = dict(op='begin')
        elif self.take('START'):
            self.need('TRANSACTION')
            result = dict(op='begin')
        elif self.take('COMMIT'):
            result = dict(op='commit')
        elif self.take('ROLLBACK'):
            result = dict(op='rollback')
        else:
            fail('unsupported SQL statement', 'unsupported_sql')
        self.take(';')
        if self.tokens[self.pos].kind != 'eof':
            fail('unexpected trailing SQL; only one supported statement is allowed', 'unsupported_sql')
        return result
