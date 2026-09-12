"""VLAN is one class for both the read side (what parsers build from a real
device) and the write side. See docs/DEVICE_IMPLEMENTATION_PLAN.md D7.

vlan_id (range + not-reserved) validates unconditionally at construction —
safe for both paths, since parsers already exclude reserved VLANs before
building one. name format does NOT validate at construction (real devices
can report names/descriptions with characters the strict input-validation
regex would reject, e.g. spaces) — it validates only via the explicit
validar() (renamed from validate_name() in the FINAL_ARCHITECTURE.md
migration — same method, called by Orquestador.ejecutar() unconditionally,
short-circuited for eliminar=True before it ever looks at name).

The Device.create_vlan()/update_vlan_description()/delete_vlan() delegate
tests that used to live here were deleted along with those methods --
Device now exposes only a single `.driver` property; VLAN.aplicar() calls
device.driver.xxx(...) directly, there's no delegate layer left to test."""
import pytest

from app.models.vlan import VLAN


def test_vlan_id_out_of_range_raises_on_construction():
    with pytest.raises(ValueError):
        VLAN(vlan_id=0)
    with pytest.raises(ValueError):
        VLAN(vlan_id=4095)


def test_vlan_reserved_id_raises_on_construction():
    for reserved in (1, 1002, 1003, 1004, 1005):
        with pytest.raises(ValueError):
            VLAN(vlan_id=reserved)


def test_vlan_construction_never_validates_name():
    """A name a real device might report (e.g. with a space) must not
    prevent building the object — that would break reading real VLANs."""
    vlan = VLAN(vlan_id=10, name="Guest Network")
    assert vlan.name == "Guest Network"


def test_vlan_without_name_is_valid_for_delete():
    vlan = VLAN(vlan_id=10)
    assert vlan.name == ""


def test_validate_name_rejects_bad_format():
    vlan = VLAN(vlan_id=10, name="Guest Network")
    with pytest.raises(ValueError):
        vlan.validar()


def test_validate_name_accepts_good_format():
    vlan = VLAN(vlan_id=10, name="Guest-Network")
    vlan.validar()  # must not raise


def test_validar_skips_name_check_when_eliminar():
    """eliminar=True short-circuits before looking at name -- a delete
    VLAN(vlan_id=..., eliminar=True) always has name="" and must not fail
    validar() for it (Orquestador.ejecutar() calls validar() unconditionally,
    regardless of operation)."""
    vlan = VLAN(vlan_id=10, eliminar=True)
    vlan.validar()  # must not raise despite name == ""
