"""Comments survive rebuilding the same credential, including model annotations."""
from pathlib import Path
from unittest.mock import patch
import collections
import sys
import unittest
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe.plan import SectionPlan, ImportPlan
from cpa_probe import writeback, cpa_source_probe

RAW = '''port: 8317
codex-api-key:
  - api-key: synthetic-existing
    base-url: https://fixture.invalid/v1
    priority: 3
    request-scoped-errors:
      # This quota response must cool down this credential only.
      - status: 403
        match: [insufficient_user_quota]
        action: continue-and-cooldown
    models:
      # Model-specific original evidence.
      - name: gpt-6-synthetic
        alias: gpt-6-synthetic
    # Trailing account note.
logging-to-file: false
'''

class CommentRoundtripTests(unittest.TestCase):
    def rebuild(self, raw, models=None, complete=True):
        cfg = yaml.safe_load(raw)
        sp = SectionPlan(section='codex-api-key', base_url='https://fixture.invalid/v1',
                         api_key='synthetic-existing', models=models or ['gpt-6-synthetic'], priority=3,
                         probe_usable=True,
                         model_rebuild=complete,
                         highest_models=(models or ['gpt-6-synthetic']) if complete else [],
                         verified_models=['gpt-6-synthetic', *(models or [])])
        plan = ImportPlan(host='fixture.invalid', masked_key='***', line_no=1,
                          sections={sp.section: sp})
        with patch.object(cpa_source_probe, 'cached_identity', return_value=cpa_source_probe.CpaIdentity()):
            return writeback.rebuild_config_full(cfg, {(sp.base_url, sp.api_key): plan}, raw.splitlines(keepends=True))[0]

    @staticmethod
    def comments(raw):
        return collections.Counter(line.strip() for line in raw.splitlines() if line.strip().startswith('#'))

    def test_unchanged_models_and_trailing_comments_survive(self):
        out = self.rebuild(RAW)
        self.assertFalse(self.comments(RAW) - self.comments(out))
        self.assertEqual(yaml.safe_load(RAW), yaml.safe_load(out))

    def test_rebuilding_again_does_not_multiply_comments(self):
        once = self.rebuild(RAW)
        twice = self.rebuild(once)
        self.assertEqual(self.comments(once), self.comments(twice))
        self.assertEqual(yaml.safe_load(once), yaml.safe_load(twice))

    def test_changed_model_retains_explicit_historical_comment(self):
        out = self.rebuild(RAW, ['gpt-7-synthetic'])
        self.assertFalse(self.comments(RAW) - self.comments(out))
        self.assertIn('历史注释', out)
        self.assertEqual(yaml.safe_load(out)['codex-api-key'][0]['models'][0]['name'], 'gpt-7-synthetic')

    def test_incomplete_replacement_preserves_original_text_and_comments(self):
        out = self.rebuild(RAW, ['gpt-7-synthetic'], complete=False)
        self.assertEqual(RAW, out)
        self.assertEqual(self.comments(RAW), self.comments(out))

    def test_adjacent_same_host_keys_do_not_duplicate_comments(self):
        extra = """  - api-key: synthetic-second
    base-url: https://fixture.invalid/v1
    priority: 3
    # Only the second credential has this note.
    models:
      - name: gpt-6-synthetic
        alias: gpt-6-synthetic
"""
        raw = RAW.replace('logging-to-file: false', extra + 'logging-to-file: false')
        once = self.rebuild(raw)
        twice = self.rebuild(once)
        self.assertEqual(self.comments(raw), self.comments(once))
        self.assertEqual(self.comments(once), self.comments(twice))

    def test_hash_in_block_scalar_is_data_not_an_orphan_comment(self):
        raw = RAW.replace('    models:', '    extension-text: |\n      # this is literal data\n    models:')
        out = self.rebuild(raw)
        self.assertEqual(yaml.safe_load(raw), yaml.safe_load(out))
        self.assertEqual(out.count('# this is literal data'), 1)

if __name__ == '__main__':
    unittest.main()
