"""Repeated bulk priority edits must not accumulate one stamp comment per run."""
from pathlib import Path
import collections
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe import bulk

RAW = '''codex-api-key:
  - api-key: synthetic-first
    base-url: https://fixture.invalid/v1
    priority: 10
    # Operator note that must survive every rewrite.
    models:
      - name: gpt-6-synthetic
        alias: ""
openai-compatibility:
  - name: fixture
    base-url: https://fixture.invalid/v1
    priority: 4
    api-key-entries:
      - api-key: synthetic-compat
        priority: 4
    models:
      - name: gpt-6-synthetic
        alias: ""
'''


def stamps(text):
    return sum(1 for line in text.splitlines() if "批量改档" in line)


class BulkStampIdempotenceTests(unittest.TestCase):
    def apply(self, raw, section, value, index=0, stamp="2026-01-01 00:00"):
        ops = [{"section": section, "index": index, "action": "priority", "value": value}]
        out, notes, problems = bulk.apply_bulk(raw, ops, stamp=stamp)[:3]
        self.assertEqual([], problems)
        return out, notes

    def test_second_edit_replaces_the_stamp_instead_of_adding_one(self):
        once, _ = self.apply(RAW, "codex-api-key", 11)
        self.assertEqual(1, stamps(once))
        twice, _ = self.apply(once, "codex-api-key", 12, stamp="2026-01-02 00:00")
        self.assertEqual(1, stamps(twice), "each run must not leave its own stamp line")
        self.assertIn("priority: 12", twice)
        self.assertIn("2026-01-02 00:00", twice)
        self.assertNotIn("2026-01-01 00:00", twice)

    def test_repeated_edits_do_not_grow_the_file(self):
        text = RAW
        sizes = []
        for round_index in range(4):
            text, _ = self.apply(text, "codex-api-key", 20 + round_index,
                                 stamp=f"2026-01-0{round_index + 1} 00:00")
            sizes.append(len(text.splitlines()))
        self.assertEqual({sizes[0]}, set(sizes), f"line count drifted: {sizes}")

    def test_operator_comments_are_never_dropped(self):
        text = RAW
        for round_index in range(3):
            text, _ = self.apply(text, "codex-api-key", 30 + round_index)
        before = collections.Counter(l.strip() for l in RAW.splitlines() if l.strip().startswith("#"))
        after = collections.Counter(l.strip() for l in text.splitlines() if l.strip().startswith("#"))
        self.assertFalse(before - after)

    def test_compat_provider_stamp_is_also_idempotent(self):
        once, _ = self.apply(RAW, "openai-compatibility", 5)
        twice, _ = self.apply(once, "openai-compatibility", 6, stamp="2026-01-02 00:00")
        self.assertEqual(1, stamps(twice))
        self.assertEqual(len(once.splitlines()), len(twice.splitlines()))


if __name__ == "__main__":
    unittest.main()
