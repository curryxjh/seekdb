"""Local structural checks for the published conversation.json input contract.

This does not run the organizer's evaluator, assign a score, or assert an
unpublished platform schema. The SHA256 describes the exact bytes checked.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

LIMIT = 128 * 1024
SECRET = re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|https://[^/\s]+:[^/\s]+@github\.com')


def strict_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f'non-finite JSON number: {value}')

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            reject(text)
        return value

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject, parse_float=number)


def resolve(document, pointer):
    if pointer == '':
        return document
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('invalid RFC6901 pointer')
    current = document
    for raw in pointer[1:].split('/'):
        if re.search(r'~(?:[^01]|$)', raw):
            raise ValueError('invalid RFC6901 escape')
        part = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(current, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', part):
                raise ValueError('invalid JSON array index')
            try:
                current = current[int(part)]
            except IndexError as exc:
                raise ValueError('JSON array index out of range') from exc
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError('JSON pointer target not found')
    return current


def check(raw):
    if len(raw) > LIMIT:
        raise ValueError('material exceeds 128 KiB')
    if raw.startswith(b'\xef\xbb\xbf'):
        raise ValueError('UTF-8 BOM is not accepted by this strict local check')
    text = raw.decode('utf-8')
    data = strict_loads(text)
    if not isinstance(data, dict):
        raise ValueError('top-level value must be an object')
    # Scan decoded strings too, so JSON escapes cannot hide a recognizable token.
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from strings(item)
    for value in strings(data):
        value.encode('utf-8')
        if SECRET.search(value):
            raise ValueError('recognizable GitHub credential detected; value omitted')
    citations = 0
    for item in data.get('evidence_index', []):
        for citation in item.get('citations', []):
            target = resolve(data, citation['pointer'])
            quote = citation['quote']
            if not isinstance(target, str) or not isinstance(quote, str) or not quote or len(quote) > 240:
                raise ValueError('citation must contain a nonempty <=240-character quote into a string field')
            if quote not in target:
                raise ValueError('citation quote does not match its target')
            citations += 1
    conversations = data.get('conversations', [])
    return {
        'scope': 'local-material-structure-check',
        'passed': True,
        'bytes': len(raw),
        'max_bytes': LIMIT,
        'sha256': hashlib.sha256(raw).hexdigest(),
        'checks': ['UTF-8 object', 'unique keys', 'finite JSON numbers', 'within 128 KiB',
                   'recognizable GitHub credential patterns absent', 'local evidence references resolve'],
        'evidence_citations_checked': citations,
        'student_messages': sum(item.get('role') == 'student' for item in conversations),
        'summary_present': isinstance(data.get('summary'), str) and bool(data['summary'].strip()),
        'limitations': 'Structural validation only. Secret scan covers recognizable GitHub patterns. No official score, review status, accepted branch configuration, or platform material digest is inferred. summary and evidence_index are local optional fields, not a claimed official schema.'
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--file', type=Path, default=root / 'agent/conversation.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = check(args.file.read_bytes())
        report['material_path'] = args.file.relative_to(root).as_posix() if args.file.is_relative_to(root) else args.file.name
    except (ValueError, UnicodeError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'passed': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    print(output, end='')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding='utf-8', newline='\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
