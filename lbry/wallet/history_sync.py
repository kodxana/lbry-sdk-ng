from contextlib import contextmanager
import logging


log = logging.getLogger(__name__)


class HistorySync:
    """Progress for a round of address discovery and history updates.

    Entries count work per address, not unique transactions. More work can be
    discovered while syncing, so these counters are not an overall percentage.
    """

    def __init__(self):
        self.pending = 0
        self.started = False
        self.failed = set()
        self.entries_completed = 0
        self.entries_total = 0

    def begin(self):
        if not self.pending:
            self.entries_completed = 0
            self.entries_total = 0
        self.started = True
        self.pending += 1

    def finish(self, key, success):
        self.pending -= 1
        if success:
            self.failed.discard(key)
        else:
            self.failed.add(key)

    @contextmanager
    def track(self, key):
        self.begin()
        try:
            yield
        except BaseException:
            self.finish(key, False)
            raise
        else:
            self.finish(key, True)

    def schedule(self, group, coro, key):
        # Count queued work immediately, including cancellation before the
        # coroutine starts and can enter its own tracking context.
        self.begin()
        task = group.add(coro)

        def finished(task):
            error = None if task.cancelled() else task.exception()
            if error is not None:
                log.error('Wallet history update failed', exc_info=(type(error), error, error.__traceback__))
            success = not task.cancelled() and error is None and task.result() is not False
            self.finish(key, success)

        task.add_done_callback(finished)
        return task

    @property
    def status(self):
        return {
            'state': 'syncing' if self.pending else 'incomplete' if self.failed else (
                'complete' if self.started else 'idle'
            ),
            'entries_completed': self.entries_completed,
            'entries_total': self.entries_total,
            'pending_operations': self.pending,
        }
