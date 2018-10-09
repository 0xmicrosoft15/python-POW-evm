from typing import (
    Any,
    cast,
    Dict,
    Set,
    Type,
)

from p2p.protocol import (
    Command,
    _DecodedMsgType,
)

from trinity.protocol.common.peer import BaseChainPeer
from trinity.protocol.les import commands
from trinity.protocol.les.peer import LESPeer
from trinity.protocol.les.requests import HeaderRequest
from trinity.protocol.les.monitors import LightChainTipMonitor
from trinity.sync.common.chain import BaseHeaderChainSyncer
from trinity.utils.timer import Timer


class LightChainSyncer(BaseHeaderChainSyncer):
    _exit_on_sync_complete = False

    subscription_msg_types: Set[Type[Command]] = {
        commands.GetBlockHeaders,
        commands.BlockHeaders,
    }

    tip_monitor_class = LightChainTipMonitor

    async def _run(self) -> None:
        self.run_task(self._persist_headers())
        await super()._run()

    async def _handle_msg(self, peer: BaseChainPeer, cmd: Command,
                          msg: _DecodedMsgType) -> None:
        if isinstance(cmd, commands.GetBlockHeaders):
            msg = cast(Dict[str, Any], msg)
            await self._handle_get_block_headers(cast(LESPeer, peer), msg)
        elif isinstance(cmd, commands.BlockHeaders):
            # `BlockHeaders` messages are handled at the peer level.
            pass
        else:
            self.logger.debug("Ignoring %s message from %s", cmd, peer)

    async def _handle_get_block_headers(self, peer: LESPeer, msg: Dict[str, Any]) -> None:
        if not peer.is_operational:
            return
        self.logger.debug("Peer %s made header request: %s", peer, msg)
        request = HeaderRequest(
            msg['query'].block_number_or_hash,
            msg['query'].max_headers,
            msg['query'].skip,
            msg['query'].reverse,
            msg['request_id'],
        )
        headers = await self._handler.lookup_headers(request)
        self.logger.trace("Replying to %s with %d headers", peer, len(headers))
        peer.sub_proto.send_block_headers(headers, buffer_value=0, request_id=request.request_id)

    async def _persist_headers(self) -> None:
        while self.is_operational:
            batch_id, headers = await self.wait(self.header_queue.get())

            timer = Timer()
            await self.wait(self.db.coro_persist_header_chain(headers))

            head = await self.wait(self.db.coro_get_canonical_head())
            self.logger.info(
                "Imported %d headers in %0.2f seconds, new head: #%d",
                len(headers), timer.elapsed, head.block_number)

            self.header_queue.complete(batch_id, headers)
