import unittest
from unittest import mock

from gate import rpc


class NestedDeadlineTest(unittest.TestCase):
    def test_inner_scope_cannot_extend_outer_budget_and_restores_context(self):
        with mock.patch.object(rpc.time, "monotonic", side_effect=[100, 102, 103]):
            with rpc.request_deadline(5):
                self.assertEqual(rpc._REQUEST_DEADLINE.get(), 105)
                with rpc.request_deadline(20):
                    self.assertEqual(rpc._REQUEST_DEADLINE.get(), 105)
                self.assertEqual(rpc._REQUEST_DEADLINE.get(), 105)
                with rpc.request_deadline(1):
                    self.assertEqual(rpc._REQUEST_DEADLINE.get(), 104)
                self.assertEqual(rpc._REQUEST_DEADLINE.get(), 105)
            self.assertIsNone(rpc._REQUEST_DEADLINE.get())

    def test_expired_outer_scope_is_not_reopened_and_exceptions_restore_it(self):
        with mock.patch.object(rpc.time, "monotonic", side_effect=[100, 110]):
            with rpc.request_deadline(1):
                with self.assertRaisesRegex(RuntimeError, "read failed"):
                    with rpc.request_deadline(20):
                        self.assertEqual(rpc._REQUEST_DEADLINE.get(), 101)
                        raise RuntimeError("read failed")
                self.assertEqual(rpc._REQUEST_DEADLINE.get(), 101)
            self.assertIsNone(rpc._REQUEST_DEADLINE.get())
