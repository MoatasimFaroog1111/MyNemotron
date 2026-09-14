from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.adapters.sqlite_control import SQLiteOrganizationRepository, SQLiteStaffRepository
from nemotron.staff.domain import Department, Organization, Permission, RiskLevel, Role, StaffMember, StaffPlacement


@dataclass(frozen=True, slots=True)
class DefaultStaffSpec:
    staff_id: str
    display_name: str
    role_name: str
    department_id: str
    department_name: str
    job_title: str


DEFAULT_STAFF: tuple[DefaultStaffSpec, ...] = (
    DefaultStaffSpec("staff-operations-monitor", "وكيل مراقبة العمليات", "مراقبة العمليات", "operations", "العمليات", "مراقب العمليات"),
    DefaultStaffSpec("staff-data-analyst", "وكيل تحليل البيانات", "تحليل البيانات", "data", "البيانات والتحليلات", "محلل بيانات"),
    DefaultStaffSpec("staff-systems-developer", "وكيل تطوير الأنظمة", "تطوير الأنظمة", "engineering", "الهندسة", "مطور أنظمة"),
    DefaultStaffSpec("staff-project-manager", "وكيل إدارة المشاريع", "إدارة المشاريع", "pmo", "إدارة المشاريع", "مدير مشاريع"),
    DefaultStaffSpec("staff-ux-specialist", "وكيل تجربة المستخدم", "تجربة المستخدم", "product", "المنتج والتجربة", "أخصائي تجربة المستخدم"),
    DefaultStaffSpec("staff-integration-engineer", "وكيل تكامل الأنظمة", "تكامل الأنظمة", "engineering", "الهندسة", "مهندس تكامل"),
    DefaultStaffSpec("staff-financial-accountant", "وكيل المحاسبة المالية", "المحاسبة المالية", "finance", "المالية", "محاسب مالي"),
    DefaultStaffSpec("staff-cybersecurity", "وكيل الأمن السيبراني", "الأمن السيبراني", "security", "الأمن السيبراني", "أخصائي أمن سيبراني"),
    DefaultStaffSpec("staff-content-manager", "وكيل إدارة المحتوى", "إدارة المحتوى", "content", "المحتوى", "مدير محتوى"),
    DefaultStaffSpec("staff-advanced-analytics", "وكيل التحليلات المتقدمة", "التحليلات المتقدمة", "data", "البيانات والتحليلات", "محلل متقدم"),
    DefaultStaffSpec("staff-ai-specialist", "وكيل الذكاء الاصطناعي", "الذكاء الاصطناعي", "ai", "الذكاء الاصطناعي", "أخصائي ذكاء اصطناعي"),
    DefaultStaffSpec("staff-infrastructure-manager", "وكيل إدارة البنية التحتية", "إدارة البنية التحتية", "infrastructure", "البنية التحتية", "مدير بنية تحتية"),
    DefaultStaffSpec("staff-network-manager", "وكيل إدارة الشبكات", "إدارة الشبكات", "infrastructure", "البنية التحتية", "مدير شبكات"),
    DefaultStaffSpec("staff-bank-reconciliation", "وكيل التسويات البنكية", "التسويات البنكية", "finance", "المالية", "أخصائي تسويات بنكية"),
    DefaultStaffSpec("staff-financial-reporting", "وكيل التقارير المالية", "التقارير المالية", "finance", "المالية", "أخصائي تقارير مالية"),
    DefaultStaffSpec("staff-customer-support", "وكيل دعم العملاء", "دعم العملاء", "support", "دعم العملاء", "أخصائي دعم العملاء"),
)

DEFAULT_ORGANIZATION_ID = "mynemotron-office"


def ensure_default_staff_roster(
    staff: SQLiteStaffRepository,
    organizations: SQLiteOrganizationRepository,
) -> None:
    """Ensure the office always has a real, least-privilege StaffMember per visible seat.

    The bootstrap is additive and idempotent: existing StaffMember records are never
    overwritten. This keeps the UI functional on an empty ephemeral database while
    still allowing persistent/custom registry data to take precedence.
    """

    existing_staff = {member.staff_id for member in staff.list_all()}
    read_only = Permission(action="read", resource="*", max_risk=RiskLevel.LOW)

    for spec in DEFAULT_STAFF:
        if spec.staff_id in existing_staff:
            continue
        staff.save(
            StaffMember(
                staff_id=spec.staff_id,
                display_name=spec.display_name,
                role=Role(
                    role_id=f"role-{spec.staff_id.removeprefix('staff-')}",
                    name=spec.role_name,
                    permissions=(read_only,),
                ),
            )
        )

    organization_by_id = {item.organization_id: item for item in organizations.list_all()}
    existing = organization_by_id.get(DEFAULT_ORGANIZATION_ID)

    departments = list(existing.departments if existing else ())
    department_ids = {department.department_id for department in departments}
    for spec in DEFAULT_STAFF:
        if spec.department_id not in department_ids:
            departments.append(Department(spec.department_id, spec.department_name))
            department_ids.add(spec.department_id)

    placements = list(existing.placements if existing else ())
    placed_staff_ids = {placement.staff_id for placement in placements}
    for spec in DEFAULT_STAFF:
        if spec.staff_id not in placed_staff_ids:
            placements.append(
                StaffPlacement(
                    staff_id=spec.staff_id,
                    department_id=spec.department_id,
                    job_title=spec.job_title,
                )
            )
            placed_staff_ids.add(spec.staff_id)

    organizations.save(
        Organization(
            organization_id=DEFAULT_ORGANIZATION_ID,
            name=existing.name if existing else "MyNemotron AI Office",
            departments=tuple(departments),
            placements=tuple(placements),
            chief_of_staff_id=existing.chief_of_staff_id if existing else "staff-project-manager",
        )
    )
