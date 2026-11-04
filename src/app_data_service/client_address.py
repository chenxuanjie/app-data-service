"""Resolve client addresses only across explicitly trusted numeric proxy hops."""

from ipaddress import ip_address, ip_network


def trusted_proxy_networks(values):
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise ValueError("Configure at most 32 explicit trusted proxy IPs or CIDRs")
    if any(not isinstance(value, str) or "%" in value for value in values):
        raise ValueError("Trusted proxies must be numeric IPs/CIDRs without interface scopes")
    networks = tuple(ip_network(value, strict=False) for value in values)
    if any(network.prefixlen == 0 for network in networks):
        raise ValueError("Trust specific proxies, never all client addresses")
    return networks


def address(value):
    if "%" in value:
        raise ValueError("Interface-scoped addresses are not forwarded client identities")
    parsed = ip_address(value)
    return getattr(parsed, "ipv4_mapped", None) or parsed


class TrustedClientAddress:
    def __init__(self, app, *, proxies=()):
        self.app = app
        self.networks = trusted_proxy_networks(proxies)

    def trusted(self, value):
        return any(value.version == net.version and value in net for net in self.networks)

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and scope.get("client") and self.networks:
            try:
                peer = address(scope["client"][0])
                if self.trusted(peer):
                    forwarded = [
                        value
                        for key, value in scope.get("headers", ())
                        if key.lower() == b"x-forwarded-for"
                    ]
                    if len(forwarded) == 1 and len(forwarded[0]) <= 2048:
                        hops = forwarded[0].decode("ascii").split(",")
                        if not 1 <= len(hops) <= 16:
                            raise ValueError("Too many proxy hops")
                        chain = [address(value.strip()) for value in hops]
                        if any(ip.is_unspecified or ip.is_multicast for ip in chain):
                            raise ValueError("Invalid forwarded address")
                        # Ignore any attacker-supplied prefix to the left of the
                        # first untrusted hop. An all-trusted chain uses the peer.
                        for client in reversed(chain):
                            if not self.trusted(client):
                                scope = {**scope, "client": (str(client), 0)}
                                break
            except (ValueError, UnicodeError):
                pass  # Malformed/ambiguous headers never replace the actual peer.
        await self.app(scope, receive, send)
