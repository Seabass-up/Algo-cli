from __future__ import annotations

from collections.abc import Iterable

import pytest

from algo_cli.xenon_browser_egress import (
    MAX_URL_BYTES,
    XenonEgressPolicy,
    XenonEgressRejected,
    XenonEgressSession,
    resolve_public_url,
)


PUBLIC_V4 = "8.8.8.8"
PUBLIC_V4_OTHER = "1.1.1.1"
PUBLIC_V6 = "2606:4700:4700::1111"


def _resolver(*answers: str):
    def resolve(_host: str, _port: int) -> Iterable[str]:
        return answers

    return resolve


def test_public_https_is_canonicalized_and_audit_fields_hide_destination() -> None:
    target = resolve_public_url(
        "HTTPS://ExAmPle.COM./path?q=private#fragment",
        XenonEgressPolicy(),
        resolver=_resolver(PUBLIC_V4, PUBLIC_V6, PUBLIC_V4),
    )
    assert target.canonical_url == "https://example.com/path?q=private"
    assert target.origin == "https://example.com"
    assert target.addresses == tuple(sorted((PUBLIC_V4, PUBLIC_V6)))
    assert target.decision_digest.startswith("sha256:")
    assert target.audit_fields() == {
        "scheme": "https",
        "port": 443,
        "address_count": 2,
        "decision_digest": target.decision_digest,
    }
    rendered = repr(target)
    assert "example.com" not in rendered
    assert "private" not in rendered
    assert PUBLIC_V4 not in rendered


def test_unicode_host_is_reduced_to_ascii_idna() -> None:
    target = resolve_public_url(
        "https://bücher.example/",
        XenonEgressPolicy(),
        resolver=_resolver(PUBLIC_V4),
    )
    assert target.host == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    "answer",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.0.2.1",
        "192.168.0.1",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "2001:db8::1",
    ],
)
def test_every_non_public_dns_class_is_rejected(answer: str) -> None:
    with pytest.raises(XenonEgressRejected, match="non_public_address"):
        resolve_public_url(
            "https://example.com/",
            XenonEgressPolicy(),
            resolver=_resolver(answer),
        )


def test_one_private_dns_sibling_poisons_the_entire_answer_set() -> None:
    with pytest.raises(XenonEgressRejected, match="non_public_address"):
        resolve_public_url(
            "https://example.com/",
            XenonEgressPolicy(),
            resolver=_resolver(PUBLIC_V4, "127.0.0.1"),
        )


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "a.localhost",
        "printer.local",
        "router.lan",
        "service.internal",
        "x.home.arpa",
        "metadata.google.internal",
        "instance-data.ec2.internal",
    ],
)
def test_local_discovery_names_are_rejected_before_dns(host: str) -> None:
    called = False

    def resolver(_host: str, _port: int) -> Iterable[str]:
        nonlocal called
        called = True
        return (PUBLIC_V4,)

    with pytest.raises(XenonEgressRejected, match="local_discovery_denied"):
        resolve_public_url(f"https://{host}/", XenonEgressPolicy(), resolver=resolver)
    assert called is False


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0177.0.0.1",
        "0x7f000001",
        "0x7f.0.0.1",
    ],
)
def test_browser_legacy_numeric_hosts_never_reach_dns(host: str) -> None:
    with pytest.raises(XenonEgressRejected, match="ambiguous_numeric_host"):
        resolve_public_url(
            f"https://{host}/",
            XenonEgressPolicy(),
            resolver=_resolver(PUBLIC_V4),
        )


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ws://example.com/", "websocket_denied"),
        ("wss://example.com/", "websocket_denied"),
        ("file:///etc/passwd", "scheme_denied"),
        ("data:text/plain,hello", "scheme_denied"),
        ("javascript:alert(1)", "scheme_denied"),
        ("https://user:pass@example.com/", "userinfo_denied"),
        ("https://example.com:444/", "port_denied"),
        ("https:\\example.com", "url_ambiguous"),
        ("https://example.com/\nnext", "url_ambiguous"),
        ("https://%31%32%37.0.0.1/", "host_invalid"),
        ("https://-bad.example/", "host_invalid"),
        ("https:///missing", "host_invalid"),
    ],
)
def test_ambiguous_or_unsupported_urls_fail_closed(url: str, reason: str) -> None:
    with pytest.raises(XenonEgressRejected, match=reason):
        resolve_public_url(url, XenonEgressPolicy(), resolver=_resolver(PUBLIC_V4))


def test_url_and_dns_answer_bounds_are_enforced() -> None:
    with pytest.raises(XenonEgressRejected, match="url_size"):
        resolve_public_url(
            "https://example.com/" + "a" * MAX_URL_BYTES,
            XenonEgressPolicy(),
            resolver=_resolver(PUBLIC_V4),
        )
    with pytest.raises(XenonEgressRejected, match="dns_empty"):
        resolve_public_url("https://example.com/", XenonEgressPolicy(), resolver=_resolver())
    with pytest.raises(XenonEgressRejected, match="dns_answer_limit"):
        resolve_public_url(
            "https://example.com/",
            XenonEgressPolicy(maximum_dns_answers=1),
            resolver=_resolver(PUBLIC_V4, PUBLIC_V4_OTHER),
        )


def test_resolver_exceptions_are_content_free() -> None:
    def resolver(_host: str, _port: int) -> Iterable[str]:
        raise RuntimeError("secret resolver failure with URL")

    with pytest.raises(XenonEgressRejected) as captured:
        resolve_public_url("https://example.com/private", XenonEgressPolicy(), resolver=resolver)
    assert captured.value.reason_code == "dns_resolution_failed"
    assert "secret" not in str(captured.value)
    assert "private" not in str(captured.value)


def test_same_origin_redirect_is_re_resolved_and_counted() -> None:
    calls = 0

    def resolver(_host: str, _port: int) -> Iterable[str]:
        nonlocal calls
        calls += 1
        return (PUBLIC_V4,)

    session = XenonEgressSession(XenonEgressPolicy(), resolver)
    first = session.begin("https://example.com/start")
    second = session.redirect(first, "https://example.com/next")
    assert second.canonical_url.endswith("/next")
    assert calls == 2


def test_cross_origin_redirect_requires_both_mode_and_exact_allowlist() -> None:
    denied = XenonEgressSession(XenonEgressPolicy(), _resolver(PUBLIC_V4))
    first = denied.begin("https://example.com/")
    with pytest.raises(XenonEgressRejected, match="cross_origin_redirect_denied"):
        denied.redirect(first, "https://cdn.example.net/")

    missing = XenonEgressSession(
        XenonEgressPolicy(allow_cross_origin_redirects=True),
        _resolver(PUBLIC_V4),
    )
    first = missing.begin("https://example.com/")
    with pytest.raises(XenonEgressRejected, match="redirect_origin_not_allowed"):
        missing.redirect(first, "https://cdn.example.net/")

    allowed = XenonEgressSession(
        XenonEgressPolicy(
            allow_cross_origin_redirects=True,
            redirect_origin_allowlist=("https://cdn.example.net",),
        ),
        _resolver(PUBLIC_V4),
    )
    first = allowed.begin("https://example.com/")
    assert allowed.redirect(first, "https://cdn.example.net/asset").origin == "https://cdn.example.net"


def test_redirect_downgrade_and_limit_are_rejected() -> None:
    policy = XenonEgressPolicy(
        allowed_schemes=("https", "http"),
        allowed_ports=(443, 80),
        maximum_redirects=1,
    )
    session = XenonEgressSession(policy, _resolver(PUBLIC_V4))
    first = session.begin("https://example.com/")
    with pytest.raises(XenonEgressRejected, match="redirect_downgrade"):
        session.redirect(first, "http://example.com/")

    session = XenonEgressSession(policy, _resolver(PUBLIC_V4))
    first = session.begin("https://example.com/")
    second = session.redirect(first, "https://example.com/a")
    with pytest.raises(XenonEgressRejected, match="redirect_limit"):
        session.redirect(second, "https://example.com/b")


def test_dns_rebinding_and_unpinned_peer_are_rejected() -> None:
    answers = [(PUBLIC_V4,), (PUBLIC_V4_OTHER,)]

    def resolver(_host: str, _port: int) -> Iterable[str]:
        return answers.pop(0)

    session = XenonEgressSession(XenonEgressPolicy(), resolver)
    target = session.begin("https://example.com/")
    with pytest.raises(XenonEgressRejected, match="dns_rebinding"):
        session.verify_dns_pin(target)
    assert session.verify_connected_peer(target, PUBLIC_V4) == PUBLIC_V4
    with pytest.raises(XenonEgressRejected, match="peer_not_pinned"):
        session.verify_connected_peer(target, PUBLIC_V4_OTHER)
    with pytest.raises(XenonEgressRejected, match="peer_not_pinned"):
        session.verify_connected_peer(target, "127.0.0.1")


def test_policy_rejects_malformed_and_duplicate_controls() -> None:
    with pytest.raises(XenonEgressRejected, match="policy_schemes"):
        XenonEgressPolicy(allowed_schemes=("https", "https"))
    with pytest.raises(XenonEgressRejected, match="policy_ports"):
        XenonEgressPolicy(allowed_ports=(443, True))
    with pytest.raises(XenonEgressRejected, match="policy_redirect_allowlist"):
        XenonEgressPolicy(redirect_origin_allowlist=("https://example.com/path",))
    with pytest.raises(XenonEgressRejected, match="local_discovery_denied"):
        XenonEgressPolicy(redirect_origin_allowlist=("https://localhost",))
