import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lbry.testcase import AsyncioTestCase
from lbry.extras.daemon.daemon import Daemon
from lbry.wallet import Account, Database, Headers, Ledger, Wallet, WalletStorage
from lbry.wallet.import_preview import preview_wallet


class ImportPreviewTests(AsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'wallet-import').mkdir()
        self.path = self.root / 'wallet-import' / ('a' * 32 + '.wallet')
        self.ledger = Ledger({'db': Database(':memory:'), 'headers': Headers(':memory:')})
        self.wallet = Wallet(storage=WalletStorage(str(self.path)))
        account = self.wallet.generate_account(self.ledger)
        account.add_channel_private_key(account.private_key.child(7))
        Account.from_dict(self.ledger, self.wallet, {
            'public_key': account.private_key.child(8).public_key.extended_key_string()
        })
        self.wallet.save()

    def preview(self, password=None):
        original = self.path.read_bytes()
        with mock.patch.object(Ledger, 'start', side_effect=AssertionError('Network must not start')):
            result = preview_wallet(self.path, 'lbc_mainnet', password)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))
        return result

    def test_plaintext_and_encrypted_copy(self):
        self.assertEqual({'ok': True, 'accounts': 2, 'encrypted': False, 'channels': 1}, self.preview())
        self.wallet.encrypt('fixture-password')
        self.assertEqual('password-required', self.preview()['error'])
        self.assertFalse(self.preview('wrong')['ok'])
        self.assertEqual({'ok': True, 'accounts': 2, 'encrypted': True, 'channels': 1},
                         self.preview('fixture-password'))

    def test_invalid_keys_network_and_format(self):
        original = self.wallet.to_dict()
        for change in ('network', 'public_key', 'private_key', 'duplicate', 'certificate', 'version', 'empty'):
            with self.subTest(change=change):
                data = copy.deepcopy(original)
                first = data['accounts'][0]
                if change == 'network':
                    first['ledger'] = 'lbc_testnet'
                elif change == 'public_key':
                    first['public_key'] = data['accounts'][1]['public_key']
                elif change == 'private_key':
                    first['private_key'] = self.wallet.accounts[0].private_key.child(9).extended_key_string()
                elif change == 'duplicate':
                    data['accounts'].append(copy.deepcopy(first))
                elif change == 'certificate':
                    first['certificates'] = {'bad': 'secret-invalid-certificate'}
                elif change == 'version':
                    data['version'] = 9
                else:
                    data['accounts'] = []
                self.path.write_text(json.dumps(data))
                result = self.preview()
                self.assertFalse(result['ok'])
                self.assertEqual({'ok', 'error'}, set(result))

    def test_size_and_json_errors(self):
        self.path.write_bytes(b'private malformed JSON')
        self.assertEqual('format', self.preview()['error'])
        with mock.patch('lbry.wallet.import_preview.MAX_WALLET_BYTES', 8):
            self.assertEqual('size', self.preview()['error'])

    def test_rpc_only_accepts_staged_files(self):
        daemon = SimpleNamespace(conf=SimpleNamespace(data_dir=str(self.root), blockchain_name='lbrycrd_main'))
        for name in ('../default_wallet', str(self.path), 'a.wallet', None):
            self.assertEqual({'ok': False, 'error': 'file'},
                             Daemon.jsonrpc_wallet_import_preview(daemon, name))
        self.assertTrue(Daemon.jsonrpc_wallet_import_preview(daemon, self.path.name)['ok'])

