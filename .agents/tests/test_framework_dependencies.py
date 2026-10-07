"""Offline dependency identity checks; artifacts are data, never programs."""
import contextlib
import hashlib
import io
import json
import os
import unittest
from unittest.mock import patch

import test_framework_lock as fixtures

agentctl = fixtures.agentctl


class FrameworkDependencyTests(unittest.TestCase):
    check = fixtures.FrameworkLockTests.check

    def setUp(self):
        fixtures.FrameworkLockTests.setUp(self)
        self.artifact = self.root / 'artifact'
        self.artifact.write_bytes(b'#!/bin/sh\nexit 99\n')
        self.dep = dict(id='engine', kind='native-binary', pin='engine-v0.1.5',
                        sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
                        bytes=self.artifact.stat().st_size, reviewed=False)
        self.entry['dependencies'] = [self.dep]
        self.check()

    def verify(self, framework='fixture', dependency='engine'):
        return agentctl.verify_framework_dependency(self.root, self.contract, framework,
                                                    dependency, self.artifact)

    def test_candidate_cli_verifies_without_execution_or_mutation(self):
        (self.root / '.agents/contract.json').write_text(json.dumps(dict(schema_version=1, **self.contract)))
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(agentctl.subprocess, 'run', side_effect=AssertionError('execution')):
            self.assertEqual(agentctl.main(['verify-dependency', str(self.root), '--framework', 'fixture',
                                          '--dependency', 'engine', '--artifact', str(self.artifact)]), 0)
        self.assertIn('no admission granted', output.getvalue())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_tampered_digest(self):
        self.artifact.write_bytes(b'x' * self.dep['bytes'])
        with self.assertRaisesRegex(agentctl.ContractError, 'SHA-256 mismatch'):
            self.verify()

    def test_missing_or_malformed_hash(self):
        for value in (None, True, 'a', 'A' * 64):
            self.dep['sha256'] = value
            self.check()
            with self.assertRaisesRegex(agentctl.ContractError, 'SHA-256'):
                self.verify()

    def test_bad_pins(self):
        for value in (None, True, '', ' main ', 'latest', 'main', 'master', '*', '^1.2.3', '~1.2.3', '1.x', '>=1.2.3', 'nightly'):
            self.dep['pin'] = value
            self.check()
            with self.subTest(pin=value), self.assertRaisesRegex(agentctl.ContractError, 'pin'):
                self.verify()

    def test_exact_version_and_commit_pins(self):
        for value in ('1.2.3', 'v1.2.3-rc.1', 'a' * 40, 'a' * 64):
            self.dep['pin'] = value
            self.check()
            self.assertEqual(self.verify(), self.dep['bytes'])

    def test_inventoried_vendored_pin(self):
        path = 'skill/ref.md'
        self.dep.update(pin='vendored-content', path=path, sha256=self.entry['files'][path])
        self.artifact = self.root / path
        self.dep['bytes'] = self.artifact.stat().st_size
        self.assertEqual(self.check(), [])
        self.assertEqual(self.verify(), self.dep['bytes'])
        del self.dep['path']
        self.check()
        with self.assertRaisesRegex(agentctl.ContractError, 'pin'):
            self.verify()

    def test_replacement_between_inspection_and_open(self):
        original_open = agentctl.os.open
        def replace_then_open(path, flags):
            self.artifact.rename(self.root / 'original')
            self.artifact.write_bytes(b'replaced')
            return original_open(path, flags)
        with patch.object(agentctl.os, 'open', side_effect=replace_then_open):
            with self.assertRaisesRegex(agentctl.ContractError, 'changed'):
                self.verify()

    def test_mutation_during_read(self):
        original_fstat = agentctl.os.fstat
        calls = 0
        def mutate_before_final_stat(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.artifact.write_bytes(b'changed')
            return original_fstat(fd)
        with patch.object(agentctl.os, 'fstat', side_effect=mutate_before_final_stat):
            with self.assertRaisesRegex(agentctl.ContractError, 'changed'):
                self.verify()

    def test_optional_size_and_bad_size(self):
        del self.dep['bytes']
        self.check()
        self.assertEqual(self.verify(), self.artifact.stat().st_size)
        for value in (True, 0, -1, '17', 1.5, 999):
            self.dep['bytes'] = value
            self.check()
            with self.subTest(size=value), self.assertRaisesRegex(agentctl.ContractError, 'size|bytes'):
                self.verify()

    def test_missing_and_directory(self):
        self.artifact.unlink()
        with self.assertRaises(agentctl.ContractError):
            self.verify()
        self.artifact.mkdir()
        with self.assertRaisesRegex(agentctl.ContractError, 'regular'):
            self.verify()

    def test_symlink_and_parent_symlink(self):
        target = self.root / 'real'
        self.artifact.rename(target)
        try:
            self.artifact.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(agentctl.ContractError, 'symlink|regular'):
            self.verify()
        self.artifact.unlink()
        directory = self.root / 'alias'
        directory.symlink_to(self.root, target_is_directory=True)
        self.artifact = directory / 'real'
        with self.assertRaisesRegex(agentctl.ContractError, 'symlink'):
            self.verify()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO unavailable')
    def test_fifo_rejected_before_open(self):
        self.artifact.unlink()
        os.mkfifo(self.artifact)
        with patch.object(agentctl.os, 'open', side_effect=AssertionError('must reject before open')):
            with self.assertRaisesRegex(agentctl.ContractError, 'regular'):
                self.verify()

    def test_oversized(self):
        with patch.object(agentctl, 'DEPENDENCY_MAX_BYTES', 2):
            with self.assertRaisesRegex(agentctl.ContractError, 'limit'):
                self.verify()

    def test_wrong_identifiers(self):
        for framework, dependency in [('absent', 'engine'), ('fixture', 'absent'), (' fixture', 'engine'), ('fixture', None)]:
            with self.subTest(framework=framework, dependency=dependency), self.assertRaises(agentctl.ContractError):
                self.verify(framework, dependency)

    def test_inventory_validation_precedes_artifact_read(self):
        (self.root / 'skill/ref.md').write_text('tampered')
        with patch.object(agentctl.os, 'open', side_effect=AssertionError('must validate first')):
            with self.assertRaisesRegex(agentctl.ContractError, 'hash mismatch'):
                self.verify()


if __name__ == '__main__':
    unittest.main()
