# Wallet storage and recovery

The first step toward independent wallet backup and sync is making local saves
and recovery reliable. Wallet format 1 remains readable by existing SDK releases;
this patch does not automatically convert wallets to a new encryption format.

## Persistence safeguards

- A wallet with `encrypt-on-disk` enabled must not silently save unlocked private
  keys in plaintext when its encryption password is missing or empty. Saving
  already encrypted accounts while locked is still supported.
- A failed encryption, decryption, preference write, or sync save restores the
  previous in-memory encryption preference and password. A later background
  save must not commit the failed encryption change.
- `wallet_import` preserves the destination's local encryption setting and
  password. The import password decrypts the backup; it does not replace the
  local password. Other preferences and accounts still merge. The legacy
  `sync_apply` password contract is unchanged on successful saves.
- Wallet writes use an exclusively created temporary file in the destination
  directory, flush and sync its contents, then replace the destination. Failures
  before replacement leave the previous file intact and clean up the temporary
  file. POSIX temporary files start with mode 0600. Windows files inherit the
  directory's ACLs. Existing destination mode bits are preserved.
- Legacy lbryum migration writes, syncs and verifies an exact recovery copy before
  replacing the original. Existing recovery copies are never overwritten. A
  failed replacement leaves both the original and verified recovery copy.
- Unsupported explicit wallet format versions fail to load instead of being
  treated as an empty wallet. Unversioned legacy JSON remains supported.

These safeguards do not promise durability against every filesystem, hardware
failure, or power loss. Account and channel merges still happen in memory before
the final save; this is not a transaction over all wallet state. Concurrent SDK
processes writing the same wallet are not supported by this patch.

## Existing encryption limits

Format 1 defaults to plaintext. Its optional local encryption protects account
seeds and spending keys, but imported channel certificates remain plaintext.
Local encryption uses the existing AES-CBC scheme without an authentication tag.
The existing packed backup/sync format also remains unchanged. A seed alone is
not sufficient to recover additional imported accounts and channel private keys.

Legacy recovery copies preserve the original encryption state, including any
plaintext keys. Desktop ZIP backups copy wallet files without adding archive
encryption. Neither should be advertised as a fully encrypted backup. This patch
does not delete historical recovery copies or rewrite personal backups.

## Next format: recovery before automatic migration

The intended direction is a local wallet that works without an Odysee account,
with optional encrypted backup transport. Blockchain balance/history retrieval
through a chosen Hub remains separate from cross-device wallet backup and sync.

Implement and review the SDK format before changing Desktop setup:

1. Encrypt the entire wallet payload, including imported account and channel
   private keys, with a versioned authenticated envelope using established
   cryptography-library primitives. Evaluate Argon2id and AES-GCM parameters on
   supported machines; bound all untrusted lengths and KDF parameters before
   allocation. Keep the local password independent of account-service passwords.
2. Provide portable encrypted export and restore before enabling conversion.
   Restore must verify account identities, receiving/change addresses, imported
   keys and signing ability. An OS password store may enable optional automatic
   unlock, but must not be the only route to recovery on another computer.
3. Migrate only after unlocking the source and verifying a recoverable backup.
   Write and verify the replacement before committing it. Wrong passwords,
   corruption, cancellation and write failures must leave a usable recovery
   path. Do not erase an older plaintext backup automatically or imply that
   overwriting it securely erases SSD snapshots and other copies.
4. Make unsupported readers fail safely, including older binaries that lack the
   new version check. A new JSON object with missing `accounts` is unsafe for
   those readers. Verify the final file marker and location against actual old
   releases; do not remove the old startup path and let them create a new wallet.
5. Add optional encrypted snapshot transport after local recovery is proven.
   Start with user-controlled files or a self-hosted endpoint. Include revision
   checks and explicit conflict handling; a stale device must not silently
   replace newer keys. Servers must not receive wallet decryption secrets.

Desktop can then offer clear backup, unlock and migration choices. Existing
users should retain a working wallet throughout that rollout; completing this
persistence patch alone is not grounds for mandatory encryption migration.

## Validation

Use generated, unfunded fixture wallets and the isolated regtest environment:

```sh
python -m unittest discover -v tests.unit.wallet
sh scripts/test.sh test tests.integration.blockchain.test_wallet_commands.WalletEncryptionAndSynchronization
```

The persistence tests cover interrupted writes, missing passwords, failed
password changes, preservation of local encryption on import, exact legacy
recovery copies, retry, and unsupported versions. Integration tests reload an
encrypted wallet from disk after a failed password change, sign with an imported
channel key and confirm a payment using the original spending keys. Existing
sync, export/import, locking and channel tests remain part of the regression run.
