"""Exercise a packaged daemon with disposable wallets and a loopback Hub stub.

Only the standard library is used here: wallet code must come from the binary.
The stub has no blocks or transactions; real chain behavior is tested in regtest.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


SKIPPED_COMPONENTS = [
    'blob_manager', 'wallet_server_payments', 'dht', 'hash_announcer',
    'file_manager', 'disk_space', 'background_downloader', 'peer_protocol_server',
    'upnp', 'exchange_rate_manager', 'tracker_announcer_component', 'libtorrent_component',
]
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'wallets'


class HubHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.respond()
        except ConnectionError:
            pass  # the daemon can close its socket during failure cleanup

    def respond(self):
        for line in self.rfile:
            request = json.loads(line)
            method, params = request['method'], request.get('params', [])
            responses = {
                'server.version': ['packaged-wallet-test', '0.113.0'],
                'server.features': {},
                'server.peers.subscribe': [],
                'server.ping': None,
                'blockchain.headers.subscribe': {'height': 0, 'hex': ''},
                'blockchain.block.headers': {'count': 0, 'hex': ''},
                'blockchain.address.subscribe': [None] * len(params),
                'blockchain.address.unsubscribe': True,
            }
            response = {'jsonrpc': '2.0', 'id': request['id']}
            if method in responses:
                response['result'] = responses[method]
            else:
                self.server.unexpected.add(method)
                response['error'] = {'code': -32601, 'message': 'Unexpected test Hub method'}
            self.wfile.write(json.dumps(response).encode() + b'\n')


class HubStub(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), HubHandler)
        self.unexpected = set()


class BinaryTests(unittest.TestCase):
    binary = None
    log_dir = None
    expected_version = None

    def setUp(self):
        # Cleanup runs even if startup or a later assertion fails.
        # pylint: disable-next=consider-using-with
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='lbry binary ')))
        self.work = self.root / 'wallet test ł'
        self.work.mkdir()
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(('LBRY_', 'PYTHON', '_PYI_'))
        }
        self.environment['PYTHONUTF8'] = '1'
        for name in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_DATA_HOME',
                     'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
            directory = self.work / name.lower()
            directory.mkdir()
            self.environment[name] = str(directory)
        # Windows resolves known folders relative to USERPROFILE independently
        # of APPDATA. Supply the usual layout inside the disposable profile.
        for folder in ('AppData/Roaming', 'AppData/Local', 'Downloads'):
            (Path(self.environment['USERPROFILE']) / folder).mkdir(parents=True)
        self.hub = self.enterContext(HubStub())
        thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.hub.shutdown)
        self.config = self.work / 'daemon.yml'
        self.wallets = self.work / 'wallet' / 'wallets'
        self.wallets.mkdir(parents=True)
        self.config.write_text(json.dumps({
            'data_dir': str(self.work / 'data'),
            'wallet_dir': str(self.wallets.parent),
            'download_dir': str(self.work / 'downloads'),
            'api': '127.0.0.1:0',
            'streaming_server': '127.0.0.1:0',
            'blockchain_name': 'lbrycrd_regtest',
            'lbryum_servers': [f'127.0.0.1:{self.hub.server_address[1]}'],
            'components_to_skip': SKIPPED_COMPONENTS,
            'share_usage_data': False,
            'use_upnp': False,
        }), encoding='utf-8')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.sequence = 0
        self.url = None

    def rpc(self, method, **params):
        request = urllib.request.Request(
            self.url, json.dumps({'method': method, 'params': params}).encode(),
            {'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=10) as response:
            data = json.load(response)
        if 'error' in data:
            raise AssertionError(f'{method} failed: {data["error"]}')
        return data['result']

    def cli(self, *args):
        return subprocess.run(
            [str(self.binary), '--config', str(self.config), *args],
            cwd=self.work, env=self.environment, capture_output=True, text=True,
            timeout=60, check=True).stdout

    @contextlib.contextmanager
    def daemon(self, settings_only=False):
        if settings_only:
            configuration = json.loads(self.config.read_text(encoding='utf-8'))
            configuration['components_to_skip'] = SKIPPED_COMPONENTS + ['database', 'wallet']
            self.config.write_text(json.dumps(configuration), encoding='utf-8')
        self.sequence += 1
        log_path = self.log_dir / f'{self._testMethodName}-{self.sequence}.log'
        with log_path.open('w', encoding='utf-8') as log:
            process = subprocess.Popen(
                [str(self.binary), 'start', '--config', str(self.config)],
                cwd=self.work, env=self.environment, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), 'daemon exited during startup')
                    match = re.search(r'RPC server listening on TCP 127\.0\.0\.1:(\d+)',
                                      log_path.read_text(encoding='utf-8', errors='replace'))
                    if match:
                        self.url = f'http://127.0.0.1:{match[1]}/lbryapi'
                        try:
                            status = self.rpc('status')
                        except (urllib.error.URLError, TimeoutError):
                            pass  # the HTTP listener starts before the wallet is available
                        else:
                            if status['is_running']:
                                self.assertEqual(status['startup_status'], {} if settings_only else
                                                 {'database': True, 'wallet': True})
                                break
                    time.sleep(.2)
                else:
                    self.fail('daemon did not become ready within 60 seconds')
                self.assertEqual(self.rpc('version')['lbrynet_version'], self.expected_version)
                yield
                self.assertEqual(self.rpc('stop'), 'Shutting down')
                self.assertEqual(process.wait(timeout=30), 0, 'daemon shutdown failed')
                self.assertNotIn('Task was destroyed but it is pending!',
                                 log_path.read_text(encoding='utf-8', errors='replace'))
                self.assertFalse(self.hub.unexpected, f'Unexpected Hub methods: {self.hub.unexpected}')
            except BaseException:
                print(log_path.read_text(encoding='utf-8', errors='replace'), flush=True)
                raise
            finally:
                if process.poll() is None:
                    # Kill the PyInstaller bootloader and its child on Windows.
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       capture_output=True, check=False, timeout=15)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

    def state(self, wallet_id, encrypted, locked):
        self.assertEqual(self.rpc('wallet_status', wallet_id=wallet_id), {
            'is_encrypted': encrypted, 'is_locked': locked, 'is_syncing': False,
        })

    def exported_account(self, wallet_id):
        wallet = json.loads(self.rpc('wallet_export', wallet_id=wallet_id))
        self.assertEqual(len(wallet['accounts']), 1)
        return wallet['accounts'][0]

    def addresses(self, wallet_id='default_wallet'):
        result = self.rpc('address_list', wallet_id=wallet_id, page_size=1000)
        self.assertEqual(result['total_items'], len(result['items']))
        return sorted(item['address'] for item in result['items'])

    def encrypted_on_disk(self, wallet_id, original):
        path = self.wallets / wallet_id
        stored = json.loads(path.read_text(encoding='utf-8'))['accounts'][0]
        self.assertTrue(stored['encrypted'])
        self.assertEqual(stored['public_key'], original['public_key'])
        for field in ('seed', 'private_key'):
            self.assertTrue(original[field])
            self.assertNotIn(original[field], path.read_text(encoding='utf-8'))

    def test_import_preview_before_wallet_startup(self):
        with self.daemon():
            self.rpc('wallet_encrypt', new_password='import-fixture-password')
        original = (self.wallets / 'default_wallet').read_bytes()
        directory = self.work / 'data' / 'wallet-import'
        directory.mkdir()
        file_name = 'a' * 32 + '.wallet'
        staged = directory / file_name
        staged.write_bytes(original)
        with self.daemon(settings_only=True):
            self.assertEqual(self.rpc('wallet_import_preview', file_name='../default_wallet'),
                             {'ok': False, 'error': 'file'})
            self.assertEqual(self.rpc('wallet_import_preview', file_name=file_name)['error'], 'password-required')
            self.assertFalse(self.rpc('wallet_import_preview', file_name=file_name, password='wrong')['ok'])
            self.assertEqual(self.rpc('wallet_import_preview', file_name=file_name, password='import-fixture-password'),
                             {'ok': True, 'accounts': 1, 'channels': 0, 'encrypted': True})
            self.assertEqual(self.rpc('status')['startup_status'], {})
        self.assertEqual(original, staged.read_bytes())
        self.assertEqual(original, (self.wallets / 'default_wallet').read_bytes())

    def test_new_wallet_restart(self):
        self.assertEqual(self.cli('--version').strip(), f'lbrynet {self.expected_version}')
        wallet_id = 'created_wallet'
        with self.daemon():
            self.assertEqual(self.rpc('wallet_list')['items'][0]['id'], 'default_wallet')
            self.assertEqual(self.rpc('wallet_create', wallet_id=wallet_id, create_account=True)['id'], wallet_id)
            original = self.exported_account(wallet_id)
            addresses = self.addresses(wallet_id)
            self.assertTrue(addresses)
            self.assertIn(self.rpc('address_unused', wallet_id=wallet_id), addresses)
            self.state(wallet_id, False, False)
            self.assertTrue(self.rpc('wallet_encrypt', wallet_id=wallet_id, new_password='binary test password'))
            self.encrypted_on_disk(wallet_id, original)
            self.assertTrue(self.rpc('wallet_lock', wallet_id=wallet_id))
            self.state(wallet_id, True, True)
            self.assertFalse(self.rpc('wallet_unlock', wallet_id=wallet_id, password='wrong password'))
            self.state(wallet_id, True, True)
            self.assertTrue(self.rpc('wallet_unlock', wallet_id=wallet_id, password='binary test password'))
            self.state(wallet_id, True, False)
            self.assertEqual(self.exported_account(wallet_id), original)
        with self.daemon():
            self.state(wallet_id, True, True)
            self.encrypted_on_disk(wallet_id, original)
            self.assertTrue(self.rpc('wallet_unlock', wallet_id=wallet_id, password='binary test password'))
            self.assertEqual(self.exported_account(wallet_id), original)
            self.assertEqual(self.addresses(wallet_id), addresses)
            self.assertTrue(self.rpc('wallet_decrypt', wallet_id=wallet_id))
            self.state(wallet_id, False, False)
        with self.daemon():
            self.state(wallet_id, False, False)
            self.assertEqual(self.exported_account(wallet_id), original)
            self.assertEqual(self.addresses(wallet_id), addresses)

    def test_legacy_wallet_restart(self):
        expected = json.loads((FIXTURES / 'expected.json').read_text(encoding='utf-8'))
        for name in ('plain', 'encrypted'):
            with self.subTest(wallet=name):
                (self.wallets / 'default_wallet').write_bytes((FIXTURES / f'{name}.json').read_bytes())
                with self.daemon():
                    self.state('default_wallet', name == 'encrypted', name == 'encrypted')
                    if name == 'encrypted':
                        self.assertFalse(self.rpc('wallet_unlock', password='wrong password'))
                        self.state('default_wallet', True, True)
                        self.assertTrue(self.rpc('wallet_unlock', password=expected['password']))
                    account = self.exported_account('default_wallet')
                    for field, value in expected['account'].items():
                        self.assertEqual(account[field], value, field)
                    self.assertEqual(self.addresses(), expected['addresses'])
                    self.assertTrue(self.rpc('wallet_encrypt', new_password='new fixture password'))
                    self.encrypted_on_disk('default_wallet', account)
                with self.daemon():
                    self.state('default_wallet', True, True)
                    self.assertTrue(self.rpc('wallet_unlock', password='new fixture password'))
                    self.assertEqual(self.exported_account('default_wallet'), account)
                    self.assertEqual(self.addresses(), expected['addresses'])


def main():
    sys.stdout.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--log-dir', type=Path, default=Path('ci-results/binary'))
    args = parser.parse_args()
    BinaryTests.binary = args.binary.resolve(strict=True)
    BinaryTests.log_dir = args.log_dir.resolve()
    BinaryTests.log_dir.mkdir(parents=True, exist_ok=True)
    BinaryTests.expected_version = args.expected_version
    print(f'Binary SHA-256: {hashlib.sha256(BinaryTests.binary.read_bytes()).hexdigest()}', flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BinaryTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
