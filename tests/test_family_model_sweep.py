import unittest
from unittest.mock import patch
from cpa_probe.pipeline import Prober,SectionVerdict,Attempt,ProbePolicyStop
from cpa_probe.parse import ParsedRow
class FamilySweepTests(unittest.TestCase):
 def run_scan(self,budget=8,stop=False,accept=True):
  p=object.__new__(Prober);p.max_models=1;p.max_model_attempts=budget;p.on_event=lambda *a:None
  p._profile_kwargs=lambda *a:{}
  names=['gpt-6','gpt-6-sol','gpt-6-astra'];v=SectionVerdict(section='codex-api-key',base_url='https://fixture.example/v1',models=names[:1],catalog=names)
  calls=[]
  def call(section,base,key,model,**kw):
   if stop and calls: raise ProbePolicyStop('budget','fixture budget exhausted')
   calls.append(model)
   return Attempt(section=section,model=model,combo='model-scan',status='200',category='ok',action='accept',elapsed_ms=1,resp_model=model,response_valid=True,successful_base_url=base)
  p._call=call;p._accept=lambda *a:accept
  with patch('cpa_probe.model_catalog.remote_names',return_value=(names,'')):
   try:p._stage2(ParsedRow(1,'','https://fixture.example','fixture-key'),v)
   except ProbePolicyStop:pass
  return v,calls
 def test_family_continues_past_success_quota(self):
  v,calls=self.run_scan();self.assertEqual(len(calls),2);self.assertEqual(len(v.models),3);self.assertEqual(v.model_scan_pending,[])
 def test_budget_preserves_pending_candidates(self):
  v,calls=self.run_scan(budget=1);self.assertEqual(len(calls),1);self.assertEqual(len(v.model_scan_pending),1);self.assertIn('预算',v.model_scan_pending_reason)
 def test_policy_stop_preserves_completed_evidence(self):
  v,calls=self.run_scan(stop=True);self.assertEqual(len(v.models),2);self.assertEqual(len(v.model_scan_pending),1);self.assertIn('budget',v.model_scan_pending_reason)
 def test_completed_failed_candidates_are_not_pending(self):
  v,calls=self.run_scan(accept=False);self.assertEqual(len(calls),2);self.assertEqual(len(v.models),1);self.assertEqual(v.model_scan_pending,[])
if __name__=='__main__':unittest.main()

