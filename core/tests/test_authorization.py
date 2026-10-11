"""Role-based access: what each role may reach, and unauthorized attempts."""

from django.urls import reverse

from core.services import SESSION_KEY_STAFF_ID
from core.tests.base import RoleTestCase

#: Every management screen that must be reachable by Administrators only.
ADMIN_ONLY_URLS = (
    "core:role_list",
    "core:staff_list",
    "core:staff_create",
    "core:technician_list",
    "core:technician_create",
    "core:service_list",
    "core:service_create",
    "core:reports",
    "core:audit_log",
)

#: Management screens that take a row id, so they cannot be reversed without an
#: argument. Checked with a real record so the guard is what denies the request
#: rather than a missing object.
ADMIN_ONLY_DETAIL_URLS = ("core:service_edit",)


class _AuthedCase(RoleTestCase):
    """Logs in a freshly created account for the requested role."""

    def login_role(self, role_name: str):
        role_map = {
            "Administrator": self.admin_role,
            "Receptionist": self.receptionist_role,
            "Technician": self.technician_role,
        }
        staff = self.make_staff(
            email=f"{role_name.lower()}@test.local",
            role=role_map[role_name],
            first_name=role_name,
            last_name="Tester",
        )
        response = self.client.post(
            reverse("core:login"),
            {"email": staff.Email, "password": "Passw0rd!"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        return staff


class AdministratorPermissionTests(_AuthedCase):
    """Administrator can access every management function."""

    def setUp(self):
        super().setUp()
        self.admin = self.login_role("Administrator")

    def test_can_access_all_management_screens(self):
        for url_name in ADMIN_ONLY_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)

    def test_can_access_row_level_management_screens(self):
        service = self.make_service()
        for url_name in ADMIN_ONLY_DETAIL_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(
                    reverse(url_name, args=[service.Service_code])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Edit service")

    def test_dashboard_shows_admin_stats(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, "Staff by role")

    def test_profile_available(self):
        response = self.client.get(reverse("core:profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.admin.Email)


class ReceptionistPermissionTests(_AuthedCase):
    """Receptionist gets dashboard/profile but no management."""

    def setUp(self):
        super().setUp()
        self.receptionist = self.login_role("Receptionist")

    def test_dashboard_and_profile_allowed(self):
        self.assertEqual(
            self.client.get(reverse("core:dashboard")).status_code, 200
        )
        self.assertEqual(
            self.client.get(reverse("core:profile")).status_code, 200
        )

    def test_management_screens_denied(self):
        for url_name in ADMIN_ONLY_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name), follow=True)
                self.assertRedirects(
                    response,
                    reverse("core:access_denied"),
                    target_status_code=403,
                )
                self.assertContains(response, "Access denied", status_code=403)

    def test_row_level_management_screens_denied(self):
        service = self.make_service()
        for url_name in ADMIN_ONLY_DETAIL_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(
                    reverse(url_name, args=[service.Service_code]), follow=True
                )
                self.assertRedirects(
                    response,
                    reverse("core:access_denied"),
                    target_status_code=403,
                )
                self.assertContains(response, "Access denied", status_code=403)

    def test_dashboard_hides_admin_stats(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(response, "Staff by role")


class TechnicianPermissionTests(_AuthedCase):
    """Technician sees own profile including the technician record."""

    def setUp(self):
        super().setUp()
        self.tech_staff = self.login_role("Technician")

    def test_dashboard_and_profile_allowed(self):
        self.assertEqual(
            self.client.get(reverse("core:dashboard")).status_code, 200
        )
        response = self.client.get(reverse("core:profile"))
        self.assertEqual(response.status_code, 200)

    def test_profile_shows_no_technician_section_without_record(self):
        response = self.client.get(reverse("core:profile"))
        self.assertNotContains(response, "Assigned services")

    def test_profile_shows_technician_record_and_assignments(self):
        from core import services

        technician = services.create_technician(
            self.tech_staff.StaffID, "40.00"
        )
        service = self.make_service()
        services.assign_service(
            technician.TechnicianID, service.Service_code
        )
        response = self.client.get(reverse("core:profile"))
        self.assertContains(response, "Technician record")
        self.assertContains(response, "Assigned services")
        self.assertContains(response, service.ServiceName)

    def test_management_screens_denied(self):
        for url_name in ADMIN_ONLY_URLS:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name))
                self.assertRedirects(
                    response,
                    reverse("core:access_denied"),
                    target_status_code=403,
                )


class UnauthenticatedAccessTests(RoleTestCase):
    """No session → redirected to login, never to data."""

    def test_protected_pages_redirect_to_login(self):
        protected = ADMIN_ONLY_URLS + ("core:dashboard", "core:profile")
        for url_name in protected:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("core:login")))
                self.assertIn("next=", response.url)

    def test_no_session_data_written_on_redirect(self):
        self.assertNotIn(SESSION_KEY_STAFF_ID, self.client.session)

    def test_access_denied_page_renders_403(self):
        response = self.client.get(reverse("core:access_denied"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Access denied", status_code=403)
