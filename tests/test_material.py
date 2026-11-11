"""Adversarial tests for local material validation, without evaluating scores."""
import json
import unittest

from scripts.check_material import check, resolve


class MaterialValidation(unittest.TestCase):
    def test_published_input_does_not_require_local_optional_fields(self):
        self.assertTrue(check(b'{}')['passed'])

    def test_invalid_encodings_and_size(self):
        for raw in (b'\xff', b'\xef\xbb\xbf{}', b' ' * 131073, b'[]'):
            with self.subTest(raw=raw[:8]), self.assertRaises((ValueError, UnicodeError)):
                check(raw)

    def test_duplicate_nested_keys_and_nonfinite_numbers(self):
        for raw in (b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e309}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                check(raw)

    def test_json_pointer_escape_and_string_citation(self):
        data = {'a/b': {'~key': ['verified result']}, 'evidence_index': [
            {'citations': [{'pointer': '/a~1b/~0key/0', 'quote': 'verified result'}]}]}
        self.assertEqual(resolve(data, '/a~1b/~0key/0'), 'verified result')
        self.assertEqual(check(json.dumps(data).encode())['evidence_citations_checked'], 1)
        data['evidence_index'][0]['citations'][0]['quote'] = 'invented result'
        with self.assertRaises(ValueError):
            check(json.dumps(data).encode())

    def test_reject_malformed_pointers(self):
        for pointer in ('x', '/a~2b', '/a~', '/x/01', '/x/9'):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                resolve({'x': ['text']}, pointer)

    def test_recognizable_token_is_rejected_after_json_decode(self):
        token = 'gh' + 'p_' + 'a' * 36
        raw = json.dumps({'x': token}, ensure_ascii=True).encode()
        with self.assertRaises(ValueError):
            check(raw.replace(b'g', b'\\u0067'))


if __name__ == '__main__':
    unittest.main()
