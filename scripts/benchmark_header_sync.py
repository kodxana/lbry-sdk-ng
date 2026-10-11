"""Compare serial and prefetched sync using public headers and a loopback hub.

No production wallet is opened and no external server is contacted. Input must
contain contiguous mainnet headers from genesis; validation remains enabled.
"""
import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from lbry.wallet import Ledger, Headers
from lbry.wallet.header_sync import HeaderDownloader
from lbry.wallet.network import ClientSession
from lbry.wallet.rpc.jsonrpc import JSONRPCv2


async def measure(raw, delay, window):
    connections, responses = set(), set()
    requests = 0
    validating = 0.0

    async def respond(writer, request):
        nonlocal requests
        requests += 1
        height, count, _, _ = request['params']
        data = raw[height * Headers.header_size:(height + count) * Headers.header_size]
        await asyncio.sleep(delay)
        result = {'hex': data.hex(), 'count': len(data) // Headers.header_size}
        writer.write(JSONRPCv2.response_message(result, request['id']) + b'\n')
        await writer.drain()

    async def serve(reader, writer):
        connections.add(asyncio.current_task())
        try:
            while line := await reader.readline():
                task = asyncio.create_task(respond(writer, json.loads(line)))
                responses.add(task)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    with tempfile.TemporaryDirectory() as directory:
        headers = Headers(str(Path(directory) / 'headers'))
        ledger = Ledger({'data_path': directory, 'headers': headers})
        headers.checkpoints = {}
        await headers.open()
        ledger.network.remote_height = len(raw) // Headers.header_size - 1
        ledger.network.running = True
        client = ClientSession(network=ledger.network, server=server.sockets[0].getsockname())
        await client.create_connection()
        ledger.network.client = client
        connect = headers.connect

        async def timed_connect(*args):
            nonlocal validating
            start = perf_counter()
            result = await connect(*args)
            validating += perf_counter() - start
            return result

        try:
            start = perf_counter()
            with patch.object(HeaderDownloader, 'window', window), patch.object(headers, 'connect', timed_connect):
                await asyncio.wait_for(ledger.update_headers(), 120)
            elapsed = perf_counter() - start
            if headers._read(0, len(headers)) != raw:
                raise AssertionError('Synced headers differ from the input.')
            return {'window': window, 'seconds': round(elapsed, 3),
                    'validation_seconds': round(validating, 3), 'requests': requests,
                    'headers': len(headers), 'sha256': hashlib.sha256(raw).hexdigest()}
        finally:
            await client.close()
            server.close()
            await server.wait_closed()
            for task in connections | responses:
                task.cancel()
            await asyncio.gather(*(connections | responses), return_exceptions=True)
            await headers.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('headers', type=Path)
    parser.add_argument('--count', type=int, default=20010)
    parser.add_argument('--latency-ms', type=float, default=120)
    args = parser.parse_args()
    if args.count < 1 or args.latency_ms < 0:
        parser.error('count must be positive and latency must be nonnegative')
    with args.headers.open('rb') as source:
        raw = source.read(args.count * Headers.header_size)
    if len(raw) != args.count * Headers.header_size:
        parser.error('the input does not contain the requested number of headers')
    results = [await measure(raw, args.latency_ms / 1000, window) for window in (1, 4)]
    print(json.dumps({'simulated_latency_ms': args.latency_ms, 'results': results}, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
