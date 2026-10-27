"""Check an import copy without starting a ledger or changing a wallet."""
import json
from pathlib import Path

from .account import Account
from .bip32 import PrivateKey, PublicKey, from_extended_key_string
from .manager import WalletManager
from .database import Database
from .header import Headers
from .wallet import Wallet, WalletStorage

MAX_WALLET_BYTES = 8 * 1024 * 1024


def preview_wallet(path, ledger_id, password=None):
    try:
        if password is not None and (not isinstance(password, str) or len(password) > 1024):
            return {'ok': False, 'error': 'password'}
        with Path(path).open('rb') as source:
            raw = source.read(MAX_WALLET_BYTES + 1)
        if len(raw) > MAX_WALLET_BYTES:
            return {'ok': False, 'error': 'size'}
        data = WalletStorage().upgrade(json.loads(raw))
        preferences = data.get('preferences', {})
        if not isinstance(data.get('name', 'Wallet'), str) or not isinstance(preferences, dict):
            return {'ok': False, 'error': 'format'}
        if any(not isinstance(value, dict) or 'value' not in value or
               not isinstance(value.get('ts'), (int, float)) for value in preferences.values()):
            return {'ok': False, 'error': 'format'}
        accounts = data.get('accounts')
        if not isinstance(accounts, list) or not accounts or len(accounts) > 1000:
            return {'ok': False, 'error': 'format'}
        if any(not isinstance(account, dict) or account.get('ledger') != ledger_id for account in accounts):
            return {'ok': False, 'error': 'network'}
        encrypted = sum(account.get('encrypted') is True for account in accounts)
        if encrypted and password is None:
            return {'ok': False, 'error': 'password-required'}
        manager = WalletManager()
        ledger = manager.get_or_create_ledger(ledger_id, {'db': Database(':memory:'), 'headers': Headers(':memory:')})
        wallet = Wallet()
        identities = set()
        channels = 0
        for values in accounts:
            if not isinstance(values.get('encrypted', False), bool):
                return {'ok': False, 'error': 'format'}
            public_key = from_extended_key_string(ledger, values['public_key'])
            if not isinstance(public_key, PublicKey):
                return {'ok': False, 'error': 'keys'}
            account = Account.from_dict(ledger, wallet, values)
            if account.encrypted and not account.decrypt(password):
                return {'ok': False, 'error': 'password'}
            expected = public_key.extended_key_string()
            if account.public_key.extended_key_string() != expected:
                return {'ok': False, 'error': 'keys'}
            if account.seed:
                derived = Account.get_private_key_from_seed(ledger, account.seed, '')
                if derived.public_key.extended_key_string() != expected:
                    return {'ok': False, 'error': 'keys'}
            if account.private_key and account.private_key.public_key.extended_key_string() != expected:
                return {'ok': False, 'error': 'keys'}
            if not values.get('encrypted') and values.get('private_key'):
                private_key = from_extended_key_string(ledger, values['private_key'])
                if not isinstance(private_key, PrivateKey) or private_key.public_key.extended_key_string() != expected:
                    return {'ok': False, 'error': 'keys'}
            if account.id in identities:
                return {'ok': False, 'error': 'keys'}
            identities.add(account.id)
            if not isinstance(account.channel_keys, dict):
                return {'ok': False, 'error': 'keys'}
            for certificate in account.channel_keys.values():
                PrivateKey.from_pem(ledger, certificate)
                channels += 1
        return {'ok': True, 'accounts': len(accounts), 'encrypted': bool(encrypted), 'channels': channels}
    except OSError:
        return {'ok': False, 'error': 'file'}
    except Exception:  # Malformed keys and passwords must never appear in RPC errors or logs.
        return {'ok': False, 'error': 'format'}
