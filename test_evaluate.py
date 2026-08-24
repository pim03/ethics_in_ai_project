import unittest

from evaluate import distribution, gini, jain_index, safe_ratio


class MetricTests(unittest.TestCase):
    def test_equal_distribution_is_maximally_fair(self):
        self.assertEqual(gini([4, 4, 4]), 0.0)
        self.assertEqual(jain_index([4, 4, 4]), 1.0)

    def test_known_unequal_distribution(self):
        self.assertAlmostEqual(gini([0, 0, 6]), 2 / 3)
        self.assertAlmostEqual(jain_index([0, 0, 6]), 1 / 3)

    def test_zero_distribution_is_well_defined(self):
        self.assertEqual(gini([0, 0]), 0.0)
        self.assertEqual(jain_index([0, 0]), 0.0)

    def test_empty_distribution_defaults(self):
        summary = distribution([])
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["jain_index"], 1.0)

    def test_safe_ratio(self):
        self.assertEqual(safe_ratio(4, 2), 2)
        self.assertEqual(safe_ratio(4, 0), 0)


if __name__ == "__main__":
    unittest.main()
