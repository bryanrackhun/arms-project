"""End-to-end UI flows: changes made through the screens are stored correctly
in the database. Covers staff, technicians, services and logout, plus
dashboard and profile rendering."""

from django.contrib.auth.hashers import check_password
from django.urls import reverse

from core.models import Service, Staff, Technician, TechnicianService
from core.services import SESSION_KEY_STAFF_ID
from core.tests.base import RoleTestCase


class _AdminLogged(RoleTestCase):
    """Every test in this module acts as a logged-in Administrator."""

    def setUp(self):
        super().setUp()
        self.admin = self.make_admin()
        response = self.client.post(
            reverse("core:login"),
            {"email": self.admin.Email, "password": "Passw0rd!"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))


class DashboardProfileViewTests(_AdminLogged):
    """Dashboard shell and own-profile screen."""

    def test_dashboard_lists_counts(self):
        self.make_service()
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard")
        self.assertEqual(response.context["service_total"], 1)
        self.assertEqual(response.context["is_admin"], True)

    def test_dashboard_lists_disabled_future_modules(self):
        """Only genuinely unbuilt modules are listed as planned.

        Timebox 3 moved inspections, repair jobs and service bays from this
        list into working screens, so the only entries left are the Timebox 4
        modules. Asserting on them (rather than on the old Phase 2 entries)
        keeps this test honest: it fails again the moment a phase is built and
        someone forgets to remove it from the "later phases" card, and it fails
        if a module is removed from the card before it actually exists.
        """
        response = self.client.get(reverse("core:dashboard"))
        # Remaining future work is the Timebox 4 set, all marked Phase 4.
        self.assertContains(response, "Later phases")
        self.assertContains(response, "Phase 4")
        for name in ("Inventory", "Invoices", "Payroll", "Reports"):
            self.assertContains(response, name)
        # Timebox 3 modules are no longer planned placeholders: they are real
        # navigation links, so their list entries must be gone from the card.
        self.assertNotContains(response, "Phase 2")
        self.assertNotContains(response, "Phase 3")
        self.assertContains(response, reverse("core:repair_list"))
        self.assertContains(response, reverse("core:bay_list"))

    def test_profile_shows_own_account(self):
        response = self.client.get(reverse("core:profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.admin.Email)
        self.assertContains(response, "Administrator")

    def test_role_list_shows_three_roles(self):
        response = self.client.get(reverse("core:role_list"))
        for name in ("Administrator", "Receptionist", "Technician"):
            self.assertContains(response, name)


class LogoutViewTests(_AdminLogged):
    """Logout function through the UI."""

    def test_logout_redirects_and_clears_session(self):
        response = self.client.post(reverse("core:logout"))
        self.assertRedirects(response, reverse("core:login"))
        self.assertNotIn(SESSION_KEY_STAFF_ID, self.client.session)

    def test_protected_page_after_logout_redirects_to_login(self):
        self.client.post(reverse("core:logout"))
        response = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(
            response, reverse("core:login") + "?next=/dashboard/"
        )


class StaffViewIntegrationTests(_AdminLogged):
    """Staff listing, creation, editing and deactivation via HTTP."""

    def test_staff_list_shows_accounts_and_search(self):
        self.make_staff(
            email="searchable@test.local", role=self.receptionist_role,
            first_name="Searchable", last_name="Person",
        )
        response = self.client.get(reverse("core:staff_list"))
        self.assertContains(response, "searchable@test.local")

        response = self.client.get(reverse("core:staff_list"), {"q": "searchable"})
        self.assertContains(response, "searchable@test.local")
        self.assertNotContains(response, self.admin.Email)

    def test_create_staff_through_form_persists_hashed_password(self):
        response = self.client.post(
            reverse("core:staff_create"),
            {
                "first_name": "Rita",
                "last_name": "Reception",
                "email": "rita@test.local",
                "phone": "+1 555-2222",
                "role": self.receptionist_role.RoleID,
                "password": "Passw0rd!",
                "confirm_password": "Passw0rd!",
            },
        )
        self.assertRedirects(response, reverse("core:staff_list"))
        row = Staff.objects.get(Email="rita@test.local")  # stored in the DB
        self.assertEqual(row.FirstName, "Rita")
        self.assertEqual(row.Phone, "+1 555-2222")
        self.assertEqual(row.RoleID.RoleName, "Receptionist")
        self.assertNotEqual(row.Password, "Passw0rd!")
        self.assertTrue(check_password("Passw0rd!", row.Password))

    def test_create_staff_duplicate_email_shows_inline_error(self):
        payload = {
            "first_name": "A", "last_name": "B", "email": self.admin.Email,
            "role": self.admin_role.RoleID, "phone": "",
            "password": "Passw0rd!", "confirm_password": "Passw0rd!",
        }
        response = self.client.post(reverse("core:staff_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already in use")
        self.assertEqual(Staff.objects.count(), 1)  # nothing written

    def test_edit_staff_updates_database(self):
        staff = self.make_staff(
            email="edit@test.local", role=self.receptionist_role,
            first_name="Before", last_name="Change",
        )
        response = self.client.post(
            reverse("core:staff_edit", args=[staff.StaffID]),
            {
                "first_name": "After",
                "last_name": "Change",
                "email": "edit@test.local",
                "phone": "",
                "role": self.admin_role.RoleID,
                "password": "",
                "confirm_password": "",
            },
        )
        self.assertRedirects(response, reverse("core:staff_list"))
        staff.refresh_from_db()
        self.assertEqual(staff.FirstName, "After")
        self.assertEqual(staff.RoleID.RoleName, "Administrator")  # role update
        self.assertTrue(check_password("Passw0rd!", staff.Password))  # unchanged

    def test_deactivate_and_reactivate_round_trip(self):
        staff = self.make_staff(
            email="cycle@test.local", role=self.receptionist_role
        )
        response = self.client.post(
            reverse("core:staff_deactivate", args=[staff.StaffID])
        )
        self.assertRedirects(response, reverse("core:staff_list"))
        staff.refresh_from_db()
        self.assertFalse(staff.IsActive)

        self.client.post(
            reverse("core:staff_reactivate", args=[staff.StaffID])
        )
        staff.refresh_from_db()
        self.assertTrue(staff.IsActive)

    def test_cannot_deactivate_own_account(self):
        self.client.post(
            reverse("core:staff_deactivate", args=[self.admin.StaffID])
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.IsActive)


class TechnicianViewIntegrationTests(_AdminLogged):
    """Technician screens and assignment flow via HTTP."""

    def test_technician_list_shows_records_and_available_staff(self):
        tech_staff = self.make_staff(
            email="avail@test.local", role=self.technician_role,
            first_name="Avail", last_name="Able",
        )
        response = self.client.get(reverse("core:technician_list"))
        self.assertEqual(list(response.context["available_staff"]), [tech_staff])
        self.assertEqual(response.context["available_count"], 1)
        # A technician record shows up in the rendered table.
        _staff, technician = self.make_technician(
            email="listed@test.local", rate="25.00"
        )
        response = self.client.get(reverse("core:technician_list"))
        self.assertContains(response, "Tom Tech")
        self.assertContains(response, "25.00")

    def test_create_technician_through_form(self):
        tech_staff = self.make_staff(
            email="newtech@test.local", role=self.technician_role
        )
        response = self.client.post(
            reverse("core:technician_create"),
            {"staff": tech_staff.StaffID, "hourly_rate": "38.50"},
        )
        self.assertRedirects(response, reverse("core:technician_list"))
        technician = Technician.objects.get(StaffID=tech_staff)
        self.assertEqual(float(technician.Hourly_rate), 38.50)

    def test_create_technician_zero_rate_rejected(self):
        tech_staff = self.make_staff(
            email="zero@test.local", role=self.technician_role
        )
        response = self.client.post(
            reverse("core:technician_create"),
            {"staff": tech_staff.StaffID, "hourly_rate": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "greater than zero")
        self.assertEqual(Technician.objects.count(), 0)

    def test_edit_technician_updates_rate(self):
        _staff, technician = self.make_technician(rate="30.00")
        response = self.client.post(
            reverse("core:technician_edit", args=[technician.TechnicianID]),
            {"hourly_rate": "61.25"},
        )
        self.assertRedirects(response, reverse("core:technician_list"))
        technician.refresh_from_db()
        self.assertEqual(float(technician.Hourly_rate), 61.25)

    def test_assign_service_through_ui(self):
        _staff, technician = self.make_technician()
        service = self.make_service()
        response = self.client.post(
            reverse(
                "core:technician_assign_service",
                args=[technician.TechnicianID],
            ),
            {"service": service.Service_code},
        )
        self.assertRedirects(
            response,
            reverse("core:technician_edit", args=[technician.TechnicianID]),
        )
        self.assertTrue(
            TechnicianService.objects.filter(
                TechnicianID=technician, ServiceCode=service
            ).exists()
        )

    def test_duplicate_assignment_shows_warning(self):
        """Duplicate blocked with a clear message; no second row."""
        _staff, technician = self.make_technician()
        service = self.make_service()
        TechnicianService.objects.create(
            TechnicianID=technician, ServiceCode=service
        )
        response = self.client.post(
            reverse(
                "core:technician_assign_service",
                args=[technician.TechnicianID],
            ),
            {"service": service.Service_code},
            follow=True,
        )
        self.assertEqual(TechnicianService.objects.count(), 1)
        # Form offers only unassigned services, so an already-assigned code is
        # rejected as an invalid choice or as a duplicate — either way blocked.
        self.assertEqual(response.status_code, 200)

    def test_remove_assignment_through_ui(self):
        _staff, technician = self.make_technician()
        service = self.make_service()
        TechnicianService.objects.create(
            TechnicianID=technician, ServiceCode=service
        )
        response = self.client.post(
            reverse(
                "core:technician_remove_service",
                args=[technician.TechnicianID, service.Service_code],
            ),
        )
        self.assertRedirects(
            response,
            reverse("core:technician_edit", args=[technician.TechnicianID]),
        )
        self.assertEqual(TechnicianService.objects.count(), 0)

    def test_deactivate_technician_through_ui(self):
        staff, technician = self.make_technician()
        response = self.client.post(
            reverse("core:technician_deactivate", args=[technician.TechnicianID])
        )
        self.assertRedirects(response, reverse("core:technician_list"))
        staff.refresh_from_db()
        self.assertFalse(staff.IsActive)


class ServiceViewTests(_AdminLogged):
    """Services screen via HTTP."""

    def test_service_list_shows_rows(self):
        self.make_service(name="Oil Change")
        response = self.client.get(reverse("core:service_list"))
        self.assertContains(response, "Oil Change")
        self.assertContains(response, "49.99")

    def test_create_service_persists(self):
        response = self.client.post(
            reverse("core:service_create"),
            {
                "service_name": "Wheel Alignment",
                "base_cost": "89.99",
                "est_duration": "60",
            },
        )
        self.assertRedirects(response, reverse("core:service_list"))
        row = Service.objects.get(ServiceName="Wheel Alignment")
        self.assertEqual(float(row.BaseCost), 89.99)
        self.assertEqual(row.Est_Duration, 60)

    def test_create_service_duplicate_name_rejected(self):
        self.make_service(name="Oil Change")
        response = self.client.post(
            reverse("core:service_create"),
            {
                "service_name": "oil change",
                "base_cost": "10",
                "est_duration": "5",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already in use")
        self.assertEqual(Service.objects.count(), 1)

    def test_create_service_negative_cost_rejected(self):
        response = self.client.post(
            reverse("core:service_create"),
            {"service_name": "Bad", "base_cost": "-5", "est_duration": "10"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Service.objects.count(), 0)