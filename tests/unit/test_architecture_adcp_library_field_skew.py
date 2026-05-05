"""Architectural fitness function: detect when the rc.3 -> 3.0.6 wire shim is removable.

The buying_mode/refine wireup carries two compatibility shims (Layer 1.1 inbound and
Layer 7 outbound on GetProductsResponse) that exist solely because the installed adcp
Python library (version 3.12.0) targets spec rc.3, while the released spec (and the
@adcp/sdk@6.11.0 storyboard runner) is 3.0.6. The two specs differ on refine entry id
fields: rc.3 uses `id`, 3.0.6 uses `product_id` / `proposal_id`.

When the adcp library upgrades to a 3.0.6+ alignment, this test fails — telling the next
engineer where to delete the shims:

  - src/core/schemas/product.py
      _normalize_refine_entry_id_field (mode='before' validator)
      GetProductsResponse.model_dump (refinement_applied rename)
  - tests/unit/test_get_products_buying_mode.py
      TestRefineEntryFieldNameNormalizer
  - tests/unit/test_get_products_mode_branching.py
      TestOutboundWireCompat

This is the project's "architectural fitness function" pattern: a structural guard for a
temporary state. See .claude/notes/buying-mode-refine-wireup/PLAN.md §6.
"""

from __future__ import annotations

from adcp.types.generated_poc.media_buy.get_products_request import Refine1, Refine2


def test_rc3_refine_id_field_still_present() -> None:
    """Refine1 (product scope) and Refine2 (proposal scope) use `id` field name (rc.3 shape).

    When this test fails, the adcp library has upgraded to 3.0.6 wire format (where the
    field is named `product_id` / `proposal_id`). Remove the inbound and outbound shims
    described in this module's docstring.
    """
    refine1_fields = set(Refine1.model_fields.keys())
    refine2_fields = set(Refine2.model_fields.keys())

    expected_rc3_fields = {"action", "ask", "id", "scope"}

    assert refine1_fields == expected_rc3_fields, (
        f"Refine1 (product scope) fields changed from rc.3 shape {expected_rc3_fields!r} "
        f"to {refine1_fields!r}. The adcp library has likely upgraded to 3.0.6 wire format. "
        f"Remove the rc.3 -> 3.0.6 compatibility shims documented in this module's docstring."
    )
    assert refine2_fields == expected_rc3_fields, (
        f"Refine2 (proposal scope) fields changed from rc.3 shape {expected_rc3_fields!r} "
        f"to {refine2_fields!r}. The adcp library has likely upgraded to 3.0.6 wire format. "
        f"Remove the rc.3 -> 3.0.6 compatibility shims documented in this module's docstring."
    )
