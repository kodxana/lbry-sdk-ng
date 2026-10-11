import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lbry.testcase import AsyncioTestCase
from lbry.wallet import Ledger, Headers
from lbry.wallet.header_sync import HeaderDownloader
from lbry.wallet.network import ClientSession
from lbry.wallet.rpc import RPCError
from lbry.wallet.rpc.jsonrpc import JSONRPCv2
from tests.unit.wallet.test_headers import HEADERS, block_bytes


class HeaderSyncTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture_errors = []
        self.addCleanup(self.assertEqual, self.fixture_errors, [])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = Ledger({'db': Mock(), 'headers': Headers(str(Path(directory.name) / 'headers'))})
        self.ledger.headers.checkpoints = {}
        await self.ledger.headers.open()
        self.addCleanup(self.ledger.headers.close)
        self.ledger.header_sync_start_height = -1
        self.ledger.network.remote_height = 19
        self.ledger.network.running = True
        self.requests = []
        self.tasks = set()
        self.writers = set()
        self.received = asyncio.Queue()
        self.reply = self.headers_response
        batch_size = patch.object(HeaderDownloader, 'batch_size', 5)
        batch_size.start()
        self.addCleanup(batch_size.stop)
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        self.addCleanup(self.close_server)
        await self.connect()

    async def connect(self):
        client = ClientSession(network=self.ledger.network, server=self.server.sockets[0].getsockname(), timeout=2)
        await client.create_connection()
        self.addCleanup(client.close)
        self.ledger.network.client = client
        self.ledger.network._on_connected_controller.add(True)

    async def close_server(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def serve(self, reader, writer):
        self.writers.add(writer)
        self.tasks.add(asyncio.current_task())
        replies = set()
        try:
            while line := await reader.readline():
                request = json.loads(line)
                self.assertEqual(request['method'], 'blockchain.block.headers')
                height, count, _, _ = request['params']
                self.requests.append(height)
                task = asyncio.create_task(self.respond(writer, request['id'], height, count))
                replies.add(task)
        except ConnectionError:
            pass
        except Exception as error:
            self.fixture_errors.append(error)
        finally:
            for task in replies:
                task.cancel()
            results = await asyncio.gather(*replies, return_exceptions=True)
            self.fixture_errors.extend(
                result for result in results if isinstance(result, Exception) and not isinstance(result, ConnectionError)
            )
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(asyncio.current_task())

    async def respond(self, writer, request_id, height, count):
        self.received.put_nowait(height)
        result = await self.reply(height, count)
        writer.write(JSONRPCv2.response_message(result, request_id) + b'\n')
        await writer.drain()

    async def headers_response(self, height, count):
        raw = HEADERS[block_bytes(height):block_bytes(height + count)]
        return {'hex': raw.hex(), 'count': len(raw) // Headers.header_size}

    async def next_request(self):
        return await asyncio.wait_for(self.received.get(), 3)

    async def finish(self, task):
        return await asyncio.wait_for(task, 5)

    async def test_out_of_order_responses_are_validated_in_order_and_bounded(self):
        # Advertise more than a window, so the fixture also catches unbounded
        # prefetch rather than stopping requests at the remote tip by accident.
        self.ledger.network.remote_height = 99
        gates = {height: asyncio.Event() for height in (0, 5, 10, 15)}

        async def reply(height, count):
            if height in gates:
                await gates[height].wait()
            return await self.headers_response(height, count)

        self.reply = reply
        sync = asyncio.create_task(self.ledger.update_headers())
        self.addCleanup(sync.cancel)
        self.assertEqual([await self.next_request() for _ in range(4)], [0, 5, 10, 15])
        gates[15].set()
        gates[10].set()
        gates[5].set()
        # Later replies cannot advance the chain or percentage before genesis.
        await asyncio.sleep(0.02)
        self.assertEqual(self.ledger.headers.height, -1)
        self.assertEqual(self.ledger.header_sync_progress, 0)
        self.assertEqual(self.requests, [0, 5, 10, 15])
        self.ledger.network.remote_height = 19
        gates[0].set()
        await self.finish(sync)
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)
        self.assertEqual(self.ledger.header_sync_progress, 100)
        self.assertEqual(self.requests, [0, 5, 10, 15, 20])

    async def test_short_batches_restart_at_the_actual_next_height(self):
        async def reply(height, count):
            return await self.headers_response(height, min(count, 3))
        self.reply = reply
        await self.finish(self.ledger.update_headers())
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)
        self.assertIn(3, self.requests)
        self.assertIn(18, self.requests)

    async def test_invalid_proof_of_work_is_not_applied_and_rewind_discards_read_ahead(self):
        corrupted = False
        saw_rewind = False

        async def reply(height, count):
            nonlocal corrupted, saw_rewind
            response = await self.headers_response(height, count)
            if height == 10 and not corrupted:
                corrupted = True
                raw = bytearray.fromhex(response['hex'])
                raw[111] ^= 1  # corrupt the nonce of a real mainnet header
                response['hex'] = raw.hex()
            elif height == 9:
                saw_rewind = True
                self.assertEqual(self.ledger.headers.height, 9)
                self.assertEqual(self.ledger.headers._read(0, 10), HEADERS[:block_bytes(10)])
            return response

        self.reply = reply
        self.ledger.db.rewind_blockchain = AsyncMock()
        await self.finish(self.ledger.update_headers())
        self.assertTrue(saw_rewind)
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)

    async def test_cancellation_drains_prefetch_and_restart_preserves_applied_headers(self):
        blocked = asyncio.Event()

        async def reply(height, count):
            if height >= 5:
                await blocked.wait()
            return await self.headers_response(height, count)

        self.reply = reply
        sync = asyncio.create_task(self.ledger.update_headers())
        for _ in range(4):
            await self.next_request()
        while self.ledger.headers.height < 4:
            await asyncio.sleep(0)
        sync.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await sync
        self.assertEqual(self.ledger.network.client.concurrency, 32)
        await self.ledger.headers.close()
        await self.ledger.headers.open()
        self.assertEqual(self.ledger.headers._read(0, 5), HEADERS[:block_bytes(5)])
        self.reply = self.headers_response
        blocked.set()
        await self.finish(self.ledger.update_headers())
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)

    async def test_new_connection_discards_old_prefetched_responses(self):
        async with HeaderDownloader(self.ledger.network) as downloader:
            await downloader.get(0)
            old_client = self.ledger.network.client
            await self.connect()
            await old_client.close()
            await downloader.get(5)
            self.assertEqual(self.requests.count(5), 2)
            self.assertEqual(self.requests.count(10), 2)

    async def test_connection_change_while_waiting_retries_on_new_connection(self):
        gate = asyncio.Event()

        async def reply(height, count):
            await gate.wait()
            return await self.headers_response(height, count)

        self.reply = reply
        async with HeaderDownloader(self.ledger.network) as downloader:
            get = asyncio.create_task(downloader.get(0))
            for _ in range(4):
                await self.next_request()
            await self.connect()
            self.reply = self.headers_response
            gate.set()
            result = await self.finish(get)
            self.assertEqual(result, await self.headers_response(0, 5))
            self.assertEqual(self.requests.count(0), 2)

    async def test_disconnect_during_catchup_resumes_on_replacement_connection(self):
        async def blocked_reply(height, count):
            await asyncio.Event().wait()
        self.reply = blocked_reply
        sync = asyncio.create_task(self.ledger.update_headers())
        self.addCleanup(sync.cancel)
        for _ in range(4):
            await self.next_request()
        await self.ledger.network.client.close()
        self.reply = self.headers_response
        await self.connect()
        await self.finish(sync)
        self.assertEqual(self.requests.count(0), 2)
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)

    async def test_subscription_header_does_not_start_speculative_requests(self):
        await self.ledger.headers.connect(0, HEADERS[:block_bytes(19)])
        await self.ledger.update_headers(19, HEADERS[block_bytes(19):].hex(), subscription_update=True)
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)
        self.assertEqual(self.requests, [])

    async def test_remote_tip_can_advance_during_catchup(self):
        async def reply(height, count):
            if height == 0:
                self.ledger.network.remote_height = 19
            return await self.headers_response(height, count)
        self.ledger.network.remote_height = 9
        self.reply = reply
        await self.finish(self.ledger.update_headers())
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)

    async def test_malformed_or_failed_batch_drains_other_requests(self):
        for bad_reply in ({'hex': '00'}, {'hex': '00' * block_bytes(6)}, {'hex': None},
                          {'hex': 'z' * block_bytes(5) * 2}, RPCError(1, 'failed')):
            with self.subTest(reply=type(bad_reply).__name__):
                async def reply(height, count):
                    if height == 0:
                        return bad_reply
                    await asyncio.Event().wait()
                self.reply = reply
                with self.assertRaises((ValueError, RPCError)):
                    await self.finish(self.ledger.update_headers())
                self.assertEqual(self.ledger.headers.height, -1)
                self.assertEqual(self.ledger.network.client.concurrency, 32)

    async def test_timeout_retries_without_accumulating_requests(self):
        first = True

        async def reply(height, count):
            nonlocal first
            if height == 0 and first:
                first = False
                await asyncio.Event().wait()
            return await self.headers_response(height, count)

        self.reply = reply
        self.ledger.network.client.timeout = 0.5
        await self.finish(self.ledger.update_headers())
        self.assertEqual(self.requests.count(0), 2)
        self.assertEqual(self.ledger.network.client.concurrency, 32)
        self.assertEqual(self.ledger.headers._read(0, 20), HEADERS)
