"""
TEST DOUBLE for the internal `platform_sdk` package.

The real module lives inside the platform and is not part of this repository.
It is stubbed here for exactly one reason: `sra_runtime.py` imports it at module
scope, so without a stub nothing in this repository can be imported, and
therefore nothing can be tested.

Every function raises `NotImplementedError`. Tests inject their own doubles
through `sra_runtime.Deps` rather than monkey-patching this module, so a call
that actually reaches here indicates a test that forgot to inject something —
not a missing feature. Failing loudly is deliberate: a stub that returned `None`
would let a test pass while the code under test silently did nothing.

`crm.fetch_admin_contacts` and `provisioning.revoke_module_access` are listed
here as required platform surface. See docs/DESIGN.md §11.
"""

from __future__ import annotations

from types import SimpleNamespace


def _unavailable(qualified_name: str):
    def _call(*args: object, **kwargs: object):
        raise NotImplementedError(
            f"platform_sdk.{qualified_name} is a test double. Inject a client "
            f"through sra_runtime.Deps instead of calling this directly."
        )

    _call.__name__ = qualified_name.replace(".", "_")
    return _call


llm = SimpleNamespace(
    complete=_unavailable("llm.complete"),
)

kb = SimpleNamespace(
    fetch_product_brain=_unavailable("kb.fetch_product_brain"),
    search=_unavailable("kb.search"),
    known_issues=_unavailable("kb.known_issues"),
)

crm = SimpleNamespace(
    fetch_customer_record=_unavailable("crm.fetch_customer_record"),
    get_field=_unavailable("crm.get_field"),
    # Required for the access-request capability. The brief cites a median age
    # across 900 accounts, so a last-updated timestamp exists somewhere;
    # surfacing it through the SDK may be work for another team.
    fetch_admin_contacts=_unavailable("crm.fetch_admin_contacts"),
    fetch_directory=_unavailable("crm.fetch_directory"),
)

ticketing = SimpleNamespace(
    recent_tickets=_unavailable("ticketing.recent_tickets"),
    history=_unavailable("ticketing.history"),
    post_reply=_unavailable("ticketing.post_reply"),
    add_note=_unavailable("ticketing.add_note"),
    assign_to_queue=_unavailable("ticketing.assign_to_queue"),
)

provisioning = SimpleNamespace(
    grant_module_access=_unavailable("provisioning.grant_module_access"),
    # Stated in the brief as a separate call. There is no built-in undo, which
    # is why grant_executor.py builds one.
    revoke_module_access=_unavailable("provisioning.revoke_module_access"),
)
