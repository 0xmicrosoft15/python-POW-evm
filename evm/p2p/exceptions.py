class AuthenticationError(Exception):
    pass


class DecryptionError(Exception):
    pass


class PeerConnectionLost(Exception):
    pass


class PeerDisconnected(Exception):
    pass


class UnknownProtocolCommand(Exception):
    pass


class UselessPeer(Exception):
    pass


class UnreachablePeer(Exception):
    pass


class EmptyGetBlockHeadersReply(Exception):
    pass


class LESAnnouncementProcessingError(Exception):
    pass


class TooManyTimeouts(Exception):
    pass


class StopRequested(Exception):
    pass
