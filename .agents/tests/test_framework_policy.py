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
