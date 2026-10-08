"""Portable schema and digest compatibility with the server's fixed-path helper."""
import copy
import importlib.util
from pathlib import Path
import unittest

_spec=importlib.util.spec_from_file_location('agentctl',Path(__file__).parents[1]/'agentctl.py')
agentctl=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agentctl)
policy=agentctl.framework_policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.lock={'schema_version':1,'frameworks':[{'id':'x','state':'candidate','concerns':['ui'],
            'observation':{'mode':'observe','trusted_github_reviewers':[],
                'concerns':{'ui':{'tier':'required_review','include_paths':['web/**'],'exclude_paths':['docs/**/*.md']}}}}]}

    def test_invalid_policy(self):
        for key, value in [('mode','enforce'),('trusted_github_reviewers',['a','A']),
                           ('trusted_github_reviewers',['../x']),('concerns',{}),('extra',True)]:
            lock=copy.deepcopy(self.lock);lock['frameworks'][0]['observation'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                policy.validate_declarations(lock)
        for key,value in [('tier','unknown'),('include_paths',['../x']),('exclude_paths',['/tmp']),
                          ('include_paths',['web/**','web/**']),('extra',True)]:
            lock=copy.deepcopy(self.lock);lock['frameworks'][0]['observation']['concerns']['ui'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                policy.validate_declarations(lock)

    def exception(self, **kw):
        value={'concern':'ui','pull_request':7,'owner':'carol','reason':'pilot outage',
               'expires_at':'2026-10-09T00:00:00Z','compensating_evidence':'manual review in #7'}
        value.update(kw)
        return value

    def test_modes_and_exceptions(self):
        for mode in policy.MODES:
            lock=copy.deepcopy(self.lock);lock['frameworks'][0]['observation']['mode']=mode
            lock['frameworks'][0]['observation']['concerns']['ui']['mode']=mode
            policy.validate_declarations(lock)
        lock=copy.deepcopy(self.lock);entry=lock['frameworks'][0]
        entry['observation']['mode']='evidence_required'
        self.assertEqual(policy.effective_mode(entry,'ui'),'evidence_required')
        entry['observation']['concerns']['ui']['mode']='enforced'
        self.assertEqual(policy.effective_mode(entry,'ui'),'enforced')
        self.assertEqual(policy.effective_mode(entry,'other'),'observe')
        self.assertEqual(policy.effective_mode({'id':'y'},'ui'),'observe')
        entry['observation']['exceptions']=[self.exception(),self.exception(pull_request=8,expires_at='2026-10-09T00:00:00+02:00')]
        policy.validate_declarations(lock)

    def test_invalid_modes_and_exceptions(self):
        lock=copy.deepcopy(self.lock);lock['frameworks'][0]['observation']['concerns']['ui']['mode']='enforce'
        with self.assertRaises(ValueError):
            policy.validate_declarations(lock)
        bad=[self.exception(concern='other'),self.exception(pull_request=0),self.exception(pull_request=True),
             self.exception(pull_request='7'),self.exception(owner='../x'),self.exception(owner='bad login'),
             self.exception(reason=''),self.exception(reason=' x'),self.exception(reason='x'*1001),
             self.exception(compensating_evidence='  '),self.exception(expires_at='2026-10-09T00:00:00'),
             self.exception(expires_at='tomorrow'),self.exception(expires_at=None),dict(self.exception(),extra=1),
             {k:v for k,v in self.exception().items() if k!='owner'},None]
        for value in bad:
            lock=copy.deepcopy(self.lock);lock['frameworks'][0]['observation']['exceptions']=[value]
            with self.subTest(value=value),self.assertRaises(ValueError):
                policy.validate_declarations(lock)
        for value in ([self.exception(),self.exception(owner='dave')],[self.exception()]*51,{}):
            lock=copy.deepcopy(self.lock);lock['frameworks'][0]['observation']['exceptions']=value
            with self.subTest(value=value),self.assertRaises(ValueError):
                policy.validate_declarations(lock)

    def test_same_principal_strips_bot_suffix(self):
        self.assertTrue(policy.same_principal('Renovate[bot]','renovate'))
        self.assertTrue(policy.same_principal('alice','ALICE'))
        self.assertFalse(policy.same_principal('alice','bob'))
        self.assertFalse(policy.same_principal(None,'bob'))

    def test_schema_and_digest(self):
        policy.validate_declarations(self.lock)
        self.assertEqual(policy.framework_digest(self.lock),policy.canonical_digest(self.lock))
        self.assertEqual(policy.scope_digest(['b','a','a']),policy.scope_digest(['a','b']))
        for lock in (None,{},dict(self.lock,schema_version=True),dict(self.lock,frameworks=[None]),
                     dict(self.lock,frameworks=self.lock['frameworks']*2)):
            with self.subTest(lock=lock),self.assertRaises(ValueError):
                policy.validate_declarations(lock)

    def test_onboard_copies_executable_helper(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            agentctl.migrate(root)
            self.assertTrue((root/'.agents/framework_policy.py').is_file())
            result=subprocess.run(['python3',str(root/'.agents/agentctl.py'),'check',str(root)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
