import copy
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lbry.error import WalletError
from lbry.extras.daemon.daemon import Daemon
from lbry.testcase import AsyncioTestCase
from lbry.wallet import Account, Database, Headers, Ledger, Wallet, WalletManager, WalletStorage, ENCRYPT_ON_DISK


class WalletPersistenceTests(AsyncioTestCase):

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'default_wallet'
        self.ledger = Ledger({'db': Database(':memory:'), 'headers': Headers(':memory:')})
        self.wallet = Wallet(storage=WalletStorage(str(self.path)))
        account = self.wallet.generate_account(self.ledger)
        account.add_channel_private_key(account.private_key.child(7))
        Account.from_dict(self.ledger, self.wallet, {
            'private_key': account.private_key.child(8).extended_key_string()
        })
        Account.from_dict(self.ledger, self.wallet, {
            'public_key': account.private_key.child(9).public_key.extended_key_string()
        })
        self.original_accounts = copy.deepcopy(self.wallet.to_dict()['accounts'])
        self.wallet.save()

    def assert_reopens(self, password=None):
        manager = WalletManager(ledgers={Ledger: self.ledger})
        reopened = manager.import_wallet(str(self.path))
        for account in reopened.accounts:
            if account.encrypted:
                self.assertTrue(account.decrypt(password))
        self.assertEqual(self.original_accounts, reopened.to_dict()['accounts'])
        self.assertEqual(['default_wallet'], os.listdir(self.path.parent))

    def test_missing_password_cannot_overwrite_encrypted_wallet(self):
        self.wallet.encrypt('original password')
        original = self.path.read_bytes()
        for password in (None, ''):
            with self.subTest(password=password):
                self.wallet.encryption_password = password
                with self.assertRaises(WalletError):
                    self.wallet.save()
                self.assertTrue(self.wallet.preferences[ENCRYPT_ON_DISK])
                self.assertEqual(original, self.path.read_bytes())
                self.assert_reopens('original password')

    def test_partially_locked_wallet_cannot_save_plaintext_keys(self):
        self.wallet.encrypt('original password')
        original = self.path.read_bytes()
        self.wallet.accounts[0].encrypt('original password')
        self.wallet.encryption_password = None
        self.assertTrue(self.wallet.is_locked)
        with self.assertRaises(WalletError):
            self.wallet.save()
        self.assertEqual(original, self.path.read_bytes())
        self.assert_reopens('original password')

    def test_locked_wallet_can_save_existing_ciphertext(self):
        self.wallet.encrypt('original password')
        self.wallet.lock()
        self.wallet.encryption_password = None
        self.wallet.preferences['theme'] = 'dark'
        self.wallet.save()
        self.assert_reopens('original password')
        self.assertEqual('dark', WalletStorage(str(self.path)).read()['preferences']['theme']['value'])

    def test_failed_encryption_change_preserves_memory_and_disk(self):
        for operation in ('encrypt', 'change-password', 'decrypt'):
            for failure in ('fsync', 'chmod', 'replace'):
                with self.subTest(operation=operation, failure=failure):
                    self.wallet.encryption_password = None
                    self.wallet.preferences.data.clear()
                    self.wallet.save()
                    old_password = None
                    if operation != 'encrypt':
                        old_password = 'original password'
                        self.wallet.encrypt(old_password)
                    original = self.path.read_bytes()
                    preferences = copy.deepcopy(self.wallet.preferences.data)
                    with mock.patch(f'lbry.wallet.wallet.os.{failure}', side_effect=OSError('injected failure')):
                        with self.assertRaises(OSError):
                            if operation == 'decrypt':
                                self.wallet.decrypt()
                            else:
                                self.wallet.encrypt('new password')
                    self.assertEqual(original, self.path.read_bytes())
                    self.assertEqual(preferences, self.wallet.preferences.data)
                    self.assertEqual(old_password, self.wallet.encryption_password)
                    self.assert_reopens(old_password)
                    # A later save must not silently commit the failed request.
                    self.wallet.save()
                    self.assert_reopens(old_password)

    def test_failed_preference_change_cannot_disable_encryption(self):
        self.wallet.encrypt('original password')
        previous_preferences = copy.deepcopy(self.wallet.preferences.data)
        daemon = SimpleNamespace(wallet_manager=SimpleNamespace(
            get_wallet_or_default=lambda wallet_id: self.wallet
        ))
        with mock.patch('lbry.wallet.wallet.os.replace', side_effect=OSError('injected failure')):
            with self.assertRaises(OSError):
                Daemon.jsonrpc_preference_set(daemon, ENCRYPT_ON_DISK, False)
        self.assertEqual(previous_preferences, self.wallet.preferences.data)
        self.wallet.save()
        self.assert_reopens('original password')

    def test_local_import_preserves_destination_encryption(self):
        source = Wallet()
        imported_account = source.generate_account(self.ledger)
        imported_account.add_channel_private_key(imported_account.private_key.child(7))
        manager = WalletManager(ledgers={Ledger: self.ledger})
        for local_encrypted in (False, True):
            for source_encrypted in (False, True):
                with self.subTest(local=local_encrypted, source=source_encrypted):
                    if local_encrypted:
                        self.wallet.encrypt('local password')
                    else:
                        self.wallet.decrypt()
                    original_preference = copy.deepcopy(self.wallet.preferences.data[ENCRYPT_ON_DISK])
                    original_password = self.wallet.encryption_password
                    source.preferences[ENCRYPT_ON_DISK] = source_encrypted
                    source.preferences['theme'] = 'dark'
                    self.wallet.merge(manager, 'backup password', source.pack('backup password'),
                                      preserve_local_encryption=True)
                    self.wallet.save()
                    self.assertEqual(original_preference, self.wallet.preferences.data[ENCRYPT_ON_DISK])
                    self.assertEqual(original_password, self.wallet.encryption_password)
                    reopened = manager.import_wallet(str(self.path))
                    self.assertEqual(local_encrypted, reopened.is_locked)
                    imported = reopened.get_account_or_error(imported_account.id)
                    if local_encrypted:
                        self.assertTrue(imported.decrypt('local password'))
                    self.assertEqual(imported_account.to_dict(), imported.to_dict())
                    self.assertEqual('dark', reopened.preferences['theme'])

    async def test_failed_sync_password_change_preserves_local_encryption(self):
        self.wallet.encrypt('original password')
        original = self.path.read_bytes()
        preferences = copy.deepcopy(self.wallet.preferences.data)
        manager = WalletManager(wallets=[self.wallet], ledgers={Ledger: self.ledger})
        daemon = SimpleNamespace(wallet_manager=manager)
        for remote_encrypted in (False, True):
            with self.subTest(remote_encrypted=remote_encrypted):
                source = Wallet()
                source.preferences[ENCRYPT_ON_DISK] = remote_encrypted
                data = source.pack('sync password')
                with mock.patch('lbry.wallet.wallet.os.replace', side_effect=OSError('injected failure')):
                    with self.assertRaises(OSError):
                        await Daemon.jsonrpc_sync_apply.__wrapped__(daemon, 'sync password', data=data)
                self.assertEqual(original, self.path.read_bytes())
                self.assertEqual('original password', self.wallet.encryption_password)
                self.assertEqual(preferences, self.wallet.preferences.data)
                self.wallet.save()
                self.assert_reopens('original password')
                original = self.path.read_bytes()

    def test_future_encrypted_format_is_not_loaded_as_an_empty_wallet(self):
        original = b'{"version": 2, "ciphertext": "unsupported encrypted wallet"}'
        self.path.write_bytes(original)
        manager = WalletManager(ledgers={Ledger: self.ledger})
        with self.assertRaises(WalletError):
            manager.import_wallet(str(self.path))
        self.assertEqual([], manager.wallets)
        self.assertEqual(original, self.path.read_bytes())


class LegacyWalletMigrationTests(AsyncioTestCase):

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'default_wallet'
        self.ledger = Ledger({'db': Database(':memory:'), 'headers': Headers(':memory:')})
        wallet = Wallet()
        self.account = wallet.generate_account(self.ledger)
        self.account.add_channel_private_key(self.account.private_key.child(7))
        self.original_account = copy.deepcopy(self.account.to_dict())

    def write_legacy(self, password=None):
        account = self.account.to_dict(encrypt_password=password)
        receiving = self.account.public_key.child(0).child(0).pubkey_bytes
        change = self.account.public_key.child(1).child(0).pubkey_bytes
        legacy = {
            'master_public_keys': {'x/': account['public_key']},
            'master_private_keys': {'x/': account['private_key']},
            'seed': account['seed'], 'seed_version': 11, 'use_encryption': bool(password),
            'claim_certificates': account['certificates'], 'addr_history': {'fixture': []},
            'accounts': {'0': {'receiving': [receiving.hex()], 'change': [change.hex()]}}
        }
        original = json.dumps(legacy, indent=2).replace('\n', '\r\n').encode()
        self.path.write_bytes(original)
        return original, ({receiving}, {change})

    def assert_migrated_keys(self, password=None):
        manager = WalletManager(ledgers={Ledger: self.ledger})
        migrated = manager.import_wallet(str(self.path)).default_account
        if password:
            self.assertTrue(migrated.decrypt(password))
        actual = migrated.to_dict()
        for key in ('seed', 'private_key', 'public_key', 'certificates'):
            self.assertEqual(self.original_account[key], actual[key])
        self.assertEqual(self.account.id, migrated.id)
        for chain in (0, 1):
            self.assertEqual(self.account.public_key.child(chain).child(0).address,
                             migrated.public_key.child(chain).child(0).address)

    def test_plaintext_and_encrypted_legacy_wallets_keep_exact_recovery_copies(self):
        for password in (None, 'legacy password'):
            with self.subTest(encrypted=bool(password)):
                previous = set(self.path.parent.iterdir())
                original, addresses = self.write_legacy(password)
                self.assertEqual(addresses, WalletManager.migrate_lbryum_to_torba(str(self.path)))
                backups = set(self.path.parent.glob('old_lbryum_wallet_*')) - previous
                self.assertEqual(1, len(backups))
                self.assertEqual(original, backups.pop().read_bytes())
                self.assert_migrated_keys(password)
                files = {path: path.read_bytes() for path in self.path.parent.iterdir()}
                self.assertEqual((None, None), WalletManager.migrate_lbryum_to_torba(str(self.path)))
                self.assertEqual(files, {path: path.read_bytes() for path in self.path.parent.iterdir()})

    def test_existing_recovery_copy_is_never_overwritten(self):
        previous = self.path.parent / 'old_lbryum_wallet_1'
        previous.write_bytes(b'existing recovery copy')
        original, _ = self.write_legacy()
        WalletManager.migrate_lbryum_to_torba(str(self.path))
        self.assertEqual(b'existing recovery copy', previous.read_bytes())
        self.assertEqual(original, (self.path.parent / 'old_lbryum_wallet_2').read_bytes())
        self.assert_migrated_keys()

    def test_failed_recovery_copy_preserves_original(self):
        original, _ = self.write_legacy()
        with mock.patch('lbry.wallet.manager.os.fsync', side_effect=OSError('injected backup failure')):
            with self.assertRaises(OSError):
                WalletManager.migrate_lbryum_to_torba(str(self.path))
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(['default_wallet'], os.listdir(self.path.parent))

    def test_recovery_verification_failure_preserves_original(self):
        original, _ = self.write_legacy()
        real_open = open

        def corrupt_recovery_read(path, *args, **kwargs):
            if Path(path).name.startswith('old_lbryum_wallet_'):
                return mock.mock_open(read_data=b'incomplete recovery copy')()
            return real_open(path, *args, **kwargs)

        with mock.patch('lbry.wallet.manager.open', side_effect=corrupt_recovery_read):
            with self.assertRaisesRegex(OSError, 'recovery copy verification failed'):
                WalletManager.migrate_lbryum_to_torba(str(self.path))
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(['default_wallet'], os.listdir(self.path.parent))

    def test_failed_replacement_keeps_original_and_verified_backup_for_retry(self):
        for failure in ('fsync', 'chmod', 'replace'):
            with self.subTest(failure=failure):
                previous = set(self.path.parent.iterdir())
                original, _ = self.write_legacy('legacy password')
                error = OSError('injected replacement failure')
                effects = [None, error] if failure == 'fsync' else error
                with mock.patch(f'lbry.wallet.wallet.os.{failure}', side_effect=effects):
                    with self.assertRaises(OSError):
                        WalletManager.migrate_lbryum_to_torba(str(self.path))
                self.assertEqual(original, self.path.read_bytes())
                backups = set(self.path.parent.glob('old_lbryum_wallet_*')) - previous
                self.assertEqual(1, len(backups))
                backup = backups.pop()
                self.assertEqual(original, backup.read_bytes())
                self.assertEqual([], list(self.path.parent.glob('*.tmp')))
                WalletManager.migrate_lbryum_to_torba(str(self.path))
                self.assert_migrated_keys('legacy password')
                self.assertEqual(original, backup.read_bytes())
