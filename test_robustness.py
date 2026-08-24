import datetime as dt
import unittest

from robustness import assignment_set, compare_schedules, scenario_catalog


class RobustnessTests(unittest.TestCase):
    def test_assignment_changes_and_affected_workers(self):
        before = [{"person": "A", "date": "2026-09-01", "shift": "M"}]
        after = [{"person": "B", "date": "2026-09-01", "shift": "M"}]
        result = compare_schedules(before, after)
        self.assertEqual(result["assignment_edits"], 2)
        self.assertEqual(result["replacement_equivalents"], 1)
        self.assertEqual(result["affected_workers"], 2)

    def test_assignment_set_keeps_shift_identity(self):
        rows = [{"person": "A", "date": "2026-09-01", "shift": "EA"}]
        self.assertIn(("A", dt.date(2026, 9, 1), "EA"), assignment_set(rows))

    def test_catalog_contains_required_perturbation_types(self):
        rows = []
        for day in range(1, 31):
            rows.append({"person": "A", "date": f"2026-09-{day:02d}", "shift": "N" if day == 10 else "M"})
            rows.append({"person": "B", "date": f"2026-09-{day:02d}", "shift": "A"})
        names = {scenario.name for scenario in scenario_catalog(rows)}
        self.assertEqual(names, {
            "single_day_absence", "night_worker_absence", "three_day_absence",
            "morning_demand_increase", "night_demand_increase", "simultaneous_absences",
        })


if __name__ == "__main__":
    unittest.main()
