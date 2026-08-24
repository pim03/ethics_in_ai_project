import datetime as dt
import unittest

from explanations import capacity_diagnostics, schedule_indexes


class ExplanationTests(unittest.TestCase):
    def test_schedule_indexes_normalize_early_afternoon_for_staffing(self):
        rows = [{"person": "Telmo", "date": "2026-09-01", "shift": "EA"}]
        by_day, _, staffing = schedule_indexes(rows)
        self.assertEqual(by_day["Telmo", dt.date(2026, 9, 1)], "EA")
        self.assertEqual(staffing[dt.date(2026, 9, 1), 1], 1)

    def test_capacity_diagnostic_shape(self):
        diagnostics = capacity_diagnostics(2026, 9, {})
        self.assertEqual(len(diagnostics), 30 * 3)
        self.assertTrue(all("capacity_margin" in item for item in diagnostics))


if __name__ == "__main__":
    unittest.main()
