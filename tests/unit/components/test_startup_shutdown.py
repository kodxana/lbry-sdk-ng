import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from lbry.extras.daemon.daemon import Daemon
from lbry.conf import Config
from lbry.extras.daemon.componentmanager import ComponentManager
from lbry.extras.daemon.components import WalletComponent


class StartupShutdownTests(IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_every_component_in_the_current_stage(self):
        entered = [asyncio.Event(), asyncio.Event()]
        cleaning = [asyncio.Event(), asyncio.Event()]
        finish_cleanup = asyncio.Event()
        tasks = []

        async def start(index):
            tasks.append(asyncio.current_task())
            entered[index].set()
            try:
                await asyncio.Future()
            finally:
                cleaning[index].set()
                await finish_cleanup.wait()

        siblings = [SimpleNamespace(running=False, _setup=lambda i=i: start(i)) for i in range(2)]
        dependent = SimpleNamespace(running=False, _setup=AsyncMock())
        manager = ComponentManager(Config(), skip_components=list(ComponentManager.default_component_classes))
        manager.sort_components = Mock(return_value=[siblings, [dependent]])
        startup = asyncio.create_task(manager.start())
        await asyncio.gather(*(event.wait() for event in entered))
        startup.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in cleaning)), 1)
            self.assertFalse(startup.done())
        finally:
            finish_cleanup.set()
            await asyncio.gather(startup, return_exceptions=True)
            # Also drain orphan tasks if the regression comes back.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(startup.cancelled())
        self.assertFalse(manager.started.is_set())
        dependent._setup.assert_not_called()

    async def test_daemon_waits_for_startup_before_closing_partial_wallet(self):
        entered = asyncio.Event()
        events = []

        async def start():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                events.append('startup cancelled')

        wallet = SimpleNamespace(running=False, stop=AsyncMock(side_effect=lambda: events.append('wallet stopped')))
        manager = SimpleNamespace(
            started=asyncio.Event(), get_actual_component=Mock(return_value=wallet),
            stop=AsyncMock(side_effect=lambda: events.append('components stopped')),
        )
        daemon = self.make_daemon(manager, asyncio.create_task(start()))
        await entered.wait()
        await daemon.stop()
        self.assertEqual(events, ['startup cancelled', 'wallet stopped', 'components stopped'])
        self.assertTrue(daemon.component_startup_task.cancelled())
        daemon.rpc_runner.cleanup.assert_awaited_once()

    async def test_already_cancelled_startup_still_cleans_partial_wallet(self):
        wallet = SimpleNamespace(running=False, stop=AsyncMock())
        manager = SimpleNamespace(
            started=asyncio.Event(), get_actual_component=Mock(return_value=wallet), stop=AsyncMock(),
        )
        task = asyncio.create_task(asyncio.sleep(100))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.make_daemon(manager, task).stop()
        wallet.stop.assert_awaited_once()
        manager.stop.assert_awaited_once()

    async def test_running_wallet_is_stopped_only_by_component_manager(self):
        wallet = SimpleNamespace(running=True, stop=AsyncMock())
        manager = SimpleNamespace(
            started=asyncio.Event(), get_actual_component=Mock(return_value=wallet), stop=AsyncMock(),
        )
        task = asyncio.create_task(asyncio.sleep(100))
        await self.make_daemon(manager, task).stop()
        wallet.stop.assert_not_awaited()
        manager.stop.assert_awaited_once()

    async def test_stop_before_wallet_manager_is_created(self):
        wallet = WalletComponent(SimpleNamespace(conf=Config()))
        await wallet.stop()
        self.assertIsNone(wallet.wallet_manager)

    @staticmethod
    def make_daemon(manager, task):
        daemon = object.__new__(Daemon)
        daemon.component_manager = manager
        daemon.component_startup_task = task
        daemon.rpc_runner = SimpleNamespace(cleanup=AsyncMock())
        daemon.streaming_runner = SimpleNamespace(cleanup=AsyncMock())
        daemon.metrics_runner = SimpleNamespace(cleanup=AsyncMock())
        daemon.analytics_manager = SimpleNamespace(is_started=False)
        return daemon
