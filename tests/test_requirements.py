#!/usr/bin/env python3

# Electronic Cats
# test_requirements.py — keeps the capability -> provider map
# (modules/core/requirements.py) honest against the firmware registry.
# docs/AUTOFLASH_PLAN.md F1.

import pytest

from modules.core import requirements as req
from modules.core.firmwares import CAP_MONITOR, REGISTRY


def test_every_provider_id_exists_in_the_registry():
    for capability, provider_id in req.CAPABILITY_PROVIDER.items():
        assert (
            provider_id in REGISTRY
        ), f"{capability!r} maps to unknown firmware id {provider_id!r}"


def test_every_provider_declares_the_capability_it_provides():
    """A desynced map (provider that doesn't actually claim the capability)
    would make ensure_firmware() flash an image and still fail satisfied_by()."""
    for capability, provider_id in req.CAPABILITY_PROVIDER.items():
        fw = REGISTRY[provider_id]
        assert fw.can(
            capability
        ), f"provider {provider_id!r} does not declare capability {capability!r}"


def test_image_name_matches_the_registry_uf2_without_extension():
    for capability, provider_id in req.CAPABILITY_PROVIDER.items():
        requirement = req.requirement_for(capability, command="test")
        fw = REGISTRY[provider_id]
        assert requirement.image_name == fw.uf2.rsplit(".", 1)[0]


def test_requirement_for_unprovided_capability_raises():
    with pytest.raises(ValueError):
        req.requirement_for(CAP_MONITOR, command="identify")
