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

## First run, existing users and separate profiles

Desktop NG currently creates a dedicated application profile, supplies explicit
SDK data/wallet/download/config paths, filters inherited SDK overrides and checks
ownership before using or stopping a daemon. Standalone SDK defaults in
`lbry/conf.py` still discover legacy `lbrynet` and `lbryum` directories. This patch
does not change those defaults. Moving them without integrated setup could make
an existing user's wallet appear to be missing.

Before enabling the new format, SDK and Desktop must provide an explicit NG
profile and a setup flow that runs before wallet creation and synchronization:

- New users choose **Import an existing wallet** or **Create a separate wallet**.
  Explain backup requirements and Odysee sync compatibility before confirmation.
- Existing NG users get **Keep my current wallet**, **Migrate a copy**, or
  **Create a separate wallet**. Dismissing or postponing this prompt keeps their
  current wallet. Creating a separate wallet must never overwrite or hide the
  existing one; switching back must remain available.
- Read legacy sources only to make an explicitly selected, consistent copy.
  Leave their wallets, databases, config, blobs, downloads and backups intact.
  Require the source application to be stopped for a file-level snapshot.
  Never point the NG daemon at the source or reuse its running RPC service.
- Resolve real paths, junctions and symlinks before checking ownership and
  overlap. Reject source/destination aliases and nested directories that could
  expose legacy files to NG migration or cleanup. Honor platform case rules.
- Persist setup decisions and migration completion so updates do not repeatedly
  prompt or silently migrate. Test interruption before and after each commit
  point, resume/retry, rollback and switching back to the old application.

Use a concrete notice before converting an Odysee-connected wallet:

> This creates a local NG copy. Automatic wallet synchronization with Odysee
> will be disabled for this copy until a compatible sync method is available.
> Your original wallet will stay in its current folder. Both copies control the
> same blockchain funds, so payments made from either copy affect that balance.

Odysee login and wallet sync must be separate choices. Do not upload a new-format
wallet to the legacy service or overwrite its remote snapshot as a side effect
of login or migration. A compatibility mode may keep the existing format and
sync contract while the user postpones conversion. Test login, logout, offline
startup, password changes and retry with both old and migrated profiles.

## Optional blob import

Offer blob import separately from wallet migration, with a destination, space
estimate and a cancel/resume option. Copy into NG-owned storage, verify each
blob's content hash and size, and only then register it as complete. Do not trust
hash-looking filenames or silently adopt the old blob directory. Skip identical
verified blobs and leave corrupt, partial or unreadable source files untouched.

Import or reconstruct stream-descriptor relationships and relevant download
metadata so valid imported content can be found and served. Having blob files
alone does not recreate the complete downloads library. Test discovery,
playback, document materialization and P2P serving after import, including
missing descriptors and interrupted copies. Media-file import is a separate
option; users should not have to create both media and blob copies just to seed.

Ordinary copying needs additional disk space. A verified copy-on-write clone
may avoid that on supported filesystems, but it must not be promised across
drives. Do not use shared writable folders or symlinks to bypass profile
isolation. NG cleanup must never delete files in the legacy source.

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
