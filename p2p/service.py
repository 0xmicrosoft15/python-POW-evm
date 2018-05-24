from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Callable, Optional

from p2p.cancel_token import CancelToken
from p2p.exceptions import OperationCancelled


class BaseService(ABC):
    logger: logging.Logger = None
    # Number of seconds cancel() will wait for run() to finish.
    _wait_until_finished_timeout = 5

    def __init__(self, token: CancelToken=None) -> None:
        if self.logger is None:
            self.logger = logging.getLogger(self.__module__ + '.' + self.__class__.__name__)

        self.finished = asyncio.Event()

        base_token = CancelToken(type(self).__name__)
        if token is None:
            self.cancel_token = base_token
        else:
            self.cancel_token = base_token.chain(token)

    async def run(
            self,
            finished_callback: Optional[Callable[['BaseService'], None]] = None) -> None:
        """Await for the service's _run() coroutine.

        Once _run() returns, set the finished event, call cleanup() and
        finished_callback (if one was passed).
        """
        try:
            await self._run()
        except OperationCancelled as e:
            self.logger.info("%s finished: %s", self, e)
        except Exception:
            self.logger.exception("Unexpected error in %s, exiting", self)
        else:
            self.logger.debug("%s finished cleanly", self)
        finally:
            # Set self.finished before anything else so that other coroutines started by this
            # service exit while we wait for cleanup().
            self.finished.set()
            await self.cleanup()
            if finished_callback is not None:
                finished_callback(self)

    async def cleanup(self) -> None:
        """Run the service's _cleanup() coroutine."""
        await self._cleanup()

    async def cancel(self):
        """Trigger the CancelToken and wait for the run() coroutine to finish."""
        self.logger.debug("Cancelling %s", self)
        self.cancel_token.trigger()
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=self._wait_until_finished_timeout)
        except asyncio.futures.TimeoutError:
            self.logger.info("Timed out waiting for %s to finish, exiting anyway", self)

    @property
    def is_finished(self) -> bool:
        return self.finished.is_set()

    @abstractmethod
    async def _run(self) -> None:
        """Run the service's loop.

        Should return or raise OperationCancelled when the CancelToken is triggered.
        """
        raise NotImplementedError()

    @abstractmethod
    async def _cleanup(self) -> None:
        """Clean up any resources held by this service.

        Called after the service's _run() method returns.
        """
        raise NotImplementedError()


class EmptyService(BaseService):
    async def _run(self) -> None:
        pass

    async def _cleanup(self) -> None:
        pass
