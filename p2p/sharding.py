import asyncio
import logging
import time
from typing import (
    List,
)

import rlp

from evm.rlp.collations import Collation
from evm.rlp.headers import CollationHeader
from evm.chains.shard import Shard


from evm.db.shard import (
    Availability,
)

from evm.utils.padding import (
    zpad_right,
)
from evm.utils.blobs import (
    calc_chunk_root,
)

from evm.constants import (
    COLLATION_SIZE,
)

from p2p.cancel_token import (
    CancelToken,
    wait_with_token,
)
from p2p import protocol
from p2p.protocol import (
    Command,
    Protocol,
)
from p2p.peer import (
    BasePeer,
    PeerPool,
    PeerPoolSubscriber,
)
from p2p.p2p_proto import (
    DisconnectReason,
)
from p2p.exceptions import (
    HandshakeFailure,
    OperationCancelled,
)


COLLATION_PERIOD = 1


class Status(Command):
    _cmd_id = 0


class Collations(Command):
    _cmd_id = 1

    structure = rlp.sedes.CountableList(Collation)


class ShardingProtocol(Protocol):
    name = "sha"
    version = 0
    _commands = [Status, Collations]
    cmd_length = 2

    logger = logging.getLogger("p2p.sharding.ShardingProtocol")

    def send_handshake(self) -> None:
        cmd = Status(self.cmd_id_offset)
        self.logger.debug("Sending status msg")
        self.send(*cmd.encode([]))

    def send_collations(self, collations: List[Collation]) -> None:
        cmd = Collations(self.cmd_id_offset)
        self.logger.debug("Sending {} collations".format(len(collations)))
        self.send(*cmd.encode(collations))


class ShardingPeer(BasePeer):
    _supported_sub_protocols = [ShardingProtocol]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.incoming_collation_queue = asyncio.Queue()
        self.known_collation_hashes = set()

    #
    # Handshake
    #
    async def send_sub_proto_handshake(self) -> None:
        self.sub_proto.send_handshake()

    async def process_sub_proto_handshake(self,
                                          cmd: Command,
                                          msg: protocol._DecodedMsgType) -> None:
        if not isinstance(cmd, Status):
            self.disconnect(DisconnectReason.other)
            raise HandshakeFailure("Expected status msg, got {}, disconnecting".format(cmd))

    #
    # Receiving Collations
    #
    def handle_sub_proto_msg(self, cmd: Command, msg: protocol._DecodedMsgType) -> None:
        if isinstance(cmd, Collations):
            self._handle_collations_msg(msg)
        else:
            super().handle_sub_proto_msg(cmd, msg)

    def _handle_collations_msg(self, msg: List[Collation]) -> None:
        self.logger.debug("Received {} collations".format(len(msg)))
        for collation in msg:
            try:
                self.incoming_collation_queue.put_nowait(collation)
            except asyncio.QueueFull:
                self.logger.warning("Incoming collation queue full, dropping received collation")
            else:
                self.known_collation_hashes.add(collation.hash)

    def send_collations(self, collations: List[Collation]) -> None:
        self.logger.debug("Sending {} collations".format(len(collations)))
        for collation in collations:
            if collation.hash not in self.known_collation_hashes:
                self.known_collation_hashes.add(collation.hash)
                self.sub_proto.send_collations(collations)


class ShardSyncer(PeerPoolSubscriber):
    logger = logging.getLogger("p2p.sharding.ShardSyncer")

    def __init__(self, shard: Shard, peer_pool: PeerPool, token: CancelToken = None) -> None:
        self.shard = shard
        self.peer_pool = peer_pool
        self.peer_pool.subscribe(self)

        self.incoming_collation_queue = asyncio.Queue()

        self.collations_received_event = asyncio.Event()
        self.collations_proposed_event = asyncio.Event()

        self.cancel_token = CancelToken("ShardSyncer")
        if token is not None:
            self.cancel_token = self.cancel_token.chain(token)

        self.start_time = time.time()

    async def run(self) -> None:
        while True:
            collation = await wait_with_token(
                self.incoming_collation_queue.get(),
                token=self.cancel_token
            )

            if collation.shard_id != self.shard.shard_id:
                self.logger.debug("Ignoring received collation belonging to wrong shard")
                continue
            if self.shard.get_availability(collation.header) is Availability.AVAILABLE:
                self.logger.debug("Ignoring already available collation")
                continue

            self.logger.debug("Adding collation {} to shard".format(collation))
            self.shard.add_collation(collation)
            for peer in self.peer_pool.peers:
                peer.send_collations([collation])

            self.collations_received_event.set()
            self.collations_received_event.clear()

    def propose(self) -> None:
        """Broadcast a new collation to the network and add it to the local shard."""
        # create collation for current period
        period = self.get_current_period()
        body = zpad_right(str(self).encode("utf-8"), COLLATION_SIZE)
        header = CollationHeader(self.shard.shard_id, calc_chunk_root(body), period, b"\x11" * 20)
        collation = Collation(header, body)

        self.logger.debug("Proposing collation {}".format(collation))

        # add collation to local chain
        self.shard.add_collation(collation)

        # broadcast collation
        for peer in self.peer_pool.peers:
            peer.send_collations([collation])

        self.collations_proposed_event.set()
        self.collations_proposed_event.clear()

        return collation

    def register_peer(self, peer):
        asyncio.ensure_future(self.handle_peer(peer))

    async def handle_peer(self, peer):
        while True:
            try:
                collation = await wait_with_token(
                    peer.incoming_collation_queue.get(),
                    token=self.cancel_token
                )
                await wait_with_token(
                    self.incoming_collation_queue.put(collation),
                    token=self.cancel_token
                )
            except OperationCancelled:
                break  # stop handling peer if cancel token is triggered

    def get_current_period(self):
        # TODO: get this from main chain
        return int((time.time() - self.start_time) // COLLATION_PERIOD)
