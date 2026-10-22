"""Strict native validation preserves valid text and rejects malformed text."""
import importlib.util
import itertools
import os
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch


def load_validator():
    path = Path(os.environ.get('R6_UTF8_UTILS_FILE') or
                Path(__file__).resolve().parents[1]/'vendor/websocket/_utils.py')
    spec = importlib.util.spec_from_file_location('r6_reviewed_utf8', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wsaccel': None, 'wsaccel.utf8validator': None}):
        spec.loader.exec_module(module)
    return module


def strict_reference(value):
    try:
        value.decode('utf-8', 'strict')
        return True
    except UnicodeDecodeError:
        return False


class NativeUtf8Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_validator()

    def test_valid_rfc_boundaries_and_market_json(self):
        texts = ['', '\0', '\x7f', '\x80', '\u07ff', '\u0800', '\ud7ff',
                 '\ue000', '\uffff', '\U00010000', '\U0010ffff',
                 '{"trnm":"REAL","data":[{"type":"0B","name":"주식시세"}]}']
        for value in texts:
            with self.subTest(value=repr(value)):
                self.assertIs(self.module.validate_utf8(value.encode()), True)

    def test_malformed_overlong_surrogate_out_of_range_and_continuations(self):
        for value in (b'\x80', b'\xff', b'\xfe', b'\xc0\x80', b'\xc1\xbf',
                      b'\xe0\x80\x80', b'\xf0\x80\x80\x80', b'\xed\xa0\x80',
                      b'\xed\xbf\xbf', b'\xf4\x90\x80\x80', b'\xf5\x80\x80\x80',
                      b'\xc2A', b'\xe2(\xa1', b'\xf0\x90A\x80'):
            with self.subTest(value=value):
                self.assertIs(self.module.validate_utf8(value), False)

    def test_truncated_final_sequences_are_not_valid_complete_text(self):
        for value in (b'\xc2', b'\xe2', b'\xe2\x82', b'\xf0',
                      b'\xf0\x90', b'\xf0\x90\x80', b'normal\xe2\x82'):
            with self.subTest(value=value):
                self.assertIs(self.module.validate_utf8(value), False)

    def test_all_one_and_two_byte_inputs_match_strict_reference(self):
        for length in (0, 1, 2):
            for values in itertools.product(range(256), repeat=length):
                payload = bytes(values)
                actual = self.module.validate_utf8(payload)
                expected = strict_reference(payload)
                if actual is not expected:
                    self.fail(f'{payload!r}: actual={actual}, expected={expected}')

    def test_seeded_arbitrary_bytes_match_strict_reference(self):
        rng = random.Random(20261022)
        for _ in range(10000):
            payload = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 33)))
            self.assertIs(self.module.validate_utf8(payload), strict_reference(payload))

    def test_valid_unicode_samples_match_legacy_complete_state(self):
        rng = random.Random(311)
        for _ in range(1000):
            points = [rng.randrange(0x110000) for _ in range(12)]
            payload = ''.join(chr(x) for x in points if not 0xd800 <= x <= 0xdfff).encode()
            state = self.module._UTF8_ACCEPT
            codepoint = 0
            for byte in payload:
                state, codepoint = self.module._decode(state, codepoint, byte)
            self.assertEqual(state, self.module._UTF8_ACCEPT)
            self.assertIs(self.module.validate_utf8(payload), True)

    def test_bytes_bytearray_and_unsigned_memoryview_contract(self):
        for factory in (bytes, bytearray, memoryview):
            for payload in ('호가잔량'.encode(), b'', b'\xff', b'\xe2\x82'):
                with self.subTest(factory=factory.__name__, payload=payload):
                    self.assertIs(self.module.validate_utf8(factory(payload)), strict_reference(payload))

    def test_contiguous_native_inputs_do_not_run_per_byte_python_dfa(self):
        with patch.object(self.module, '_decode', side_effect=AssertionError('per-byte DFA called')):
            for factory in (bytes, bytearray, memoryview):
                self.assertIs(self.module.validate_utf8(factory(('정규장 시세' * 100).encode())), True)
                self.assertIs(self.module.validate_utf8(factory(b'\xff')), False)

    def test_unusual_iterable_compatibility_remains_strict(self):
        self.assertIs(self.module.validate_utf8([0x41, 0xc2, 0x80]), True)
        self.assertIs(self.module.validate_utf8([0xff]), False)
        self.assertIs(self.module.validate_utf8([0xe2, 0x82]), False)


if __name__ == '__main__':
    unittest.main()
