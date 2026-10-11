import asyncio
import socket
import sys
import unittest
from unittest import mock

from lbry.testcase import AdvanceTimeTestCase, AsyncioTestCase
from lbry.blob_exchange.serialization import BlobRequest
from lbry.blob_exchange.server import BlobServerProtocol


class TestBlobServerIdleTimeout(AdvanceTimeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.loop.set_debug(False)
        self.protocol = BlobServerProtocol(
            self.loop, mock.Mock(), 'bQEaw42GXsgCAGio1nxFncJSyRmnztSCjP', idle_timeout=1)
        self.transport = mock.Mock(spec=asyncio.Transport)
        self.transport.get_extra_info.side_effect = lambda name: (
            ('127.0.0.1', 33333) if name == 'peername' else None)
        self.protocol.connection_made(self.transport)
        self.addCleanup(self.protocol.connection_lost, None)

    async def test_idle_connection_closes_after_timeout(self):
        await self.advance(0.9)
        self.transport.close.assert_not_called()
        await self.advance(0.2)
        self.transport.close.assert_called_once_with()

    async def test_transfer_resets_idle_timeout(self):
        blob_hash = '0' * 96
        blob = self.protocol.blob_manager.get_blob.return_value
        blob.blob_hash = blob_hash
        blob.length = 1
        blob.get_is_verified.return_value = True
        self.protocol.blob_manager.completed_blob_hashes = {blob_hash}

        for _ in range(2):
            finish_transfer = asyncio.Event()

            async def sendfile(_protocol):
                await finish_transfer.wait()
                return 1

            blob.sendfile = mock.AsyncMock(side_effect=sendfile)
            await self.advance(0.5)
            transfer = self.loop.create_task(
                self.protocol.handle_request(BlobRequest.make_request_for_blob_hash(blob_hash)))
            await self.advance(0)
            blob.sendfile.assert_awaited_once_with(self.protocol)
            await self.advance(5)
            self.transport.close.assert_not_called()
            finish_transfer.set()
            await self.advance(0)
            self.assertTrue(transfer.done())
            await transfer

        await self.advance(0.9)
        self.transport.close.assert_not_called()
        await self.advance(0.2)
        self.transport.close.assert_called_once_with()


@unittest.skipUnless(sys.platform == 'win32', 'Windows Proactor transport')
class TestWindowsBlobServerShutdown(AsyncioTestCase):
    async def test_reset_socket_does_not_block_server_shutdown(self):
        await self.check_shutdown(ConnectionResetError(10054, 'Connection reset by peer'))

    async def test_clean_socket_does_not_block_server_shutdown(self):
        await self.check_shutdown()

    async def check_shutdown(self, shutdown_error=None):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        from asyncio.trsock import TransportSocket

        sock = mock.Mock(spec=socket.socket)
        sock.fileno.return_value = 42
        sock.shutdown.side_effect = shutdown_error
        sock.close.side_effect = lambda: setattr(sock.fileno, 'return_value', -1)
        server = await self.loop.create_server(asyncio.Protocol, '127.0.0.1', 0)
        self.addCleanup(server.close)
        protocol = BlobServerProtocol(self.loop, mock.Mock(), 'bQEaw42GXsgCAGio1nxFncJSyRmnztSCjP')
        self.addCleanup(sock.close)
        # Exercise Proactor's real close callback with a deterministic socket reset.
        transport = _ProactorBasePipeTransport(
            self.loop, sock, protocol, server=server,
            extra={'peername': ('127.0.0.1', 33333), 'socket': TransportSocket(sock)})
        errors = []
        self.loop.set_exception_handler(lambda _loop, context: errors.append(context))
        self.addCleanup(self.loop.set_exception_handler, None)
        await asyncio.sleep(0)

        server.close()
        transport.close()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(errors, [])
        self.assertEqual(sock.fileno(), -1)
        self.assertIsNone(protocol.transport)
        await asyncio.wait_for(server.wait_closed(), 1)
        protocol.blob_manager.connection_manager.incoming_connection_lost.assert_called_once()
