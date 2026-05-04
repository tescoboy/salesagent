"""Tests for `compute_valid_actions` — the AdCP ValidAction matrix helper.

Single source of truth for the per-state action list. Used by every response
that surfaces `valid_actions` (update_media_buy success and get_media_buys
per-buy entries).
"""

from __future__ import annotations

import pytest
from adcp.types import MediaBuyStatus
from adcp.types.generated_poc.media_buy.update_media_buy_response import ValidAction

from src.core.helpers.valid_actions import compute_valid_actions


@pytest.mark.parametrize(
    "terminal_status",
    [MediaBuyStatus.canceled, MediaBuyStatus.completed, MediaBuyStatus.rejected],
)
def test_terminal_states_yield_empty_actions(terminal_status):
    """Terminal states allow no further transitions per spec."""
    assert compute_valid_actions(terminal_status, has_pending_creatives=False) == []
    assert compute_valid_actions(terminal_status, has_pending_creatives=True) == []


def test_pending_activation_offers_cancel_and_sync_creatives():
    """Local Python enum still has `pending_activation`; treat it as the
    legacy alias of pending_creatives/pending_start. Spec §339-345 limits
    pending states to cancel + sync_creatives only.
    """
    actions = compute_valid_actions(MediaBuyStatus.pending_activation, has_pending_creatives=False)
    assert {a.value for a in actions} == {"cancel", "sync_creatives"}


def test_active_offers_pause_cancel_and_full_mid_flight_surface():
    """Active buys may pause, cancel, update budget/dates/packages, add
    packages. sync_creatives only when there are pending creative reviews.
    """
    no_pending = compute_valid_actions(MediaBuyStatus.active, has_pending_creatives=False)
    assert no_pending[0] == ValidAction.pause, "pause leads for active state"
    assert {a.value for a in no_pending} == {
        "pause",
        "cancel",
        "update_budget",
        "update_dates",
        "update_packages",
        "add_packages",
    }
    with_pending = compute_valid_actions(MediaBuyStatus.active, has_pending_creatives=True)
    assert ValidAction.sync_creatives in with_pending


def test_paused_replaces_pause_with_resume():
    """Paused buys offer resume (the inverse) plus the same mid-flight
    surface as active.
    """
    actions = compute_valid_actions(MediaBuyStatus.paused, has_pending_creatives=False)
    assert actions[0] == ValidAction.resume
    assert ValidAction.pause not in actions
    assert {a.value for a in actions} == {
        "resume",
        "cancel",
        "update_budget",
        "update_dates",
        "update_packages",
        "add_packages",
    }


def test_returns_list_of_enum_members_not_strings():
    """Buyers may iterate the list looking for enum identity; emitting
    strings would silently break that pattern.
    """
    actions = compute_valid_actions(MediaBuyStatus.active, has_pending_creatives=False)
    assert all(isinstance(a, ValidAction) for a in actions)
