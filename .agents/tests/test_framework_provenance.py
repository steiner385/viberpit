"""Offline source verification acceptance fixtures (no network or execution)."""
import gzip
import hashlib
import io
import json
import contextlib
import tarfile
import unittest
from unittest.mock import patch

import test_framework_lock as fixtures

agentctl = fixtures.agentctl


class FrameworkProvenanceTests(unittest.TestCase):
    check = fixtures.FrameworkLockTests.check
    def setUp(self):
        fixtures.FrameworkLockTests.setUp(self)
        self.prefix = 'fixture-' + 'a' * 40 + '/'
        self.entry['upstream'] = dict(repository='https://github.com/example/fixture', commit='a' * 40)
        self.archive = self.root / 'source.tgz'

    def pack(self, members=None):
        if members is None:
            members = [(self.prefix + name, (self.root / name).read_bytes(), tarfile.REGTYPE)
                       for name in self.entry['files']]
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, data, kind in members:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                member.linkname = 'outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                archive.addfile(member, io.BytesIO(data) if member.size else None)
        self.entry['upstream']['archive'] = dict(
            url='https://codeload.github.com/example/fixture/tar.gz/' + 'a' * 40,
            prefix=self.prefix, sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.check()

    def verify(self):
        return agentctl.verify_framework_source(self.root, self.contract, 'fixture', self.archive)

    def test_complete_source_does_not_promote_or_write(self):
        self.pack()
        before = (self.root / '.agents/frameworks.lock.json').read_bytes()
        self.assertEqual(self.verify(), 2)
        self.assertEqual(before, (self.root / '.agents/frameworks.lock.json').read_bytes())

    def test_archive_tampering_before_parsing(self):
        self.pack()
        self.archive.write_bytes(b'not an archive')
        with self.assertRaisesRegex(agentctl.ContractError, 'archive SHA-256'):
            self.verify()

    def test_invalid_current_inventory_is_rejected_first(self):
        self.pack()
        (self.root / 'skill/ref.md').write_text('tampered')
        with self.assertRaisesRegex(agentctl.ContractError, 'hash mismatch'):
            self.verify()

    def test_missing_different_duplicate_and_links(self):
        name = self.prefix + 'skill/ref.md'
        for members, error in [([], 'missing'), ([(name, b'bad', tarfile.REGTYPE)], 'hash mismatch'),
            ([(name, b'# Heading\n', tarfile.REGTYPE)] * 2, 'duplicate'),
            ([(name, b'', tarfile.SYMTYPE)], 'regular'), ([(name, b'', tarfile.LNKTYPE)], 'regular'),
            ([(name, b'', tarfile.DIRTYPE)], 'regular')]:
            with self.subTest(error=error, members=members):
                self.pack(members)
                with self.assertRaisesRegex(agentctl.ContractError, error):
                    self.verify()

    def test_unrelated_members_are_not_extracted(self):
        members = [(self.prefix + name, (self.root / name).read_bytes(), tarfile.REGTYPE)
                   for name in self.entry['files']]
        members.append(('../../escape', b'do not extract', tarfile.REGTYPE))
        self.pack(members)
        self.assertEqual(self.verify(), 2)

    def test_prefix_mismatch_and_malformed_archive(self):
        self.pack([('wrong/skill/ref.md', b'# Heading\n', tarfile.REGTYPE)])
        with self.assertRaisesRegex(agentctl.ContractError, 'missing'):
            self.verify()
        self.archive.write_bytes(gzip.compress(b'not tar'))
        self.entry['upstream']['archive']['sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.check()
        with self.assertRaisesRegex(agentctl.ContractError, 'archive'):
            self.verify()

    def test_archive_metadata_validation(self):
        for field, value in [('sha256', 'bad'), ('prefix', '../'), ('prefix', '/abs/'),
                             ('url', 'https://attacker.invalid/' + 'a' * 40), ('prefix', '')]:
            self.pack()
            self.entry['upstream']['archive'][field] = value
            self.assertTrue(self.check(), (field, value))

    def test_limits(self):
        self.pack()
        for limit in ('SOURCE_ARCHIVE_MAX_BYTES', 'SOURCE_SCAN_MAX_BYTES',
                      'SOURCE_MEMBER_MAX_BYTES', 'SOURCE_MAX_MEMBERS'):
            with self.subTest(limit=limit), patch.object(agentctl, limit, 1):
                with self.assertRaisesRegex(agentctl.ContractError, 'limit'):
                    self.verify()

    def test_no_metadata_and_unknown_selection(self):
        self.check()
        with self.assertRaisesRegex(agentctl.ContractError, 'archive metadata'):
            self.verify()
        with self.assertRaisesRegex(agentctl.ContractError, 'not selected'):
            agentctl.verify_framework_source(self.root, self.contract, 'absent', self.archive)

    def test_cli_checks_source_without_mutation(self):
        self.pack()
        (self.root / '.agents/contract.json').write_text(json.dumps(dict(agentctl.DEFAULT_CONTRACT, **self.contract)))
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agentctl.main(['verify-source', str(self.root), '--framework',
                                           'fixture', '--archive', str(self.archive)]), 0)
        self.assertIn('2 locked files; no admission granted', output.getvalue())
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes()
                                 for path in self.root.rglob('*') if path.is_file()})

    def test_trailing_gzip_bomb_is_bounded(self):
        self.pack()
        self.archive.write_bytes(self.archive.read_bytes() + gzip.compress(b'x' * 100000))
        self.entry['upstream']['archive']['sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.check()
        with patch.object(agentctl, 'SOURCE_SCAN_MAX_BYTES', 20000):
            with self.assertRaisesRegex(agentctl.ContractError, 'expanded scan limit'):
                self.verify()

    def test_invalid_gzip_deflate_is_a_diagnostic(self):
        self.pack()
        self.archive.write_bytes(bytes.fromhex('1f8b0800000000000003') + b'\xff' * 30)
        self.entry['upstream']['archive']['sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.check()
        with self.assertRaisesRegex(agentctl.ContractError, 'invalid source archive'):
            self.verify()

    def test_invalid_concatenated_gzip_is_a_diagnostic(self):
        self.pack()
        self.archive.write_bytes(self.archive.read_bytes() +
                                 bytes.fromhex('1f8b0800000000000003') + b'\xff' * 30)
        self.entry['upstream']['archive']['sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.check()
        with self.assertRaisesRegex(agentctl.ContractError, 'invalid source archive'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
