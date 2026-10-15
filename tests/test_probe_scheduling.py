"""Scheduling regressions: no network and no reduced probe coverage."""
import concurrent.futures
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe.batch import BatchProber
from cpa_probe.parse import ParsedRow, SECTIONS
from cpa_probe.pipeline import CandidateResult, Prober, SectionVerdict


class SchedulingTests(unittest.TestCase):
    def test_grouped_credentials_round_robin_without_losing_rows(self):
        rows = [ParsedRow(i, "", f"https://{host}.invalid", str(i))
                for i, host in enumerate(["a", "a", "a", "b", "b", "c"])]
        started = []

        class FixtureProber:
            def probe(self, row):
                started.append(row)
                return CandidateResult(row=row)

        results = BatchProber(FixtureProber(), max_workers=1).probe_batch(rows)
        self.assertEqual([0, 3, 5, 1, 4, 2], [row.line_no for row in started])
        self.assertEqual(len(rows), len(results))
        self.assertEqual({(row.bare, row.api_key) for row in rows}, set(results))

    def test_completed_section_reported_while_other_section_is_running(self):
        p = object.__new__(Prober)
        p.workers = 4
        reported = threading.Event()
        slow_entered = threading.Event()
        release = threading.Event()
        events = []

        def probe_section(row, section):
            if section == SECTIONS[0]:
                slow_entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release slow section")
            return SectionVerdict(section)

        def on_event(event, data):
            events.append((event, data))
            if event == "section-done" and data["section"] != SECTIONS[0]:
                reported.set()

        p._probe_one_section = probe_section
        p.on_event = on_event
        row = ParsedRow(1, "", "https://fixture.invalid", "fixture")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(p.probe, row)
            try:
                self.assertTrue(slow_entered.wait(2))
                self.assertTrue(reported.wait(2), "finished sections must be visible immediately")
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(timeout=5)
        self.assertEqual(list(SECTIONS), list(result.sections))
        completed = [data["section"] for event, data in events if event == "section-done"]
        self.assertCountEqual(SECTIONS, completed)


if __name__ == "__main__":
    unittest.main()
