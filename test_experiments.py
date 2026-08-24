import unittest

from experiments import PROFILES, flatten_result, preference_cost


class ExperimentTests(unittest.TestCase):
    def test_profiles_cover_expected_tradeoffs(self):
        self.assertEqual(set(PROFILES), {
            "operational_baseline", "workload_focused", "weekend_focused",
            "preference_focused", "balanced",
        })
        self.assertGreater(PROFILES["weekend_focused"].weekend_imbalance,
                           PROFILES["workload_focused"].weekend_imbalance)

    def test_preference_cost_excludes_scaled_workload(self):
        self.assertEqual(preference_cost({"workload_imbalance": 999, "split_weekend": 3, "isolated_rest_day": 2}), 5)

    def test_flatten_result_extracts_comparable_metrics(self):
        solver_report = {
            "status": "FEASIBLE", "solve_seconds": 1.0, "objective": 4,
            "best_bound": 3, "objective_components": {"workload_imbalance": 10, "split_weekend": 2},
        }
        metric = {"gini": 0.1, "jain_index": 0.9}
        fairness_report = {"metrics": {
            "assigned_hours": metric,
            "availability_normalized_workload": metric,
            "availability_normalized_night_burden": metric,
            "availability_normalized_weekend_burden": metric,
        }}
        row = flatten_result("test", solver_report, fairness_report)
        self.assertEqual(row["profile"], "test")
        self.assertEqual(row["preference_cost"], 2)


if __name__ == "__main__":
    unittest.main()
