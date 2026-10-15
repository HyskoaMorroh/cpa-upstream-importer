"""Exercise model evidence across the real plan JSON and frontend consumer."""
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import unittest

from cpa_probe.plan import SectionPlan
from server import plan_json


class PlanJsonModelEvidenceTests(unittest.TestCase):
    def public_section(self, verified):
        section = SectionPlan(
            section="codex-api-key", base_url="https://fixture.invalid/v1",
            api_key="synthetic-fixture-key",
            models=["fixture-tested", "fixture-history"],
            highest_models=["fixture-tested", "fixture-history"],
            verified_models=verified,
            model_provenance={"fixture-tested": "verified", "fixture-history": "inferred"},
            warnings=["Historical model is inferred, not a successful current probe."],
        )
        plan = SimpleNamespace(host="fixture.invalid", line_no=1,
                               masked_key="fixture-mask", skipped=False,
                               any_writable=False, sections={section.section: section})
        return json.loads(json.dumps(plan_json(plan)))["sections"][section.section]

    def test_serialization_preserves_verified_and_inferred_evidence(self):
        public = self.public_section(["fixture-tested"])
        self.assertEqual(public["verified_models"], ["fixture-tested"])
        self.assertEqual(public["model_provenance"]["fixture-history"], "inferred")
        self.assertIn("not a successful current probe", public["warnings"][0])

    def test_empty_evidence_never_infers_success_from_section_source(self):
        public = self.public_section([])
        self.assertEqual(public["model_source"], "probed")
        self.assertEqual(public["verified_models"], [])

    def test_frontend_consumes_serialized_evidence_without_reverse_mislabel(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        # Execute the actual main-table evidence calculation and single-site
        # candidate filter against the actual backend payload, not a JS fixture.
        main = re.search(r"const verified = new Set\(sp\.verified_models \|\| \[\]\);\s*const pending = [^;]+;", source)
        self.assertIsNotNone(main)
        single = re.search(r"\(sp\.highest_models \|\| \[\]\)\.filter\(m => !\(sp\.verified_models \|\| \[\]\)\.includes\(m\)\)", source)
        self.assertIsNotNone(single)
        script = ("const fs=require('node:fs');const sp=JSON.parse(fs.readFileSync(0,'utf8'));"
                  + main.group(0) + ";console.log(JSON.stringify({main:pending,single:"
                  + single.group(0) + "}));")
        for verified, expected in [(["fixture-tested"], ["fixture-history"]),
                                   ([], ["fixture-tested", "fixture-history"])]:
            with self.subTest(verified=verified):
                result = subprocess.run(["node", "-e", script],
                                        input=json.dumps(self.public_section(verified)),
                                        text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(result.stdout), {"main": expected, "single": expected})


if __name__ == "__main__":
    unittest.main()
