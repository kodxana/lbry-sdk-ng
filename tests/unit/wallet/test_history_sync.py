import asyncio
from binascii import hexlify
from hashlib import sha256
from unittest.mock import AsyncMock

from tests.unit.wallet.test_ledger import LedgerTestCase, MockNetwork
from tests.unit.wallet.test_transaction import get_transaction, get_output


class HistorySyncTests(LedgerTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.address = await self.account.receiving.get_or_create_usable_address()
        address_hash = self.ledger.address_to_hash160(self.address)
        transactions = [get_transaction(get_output(i, address_hash)) for i in range(1, 251)]
        self.history = [{'tx_hash': tx.id, 'height': 0} for tx in transactions]
        self.remote_status = sha256(''.join(f'{tx.id}:0:' for tx in transactions).encode()).hexdigest()
        self.network = MockNetwork(self.history, {tx.id: hexlify(tx.raw) for tx in transactions})
        self.network.client = None
        self.ledger.network = self.network

    async def asyncTearDown(self):
        self.ledger._update_tasks.cancel()
        await asyncio.gather(*self.ledger._update_tasks._tasks, return_exceptions=True)
        await asyncio.sleep(0)
        self.ledger.headers.io.close()
        await super().asyncTearDown()

    @property
    def progress(self):
        return self.ledger.history_sync.status

    async def test_long_history_counts_saved_batches_not_downloaded_transactions(self):
        writing = asyncio.Event()
        allow_write = asyncio.Event()
        second_batch = asyncio.Event()
        allow_second = asyncio.Event()
        save = self.ledger.db.save_transaction_io_batch
        fetch = self.network.get_transaction_batch
        calls = 0

        async def gated_save(*args):
            writing.set()
            await allow_write.wait()
            return await save(*args)

        async def gated_fetch(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                second_batch.set()
                await allow_second.wait()
            return await fetch(*args)

        self.ledger.db.save_transaction_io_batch = gated_save
        self.network.get_transaction_batch = gated_fetch
        task = asyncio.create_task(self.ledger.update_history(self.address, self.remote_status))
        try:
            await asyncio.wait_for(writing.wait(), 5)
            self.assertEqual(self.progress['state'], 'syncing')
            self.assertEqual(self.progress['entries_total'], 250)
            self.assertEqual(self.progress['entries_completed'], 0)
            allow_write.set()
            await asyncio.wait_for(second_batch.wait(), 5)
            self.assertEqual(self.progress['entries_completed'], 100)
            self.assertEqual(await self.account.get_balance(), sum(range(1, 101)))
            allow_second.set()
            self.assertTrue(await task)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.progress['state'], 'complete')
        self.assertEqual(self.progress['entries_completed'], 250)
        self.assertEqual(await self.account.get_balance(), sum(range(1, 251)))
        # A caught-up notification must not download or count the same history again.
        self.network.get_transaction_called.clear()
        await self.ledger.update_history(self.address, self.remote_status)
        self.assertEqual(self.progress['entries_total'], 0)
        self.assertEqual(self.network.get_transaction_called, [])

    async def test_write_failure_stays_incomplete_until_that_address_recovers(self):
        save = self.ledger.db.save_transaction_io_batch
        self.ledger.db.save_transaction_io_batch = AsyncMock(side_effect=OSError('fixture disk failure'))
        with self.assertRaisesRegex(OSError, 'fixture disk failure'):
            await self.ledger.update_history(self.address, self.remote_status)
        self.assertEqual(self.progress['state'], 'incomplete')
        self.assertEqual(self.progress['entries_completed'], 0)
        self.assertEqual(await self.account.get_balance(), 0)
        await self.ledger.update_history('unrelated empty address', None)
        self.assertEqual(self.progress['state'], 'incomplete')
        self.ledger.db.save_transaction_io_batch = save
        await self.ledger.update_history(self.address, self.remote_status)
        self.assertEqual(self.progress['state'], 'complete')
        self.assertEqual(await self.account.get_balance(), sum(range(1, 251)))

    async def test_cancellation_retains_partial_progress_and_retry_preserves_balance(self):
        blocked = asyncio.Event()
        fetch = self.network.get_transaction_batch
        calls = 0

        async def interrupted_fetch(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                blocked.set()
                await asyncio.Future()
            return await fetch(*args)

        self.network.get_transaction_batch = interrupted_fetch
        task = asyncio.create_task(self.ledger.update_history(self.address, self.remote_status))
        try:
            await asyncio.wait_for(blocked.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.progress['state'], 'incomplete')
        self.assertEqual(self.progress['entries_completed'], 100)
        self.network.get_transaction_batch = fetch
        await self.ledger.update_history(self.address, self.remote_status)
        self.assertEqual(self.progress['state'], 'complete')
        self.assertEqual(await self.account.get_balance(), sum(range(1, 251)))

    async def test_queued_updates_are_visible_even_when_cancelled_before_start(self):
        self.ledger.process_status_update((self.address, self.remote_status))
        self.assertEqual(self.progress['state'], 'syncing')
        tasks = list(self.ledger._update_tasks._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.progress['state'], 'incomplete')
        self.assertEqual(self.progress['pending_operations'], 0)
        await self.ledger.update_history(self.address, self.remote_status)
        self.assertEqual(self.progress['state'], 'complete')

    async def test_address_discovery_and_duplicate_notifications_remain_pending(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        self.network.is_connected = True

        async def subscribe(*addresses):
            if self.address in addresses:
                entered.set()
                await release.wait()
            return [self.remote_status if a == self.address else None for a in addresses]

        self.network.subscribe_address = subscribe
        task = asyncio.create_task(self.ledger.subscribe_account(self.account))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(self.progress['state'], 'syncing')
            self.assertEqual(self.progress['entries_total'], 0)
            self.ledger.process_status_update((self.address, self.remote_status))
            self.ledger.process_status_update((self.address, self.remote_status))
            release.set()
            await task
            await self.ledger._update_tasks.done.wait()
            await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.progress['state'], 'complete')
        self.assertEqual(self.progress['entries_total'], 250)
        self.assertEqual(self.progress['entries_completed'], 250)
        self.assertEqual(await self.account.get_balance(), sum(range(1, 251)))
