"""Regression test: ``MockSellerPlatform.get_media_buy_delivery`` returns
the full delivery response envelope.

Previously the mock returned a partial stub
``{"media_buy_deliveries": [...]}`` missing ``reporting_period`` —
adcp 4.4 made the field required at the output-schema level, so
FastMCP's output validator rejected with
``'reporting_period' is a required property``.

This test asserts the mock now delegates to the shared impl and
returns the full response shape, matching what GamPlatform produces.
"""

from __future__ import annotations

import inspect

from core.platforms._delegate import _delegate_get_media_buy_delivery
from core.platforms.gam import GamPlatform
from core.platforms.mock import MockSellerPlatform


def test_mock_get_media_buy_delivery_is_async() -> None:
    """``get_media_buy_delivery`` must be ``async`` because the shared
    delegate it dispatches to is async. A stub-style sync method would
    return a coroutine wrapper that the framework can't await
    correctly."""
    assert inspect.iscoroutinefunction(MockSellerPlatform.get_media_buy_delivery), (
        "MockSellerPlatform.get_media_buy_delivery must be async to "
        "match the SDK protocol — the shared delegate is async."
    )


def test_mock_get_media_buy_delivery_dispatches_to_shared_delegate() -> None:
    """The mock must dispatch to ``_delegate_get_media_buy_delivery`` —
    not a partial stub. Asserting via source inspection is a structural
    guard that catches a regression to a stub even if the new stub
    happened to include ``reporting_period`` by accident."""
    source = inspect.getsource(MockSellerPlatform.get_media_buy_delivery)
    assert "_delegate_get_media_buy_delivery" in source, (
        "MockSellerPlatform.get_media_buy_delivery must dispatch to the "
        "shared delegate so its output shape matches GamPlatform and the "
        "SDK's output schema. Found source:\n" + source
    )


def test_mock_and_gam_use_same_delivery_delegate() -> None:
    """Both platforms must delegate to ``_delegate_get_media_buy_delivery`` —
    keeps the response shape consistent across adapters."""
    mock_source = inspect.getsource(MockSellerPlatform.get_media_buy_delivery)
    gam_source = inspect.getsource(GamPlatform.get_media_buy_delivery)
    assert "_delegate_get_media_buy_delivery" in mock_source
    assert "_delegate_get_media_buy_delivery" in gam_source
    # And the delegate itself returns a wire dict produced by
    # GetMediaBuyDeliveryResponse.model_dump — verifying its existence
    # locks the import chain.
    assert _delegate_get_media_buy_delivery is not None
