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
    assert len(members) == 17
    assert {member.staff_id for member in members} == {spec.staff_id for spec in DEFAULT_STAFF}
    assert all(member.status.value == "active" for member in members)

    ordinary = [member for member in members if member.staff_id != "staff-sherman-trainer"]
    assert all(
        {(permission.action, permission.resource, permission.max_risk.value) for permission in member.role.permissions}
        == {("read", "*", "low"), ("memory.read", "staff-memory", "low")}
        for member in ordinary
    )

    sherman = next(member for member in members if member.staff_id == "staff-sherman-trainer")
    assert sherman.display_name == "الشيرمان"
    assert {(permission.action, permission.resource, permission.max_risk.value) for permission in sherman.role.permissions} == {
        ("read", "staff-directory", "low"),
        ("skill.import", "skill-registry", "medium"),
        ("skill.assign", "skill-training", "medium"),
    }

    organization = organizations.get("mynemotron-office")
    assert organization.name == "MyNemotron AI Office"
    assert len(organization.placements) == 17
    assert organization.chief_of_staff_id == "staff-project-manager"
    assert {placement.staff_id for placement in organization.placements} == {
        spec.staff_id for spec in DEFAULT_STAFF
    }
    placement = organization.placement_for("staff-sherman-trainer")
    department = organization.department_for("staff-sherman-trainer")
    assert placement.job_title == "مدرب المهارات"
    assert department.department_id == "training"

    ensure_default_staff_roster(staff, organizations)
    assert len(staff.list_all()) == 17
    assert len(organizations.get("mynemotron-office").placements) == 17
