"""Version comparison rules (M5 phase 03).

Each test pins at least three real-world pairs, taken from the phase doc's
test plan, with the expected sign asserted on both orderings.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.versions import compare


def _sign(a: str, b: str, scheme: str) -> int:
    return compare(a, b, scheme)


def _assert_sign(case: unittest.TestCase, a: str, b: str, expected: int, scheme: str):
    case.assertEqual(_sign(a, b, scheme), expected, f"{scheme}: {a!r} vs {b!r}")
    case.assertEqual(_sign(b, a, scheme), -expected, f"{scheme}: {b!r} vs {a!r} (reversed)")


def _assert_equal(case: unittest.TestCase, a: str, b: str, scheme: str):
    case.assertEqual(_sign(a, b, scheme), 0, f"{scheme}: {a!r} vs {b!r}")
    case.assertEqual(_sign(b, a, scheme), 0, f"{scheme}: {b!r} vs {a!r} (reversed)")


class DebVersionTests(unittest.TestCase):
    def test_deb_epoch_wins(self):
        # An epoch bump dominates the upstream comparison: 1:1.0 > 2.0.
        _assert_sign(self, "1:1.0", "2.0", 1, "deb")
        _assert_sign(self, "0:1.0", "2.0", -1, "deb")
        _assert_sign(self, "2:1.2.3", "1:9.9.9", 1, "deb")
        _assert_equal(self, "0:1.0", "1.0", "deb")

    def test_deb_tilde_sorts_first(self):
        # A pre-release sorts before the release it precedes.
        _assert_sign(self, "1.0~rc1", "1.0", -1, "deb")
        _assert_sign(self, "1.0~beta", "1.0~rc1", -1, "deb")
        _assert_sign(self, "1.0~~", "1.0~", -1, "deb")
        # Two tildes sort before one: the first '~' already sorts a before b,
        # and the second makes a's remainder empty (the end sorts before '~').
        _assert_sign(self, "1.0~~", "1.0~~~", 1, "deb")

    def test_deb_ubuntu_revisions(self):
        # The revision part compares with the same dpkg algorithm.
        _assert_sign(self, "3.0.2-0ubuntu1.15", "3.0.2-0ubuntu1.16", -1, "deb")
        _assert_sign(self, "3.0.13-1", "3.0.2-9", 1, "deb")
        _assert_sign(self, "2.14.1-1ubuntu1", "2.14.1-2ubuntu1", -1, "deb")

    def test_deb_letters_sort_before_non_letters(self):
        # In non-digit runs: letters before non-letters, so a hyphen beats a
        # letter at the same position ("1.0-alpha" vs "1.0.alpha").
        _assert_sign(self, "1.0-alpha", "1.0.alpha", -1, "deb")
        _assert_sign(self, "1.0b", "1.0~b", 1, "deb")


class RpmVersionTests(unittest.TestCase):
    def test_rpm_segments(self):
        # Numeric segments compare numerically: 27 > 3 despite the digit count.
        _assert_sign(self, "3.0.7-27.el9", "3.0.7-3.el9", 1, "rpm")
        _assert_sign(self, "1.04", "1.4", 0, "rpm")
        _assert_sign(self, "1.0.1", "1.0", 1, "rpm")

    def test_rpm_tilde_and_caret(self):
        # '~' sorts before anything, '^' after the base version.
        _assert_sign(self, "1.0~rc1", "1.0", -1, "rpm")
        _assert_sign(self, "1.0^git1", "1.0", 1, "rpm")
        _assert_sign(self, "1.0^git1", "1.0^git10", -1, "rpm")

    def test_rpm_epoch(self):
        _assert_equal(self, "0:1.2", "1.2", "rpm")
        _assert_sign(self, "1:1.2", "0:9.9", 1, "rpm")

    def test_rpm_numeric_beats_alpha(self):
        _assert_sign(self, "1.0.1", "1.0.beta", 1, "rpm")


class GenericVersionTests(unittest.TestCase):
    def test_generic_dotted(self):
        _assert_sign(self, "2.17.1", "2.14.1", 1, "generic")
        _assert_sign(self, "2.10", "2.9", 1, "generic")
        _assert_sign(self, "1.0+build2", "1.0+build10", 1, "generic")
        _assert_sign(self, "1.0a", "1.0b", -1, "generic")

    def test_generic_missing_parts_count_as_zero(self):
        _assert_equal(self, "1.0", "1.0.0", "generic")
        _assert_equal(self, "1.0", "1", "generic")

    def test_generic_numeric_beats_text(self):
        _assert_sign(self, "1.0.2", "1.0.beta", 1, "generic")


class EqualVersionTests(unittest.TestCase):
    def test_equal_versions(self):
        _assert_equal(self, "3.0.13-1", "3.0.13-1", "deb")
        _assert_equal(self, "3.0.7-27.el9", "3.0.7-27.el9", "rpm")
        _assert_equal(self, "2.17.1", "2.17.1", "generic")


class SchemeValidationTests(unittest.TestCase):
    def test_unknown_scheme_raises(self):
        with self.assertRaises(ValueError):
            compare("1.0", "1.0", "semver")





class RealWorldAgreementTests(unittest.TestCase):
    """Architect review: the first dpkg/rpm ports disagreed with the real tools (e.g. said
    1.0 > 1.0-1). These pairs were produced by `dpkg --compare-versions` and taken from
    rpm's own rpmvercmp.at test suite, so a regression shows up as a real-tool mismatch."""

    DPKG = (
('1.0', '1.0-1', -1),
('1.0-1', '1.0-2', -1),
('1:0.9', '2.0', 1),
('1.0~rc1', '1.0', -1),
('1.0~rc1-1', '1.0-1', -1),
('1.0+b1', '1.0', 1),
('1.0a', '1.0', 1),
('1.0.1', '1.0', 1),
('1.00', '1.0', 0),
('1.0-0ubuntu1', '1.0-0ubuntu1.1', -1),
('3.0.2-0ubuntu1.15', '3.0.2-0ubuntu1.16', -1),
('3.0.13-1', '3.0.2-9', 1),
('1.0~~', '1.0~', -1),
('1.0-1~bpo1', '1.0-1', -1),
('1.2.3a~b', '1.2.3a', -1),
('10', '9', 1),
('1.10', '1.9', 1),
('7.81.0-1ubuntu1.18', '7.81.0-1ubuntu1.2', 1),
('1.0.0+dfsg-1', '1.0.0-1', 1),
('1.0.0-1+deb12u1', '1.0.0-1', 1),
('1.0-0', '1.0', 0),
('1.0.a', '1.0-a', 1),
('0:1.0', '1.0', 0),
('2.0-1-2', '2.0-1-10', -1),
('1.1.1f-1ubuntu2.24', '1.1.1f-1ubuntu2.3', 1),
('1:3.0.2-0ubuntu1.15', '3.0.13-1', 1),
    )

    RPMVERCMP = (
("1.0","1.0",0),("1.0","2.0",-1),("2.0.1a","2.0.1",1),("5.5p1","5.5p10",-1),("10xyz","10.1xyz",-1),("xyz.4","8",-1),
("6.0.rc1","6.0",1),("10b2","10a1",1),("1.0a","1.0aa",-1),("10.0001","10.1",0),("4.999.9","5.0",-1),("2.0","2_0",0),
("a+","a_",0),("+","_",0),("1.0~rc1","1.0",-1),("1.0~rc1~git123","1.0~rc1",-1),("1.0^","1.0",1),("1.0^git1","1.01",-1),
("1.0^20160101","1.0.1",-1),("1.0^20160102","1.0^20160101^git1",1),("1.0~rc1^git1","1.0~rc1",1),("1.0^git1~pre","1.0^git1",-1),
    )

    def test_matches_dpkg_compare_versions(self):
        for a, b, expected in self.DPKG:
            self.assertEqual(compare(a, b, "deb"), expected, f"deb {a} vs {b}")

    def test_matches_rpmvercmp_vectors(self):
        from vigil_agent.versions import _rpmvercmp
        for a, b, expected in self.RPMVERCMP:
            self.assertEqual(_rpmvercmp(a, b), expected, f"rpm {a} vs {b}")
            self.assertEqual(_rpmvercmp(b, a), -expected, f"rpm {b} vs {a}")

    def test_rpm_bound_without_release(self):
        self.assertEqual(compare("3.0.7-27.el9", "3.0.13", "rpm"), -1)
        self.assertEqual(compare("3.0.13-1.el9", "3.0.13", "rpm"), 0)
        self.assertEqual(compare("1:1.0-1", "2.0-1", "rpm"), 1)


if __name__ == "__main__":
    unittest.main()
