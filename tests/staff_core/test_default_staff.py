from __future__ import annotations

from nemotron.staff.adapters.sqlite_control import (
    SQLiteControlStore,
    SQLiteOrganizationRepository,
    SQLiteStaffRepository,
)
from nemotron.staff.control_plane.default_staff import DEFAULT_STAFF, ensure_default_staff_roster


def test_default_staff_roster_is_complete_and_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = SQLiteControlStore(tmp_path / "staff.sqlite3")
    staff = SQLiteStaffRepository(store)
    organizations = SQLiteOrganizationRepository(store)

    ensure_default_staff_roster(staff, organizations)

    members = staff.list_all()
    assert len(members) == 16
    assert {member.staff_id for member in members} == {spec.staff_id for spec in DEFAULT_STAFF}
    assert all(member.status.value == "active" for member in members)
    assert all(len(member.role.permissions) == 1 for member in members)
    assert all(member.role.permissions[0].action == "read" for member in members)
    assert all(member.role.permissions[0].resource == "*" for member in members)
    assert all(member.role.permissions[0].max_risk.value == "low" for member in members)

    organization = organizations.get("mynemotron-office")
    assert organization.name == "MyNemotron AI Office"
    assert len(organization.placements) == 16
    assert organization.chief_of_staff_id == "staff-project-manager"
    assert {placement.staff_id for placement in organization.placements} == {
        spec.staff_id for spec in DEFAULT_STAFF
    }

    ensure_default_staff_roster(staff, organizations)
    assert len(staff.list_all()) == 16
    assert len(organizations.get("mynemotron-office").placements) == 16
