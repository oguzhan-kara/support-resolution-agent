"""The repository must be importable before anything else can be tested."""

from __future__ import annotations

import pytest


def test_sra_runtime_is_importable():
    import sra_runtime

    assert sra_runtime.MAX_STEPS == 8


def test_platform_sdk_is_marked_as_a_test_double():
    import platform_sdk

    assert "TEST DOUBLE" in platform_sdk.__doc__


def test_unstubbed_calls_fail_loudly_rather_than_returning_none():
    import platform_sdk

    with pytest.raises(NotImplementedError):
        platform_sdk.provisioning.grant_module_access("acct", "a@b.com", "payroll")


def test_the_double_exposes_the_platform_surface_the_capability_needs():
    import platform_sdk

    assert callable(platform_sdk.crm.fetch_admin_contacts)
    assert callable(platform_sdk.provisioning.revoke_module_access)
