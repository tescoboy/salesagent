"""Lock the wire-shape invariants on ``build_update_media_buy_request``.

adcp 4.4 made ``account`` and ``idempotency_key`` required on
``UpdateMediaBuyRequest`` (see
``adcp/types/generated_poc/bundled/media_buy/update_media_buy_request.py:7544``
and ``:7685``). The e2e builder must inject both so tests behave like
real buyers and don't get rejected at the SDK validation boundary.

These tests live at unit-tier so they run in every CI cycle —
breakages get caught before the slow e2e suite even starts.
"""

from __future__ import annotations

from tests.e2e.adcp_request_builder import build_update_media_buy_request


def test_request_has_required_account_field():
    req = build_update_media_buy_request(media_buy_id="mb_test")
    account = req.get("account")
    assert account is not None, "AdCP 4.4 requires `account` on UpdateMediaBuyRequest"
    # Natural-key shape: brand + operator. account_id form is also valid
    # but not what the builder synthesises by default.
    assert "brand" in account
    assert "operator" in account


def test_request_has_required_idempotency_key():
    req = build_update_media_buy_request(media_buy_id="mb_test")
    key = req.get("idempotency_key")
    assert isinstance(key, str), "AdCP 4.4 requires `idempotency_key` on UpdateMediaBuyRequest"
    assert len(key) >= 16, "idempotency_key spec: minLength 16"
    # Pattern allowed chars per spec: ^[A-Za-z0-9_.:-]{16,255}$
    import re

    assert re.match(r"^[A-Za-z0-9_.:-]{16,255}$", key), (
        f"idempotency_key '{key}' must match the spec's character set"
    )


def test_each_call_generates_a_fresh_idempotency_key():
    """Idempotency keys must be unique per-request — buyers retrying the
    SAME logical request reuse one key (handled at the test layer above).
    The builder must not memoise."""
    a = build_update_media_buy_request(media_buy_id="mb_test")
    b = build_update_media_buy_request(media_buy_id="mb_test")
    assert a["idempotency_key"] != b["idempotency_key"]


def test_brand_override_propagates_to_account():
    """When the test passes a custom brand, the synthesised ``account``
    natural key uses it — keeps the account/brand pair consistent across
    request and downstream lookups."""
    req = build_update_media_buy_request(
        media_buy_id="mb_test",
        brand={"domain": "acme.com", "brand_id": "spark"},
    )
    assert req["account"]["brand"] == {"domain": "acme.com", "brand_id": "spark"}
    assert req["account"]["operator"] == "acme.com"


def test_optional_fields_only_present_when_passed():
    """Bare-minimum request: only the required fields. No phantom None
    values for active / budget / packages / push_notification_config."""
    req = build_update_media_buy_request(media_buy_id="mb_test")
    assert "active" not in req
    assert "budget" not in req
    assert "packages" not in req
    assert "push_notification_config" not in req
    assert "context" not in req


def test_passes_webhook_with_correct_authentication_shape():
    req = build_update_media_buy_request(
        media_buy_id="mb_test",
        webhook_url="https://example.com/cb",
    )
    pnc = req["push_notification_config"]
    assert pnc["url"] == "https://example.com/cb"
    # AdCP 4.4 ReportingWebhook authentication: type=none is the simplest valid form.
    assert pnc["authentication"] == {"type": "none"}
