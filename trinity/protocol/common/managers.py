from abc import abstractmethod
import asyncio
from typing import (
    cast,
    Generic,
    TypeVar,
    Set,
    Tuple,
    Type,
)

from cancel_token import CancelToken

from p2p.exceptions import (
    ValidationError,
)
from p2p.peer import BasePeer, PeerSubscriber
from p2p.protocol import (
    Command,
)
from p2p.service import BaseService

from trinity.exceptions import AlreadyWaiting

from .requests import BaseRequest


PeerClass = TypeVar('PeerClass', bound=BasePeer)
RequestType = TypeVar('RequestType', bound=BaseRequest)
ResponseType = TypeVar('ResponseType')
ReturnType = TypeVar('ReturnType')


class BaseRequestManager(PeerSubscriber, BaseService, Generic[PeerClass, RequestType, ResponseType, ReturnType]):
    #
    # PeerSubscriber
    #
    @property
    def subscription_msg_types(self) -> Set[Type[Command]]:
        return {self._response_msg_type}

    msg_queue_maxsize = 100

    response_timout: int = 60

    pending_request: Tuple[RequestType, 'asyncio.Future[ReturnType]'] = None

    _peer: PeerClass

    def __init__(self, peer: PeerClass, token: CancelToken) -> None:
        self._peer = peer
        super().__init__(token)

    #
    # Service API
    #
    async def _run(self) -> None:
        self.logger.debug("Running %s for peer %s", self.__class__.__name__, self._peer)

        with self.subscribe_peer(self._peer):
            while True:
                peer, cmd, msg = await self.wait(
                    self.msg_queue.get(), token=self.cancel_token)
                if peer != self._peer:
                    self.logger.error("Unexpected peer: %s  expected: %s", peer, self._peer)
                    continue
                elif isinstance(cmd, self._response_msg_type):
                    self._handle_msg(cast(ResponseType, msg))
                else:
                    self.logger.warning("Unexpected message type: %s", cmd.__class__.__name__)

    async def _cleanup(self) -> None:
        pass

    def _handle_msg(self, msg: ResponseType) -> None:
        if self.pending_request is None:
            return

        request, future = self.pending_request

        try:
            request.validate_response(msg)
        except ValidationError as err:
            self.logger.debug(
                "Response validation failure for pending %s request from peer %s: %s",
                self._response_msg_type.__name__,
                self._peer,
                err,
            )
        else:
            future.set_result(self._normalize_response(msg))
            self.pending_request = None

    @abstractmethod
    def _normalize_response(self, msg: ResponseType) -> ReturnType:
        pass

    @abstractmethod
    def __call__(self) -> ReturnType:  # type: ignore
        """
        Subclasses must both implement this method and override the call
        signature to properly construct the `Request` object and pass it into
        `get_from_peer`
        """
        pass

    @property
    @abstractmethod
    def _response_msg_type(self) -> Type[Command]:
        pass

    @abstractmethod
    def _send_sub_proto_request(self, request: RequestType) -> None:
        pass

    async def _wait_for_response(self,
                                 request: RequestType,
                                 timeout: int = None) -> ReturnType:
        if self.pending_request is not None:
            self.logger.error(
                "Already waiting for response to %s for peer: %s",
                self._response_msg_type.__name__,
                self._peer,
            )
            raise AlreadyWaiting(
                "Already waiting for response to {0} for peer: {1}".format(
                    self._response_msg_type.__name__,
                    self._peer
                )
            )

        future: 'asyncio.Future[ReturnType]' = asyncio.Future()
        self.pending_request = (request, future)

        try:
            response = await self.wait(future, timeout=timeout)
        finally:
            # Always ensure that we reset the `pending_request` to `None` on exit.
            self.pending_request = None

        return response

    async def _request_and_wait(self, request: RequestType, timeout: int=None) -> ReturnType:
        if timeout is None:
            timeout = self.response_timout
        self._send_sub_proto_request(request)
        return await self._wait_for_response(request, timeout=timeout)
