"""Shared test fixtures for the ARMS suite.

``RoleTestCase`` seeds the three system roles that every other test
depends on, plus small helpers to create staff/technician/service records the
same way :mod:`core.services` does. Tests never rely on the real database
contents — Django wraps each test in a transaction that is rolled back.
"""

from django.contrib.auth.hashers import make_password
from django.test import TestCase

from core import services
from core.models import Role, Service, Staff


class RoleTestCase(TestCase):
    """Creates Administrator / Receptionist / Technician roles in setUp."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_role = Role.objects.create(RoleName=Role.ADMINISTRATOR)
        cls.receptionist_role = Role.objects.create(RoleName=Role.RECEPTIONIST)
        cls.technician_role = Role.objects.create(RoleName=Role.TECHNICIAN)

    # ---- helpers ---------------------------------------------------------
    def make_staff(
        self,
        *,
        email,
        role,
        first_name="Test",
        last_name="User",
        password="Passw0rd!",
        phone="",
        is_active=True,
    ) -> Staff:
        """Create a Staff row directly with a hashed password."""
        return Staff.objects.create(
            RoleID=role,
            FirstName=first_name,
            LastName=last_name,
            Phone=phone,
            Email=email,
            Password=make_password(password),
            IsActive=is_active,
        )

    def make_admin(self, email="admin@test.local", **kwargs) -> Staff:
        return self.make_staff(
            email=email, role=self.admin_role, first_name="Alice",
            last_name="Admin", **kwargs,
        )

    def make_technician(self, email="tech@test.local", rate="35.00", **kwargs):
        """Create a Technician-role staff account plus its technician record."""
        staff = self.make_staff(
            email=email, role=self.technician_role, first_name="Tom",
            last_name="Tech", **kwargs,
        )
        technician = services.create_technician(staff.StaffID, rate)
        return staff, technician

    def make_service(self, name="Oil Change", cost="49.99", duration=30) -> Service:
        return Service.objects.create(
            ServiceName=name, BaseCost=cost, Est_Duration=duration
        )

    def login_as(self, staff: Staff, password="Passw0rd!"):
        """Log in through the real UI so the session contract is exercised."""
        return self.client.post(
            "/login/", {"email": staff.Email, "password": password}
        )
