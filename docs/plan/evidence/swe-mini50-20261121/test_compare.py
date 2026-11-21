"""Run with python -m unittest discover -s <this directory>."""
from pathlib import Path
import tempfile
import json
import unittest
import compare

C = json.loads((Path(__file__).parent / 'cohort.json').read_text())
IDS = [v['instance_id'] for v in C['instance_image_cache']]


class PairTest(unittest.TestCase):
    def setup_arm(self, path, name, grades=None):
        path.mkdir()
        m = {'run_id':name, 'instance_ids':IDS,'selection':{'instances_sha256':C['source_file_sha256']},
             'settings':{'hints':False,'environment':'official'},
             'limits':{'budget_usd':50,'task_usd_limit':1,'concurrency':1,'task_timeout_s':500}}
        grades = grades or {}
        rows = [{'instance_id':i,'state':'finished','environment_used':True,'grading':grades.get(i,'unresolved')} for i in IDS]
        r = {'run_id':name,'selected':50,'graded':True,'resolved':sum(v['grading']=='resolved' for v in rows),'instances':rows}
        a = {'run_id':name,'runtime_revision':name,'model_profile':'new-model','tier':'medium','settings_digest':'a'*64,
             'items':{i:{'generation_attempts':1,'model_calls':2,'actual_profiles':{'new-model':2},
                         'actual_tiers':{'medium':2},'audit_complete':True} for i in IDS}}
        for n,doc in [('run',m),('report',r),('model-audit',a)]: self.put(path,n,doc)
        return m,r,a

    def put(self,path,n,doc):
        (path / (n+'.json')).write_text(json.dumps(doc))

    def calc(self,l,r):
        return compare.compare(l,r,'new-model','medium')['groups']['public_mini_50']

    def test_protocol_and_contamination(self):
        with tempfile.TemporaryDirectory() as d:
            l,r=Path(d)/'a',Path(d)/'b';self.setup_arm(l,'a',{IDS[0]:'resolved'});m,report,a=self.setup_arm(r,'b',{IDS[1]:'resolved',IDS[2]:'resolved'})
            v=self.calc(l,r);self.assertTrue(v['formal']);self.assertEqual(v['delta_resolved_count'],1)
            self.assertEqual(v['paired_outcomes']['gained'],2);self.assertEqual(v['paired_outcomes']['lost'],1)
            a['items'][IDS[2]]['actual_profiles']={'wrong':2};a['items'][IDS[3]]['generation_attempts']=2;self.put(r,'model-audit',a)
            report['instances'][4].update(state='error',grading='run_error');self.put(r,'report',report)
            v=self.calc(l,r);self.assertFalse(v['formal']);self.assertEqual(v['valid_first_attempt_pairs'],47)
            self.assertEqual(v['arms'][1]['observed_official_resolved'],2);self.assertIsNone(v['delta_percentage_points'])
            report['instances'].append(report['instances'][0]);self.put(r,'report',report)
            with self.assertRaises(ValueError):self.calc(l,r)

    def test_missing_report_is_not_zero_and_mismatched_limits_are_not_formal(self):
        with tempfile.TemporaryDirectory() as d:
            l,r=Path(d)/'a',Path(d)/'b';self.setup_arm(l,'a');m,report,a=self.setup_arm(r,'b')
            m['limits']['concurrency']=2;self.put(r,'run',m);v=self.calc(l,r)
            self.assertFalse(v['formal'])
            (r/'report.json').unlink();v=self.calc(l,r)
            self.assertIsNone(v['arms'][1]['observed_official_resolved'])
            self.assertEqual(v['arms'][1]['issue_flags_nonexclusive']['unrun_or_ungraded'],50)
