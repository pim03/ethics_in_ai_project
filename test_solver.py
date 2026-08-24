import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import solver


def setUpModule():
    solver.load_department(Path(__file__).parent / "config" / "2026-09.json", "imagiologia")


class CalendarTests(unittest.TestCase):
    def test_easter_dates(self):
        self.assertEqual(solver.easter_sunday(2025), dt.date(2025, 4, 20))
        self.assertEqual(solver.easter_sunday(2026), dt.date(2026, 4, 5))

    def test_month_days_handles_leap_year(self):
        days = solver.month_days(dt.date(2024, 2, 1))
        self.assertEqual(len(days), 29)
        self.assertEqual(days[-1], dt.date(2024, 2, 29))

    def test_weekend_identity_is_consistent_at_year_boundary(self):
        self.assertEqual(solver.weekend_id(dt.date(2024, 12, 28)), dt.date(2024, 12, 28))
        self.assertEqual(solver.weekend_id(dt.date(2024, 12, 29)), dt.date(2024, 12, 28))

    def test_vacation_weekend_buffer(self):
        touched = solver.weekends_touching_vacation({dt.date(2026, 9, 9)})
        self.assertEqual(touched, {dt.date(2026, 9, 5), dt.date(2026, 9, 12), dt.date(2026, 9, 19)})

    def test_early_afternoon_has_correct_times(self):
        self.assertEqual(solver.SHIFT_TIMES["EA"], (14, 22))

    def test_previous_month_loader_uses_last_calendar_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "previous.csv"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["date", "person", "shift"])
                writer.writeheader()
                writer.writerow({"date": "2026-08-31", "person": "Telmo", "shift": "N"})
                writer.writerow({"date": "2026-08-30", "person": "Celia L", "shift": "M"})
            loaded = solver.load_previous_assignments(path, dt.date(2026, 9, 1))
            self.assertEqual(loaded, {"Telmo": "N"})

    def test_department_loader_keeps_sick_leave_separate_from_vacations(self):
        availability = solver.load_department(
            Path(__file__).parent / "config" / "2026-09.json", "imagiologia"
        )
        self.assertIn(dt.date(2026, 9, 1), availability["Cátia"])
        self.assertNotIn(dt.date(2026, 9, 1), availability.vacation_days["Cátia"])


class ValidationTests(unittest.TestCase):
    def test_validator_detects_understaffing(self):
        errors = solver.validate_schedule([], dt.date(2026, 9, 1), {})
        self.assertTrue(any("requires" in error for error in errors))

    def test_hospital_feedback_weights_are_active(self):
        weights = solver.Weights()
        self.assertGreater(weights.workday_balance, 0)
        self.assertGreater(weights.excess_work_streak, 0)
        self.assertGreater(weights.isolated_work_day, 0)
        self.assertGreater(weights.isolated_rest_day, 0)
        self.assertGreater(weights.holiday_rotation, 0)
        self.assertGreater(weights.excess_afternoon_streak, 0)
        self.assertGreater(weights.night_ramp_mismatch, 0)
        self.assertGreater(weights.night_after_vacation, 0)

    def test_cristina_and_ana_martins_are_not_night_eligible(self):
        original = list(solver.PEOPLE)
        try:
            solver.PEOPLE[:] = ["Cristina", "Ana Martins"]
            for person in range(2):
                self.assertFalse(solver.allowed(person, dt.date(2026, 9, 1), solver.NIGHT))
                self.assertTrue(solver.allowed(person, dt.date(2026, 9, 1), solver.MORNING))
        finally:
            solver.PEOPLE[:] = original

    def test_validator_rejects_early_afternoon_followed_by_night(self):
        rows = [
            {"date": "2026-09-01", "person": "Telmo", "shift": "EA"},
            {"date": "2026-09-02", "person": "Telmo", "shift": "N"},
        ]
        errors = solver.validate_schedule(rows, dt.date(2026, 9, 1), {})
        self.assertTrue(any("early-afternoon-to-night" in error for error in errors))

    def test_max_consecutive_afternoons_defaults_to_three(self):
        self.assertEqual(solver.Rules().max_consecutive_afternoons, 3)

    def test_prevention_validator_accepts_weekend_block_and_compensation(self):
        # 2026-09-03 is Thursday, 09-04 Friday, 09-05 Saturday: Friday+Saturday
        # form a single rotating weekend block held by the same person.
        days = [dt.date(2026, 9, day) for day in range(3, 6)]
        holders = ["Angelo", "Nuno", "Nuno"]
        prevention = [
            {"date": day.isoformat(), "person": holder}
            for day, holder in zip(days, holders)
        ]
        compensation = [{"person": "Angelo", "earned_on": "2026-09-03", "rest_date": "2026-09-04"}]
        errors = solver.validate_prevention_schedule(
            prevention, compensation, [], days, {dt.date(2026, 9, 3): "feriado"}
        )
        self.assertEqual(errors, [])

    def test_prevention_validator_detects_repeated_worker_across_blocks(self):
        # 09-03 Thursday and 09-04 Friday are separate blocks and must alternate.
        days = [dt.date(2026, 9, 3), dt.date(2026, 9, 4)]
        prevention = [
            {"date": day.isoformat(), "person": "Nuno"} for day in days
        ]
        errors = solver.validate_prevention_schedule(prevention, [], [], days, {})
        self.assertTrue(any("does not alternate" in error for error in errors))

    def test_prevention_validator_detects_split_weekend_block(self):
        # 09-04 Friday and 09-05 Saturday belong to the same block; different
        # holders should be rejected even though the plain alternation check
        # would not catch it.
        days = [dt.date(2026, 9, day) for day in range(3, 6)]
        prevention = [
            {"date": "2026-09-03", "person": "Angelo"},
            {"date": "2026-09-04", "person": "Nuno"},
            {"date": "2026-09-05", "person": "Angelo"},
        ]
        errors = solver.validate_prevention_schedule(prevention, [], [], days, {})
        self.assertTrue(any("within the Friday-Sunday block" in error for error in errors))

    def test_prevention_blocks_group_friday_to_sunday(self):
        days = solver.month_days(dt.date(2026, 9, 1))
        blocks = solver.prevention_blocks(days)
        friday_index = days.index(dt.date(2026, 9, 4))
        block = next(block for block in blocks if friday_index in block)
        self.assertEqual(
            [days[index] for index in block],
            [dt.date(2026, 9, 4), dt.date(2026, 9, 5), dt.date(2026, 9, 6)],
        )
        # Every day belongs to exactly one block.
        covered = sorted(index for block in blocks for index in block)
        self.assertEqual(covered, list(range(len(days))))


class WorkdayHistoryTests(unittest.TestCase):
    def test_resets_on_january(self):
        self.assertEqual(
            solver.resolved_workday_history(1, {"Telmo": 12}),
            {name: 0 for name in solver.PEOPLE},
        )

    def test_carries_forward_other_months(self):
        result = solver.resolved_workday_history(6, {"Telmo": 12})
        self.assertEqual(result["Telmo"], 12)
        self.assertEqual(result["Hugo"], 0)


if __name__ == "__main__":
    unittest.main()
