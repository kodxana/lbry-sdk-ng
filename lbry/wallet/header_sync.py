import asyncio

from lbry.wallet.header import Headers


class HeaderDownloader:
    """Bounded read-ahead from one hub; the ledger still validates in order."""

    batch_size = 2001
    window = 4

    def __init__(self, network):
        self.network = network
        self._client = None
        self._pending = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.clear()

    async def clear(self):
        for task in self._pending.values():
            task.cancel()
        await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()

    async def get(self, height):
        return await self.network.retriable_call(self._get, height)

    async def _get(self, height):
        # A rewind, short response or changed connection invalidates read-ahead.
        if self._client is not self.network.client or height not in self._pending:
            await self.clear()
            self._client = self.network.client
        next_height = max(self._pending) + self.batch_size if self._pending else height
        while len(self._pending) < self.window:
            if next_height > self.network.remote_height and next_height != height:
                break
            self._pending[next_height] = asyncio.ensure_future(
                self.network.get_headers(next_height, self.batch_size)
            )
            next_height += self.batch_size
        try:
            response = await self._pending[height]
            if self._client is not self.network.client or not self.network.is_connected:
                raise ConnectionError('Hub connection changed while downloading headers.')
            raw = response['hex']
            if not isinstance(raw, str) or len(raw) > self.batch_size * Headers.header_size * 2 or \
                    len(raw) % (Headers.header_size * 2):
                raise ValueError('Invalid header batch size.')
        except BaseException:
            # Retrieve speculative failures too, including those which completed
            # before a timeout or cancellation of the requested batch.
            await self.clear()
            raise
        del self._pending[height]
        return response
