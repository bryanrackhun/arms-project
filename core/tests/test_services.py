"""Business rules for staff CRUD, technicians, assignments and validation."""

from django.contrib.auth.hashers import check_password

from core import services
from core.models import Staff, Technician, TechnicianService
from core.services import (
    DuplicateError,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from core.tests.base import RoleTestCase


class _FakeSession(dict):
    """Minimal session stand-in for service-layer login tests."""

    def flush(self):
        self.clear()

    def set_expiry(self, seconds):
        self["_expiry"] = seconds


class StaffServiceTests(RoleTestCase):
    """Staff creation, update and deactivation."""

    def test_create_staff_persists(self):
        staff = services.create_staff(
            first_name="Jane", last_name="Doe",
            email="jane@test.local", password="Passw0rd!",
            role_id=self.receptionist_role.RoleID, phone="+1 555-0100",
        )
        row = Staff.objects.get(pk=staff.StaffID)  # stored in the database
        self.assertEqual(row.FirstName, "Jane")
        self.assertEqual(row.Email, "jane@test.local")
        self.assertEqual(row.Phone, "+1 555-0100")
        self.assertTrue(row.IsActive)
        self.assertEqual(row.RoleID.RoleName, "Receptionist")

    def test_create_staff_hashes_password(self):
        staff = services.create_staff(
            first_name="Jane", last_name="Doe",
            email="jane@test.local", password="Passw0rd!",
            role_id=self.receptionist_role.RoleID,
        )
        self.assertNotEqual(staff.Password, "Passw0rd!")  # never plain text
        self.assertTrue(check_password("Passw0rd!", staff.Password))

    def test_create_staff_duplicate_email_rejected(self):
        services.create_staff(
            first_name="A", last_name="B", email="dup@test.local",
            password="Passw0rd!", role_id=self.admin_role.RoleID,
        )
        with self.assertRaises(ServiceError):
            services.create_staff(
                first_name="C", last_name="D", email="dup@test.local",
                password="Passw0rd!", role_id=self.admin_role.RoleID,
            )

    def test_update_staff_changes_role_and_details(self):
        staff = services.create_staff(
            first_name="Old", last_name="Name", email="upd@test.local",
            password="Passw0rd!", role_id=self.receptionist_role.RoleID,
        )
        updated = services.update_staff(
            staff_id=staff.StaffID, first_name="New", last_name="Name",
            email="upd@test.local", role_id=self.admin_role.RoleID,
            new_password="NewPassw0rd!",
        )
        updated.refresh_from_db()
        self.assertEqual(updated.FirstName, "New")
        self.assertEqual(updated.RoleID.RoleName, "Administrator")
        self.assertTrue(check_password("NewPassw0rd!", updated.Password))

    def test_update_staff_blank_password_keeps_current(self):
        staff = services.create_staff(
            first_name="A", last_name="B", email="keep@test.local",
            password="Passw0rd!", role_id=self.admin_role.RoleID,
        )
        old_hash = staff.Password
        services.update_staff(
            staff_id=staff.StaffID, first_name="A", last_name="B",
            email="keep@test.local", role_id=self.admin_role.RoleID,
            new_password=None,
        )
        staff.refresh_from_db()
        self.assertEqual(staff.Password, old_hash)
        self.assertTrue(check_password("Passw0rd!", staff.Password))

    def test_deactivate_staff_blocks_login_but_keeps_row(self):
        """Deactivation, never deletion; handle inactive accounts."""
        staff = services.create_staff(
            first_name="A", last_name="B", email="off@test.local",
            password="Passw0rd!", role_id=self.admin_role.RoleID,
        )
        services.deactivate_staff(staff.StaffID)
        row = Staff.objects.get(pk=staff.StaffID)  # row still exists
        self.assertFalse(row.IsActive)
        with self.assertRaises(ServiceError):
            services.login(_FakeSession(), "off@test.local", "Passw0rd!")

    def test_search_staff_by_name_and_email(self):
        services.create_staff(
            first_name="Unique", last_name="Person", email="find@test.local",
            password="Passw0rd!", role_id=self.receptionist_role.RoleID,
        )
        self.assertEqual(len(services.search_staff("unique")), 1)
        self.assertEqual(len(services.search_staff("find@")), 1)
        self.assertEqual(len(services.search_staff("nope")), 0)

    def test_list_staff(self):
        self.make_admin()
        self.assertEqual(len(services.list_staff()), 1)


class TechnicianServiceTests(RoleTestCase):
    """Technician creation and update."""

    def test_create_technician_links_staff_and_rate(self):
        _staff, technician = self.make_technician(rate="42.50")
        row = Technician.objects.get(pk=technician.TechnicianID)
        self.assertEqual(row.StaffID.Email, "tech@test.local")
        self.assertEqual(float(row.Hourly_rate), 42.50)

    def test_create_technician_requires_technician_role(self):
        """A Receptionist account cannot hold a technician record."""
        staff = self.make_staff(
            email="rec@test.local", role=self.receptionist_role
        )
        with self.assertRaises(ValidationError):
            services.create_technician(staff.StaffID, "30.00")

    def test_create_technician_duplicate_blocked(self):
        _staff, technician = self.make_technician()
        with self.assertRaises(DuplicateError):
            services.create_technician(technician.StaffID_id, "30.00")

    def test_update_technician_rate(self):
        _staff, technician = self.make_technician(rate="30.00")
        services.update_technician(technician.TechnicianID, "55.00")
        technician.refresh_from_db()
        self.assertEqual(float(technician.Hourly_rate), 55.00)

    def test_update_technician_rejects_zero_rate(self):
        _staff, technician = self.make_technician()
        with self.assertRaises(ValidationError):
            services.update_technician(technician.TechnicianID, 0)

    def test_deactivate_technician_deactivates_linked_staff(self):
        staff, technician = self.make_technician()
        services.deactivate_technician(technician.TechnicianID)
        staff.refresh_from_db()
        self.assertFalse(staff.IsActive)
        self.assertTrue(
            Technician.objects.filter(pk=technician.TechnicianID).exists()
        )

    def test_get_technician_missing_raises(self):
        with self.assertRaises(NotFoundError):
            services.get_technician(99999)


class TechnicianServiceModuleTests(RoleTestCase):
    """Technician-service assignment."""

    def setUp(self):
        super().setUp()
        _staff, self.technician = self.make_technician()
        self.service = self.make_service()

    def test_assign_service_creates_row(self):
        services.assign_service(
            self.technician.TechnicianID, self.service.Service_code
        )
        self.assertTrue(
            TechnicianService.objects.filter(
                TechnicianID=self.technician, ServiceCode=self.service
            ).exists()
        )

    def test_duplicate_assignment_blocked(self):
        """Prevent duplicate technician-service assignments."""
        services.assign_service(
            self.technician.TechnicianID, self.service.Service_code
        )
        with self.assertRaises(DuplicateError):
            services.assign_service(
                self.technician.TechnicianID, self.service.Service_code
            )

    def test_remove_assignment(self):
        services.assign_service(
            self.technician.TechnicianID, self.service.Service_code
        )
        services.remove_assignment(
            self.technician.TechnicianID, self.service.Service_code
        )
        self.assertEqual(TechnicianService.objects.count(), 0)

    def test_remove_missing_assignment_raises(self):
        with self.assertRaises(NotFoundError):
            services.remove_assignment(
                self.technician.TechnicianID, self.service.Service_code
            )

    def test_list_services_for_technician(self):
        services.assign_service(
            self.technician.TechnicianID, self.service.Service_code
        )
        listed = services.list_services_for_technician(
            self.technician.TechnicianID
        )
        self.assertEqual(
            [s.Service_code for s in listed], [self.service.Service_code]
        )

    def test_list_technicians_for_service(self):
        services.assign_service(
            self.technician.TechnicianID, self.service.Service_code
        )
        listed = services.list_technicians_for_service(
            self.service.Service_code
        )
        self.assertEqual(
            [t.TechnicianID for t in listed], [self.technician.TechnicianID]
        )

    def test_assign_unknown_service_raises(self):
        with self.assertRaises(NotFoundError):
            services.assign_service(self.technician.TechnicianID, 99999)


class ServiceModuleTests(RoleTestCase):
    """Services catalogue maintenance."""

    def test_list_services(self):
        self.make_service(name="A")
        self.make_service(name="B", cost="10.00", duration=15)
        names = [s.ServiceName for s in services.list_services()]
        self.assertEqual(names, ["A", "B"])

    def test_list_roles(self):
        names = [r.RoleName for r in services.list_roles()]
        self.assertEqual(
            names, ["Administrator", "Receptionist", "Technician"]
        )


class ValidationTests(RoleTestCase):
    """Validation rules enforced in the service layer."""

    def test_empty_first_name_rejected(self):
        with self.assertRaises(ValidationError):
            services.create_staff(
                first_name="  ", last_name="Doe", email="v@test.local",
                password="Passw0rd!", role_id=self.admin_role.RoleID,
            )

    def test_invalid_email_rejected(self):
        with self.assertRaises(ValidationError):
            services.create_staff(
                first_name="A", last_name="B", email="not-an-email",
                password="Passw0rd!", role_id=self.admin_role.RoleID,
            )

    def test_invalid_phone_rejected(self):
        with self.assertRaises(ValidationError):
            services.create_staff(
                first_name="A", last_name="B", email="p@test.local",
                password="Passw0rd!", role_id=self.admin_role.RoleID,
                phone="call me maybe",
            )

    def test_short_password_rejected(self):
        with self.assertRaises(ValidationError):
            services.create_staff(
                first_name="A", last_name="B", email="s@test.local",
                password="short", role_id=self.admin_role.RoleID,
            )

    def test_unknown_role_rejected(self):
        with self.assertRaises(NotFoundError):
            services.create_staff(
                first_name="A", last_name="B", email="r@test.local",
                password="Passw0rd!", role_id=9999,
            )

    def test_missing_staff_raises(self):
        with self.assertRaises(NotFoundError):
            services.get_staff(99999)
