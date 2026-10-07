"""Portable lock acceptance fixtures; run with stdlib unittest in CI/evals."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('agentctl', Path(__file__).parents[1] / 'agentctl.py')
agentctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agentctl)


class FrameworkLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.agents').mkdir()
        (self.root / 'skill').mkdir()
        (self.root / 'skill/SKILL.md').write_text('Read [reference](ref.md#heading).\n')
        (self.root / 'skill/ref.md').write_text('# Heading\n')
        (self.root / 'report.md').write_text('Admission review\n')
        self.entry = dict(id='fixture', state='candidate', owner='maintainer',
            upstream=dict(repository='https://example.org/skills', commit=None, reason='Legacy import; upstream revision unknown'),
            concerns=['ui-design'], roots=['skill'], runtime_paths=[],
            files={str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (self.root / 'skill').iterdir()},
            provider_projections={'codex': {'root': 'skill', 'sha256': agentctl.tree_hash(self.root / 'skill')}},
            dependencies=[], compatibility=[], admission=None)
        self.contract = {'frameworks': {'lock': '.agents/frameworks.lock.json'}}

    def check(self, entries=None):
        (self.root / '.agents/frameworks.lock.json').write_text(json.dumps(
            {'schema_version': 1, 'frameworks': entries or [self.entry]}))
        return agentctl.validate_framework_lock(self.root, self.contract)

    def test_additional_files_require_hash_and_approved_fixture_passes(self):
        self.entry['additional_files'] = ['report.md']
        self.assertTrue(self.check())
        self.entry['files']['report.md'] = hashlib.sha256((self.root / 'report.md').read_bytes()).hexdigest()
        self.assertEqual(self.check(), [])
        self.entry.update(state='approved', admission={'author':'a','reviewer':'b','report':'report.md'})
        self.entry['upstream']['commit'] = 'a' * 40
        self.assertEqual(self.check(), [])

    def test_candidate_unresolved_reference_requires_reason(self):
        text = '[missing](absent.md)'
        (self.root / 'skill/ref.md').write_text(text)
        self.entry['files']['skill/ref.md'] = hashlib.sha256(text.encode()).hexdigest()
        self.entry['provider_projections']['codex']['sha256'] = agentctl.tree_hash(self.root / 'skill')
        self.assertTrue(self.check())
        self.entry['unresolved_references'] = {'skill/ref.md:absent.md': 'Not packaged upstream'}
        self.assertEqual(self.check(), [])
        self.entry.update(state='pilot', admission={'author':'a','reviewer':'b','report':'report.md'})
        self.entry['upstream']['commit'] = 'a' * 40
        self.assertTrue(any('unresolved' in e for e in self.check()))

    def test_dependency_pin_rejects_wrong_type_blank_and_floating(self):
        self.entry.update(state='approved', admission={'author':'a','reviewer':'b','report':'report.md'})
        self.entry['upstream']['commit'] = 'a' * 40
        for pin in (True, 123, ' ', ' latest ', 'main', 'master', '*'):
            with self.subTest(pin=pin):
                self.entry['dependencies'] = [{'id':'engine', 'kind':'binary', 'pin':pin,
                    'sha256':'a' * 64, 'reviewed':True}]
                self.assertTrue(self.check())

    def test_metadata_identifiers_cannot_hide_behind_whitespace(self):
        for field, value in (('id', ' '), ('owner', ' owner '), ('concerns', [' ui '])):
            with self.subTest(field=field):
                saved = self.entry[field]
                self.entry[field] = value
                self.assertTrue(self.check())
                self.entry[field] = saved
        self.entry.update(state='approved', admission={'author':'a','reviewer':' a ','report':'report.md'})
        self.entry['upstream']['commit'] = 'a' * 40
        self.assertTrue(self.check())

    def test_uppercase_windows_script_requires_dependency(self):
        self.entry.update(state='approved', admission={'author':'a','reviewer':'b','report':'report.md'})
        self.entry['upstream']['commit'] = 'a' * 40
        script = self.root / 'skill/RUN.CMD'
        script.write_text('@echo hello')
        script.chmod(0o644)
        self.entry['files']['skill/RUN.CMD'] = hashlib.sha256(script.read_bytes()).hexdigest()
        self.entry['provider_projections']['codex']['sha256'] = agentctl.tree_hash(self.root / 'skill')
        self.assertTrue(any('undeclared executable' in e for e in self.check()))

    def test_symlink_loop_returns_diagnostic(self):
        (self.root / 'skill/loop').symlink_to('loop')
        self.assertTrue(any('unsafe path' in e for e in self.check()))

    def test_unknown_upstream_needs_reason(self):
        del self.entry['upstream']['reason']
        self.assertTrue(self.check())

    def test_malformed_collections_fail_closed(self):
        for field in ('roots', 'concerns', 'runtime_paths', 'compatibility'):
            with self.subTest(field=field):
                saved = self.entry[field]
                self.entry[field] = 'skill'
                self.assertTrue(self.check())
                self.entry[field] = saved

    def test_uncovered_inventory_and_broken_symlink(self):
        self.entry['files']['report.md'] = hashlib.sha256((self.root / 'report.md').read_bytes()).hexdigest()
        self.assertTrue(self.check())
        del self.entry['files']['report.md']
        (self.root / 'skill/broken').symlink_to('absent')
        self.assertTrue(self.check())

    def test_runtime_cannot_hide_executable(self):
        self.entry.update(state='pilot', admission={'author':'a','reviewer':'b','report':'report.md'})
        self.entry['upstream']['commit'] = 'a' * 40
        (self.root / 'skill/cache').mkdir()
        (self.root / 'skill/cache/run.sh').write_text('echo unreviewed')
        self.entry['runtime_paths'] = ['skill/cache']
        self.assertTrue(any('runtime' in e for e in self.check()))

    def test_eval_ci_paths_cover_agent_changes(self):
        workflow = (Path(__file__).parents[2] / '.github/workflows/evals.yml').read_text()
        for event in ('push:', 'pull_request:'):
            # Event sections contain indented children; stop only at a sibling event.
            import re
            section = re.split(r'\n  [a-z_]+:', workflow.split('  ' + event, 1)[1])[0]
            self.assertIn(".agents/**", section)

    def test_legacy_and_candidate(self):
        self.assertEqual(agentctl.validate_framework_lock(self.root, {}), [])
        self.assertEqual(self.check(), [])

    def test_framework_lock_rejects_floating_or_missing_dependency(self):
        self.entry.update(state='approved', admission={'author':'a','reviewer':'b','report':'report.md'})
        self.assertTrue(any('commit' in e for e in self.check()))
        self.entry['upstream']['commit'] = 'a' * 40
        self.entry['dependencies'] = [{'id':'engine', 'kind':'binary', 'pin':'latest', 'sha256':None, 'reviewed':False}]
        self.assertTrue(any('dependency' in e for e in self.check()))

    def test_provider_built_projection_hashes(self):
        (self.root / 'skill/ref.md').write_text('tampered')
        self.assertTrue(any('projection' in e for e in self.check()))
        self.assertTrue(any('hash mismatch' in e for e in self.check()))

    def test_unlisted_file_and_missing_reference(self):
        (self.root / 'skill/unlisted.sh').write_text('echo hello')
        self.assertTrue(any('unlisted' in e for e in self.check()))
        (self.root / 'skill/ref.md').unlink()
        self.assertTrue(any('reference' in e for e in self.check()))

    def test_paths_and_symlinks_cannot_escape(self):
        self.entry['files']['../outside'] = '0' * 64
        self.assertTrue(any('unsafe path' in e for e in self.check()))
        del self.entry['files']['../outside']
        (self.root / 'skill/escape').symlink_to('/etc/passwd')
        self.assertTrue(any('unsafe path' in e for e in self.check()))

    def test_duplicate_concern(self):
        other = copy.deepcopy(self.entry)
        other['id'] = 'other'
        self.assertTrue(any('concern' in e for e in self.check([self.entry, other])))

    def test_promotion_needs_independent_review_and_executable_declaration(self):
        self.entry['state'] = 'pilot'
        self.entry['upstream']['commit'] = 'a' * 40
        self.entry['admission'] = {'author':'a','reviewer':'a','report':'report.md'}
        self.assertTrue(any('independent' in e for e in self.check()))
        self.entry['admission']['reviewer'] = 'b'
        (self.root / 'skill/ref.md').write_text('#!/bin/sh\necho hi\n')
        self.entry['files']['skill/ref.md'] = hashlib.sha256((self.root / 'skill/ref.md').read_bytes()).hexdigest()
        self.entry['provider_projections']['codex']['sha256'] = agentctl.tree_hash(self.root / 'skill')
        self.assertTrue(any('executable' in e for e in self.check()))

    def test_digest_is_order_independent_and_changes_with_selection(self):
        self.check()
        before = agentctl.framework_digest(self.root, self.contract)
        self.entry['files'] = dict(reversed(list(self.entry['files'].items())))
        self.check()
        self.assertEqual(before, agentctl.framework_digest(self.root, self.contract))
        self.entry['owner'] = 'new'
        self.check()
        self.assertNotEqual(before, agentctl.framework_digest(self.root, self.contract))

    def test_markdown_examples_and_runtime_files(self):
        (self.root / 'skill/cache').mkdir()
        (self.root / 'skill/cache/runtime').write_text('volatile')
        self.entry['runtime_paths'] = ['skill/cache']
        text = '```md\n[example](nonexistent.md)\n```\n`[example](absent.md)`\n[anchor](#local)\n'
        (self.root / 'skill/ref.md').write_text(text)
        self.entry['files']['skill/ref.md'] = hashlib.sha256(text.encode()).hexdigest()
        self.entry['provider_projections']['codex']['sha256'] = agentctl.framework_tree_hash(self.root / 'skill', [self.root / 'skill/cache'])
        self.assertEqual(self.check(), [])


if __name__ == '__main__':
    unittest.main()
