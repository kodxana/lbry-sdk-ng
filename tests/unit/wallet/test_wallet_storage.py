import os
import json
import stat
import tempfile
from unittest import TestCase, mock

from lbry.wallet import WalletStorage
from lbry.error import WalletError


class TestWalletStorage(TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'wallet.json')
        self.storage = WalletStorage(self.path)
        self.original = {
            'version': 1, 'name': 'Original wallet', 'preferences': {}, 'accounts': []
        }
        self.updated = dict(self.original, name='Updated wallet')
        self.storage.write(self.original)
        with open(self.path, 'rb') as wallet_file:
            self.original_bytes = wallet_file.read()

    def assert_original_wallet(self):
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, 'rb') as wallet_file:
            self.assertEqual(self.original_bytes, wallet_file.read())
        self.assertEqual(self.original, WalletStorage(self.path).read())
        self.assertEqual([os.path.basename(self.path)], os.listdir(os.path.dirname(self.path)))

    def test_create_wallet(self):
        storage = WalletStorage(self.path + '.new')
        storage.write(self.updated)
        self.assertEqual(self.updated, WalletStorage(storage.path).read())
        mode = stat.S_IMODE(os.stat(storage.path).st_mode)
        if os.name == 'nt':
            self.assertTrue(mode & stat.S_IREAD)
            self.assertTrue(mode & stat.S_IWRITE)
        else:
            self.assertEqual(stat.S_IRUSR | stat.S_IWUSR, mode)

    def test_unsupported_version_preserves_file(self):
        for version in (0, 2, -1, True, '1', None):
            with self.subTest(version=version):
                data = json.dumps(dict(self.original, version=version))
                with open(self.path, 'w') as wallet_file:
                    wallet_file.write(data)
                with self.assertRaisesRegex(WalletError, 'Unsupported wallet version'):
                    self.storage.read()
                with open(self.path) as wallet_file:
                    self.assertEqual(data, wallet_file.read())

    def test_unversioned_wallet_keeps_accounts_and_preferences(self):
        legacy = dict(self.original)
        del legacy['version']
        with open(self.path, 'w') as wallet_file:
            json.dump(legacy, wallet_file)
        self.assertEqual(legacy, self.storage.read())

    def test_non_object_wallet_is_rejected(self):
        for value in ([], None, 'invalid'):
            with self.subTest(value=value):
                with open(self.path, 'w') as wallet_file:
                    json.dump(value, wallet_file)
                with self.assertRaises(WalletError):
                    self.storage.read()

    def test_old_predictable_temporary_file_is_untouched(self):
        previous_temp_path = f'{self.path}.tmp.{os.getpid()}'
        with open(previous_temp_path, 'w') as temp_file:
            temp_file.write('unrelated file')
        self.storage.write(self.updated)
        with open(previous_temp_path) as temp_file:
            self.assertEqual('unrelated file', temp_file.read())
        self.assertEqual(self.updated, self.storage.read())

    def test_temporary_file_is_private_before_writing(self):
        if os.name == 'nt':
            self.skipTest('Windows files inherit directory ACLs, not POSIX permissions')
        fdopen = os.fdopen

        def check_permissions(fd, *args, **kwargs):
            self.assertEqual(0o600, stat.S_IMODE(os.fstat(fd).st_mode))
            return fdopen(fd, *args, **kwargs)

        previous_umask = os.umask(0)
        try:
            with mock.patch('lbry.wallet.wallet.os.fdopen', side_effect=check_permissions):
                self.storage.write(self.updated)
        finally:
            os.umask(previous_umask)

    def test_replace_wallet_preserves_permissions(self):
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        original_mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.storage.write(self.updated)
        self.assertEqual(self.updated, WalletStorage(self.path).read())
        self.assertEqual(original_mode, stat.S_IMODE(os.stat(self.path).st_mode))

    def test_serialization_failure_preserves_wallet(self):
        with self.assertRaises(TypeError):
            self.storage.write(dict(self.updated, accounts=[object()]))
        self.assert_original_wallet()

    def test_partial_write_failure_preserves_wallet_and_cleans_temporary_file(self):
        fdopen = os.fdopen

        def fail_during_write(fd, *args, **kwargs):
            opened = fdopen(fd, *args, **kwargs)
            real_write = opened.write

            def write_partial(data):
                real_write(data[:20])
                raise OSError('injected disk full')

            opened.write = write_partial
            return opened

        with mock.patch('lbry.wallet.wallet.os.fdopen', side_effect=fail_during_write):
            with self.assertRaisesRegex(OSError, 'injected disk full'):
                self.storage.write(self.updated)
        self.assert_original_wallet()

    def test_sync_failure_preserves_wallet(self):
        error = OSError('injected sync failure')
        with mock.patch('lbry.wallet.wallet.os.fsync', side_effect=error):
            with self.assertRaises(OSError) as raised:
                self.storage.write(self.updated)
        self.assertIs(error, raised.exception)
        self.assert_original_wallet()

    def test_permission_failure_preserves_wallet(self):
        error = PermissionError('injected permission failure')
        with mock.patch('lbry.wallet.wallet.os.chmod', side_effect=error):
            with self.assertRaises(PermissionError) as raised:
                self.storage.write(self.updated)
        self.assertIs(error, raised.exception)
        self.assert_original_wallet()

    def test_replacement_failure_preserves_wallet_and_allows_retry(self):
        error = PermissionError('injected replacement failure')
        # Cover both the legacy rename path and replacement without depending
        # on which filesystem operation the writer uses.
        with mock.patch('lbry.wallet.wallet.os.rename', side_effect=error), \
                mock.patch('lbry.wallet.wallet.os.replace', side_effect=error):
            with self.assertRaises(PermissionError) as raised:
                self.storage.write(self.updated)
        self.assertIs(error, raised.exception)
        self.assert_original_wallet()

        self.storage.write(self.updated)
        self.assertEqual(self.updated, WalletStorage(self.path).read())

    def test_failed_first_save_reports_replacement_error(self):
        storage = WalletStorage(self.path + '.new')
        error = PermissionError('injected replacement failure')
        with mock.patch('lbry.wallet.wallet.os.rename', side_effect=error), \
                mock.patch('lbry.wallet.wallet.os.replace', side_effect=error):
            with self.assertRaises(PermissionError) as raised:
                storage.write(self.updated)
        self.assertIs(error, raised.exception)
        self.assertFalse(os.path.exists(storage.path))
