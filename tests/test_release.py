import unittest
from scripts.release import next_version


class ReleaseTests(unittest.TestCase):
    def test_decimal_progression(self):
        for old, new in [("1.5.5", "1.5.6"), ("1.5.9", "1.6.0"),
                         ("1.9.9", "2.0.0"), ("8.9.9", "9.0.0")]:
            self.assertEqual(next_version(old), new)

    def test_all_releases_are_single_digits_and_monotonic(self):
        for value in range(999):
            version = ".".join(f"{value:03d}")
            self.assertEqual(next_version(version), ".".join(f"{value + 1:03d}"))

    def test_invalid_and_overflow_are_rejected(self):
        for value in ["1.5.10", "10.0.0", "1.5", "v1.5.5", "9.9.9"]:
            with self.assertRaises(ValueError):
                next_version(value)


if __name__ == "__main__":
    unittest.main()
