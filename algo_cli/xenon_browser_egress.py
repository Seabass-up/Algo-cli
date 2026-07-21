"""Fail-closed URL, DNS, redirect, and peer policy for isolated browsers.

This module is deliberately transport-agnostic.  It validates the destination
that an external egress broker is allowed to connect to; it does not claim that
Chrome flags enforce a network boundary.  The Docker/network boundary lives in
``boron_browser_isolation`` and must independently force traffic through the
broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import re
import socket
from typing import Callable, Iterable, NoReturn
from urllib.parse import SplitResult, urlsplit, urlunsplit


MAX_URL_BYTES = 4096
DEFAULT_MAX_DNS_ANSWERS = 16
DEFAULT_MAX_REDIRECTS = 5

_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_AMBIGUOUS_NUMERIC_HOST_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*"
)
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_DENIED_EXACT_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_DENIED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
)


class XenonEgressRejected(ValueError):
    """A browser destination failed a closed egress invariant."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


Resolver = Callable[[str, int], Iterable[str]]


def _reject(reason_code: str) -> NoReturn:
    raise XenonEgressRejected(reason_code)


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        _reject("dns_resolution_failed")
    return tuple(str(row[4][0]) for row in rows)


def _canonical_host(raw_host: str) -> str:
    if not raw_host or len(raw_host) > 253:
        _reject("host_invalid")
    if "%" in raw_host or "\\" in raw_host:
        _reject("host_invalid")
    host = raw_host.rstrip(".").casefold()
    if not host:
        _reject("host_invalid")

    # Browsers accept legacy integer, octal, and hexadecimal IPv4 spellings.
    # Python's ipaddress intentionally does not.  Reject the whole ambiguous
    # family so validation and browser interpretation cannot diverge.
    if _AMBIGUOUS_NUMERIC_HOST_RE.fullmatch(host):
        _reject("ambiguous_numeric_host")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            _reject("host_invalid")
        if len(ascii_host) > 253:
            _reject("host_invalid")
        labels = ascii_host.split(".")
        if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
            _reject("host_invalid")
        host = ascii_host
    else:
        host = address.compressed

    if host in _DENIED_EXACT_HOSTS or host.endswith(_DENIED_HOST_SUFFIXES):
        _reject("local_discovery_denied")
    return host


def _public_addresses(
    host: str,
    port: int,
    resolver: Resolver,
    *,
    maximum: int,
) -> tuple[str, ...]:
    try:
        raw_answers = tuple(resolver(host, port))
    except XenonEgressRejected:
        raise
    except Exception:
        _reject("dns_resolution_failed")
    if not raw_answers:
        _reject("dns_empty")
    if len(raw_answers) > maximum:
        _reject("dns_answer_limit")

    answers: set[str] = set()
    for raw_answer in raw_answers:
        if type(raw_answer) is not str or not raw_answer or len(raw_answer) > 64:
            _reject("dns_answer_invalid")
        try:
            address = ipaddress.ip_address(raw_answer.split("%", 1)[0])
        except ValueError:
            _reject("dns_answer_invalid")
        # is_global rejects loopback, private, link-local, shared/CGNAT,
        # multicast, documentation, benchmarking, reserved, and unspecified
        # space for both IPv4 and IPv6.  One denied answer poisons the set;
        # selecting only a public sibling would permit DNS rebinding races.
        if (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address.is_loopback
            or address.is_link_local
            or address.is_private
        ):
            _reject("non_public_address")
        answers.add(address.compressed)
    if not answers:
        _reject("dns_empty")
    return tuple(sorted(answers))


def _split_url(raw_url: str) -> SplitResult:
    if type(raw_url) is not str:
        _reject("url_type")
    if not raw_url or len(raw_url.encode("utf-8")) > MAX_URL_BYTES:
        _reject("url_size")
    if _CONTROL_RE.search(raw_url) or "\\" in raw_url:
        _reject("url_ambiguous")
    try:
        split = urlsplit(raw_url)
        # Accessing .port performs the stdlib's range and syntax checks.
        _ = split.port
    except ValueError:
        _reject("url_invalid")
    return split


@dataclass(frozen=True, slots=True)
class XenonEgressPolicy:
    """Closed policy used for one isolated browsing session."""

    allowed_schemes: tuple[str, ...] = ("https",)
    allowed_ports: tuple[int, ...] = (443,)
    maximum_dns_answers: int = DEFAULT_MAX_DNS_ANSWERS
    maximum_redirects: int = DEFAULT_MAX_REDIRECTS
    allow_cross_origin_redirects: bool = False
    redirect_origin_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.allowed_schemes) is not tuple or not self.allowed_schemes:
            _reject("policy_schemes")
        if any(scheme not in {"http", "https"} for scheme in self.allowed_schemes):
            _reject("policy_schemes")
        if len(set(self.allowed_schemes)) != len(self.allowed_schemes):
            _reject("policy_schemes")
        if type(self.allowed_ports) is not tuple or not self.allowed_ports:
            _reject("policy_ports")
        if any(type(port) is not int or not 1 <= port <= 65535 for port in self.allowed_ports):
            _reject("policy_ports")
        if len(set(self.allowed_ports)) != len(self.allowed_ports):
            _reject("policy_ports")
        if type(self.maximum_dns_answers) is not int or not 1 <= self.maximum_dns_answers <= 64:
            _reject("policy_dns_limit")
        if type(self.maximum_redirects) is not int or not 0 <= self.maximum_redirects <= 20:
            _reject("policy_redirect_limit")
        if type(self.allow_cross_origin_redirects) is not bool:
            _reject("policy_redirect_mode")
        if type(self.redirect_origin_allowlist) is not tuple:
            _reject("policy_redirect_allowlist")
        canonical_allowlist: list[str] = []
        for origin in self.redirect_origin_allowlist:
            if type(origin) is not str:
                _reject("policy_redirect_allowlist")
            split = _split_url(origin)
            scheme = split.scheme.casefold()
            if scheme not in self.allowed_schemes or split.path not in {"", "/"}:
                _reject("policy_redirect_allowlist")
            if split.query or split.fragment or split.username is not None or split.password is not None:
                _reject("policy_redirect_allowlist")
            host = _canonical_host(split.hostname or "")
            port = split.port or (443 if scheme == "https" else 80)
            if port not in self.allowed_ports:
                _reject("policy_redirect_allowlist")
            canonical_allowlist.append(_origin(scheme, host, port))
        if len(set(canonical_allowlist)) != len(canonical_allowlist):
            _reject("policy_redirect_allowlist")
        object.__setattr__(self, "redirect_origin_allowlist", tuple(canonical_allowlist))


@dataclass(frozen=True, slots=True)
class XenonResolvedTarget:
    """A public destination with DNS answers pinned for one connection."""

    canonical_url: str = field(repr=False)
    origin: str = field(repr=False)
    scheme: str
    host: str = field(repr=False)
    port: int
    addresses: tuple[str, ...] = field(repr=False)
    decision_digest: str

    def audit_fields(self) -> dict[str, object]:
        """Return structural fields safe for receipts and telemetry."""

        return {
            "scheme": self.scheme,
            "port": self.port,
            "address_count": len(self.addresses),
            "decision_digest": self.decision_digest,
        }


def _origin(scheme: str, host: str, port: int) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{bracketed}{suffix}"


def resolve_public_url(
    raw_url: str,
    policy: XenonEgressPolicy,
    *,
    resolver: Resolver | None = None,
) -> XenonResolvedTarget:
    """Canonicalize and resolve one URL, rejecting every non-public answer."""

    if type(policy) is not XenonEgressPolicy:
        _reject("policy_type")
    split = _split_url(raw_url)
    scheme = split.scheme.casefold()
    if scheme in {"ws", "wss"}:
        _reject("websocket_denied")
    if scheme not in policy.allowed_schemes:
        _reject("scheme_denied")
    if split.username is not None or split.password is not None:
        _reject("userinfo_denied")
    host = _canonical_host(split.hostname or "")
    port = split.port or (443 if scheme == "https" else 80)
    if port not in policy.allowed_ports:
        _reject("port_denied")
    addresses = _public_addresses(
        host,
        port,
        resolver or _default_resolver,
        maximum=policy.maximum_dns_answers,
    )
    origin = _origin(scheme, host, port)
    path = split.path or "/"
    bracketed = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = bracketed if port == default_port else f"{bracketed}:{port}"
    canonical_url = urlunsplit((scheme, netloc, path, split.query, ""))
    digest_input = "\n".join((origin, *addresses)).encode("utf-8")
    decision_digest = "sha256:" + hashlib.sha256(digest_input).hexdigest()
    return XenonResolvedTarget(
        canonical_url=canonical_url,
        origin=origin,
        scheme=scheme,
        host=host,
        port=port,
        addresses=addresses,
        decision_digest=decision_digest,
    )


@dataclass(slots=True)
class XenonEgressSession:
    """Redirect and DNS-rebinding guard for a single approved navigation."""

    policy: XenonEgressPolicy
    resolver: Resolver = _default_resolver
    _initial_origin: str | None = field(default=None, init=False, repr=False)
    _redirect_count: int = field(default=0, init=False)

    def begin(self, raw_url: str) -> XenonResolvedTarget:
        if self._initial_origin is not None:
            _reject("session_already_started")
        target = resolve_public_url(raw_url, self.policy, resolver=self.resolver)
        self._initial_origin = target.origin
        return target

    def redirect(
        self,
        previous: XenonResolvedTarget,
        raw_url: str,
    ) -> XenonResolvedTarget:
        if type(previous) is not XenonResolvedTarget or self._initial_origin is None:
            _reject("session_not_started")
        if self._redirect_count >= self.policy.maximum_redirects:
            _reject("redirect_limit")
        target = resolve_public_url(raw_url, self.policy, resolver=self.resolver)
        if previous.scheme == "https" and target.scheme != "https":
            _reject("redirect_downgrade")
        if target.origin != previous.origin:
            if not self.policy.allow_cross_origin_redirects:
                _reject("cross_origin_redirect_denied")
            if target.origin not in self.policy.redirect_origin_allowlist:
                _reject("redirect_origin_not_allowed")
        self._redirect_count += 1
        return target

    def verify_dns_pin(self, target: XenonResolvedTarget) -> None:
        """Reject if a second resolution is not byte-for-byte the pinned set."""

        if type(target) is not XenonResolvedTarget:
            _reject("target_type")
        current = _public_addresses(
            target.host,
            target.port,
            self.resolver,
            maximum=self.policy.maximum_dns_answers,
        )
        if current != target.addresses:
            _reject("dns_rebinding")

    def verify_connected_peer(self, target: XenonResolvedTarget, raw_peer: str) -> str:
        """Require the transport's actual peer to be one of the pinned answers."""

        if type(target) is not XenonResolvedTarget or type(raw_peer) is not str:
            _reject("peer_type")
        try:
            peer = ipaddress.ip_address(raw_peer.split("%", 1)[0])
        except ValueError:
            _reject("peer_invalid")
        compressed = peer.compressed
        if (
            not peer.is_global
            or peer.is_multicast
            or peer.is_unspecified
            or peer.is_reserved
            or peer.is_loopback
            or peer.is_link_local
            or peer.is_private
            or compressed not in target.addresses
        ):
            _reject("peer_not_pinned")
        return compressed


__all__ = [
    "DEFAULT_MAX_DNS_ANSWERS",
    "DEFAULT_MAX_REDIRECTS",
    "MAX_URL_BYTES",
    "Resolver",
    "XenonEgressPolicy",
    "XenonEgressRejected",
    "XenonEgressSession",
    "XenonResolvedTarget",
    "resolve_public_url",
]
