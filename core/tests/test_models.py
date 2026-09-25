"""Database connection, table creation and constraint enforcement."""

from django.db import connection
from django.db.utils import IntegrityError
from django.test import TransactionTestCase

from core.models import Role, Technician, TechnicianService
from core.tests.base import RoleTestCase


class DatabaseConnectionTests(TransactionTestCase):
    """'test the database connection' — real connection, real tables."""

    def test_connection_is_usable(self):
        """The configured SQLite connection answers a simple query."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_all_tables_exist(self):
        """All five tables were created by the migration."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
        for expected in ("Roles", "Staffs", "Technicians", "Services",
                         "Technician_Service"):
            self.assertIn(expected, tables, f"Missing table: {expected}")

    def test_crud_roundtrip(self):
        """'basic CRUD operations' against the real database."""
        role = Role.objects.create(RoleName="Administrator")          # Create
        role.RoleName = "Admin"                                       # Update
        role.save()
        self.assertEqual(Role.objects.get(pk=role.RoleID).RoleName, "Admin")  # Read
        role.delete()                                                 # Delete
        self.assertFalse(Role.objects.filter(pk=role.RoleID).exists())


class RoleTableTests(RoleTestCase):
    """'test role creation'."""

    def test_three_roles_seeded(self):
        self.assertEqual(Role.objects.count(), 3)

    def test_role_names(self):
        names = set(Role.objects.values_list("RoleName", flat=True))
        self.assertEqual(
            names, {"Administrator", "Receptionist", "Technician"}
        )

    def test_role_name_unique(self):
        with self.assertRaises(IntegrityError):
            Role.objects.create(RoleName=Role.ADMINISTRATOR)

    def test_role_ids_ordered(self):
        self.assertEqual(self.admin_role.RoleID, 1)
        self.assertEqual(self.receptionist_role.RoleID, 2)
        self.assertEqual(self.technician_role.RoleID, 3)

    def test_role_str(self):
        self.assertEqual(str(self.admin_role), "Administrator")


class ModelConstraintTests(RoleTestCase):
    """Schema-level constraints and defaults."""

    def test_staff_email_unique(self):
        self.make_staff(email="dup@test.local", role=self.receptionist_role)
        with self.assertRaises(IntegrityError):
            self.make_staff(email="dup@test.local", role=self.admin_role)

    def test_staff_defaults(self):
        staff = self.make_staff(email="new@test.local", role=self.admin_role)
        self.assertTrue(staff.IsActive)
        self.assertEqual(staff.full_name, "Test User")
        self.assertEqual(staff.role_name, Role.ADMINISTRATOR)

    def test_technician_staff_id_unique(self):
        """One technician record per staff account at DB level."""
        _staff, technician = self.make_technician()
        with self.assertRaises(IntegrityError):
            Technician.objects.create(
                StaffID_id=technician.StaffID_id, Hourly_rate="20.00"
            )

    def test_hourly_rate_must_be_positive(self):
        """The CHECK constraint rejects non-positive rates."""
        staff = self.make_staff(
            email="neg@test.local", role=self.technician_role
        )
        with self.assertRaises(IntegrityError):
            Technician.objects.create(StaffID=staff, Hourly_rate=0)

    def test_technician_service_composite_pk_blocks_duplicates(self):
        """Duplicate assignment is impossible at database level."""
        _staff, technician = self.make_technician()
        service = self.make_service()
        TechnicianService.objects.create(
            TechnicianID=technician, ServiceCode=service
        )
        with self.assertRaises(IntegrityError):
            TechnicianService.objects.create(
                TechnicianID=technician, ServiceCode=service
            )

    def test_service_name_unique(self):
        self.make_service(name="Brakes")
        with self.assertRaises(IntegrityError):
            self.make_service(name="Brakes", cost="10.00", duration=15)

    def test_cascade_delete_assignment_rows(self):
        """Deleting a technician removes its assignment rows (cascade delete)."""
        _staff, technician = self.make_technician()
        service = self.make_service()
        TechnicianService.objects.create(
            TechnicianID=technician, ServiceCode=service
        )
        technician.delete()
        self.assertEqual(TechnicianService.objects.count(), 0)

    def test_role_in_use_is_protected(self):
        """A role referenced by staff accounts cannot be deleted."""
        from django.db.models import ProtectedError

        self.make_staff(email="prot@test.local", role=self.admin_role)
        with self.assertRaises(ProtectedError):
            self.admin_role.delete()
