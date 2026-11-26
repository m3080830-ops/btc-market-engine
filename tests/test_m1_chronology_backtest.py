import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "historical_close_sync_compare.py"
SPEC = importlib.util.spec_from_file_location("historical_close_sync_compare", SCRIPT)
BACKTEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKTEST)


class M1ChronologyAndExecutionModelTests(unittest.TestCase):
    def candle(self, time, open_=100.0, high=101.0, low=99.0, close=100.0):
        return {"time": time, "open": open_, "high": high, "low": low, "close": close}

    def test_includes_last_m1_candle_that_closes_exactly_at_period_end(self):
        rows = [
            self.candle(120, low=98.0),
            self.candle(180, low=97.0),
            self.candle(240, low=96.0),
        ]
        selected = BACKTEST.closed_by_end(rows, 300)
        self.assertEqual([row["time"] for row in selected], [120, 180, 240])

    def test_excludes_m1_candle_that_is_still_open_at_period_end(self):
        rows = [
            self.candle(120),
            self.candle(180),
            self.candle(240),
        ]
        selected = BACKTEST.closed_by_end(rows, 299)
        self.assertEqual([row["time"] for row in selected], [120, 180])

    def test_protection_stop_waits_one_full_minute_after_signal_candle_close(self):
        rows = [
            self.candle(60, high=105.1, low=99.0, close=104.0),
            self.candle(120, high=104.0, low=94.0, close=100.0),
            self.candle(180, high=100.0, low=94.0, close=96.0),
        ]
        result = BACKTEST.simulate_position(
            rows, "LONG", {"time": 60, "price": 100.0}, 90.0
        )
        self.assertEqual(result["reason"], "SL")
        self.assertEqual(result["exit_time"], 180)
        self.assertAlmostEqual(result["exit_price"], 95.0)

    def test_confirmed_swings_use_only_supplied_closed_candles(self):
        rows = [
            self.candle(0, high=105, low=98),
            self.candle(60, high=104, low=97),
            self.candle(120, high=103, low=95),
            self.candle(180, high=104, low=97),
            self.candle(240, high=105, low=98),
        ]
        lows, _ = BACKTEST.confirmed_m1_swings(rows)
        self.assertEqual(lows, [{"confirmed_at": 240, "price": 95}])

    def test_report_marks_results_as_simulation_not_live_execution(self):
        source = SCRIPT.read_text()
        self.assertIn("HISTORICAL_SIMULATION_NOT_LIVE_EXECUTION", source)
        self.assertIn('"result_type":"historical candle simulation, not actual MEXC execution"', source)
        self.assertIn('"take_profit_closes_position":False', source)


if __name__ == "__main__":
    unittest.main()
