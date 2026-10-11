"""Form-level validation: inline errors on every ARMS form."""

from django.test import TestCase

from core.forms import (
    LoginForm,
    ServiceAssignmentForm,
    ServiceForm,
    StaffForm,
    TechnicianForm,
)
from core.tests.base import RoleTestCase


class LoginFormTests(TestCase):
    """Validate empty login fields."""

    def test_valid_payload(self):
        form = LoginForm(data={"email": "a@b.local", "password": "Passw0rd!"})
        self.assertTrue(form.is_valid())

    def test_missing_email(self):
        form = LoginForm(data={"email": "", "password": "Passw0rd!"})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_missing_password(self):
        form = LoginForm(data={"email": "a@b.local", "password": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_malformed_email(self):
        form = LoginForm(data={"email": "not-an-email", "password": "Passw0rd!"})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)


class StaffFormTests(RoleTestCase):
    """Staff form validation."""

    def _payload(self, **overrides):
        data = {
            "first_name": "Rita",
            "last_name": "Reception",
            "email": "rita@test.local",
            "phone": "",
            "role": self.receptionist_role.RoleID,
            "password": "Passw0rd!",
            "confirm_password": "Passw0rd!",
        }
        data.update(overrides)
        return data

    def test_valid_creation_payload(self):
        form = StaffForm(data=self._payload())
        self.assertTrue(form.is_valid(), form.errors)

    def test_blank_names_rejected(self):
        form = StaffForm(data=self._payload(first_name="   "))
        self.assertFalse(form.is_valid())
        self.assertIn("first_name", form.errors)

    def test_short_password_rejected(self):
        form = StaffForm(
            data=self._payload(password="short", confirm_password="short")
        )
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_mismatched_confirmation_rejected(self):
        form = StaffForm(data=self._payload(confirm_password="Other12345!"))
        self.assertFalse(form.is_valid())
        self.assertIn("confirm_password", form.errors)

    def test_bad_phone_rejected(self):
        form = StaffForm(data=self._payload(phone="phone-me"))
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)

    def test_duplicate_email_rejected(self):
        self.make_staff(email="rita@test.local", role=self.receptionist_role)
        form = StaffForm(data=self._payload())
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_edit_mode_own_email_allowed(self):
        staff = self.make_staff(
            email="rita@test.local", role=self.receptionist_role
        )
        form = StaffForm(data=self._payload(), staff=staff)
        self.assertTrue(form.is_valid(), form.errors)

    def test_edit_mode_blank_password_keeps_current(self):
        staff = self.make_staff(
            email="rita@test.local", role=self.receptionist_role
        )
        form = StaffForm(
            data=self._payload(password="", confirm_password=""), staff=staff
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.new_password)

    def test_role_field_is_model_choice_not_free_text(self):
        """Role selection interface: only existing RoleIDs are valid."""
        form = StaffForm(data=self._payload(role=99999))
        self.assertFalse(form.is_valid())
        self.assertIn("role", form.errors)


class TechnicianFormTests(RoleTestCase):
    """Technician information form."""

    def test_valid_payload(self):
        staff = self.make_staff(
            email="t1@test.local", role=self.technician_role
        )
        form = TechnicianForm(
            data={"staff": staff.StaffID, "hourly_rate": "35.00"}
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_dropdown_excludes_non_technician_roles(self):
        """Only Technician-role accounts can be linked."""
        receptionist = self.make_staff(
            email="rec@test.local", role=self.receptionist_role
        )
        form = TechnicianForm(
            data={"staff": receptionist.StaffID, "hourly_rate": "35.00"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("staff", form.errors)

    def test_dropdown_excludes_inactive_accounts(self):
        inactive = self.make_staff(
            email="off@test.local", role=self.technician_role, is_active=False
        )
        form = TechnicianForm(
            data={"staff": inactive.StaffID, "hourly_rate": "35.00"}
        )
        self.assertFalse(form.is_valid())

    def test_dropdown_excludes_accounts_with_existing_record(self):
        staff, _technician = self.make_technician(email="done@test.local")
        form = TechnicianForm()
        offered = set(
            form.fields["staff"].queryset.values_list("pk", flat=True)
        )
        self.assertNotIn(staff.StaffID, offered)

    def test_zero_rate_rejected(self):
        staff = self.make_staff(
            email="t2@test.local", role=self.technician_role
        )
        form = TechnicianForm(data={"staff": staff.StaffID, "hourly_rate": "0"})
        self.assertFalse(form.is_valid())
        self.assertIn("hourly_rate", form.errors)

    def test_edit_mode_staff_field_disabled(self):
        _staff, technician = self.make_technician()
        form = TechnicianForm(technician=technician)
        self.assertTrue(form.fields["staff"].disabled)

    def test_edit_mode_keeps_linked_account_in_dropdown(self):
        _staff, technician = self.make_technician()
        form = TechnicianForm(technician=technician)
        offered = set(
            form.fields["staff"].queryset.values_list("pk", flat=True)
        )
        self.assertIn(technician.StaffID_id, offered)


class ServiceFormTests(RoleTestCase):
    """Service catalogue form."""

    def test_valid_payload(self):
        form = ServiceForm(
            data={
                "service_name": "Oil Change",
                "base_cost": "49.99",
                "est_duration": "30",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_blank_name_rejected(self):
        form = ServiceForm(
            data={
                "service_name": "  ",
                "base_cost": "10",
                "est_duration": "5",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("service_name", form.errors)

    def test_duplicate_name_case_insensitive(self):
        self.make_service(name="Oil Change")
        form = ServiceForm(
            data={
                "service_name": "oil change",
                "base_cost": "10",
                "est_duration": "5",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("service_name", form.errors)

    def test_negative_cost_rejected(self):
        form = ServiceForm(
            data={
                "service_name": "Bad",
                "base_cost": "-1",
                "est_duration": "10",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("base_cost", form.errors)

    def test_zero_duration_rejected(self):
        form = ServiceForm(
            data={
                "service_name": "Zero",
                "base_cost": "10",
                "est_duration": "0",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("est_duration", form.errors)


class ServiceAssignmentFormTests(RoleTestCase):
    """Assignment form: only unassigned services are offered."""

    def test_offers_all_services_when_none_assigned(self):
        _staff, technician = self.make_technician()
        oil = self.make_service(name="Oil Change")
        brakes = self.make_service(
            name="Brake Service", cost="99.00", duration=60
        )
        form = ServiceAssignmentForm(technician=technician)
        offered = set(
            form.fields["service"].queryset.values_list("pk", flat=True)
        )
        self.assertEqual(offered, {oil.Service_code, brakes.Service_code})

    def test_excludes_already_assigned_services(self):
        """Duplicate pairs cannot even be selected in the UI."""
        _staff, technician = self.make_technician()
        oil = self.make_service(name="Oil Change")
        self.make_service(name="Brake Service", cost="99.00", duration=60)
        technician.service_assignments.create(ServiceCode=oil)
        form = ServiceAssignmentForm(technician=technician)
        offered = set(
            form.fields["service"].queryset.values_list("pk", flat=True)
        )
        self.assertNotIn(oil.Service_code, offered)

    def test_invalid_choice_rejected(self):
        _staff, technician = self.make_technician()
        form = ServiceAssignmentForm(
            data={"service": 99999}, technician=technician
        )
        self.assertFalse(form.is_valid())
        self.assertIn("service", form.errors)