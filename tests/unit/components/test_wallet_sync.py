import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from lbry.testcase import AsyncioTestCase
from lbry.wallet import Ledger, Headers
from lbry.conf import Config
from lbry.extras.daemon.components import WalletComponent
from tests.unit.wallet.test_headers import HEADERS, block_bytes


class WalletSyncProgressTests(AsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = Ledger({'db': Mock(), 'headers': Headers(str(Path(directory.name) / 'headers'))})
        self.ledger.headers.checkpoints = {}
        await self.ledger.headers.open()
        self.addCleanup(self.ledger.headers.close)
        self.ledger.network.remote_height = 19
        self.component = WalletComponent(SimpleNamespace(conf=Config()))
        self.component.wallet_manager = SimpleNamespace(
            ledger=self.ledger, get_best_blockhash=AsyncMock(return_value='test hash')
        )

    async def progress(self):
        return (await self.component.get_status())['headers_synchronization_progress']

    async def test_applied_headers_advance_progress_without_rounding_to_completion(self):
        self.ledger.header_sync_start_height = self.ledger.headers.height
        self.assertEqual(await self.progress(), 0)
        await self.ledger.headers.connect(0, HEADERS[:block_bytes(10)])
        self.assertEqual(await self.progress(), 50)
        await self.ledger.headers.connect(10, HEADERS[block_bytes(10):block_bytes(19)])
        self.assertEqual(await self.progress(), 95)
        await self.ledger.headers.connect(19, HEADERS[block_bytes(19):])
        self.assertEqual(await self.progress(), 100)
        # Completion is only for headers, not the wallet component's readiness.
        self.assertFalse(self.component.running)

    async def test_restart_measures_remaining_work_and_reports_background_chunks_separately(self):
        await self.ledger.headers.connect(0, HEADERS[:block_bytes(10)])
        await self.ledger.headers.close()
        await self.ledger.headers.open()
        self.ledger.header_sync_start_height = self.ledger.headers.height
        self.assertEqual(await self.progress(), 0)
        await self.ledger.headers.connect(10, HEADERS[block_bytes(10):block_bytes(15)])
        self.assertEqual(await self.progress(), 50)
        await self.ledger.headers.connect(15, HEADERS[block_bytes(15):])
        self.ledger.headers.known_missing_checkpointed_chunks = {1000, 2000}
        status = await self.component.get_status()
        self.assertEqual(status['headers_synchronization_progress'], 100)
        self.assertEqual(status['headers_sync_start_height'], 9)
        self.assertEqual(status['headers_sync_target_height'], 19)
        self.assertEqual(status['checkpoint_chunks_remaining'], 2)
        self.assertEqual(status['blocks_behind'], 0)

    async def test_unknown_start_and_changes_to_remote_tip(self):
        await self.ledger.headers.connect(0, HEADERS[:block_bytes(10)])
        self.assertEqual(await self.progress(), 0)
        self.ledger.header_sync_start_height = -1
        self.assertEqual(await self.progress(), 50)
        self.ledger.network.remote_height = 39
        self.assertEqual(await self.progress(), 25)
        self.ledger.network.remote_height = 9
        self.assertEqual(await self.progress(), 100)
        self.ledger.network.remote_height = 7
        self.assertEqual(await self.progress(), 100)
        # A shorter replacement chain can put the tip below the original start.
        self.ledger.header_sync_start_height = 30
        self.ledger.network.remote_height = 19
        self.assertEqual(await self.progress(), 0)

    async def test_unapplied_downloads_do_not_count(self):
        self.ledger.header_sync_start_height = -1
        self.ledger._download_height = 19
        self.assertEqual(await self.progress(), 0)
        # A very large catch-up still reports less than 100 with one block left.
        self.ledger.header_sync_start_height = -1000000
        await self.ledger.headers.connect(0, HEADERS[:block_bytes(19)])
        self.assertEqual(await self.progress(), 99)

    async def test_genesis_height_is_not_complete_until_present(self):
        self.ledger.network.remote_height = 0
        self.ledger.header_sync_start_height = -1
        self.assertEqual(self.ledger.header_sync_progress, 0)
        await self.ledger.headers.connect(0, HEADERS[:block_bytes(1)])
        self.assertEqual(self.ledger.header_sync_progress, 100)
