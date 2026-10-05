"""Ordering comparisons (<, <=, >, >=) for numbers only.

The spec's branching example is ``if: steps.svc.result.count > 0``. A
predicate must never raise at run time: anything that is not a number
(string, bool, missing value) makes the comparison False.
"""
from django.test import SimpleTestCase

from apps.tasks.expression import evaluate, validate


class NumberOrderingTests(SimpleTestCase):
    def test_ordering_on_numbers(self):
        ctx = {"inputs": {"x": 5, "y": 2.5}, "agent": {}, "host": {}, "steps": {}}
        # int/float mixes, all four ops
        self.assertTrue(evaluate("inputs.x > 4", ctx))
        self.assertFalse(evaluate("inputs.x > 5", ctx))
        self.assertTrue(evaluate("inputs.x >= 5", ctx))
        self.assertFalse(evaluate("inputs.x >= 6", ctx))
        self.assertTrue(evaluate("inputs.x < 6", ctx))
        self.assertFalse(evaluate("inputs.x < 5", ctx))
        self.assertTrue(evaluate("inputs.x <= 5", ctx))
        self.assertFalse(evaluate("inputs.x <= 4", ctx))
        self.assertTrue(evaluate("inputs.y < 3", ctx))
        self.assertTrue(evaluate("inputs.y > 2", ctx))
        # chained
        self.assertTrue(evaluate("inputs.x > inputs.y", ctx))
        self.assertTrue(evaluate("2 < inputs.x <= 5", ctx))
        self.assertFalse(evaluate("2 < inputs.x <= 4", ctx))

    def test_non_numbers_compare_false(self):
        ctx = {
            "inputs": {"s": "5", "b": True, "n": None},
            "agent": {}, "host": {}, "steps": {},
        }
        # str vs int — a string is not a number, even if it looks like one
        self.assertFalse(evaluate('inputs.s > 3', ctx))
        self.assertFalse(evaluate('inputs.s < 10', ctx))
        self.assertFalse(evaluate('inputs.s >= 5', ctx))
        self.assertFalse(evaluate('inputs.s <= 5', ctx))
        # None
        self.assertFalse(evaluate("inputs.n > 0", ctx))
        self.assertFalse(evaluate("inputs.n < 0", ctx))
        # missing key
        self.assertFalse(evaluate("inputs.nope > 0", ctx))
        self.assertFalse(evaluate("inputs.nope < 10", ctx))
        # bool — True is an int subclass in Python, but not a number here
        self.assertFalse(evaluate("inputs.b > 0", ctx))
        self.assertFalse(evaluate("inputs.b < 1", ctx))
        self.assertFalse(evaluate("inputs.b >= 0", ctx))
        self.assertFalse(evaluate("inputs.b <= 1", ctx))

    def test_step_result_count(self):
        ctx = {
            "inputs": {}, "agent": {}, "host": {},
            "steps": {"svc": {"status": "ok", "result": {"count": 2}}},
        }
        self.assertTrue(evaluate("steps.svc.result.count > 0", ctx))
        ctx["steps"]["svc"]["result"]["count"] = 0
        self.assertFalse(evaluate("steps.svc.result.count > 0", ctx))

    def test_validate_accepts_ordering(self):
        validate("steps.a.result.count >= 1")
        validate("inputs.x < 3")
        validate("inputs.x <= 3")
        validate("inputs.x > 3")
        validate("inputs.x >= 3")
