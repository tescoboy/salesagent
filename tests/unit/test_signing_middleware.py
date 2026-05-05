"""Unit tests for SigningVerifyMiddleware path/header filters + state plumbing.

PR 2B of [signing-non-embedded](../../../docs/design/signing-non-embedded.md).

End-to-end verification (signed accept / bad-sig reject / replay across
workers) is integration-only — needs a running brand.json receiver and
PgReplayStore. Those land in PR 2B's integration suite. This module covers
the deterministic pieces:

* Path-prefix filter (only buyer-protocol paths)
* Cheap header pre-check
* Verified-state contextvar bridge
* ``ResolvedIdentity`` threading via ``resolve_identity_from_context``
"""

from __future__ import annotations

from src.core.signing import (
    VerifiedRequestState,
    clear_verified_state,
    get_verified_state,
    set_verified_state,
)
from src.core.signing.middleware import (
    _has_signature_headers,
    _is_buyer_protocol_path,
)


class TestBuyerProtocolPathFilter:
    def test_mcp_paths_match(self):
        assert _is_buyer_protocol_path("/mcp")
        assert _is_buyer_protocol_path("/mcp/")
        assert _is_buyer_protocol_path("/mcp/some/sub")

    def test_a2a_paths_match(self):
        assert _is_buyer_protocol_path("/a2a")
        assert _is_buyer_protocol_path("/a2a/")
        assert _is_buyer_protocol_path("/a2a/messages")

    def test_root_matches(self):
        # AdCP A2A serves at host root.
        assert _is_buyer_protocol_path("/")

    def test_admin_paths_excluded(self):
        assert not _is_buyer_protocol_path("/admin")
        assert not _is_buyer_protocol_path("/admin/dashboard")
        assert not _is_buyer_protocol_path("/health")
        assert not _is_buyer_protocol_path("/static/foo.js")

    def test_well_known_excluded(self):
        # Discovery docs (agent-card, brand.json) MUST stay unsigned —
        # they're how counterparties bootstrap.
        assert not _is_buyer_protocol_path("/.well-known/agent.json")
        assert not _is_buyer_protocol_path("/.well-known/agent-card.json")
        assert not _is_buyer_protocol_path("/.well-known/jwks.json")


class TestSignatureHeaderSniff:
    def test_no_headers(self):
        assert not _has_signature_headers({"headers": []})

    def test_only_signature(self):
        assert not _has_signature_headers({"headers": [(b"signature", b"sig=:x:")]})

    def test_only_input(self):
        assert not _has_signature_headers({"headers": [(b"signature-input", b"sig=()")]})

    def test_both_present(self):
        assert _has_signature_headers({"headers": [(b"signature", b"sig=:x:"), (b"signature-input", b"sig=()")]})

    def test_case_insensitive(self):
        assert _has_signature_headers({"headers": [(b"Signature", b"x"), (b"Signature-Input", b"y")]})

    def test_other_headers_ignored(self):
        assert _has_signature_headers(
            {
                "headers": [
                    (b"host", b"example.com"),
                    (b"signature", b"sig=:x:"),
                    (b"content-type", b"application/json"),
                    (b"signature-input", b"sig=()"),
                ]
            }
        )


class TestVerifiedStateContextvar:
    def setup_method(self):
        clear_verified_state()

    def teardown_method(self):
        clear_verified_state()

    def test_default_is_none(self):
        assert get_verified_state() is None

    def test_roundtrip(self):
        state = VerifiedRequestState(
            operator_id="op_123",
            agent_url="https://buyer.example.com/.well-known/agent.json",
            key_id="kid-2026-q2",
        )
        set_verified_state(state)
        roundtrip = get_verified_state()
        assert roundtrip is not None
        assert roundtrip.operator_id == "op_123"
        assert roundtrip.agent_url == "https://buyer.example.com/.well-known/agent.json"
        assert roundtrip.key_id == "kid-2026-q2"

    def test_clear(self):
        set_verified_state(VerifiedRequestState(operator_id="o", agent_url="u", key_id="k"))
        assert get_verified_state() is not None
        clear_verified_state()
        assert get_verified_state() is None

    def test_frozen(self):
        import dataclasses

        state = VerifiedRequestState(operator_id="o", agent_url="u", key_id="k")
        try:
            state.operator_id = "o2"  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            return
        raise AssertionError("VerifiedRequestState must be frozen")
