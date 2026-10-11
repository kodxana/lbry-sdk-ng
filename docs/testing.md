# Local test baseline

The Docker runner uses CPython 3.13 on Debian Bookworm. It tests the current
checkout, including uncommitted source changes, with the dependency versions
in `docker/test-constraints.txt`. This image is a test environment.

See the [Python compatibility audit](python-compatibility.md) for the known
historical installation findings and the migration status.

The Linux test image installs the pinned Hub with `lbry-rocksdb-ng` 0.8.3 from
its hash-pinned GitHub release. Docker rebuilds the environment when these
dependency pins change. For an existing source installation with the `hub`
extra, create a fresh virtual environment: the old `lbry-rocksdb` distribution
must not coexist with the maintained binding. Wallet and Hub database
directories do not need to be replaced.

## Requirements

- Docker with Linux containers and BuildKit, on Linux or through WSL2 on Windows.
- An x86-64 machine; the regtest node binaries are Linux amd64 builds.
- Allow roughly 6 GiB of RAM for integration tests (4 GiB for the SDK and
  2 GiB for Elasticsearch), plus the host OS and space for build layers.
- Internet access for the first build and image pulls. Subsequent builds reuse
  Docker's cache.

Run these commands from the repository root in a Linux/WSL shell:

```sh
sh scripts/test.sh smoke
sh scripts/test.sh unit
sh scripts/test.sh wallet
sh scripts/test.sh integration
sh scripts/test.sh lint
```

The default is `smoke`: create a temporary regtest chain, mine test funds, and
send transactions between accounts. It does not sync mainnet or use real funds.
The unit and wallet commands do not start Elasticsearch.

To retain logs, installed package versions, and a coverage XML report after a
test run, set an output directory. Files are copied out after the container
stops; the directory is not mounted into the container.

```sh
TEST_OUTPUT_DIR=ci-results sh scripts/test.sh unit
TEST_OUTPUT_DIR=ci-results sh scripts/test.sh integration transactions
```

Use a different directory for each suite when retaining multiple reports. Logs
and package versions are also exported on test failures and timeouts. A forced
timeout can prevent Python from finishing its coverage report.

The image also contains the pinned binary build tools. To build the Linux
executable, check `--version`, and retain the result:

```sh
TEST_OUTPUT_DIR=ci-results sh scripts/test.sh build
```

Run one integration group or a specific test with:

```sh
sh scripts/test.sh integration transactions
sh scripts/test.sh test tests.unit.wallet.test_wallet.TestWalletCreation.test_read_write
```

Integration groups are `blockchain`, `claims`, `datanetwork`, `other`,
`takeovers`, and `transactions`, matching the existing tox environments.
The `test` command starts Elasticsearch so it can also run integration tests.
Each invocation has a one-hour test timeout; override it in seconds if needed:

```sh
TEST_TIMEOUT=600 sh scripts/test.sh smoke
```

From PowerShell, pass the command through WSL using your checkout's Linux path:

```powershell
wsl -d Ubuntu --exec sh -c 'cd /mnt/f/save_lbry/lbry-sdk && sh scripts/test.sh smoke'
```

## Isolation and dependencies

The build installs the SDK's existing requirements, pinned transitive versions
from `docker/test-constraints.txt`, the Hub revision in `setup.py`, FFmpeg,
and the regtest executables. The sample video comes from an immutable Android
CTS revision and is checked against the SHA-384 already expected by the claim
tests. It can also be prepared for a native test run with:

```sh
python -m tests.integration.claims.fixtures
```

Python and Elasticsearch images are pinned by digest, and the regtest archives
have SHA-256 checks. Debian packages come from the Bookworm repositories.
Python source distributions are built locally, so byte-for-byte image
reproducibility is not guaranteed.
Installed Python versions are recorded in
`/opt/baseline-packages.txt` inside the image.

Tests run as an ordinary user, with no host directories mounted and no ports
published. Unit tests get a network namespace with loopback only. Integration
tests share Elasticsearch's loopback-only namespace so the SDK, Hub, and local
regtest processes can communicate. There is no external network route during
tests. The namespace reserves ports 50001-50100 for the test Hub listeners so
outgoing connections cannot acquire them as ephemeral source ports between
restarts. This setting applies only to the temporary container namespace.
Wallets, chains, and generated media live in temporary containers; the
runner removes its containers on success, failure, or interruption. Docker's
images and build cache remain for later runs.

The runner returns unittest's exit status, or 124 when its timeout expires.
A killed process can return a different nonzero status. A passing smoke test
only verifies basic regtest wiring; run the relevant suites before changing
wallet behavior.

## Troubleshooting

An Elasticsearch startup failure prints its container log. On Linux or WSL,
check Docker's memory allocation and the host's `vm.max_map_count`; Elasticsearch
7.12 requires at least 262144 for configurations that use memory-mapped indices.
The runner does not change host sysctl settings.

If an interrupted native fixture download left an invalid video, remove
`tests/integration/claims/files/ForBiggerEscapes.mp4` and run the fixture command
again. A checksum mismatch fails explicitly rather than changing expected test
results.

## Async test runner

`AsyncioTestCase` and `AdvanceTimeTestCase` remain available from `lbry.testcase`.
Their implementation is in `lbry.testcase_async`, which imports only the standard
library. The runner's own regressions can therefore run on newer Python before
the SDK's dependency upgrades are complete:

```sh
python -m unittest -v tests.unit.test_asyncio_testcase
```

The runner delegates test results and loop ownership to
`unittest.IsolatedAsyncioTestCase`. A final cleanup cancels remaining tasks and
closes async generators, including the no-pending-task case that Python 3.9's
runner skips. Two unittest invocation hooks add the SDK's
timeout behavior and support existing `addCleanup` callbacks that return
awaitables. CI exercises these hooks on Python 3.9, 3.12, 3.13, and 3.14; this
does not establish full SDK support on the newer interpreters.

Each async setup, test, teardown, and cleanup has its own `TIMEOUT` budget
(120 seconds by default; zero disables it). Its deadline callback is removed
when that phase finishes. Exceeding the budget reports `TimeoutError`, even if
the coroutine catches cancellation and returns. Unrelated cancellation remains
an error. Synchronous code and coroutines that never yield cannot be interrupted
by these deadlines; the container runner's process timeout remains the outer
limit. Deadlines measure real elapsed time, so advancing a virtual clock does
not consume them, but their callbacks still require the loop to make progress.

## Stream restart tests

The data-network suite tests interrupted and completed file saves separately.
The interrupted-save test pauses the second blob read until stopping the file
manager cancels it, verifies that the partial file is removed, then checks the
complete contents after restart. The completed-save test verifies the saved
path and contents after restart without waiting for a new write-start event.

Save, stop, start, and write-event waits in these two tests have ten-second
limits. Timeouts and cancellation propagate as test errors. A completed file
loaded from the database does not need to emit another write-start event.

## Claim search and resolve tests

The blockchain suite also opens a TCP connection to the Hub, requests a server
banner, and stops the regtest SPV node. It checks that the client disconnects,
both session-maintenance tasks finish, and the listener and session registries
are empty. This regression fails with the previous Hub pin because its
maintenance tasks remain active after shutdown.

The blockchain suite checks that stopping a regtest SPV node finishes its
search reader and notifier tasks before deleting the temporary Elasticsearch
index. The pinned Hub waits for task cleanup before closing shared resources.
Normal Hub shutdown preserves the index; the regtest harness requests deletion
explicitly.

The claims suite checks the pinned Hub's 2,048-ID search limit. A request at the
limit returns the expected claim; one above it reports an RPC error and leaves
the same connection usable. The resolve test checks every result across three
request batches, including existing claims and missing names. These replace
the skipped test that expected large search requests to disconnect the client.

The combined source, signature, and media-type filter test is also enabled. It
checks claim identities using a signed video, a signed stream without a source,
and a video whose signing channel has been abandoned.

## Header sync

Header catch-up keeps up to four batches of 2,001 headers in flight on the
existing hub connection. It does not benchmark hubs or open extra connections.
Responses are applied in order through the existing header validator; downloads
alone do not advance the chain or progress. Rewinds, short batches and connection
changes discard speculative responses. Cancellation drains the request tasks.
Normal single-header subscription updates do not start prefetch requests.

`status.wallet.headers_synchronization_progress` measures applied headers from
the height at the start of the current sync to the hub's current tip. It is an
integer from 0 to 100, rounded down, and cannot reach 100 while a header remains.
Before the starting height is known it reports zero unless already caught up.
The target can change as new blocks arrive or after reconnecting to another hub.
This percentage is not wallet readiness or the fraction of historical header
data cached on disk. `headers_sync_start_height`, `headers_sync_target_height`
and `checkpoint_chunks_remaining` expose the baseline, target and separate
background backfill. Existing component readiness checks still apply.

Run the focused TCP and progress regressions with:

```sh
python -m unittest -v tests.unit.wallet.test_header_sync tests.unit.components.test_wallet_sync
```

The TCP fixture exercises reordered replies, bounded read-ahead, corrupt proof
of work, rewinds, short/malformed replies, timeouts, disconnects, replacement
connections, cancellation, persisted restart and a moving tip. It validates
real mainnet headers. The blockchain suite additionally checks a funded regtest
wallet catching up after restart, keeping its addresses, balance and UTXO, then
signing and confirming a transfer. Existing chain-reorganization and transaction
suites remain required.

For an optional developer benchmark, supply a local file of contiguous public
mainnet headers starting at genesis. The script reads only that file, serves it
through a loopback TCP fixture and uses a temporary ledger. It neither opens a
production wallet nor contacts a public hub:

```sh
PYTHONPATH=. python scripts/benchmark_header_sync.py /path/to/headers --count 20010 --latency-ms 120
```

A Windows Python 3.13 comparison on 2026-10-11 took 1.773 seconds with one request
at a time and 0.686 seconds with four. Both made 11 requests, validated identical
20,010-header output, and spent 0.325 seconds in validation. This is a controlled
120 ms latency fixture, not an estimate of public-hub startup time. Validation
cost and server throughput still limit real-world gains.

## Historical SQLite diagnostic

The wallet unit suite checks batch writes through `AIOSQLite`, including
concurrent generator inputs, supported parameter types, rollback after binding
and constraint errors, and successful writes after an error. The batch-write
fixture closes its database and executors during cleanup.

The old `TestSQLiteRace` case exercised raw CPython SQLite and required a
historical binding error to occur. It could fail or time out when that error did
not occur. The reproducer is now a standalone, standard-library-only diagnostic:

```sh
python scripts/diagnose_sqlite_misuse.py --attempts 120000 --timeout 120
python scripts/diagnose_sqlite_misuse.py --attempts 120000 --timeout 120 --fetchall
```

The second command drains each cursor on the worker thread, matching the SDK's
existing workaround. Both commands print the Python and SQLite versions and
whether the historical error was observed. Reaching either limit without
reproducing it is inconclusive, not a failing SDK test. These diagnostic outcomes
exit successfully; unexpected exceptions still fail. The diagnostic is not part
of unittest discovery or CI, and uses only a temporary in-memory database.

## GitHub Actions

The `ci` workflow runs on pull requests, pushes to `master`, version tags, and
manual dispatch. Linux lint, unit tests, and all six integration groups use the
same container runner described above on Ubuntu 24.04. Windows 2022 and Intel
macOS 15 run configuration tests and the full wallet unit suite natively using
Python 3.13.15. The full wallet suite also runs in the Linux unit job.
Intel macOS builds cryptography against Homebrew OpenSSL with static linking;
it needs Rust and the Xcode command-line tools during installation.

The native jobs build executables and check `--version`. A Linux build runs
after the test jobs pass. Download `ci-*` artifacts for logs, package versions,
and per-suite coverage XML; `lbrynet-*` artifacts contain the binaries. These
artifacts expire after 14 days. Coveralls credentials are not needed.

The workflow has read-only repository permissions and does not publish releases.
Version tags run validation and produce artifacts only. Follow the
[release procedure](releasing.md) to prepare a community release from the
validated commit. Passing CI does not publish a release or approve deployment
against a production wallet.
