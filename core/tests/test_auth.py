"""Authentication: valid login, invalid login and logout."""

from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from django.urls import reverse

from core import services
from core.services import (
    INVALID_CREDENTIALS_MESSAGE,
    SESSION_KEY_ROLE_ID,
    SESSION_KEY_ROLE_NAME,
    SESSION_KEY_STAFF_ID,
    AuthenticationError,
)
from core.tests.base import RoleTestCase


class _Session(dict):
    def flush(self):
        self.clear()

    def set_expiry(self, seconds):
        self["_expiry"] = seconds


class PasswordHashingTests(TestCase):
    """Passwords are hashed before storing; plain text is never persisted."""

    def test_hash_is_salted_and_verifiable(self):
        hashed = services.hash_password("Passw0rd!")
        self.assertNotEqual(hashed, "Passw0rd!")
        self.assertTrue(hashed.startswith("pbkdf2_sha256$"))
        self.assertTrue(check_password("Passw0rd!", hashed))

    def test_same_password_hashes_differently(self):
        """Per-password salt: identical inputs produce different hashes."""
        self.assertNotEqual(
            services.hash_password("Passw0rd!"),
            services.hash_password("Passw0rd!"),
        )

    def test_verify_wrong_password_false(self):
        hashed = make_password("Passw0rd!")
        self.assertFalse(services.verify_password("WrongPass1!", hashed))

    def test_verify_empty_and_malformed(self):
        self.assertFalse(services.verify_password("", make_password("Passw0rd!")))
        self.assertFalse(services.verify_password("Passw0rd!", "not-a-hash"))


class ValidLoginTests(RoleTestCase):
    """Login with valid credentials."""

    def test_service_login_sets_session_contract(self):
        staff = self.make_admin(password="Passw0rd!")
        session = _Session()
        returned = services.login(session, staff.Email, "Passw0rd!")

        self.assertEqual(returned.StaffID, staff.StaffID)
        self.assertEqual(session[SESSION_KEY_STAFF_ID], staff.StaffID)
        self.assertEqual(session[SESSION_KEY_ROLE_ID], self.admin_role.RoleID)
        self.assertEqual(session[SESSION_KEY_ROLE_NAME], "Administrator")
        self.assertEqual(session["_expiry"], 1800)  # 30-minute expiry

    def test_email_match_is_case_insensitive(self):
        staff = self.make_admin()
        session = _Session()
        services.login(session, staff.Email.upper(), "Passw0rd!")
        self.assertEqual(session[SESSION_KEY_STAFF_ID], staff.StaffID)

    def test_ui_login_redirects_to_dashboard(self):
        admin = self.make_admin()
        response = self.client.post(
            reverse("core:login"),
            {"email": admin.Email, "password": "Passw0rd!"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertIn(SESSION_KEY_STAFF_ID, self.client.session)

    def test_ui_login_honours_next_parameter(self):
        admin = self.make_admin()
        response = self.client.post(
            reverse("core:login") + "?next=/roles/",
            {"email": admin.Email, "password": "Passw0rd!"},
        )
        self.assertRedirects(response, "/roles/")


class InvalidLoginTests(RoleTestCase):
    """Invalid credentials, empty fields and inactive accounts."""

    def test_wrong_password_shows_generic_message(self):
        admin = self.make_admin()
        response = self.client.post(
            reverse("core:login"),
            {"email": admin.Email, "password": "WrongPass1!"},
        )
        self.assertEqual(response.status_code, 200)  # re-renders the form
        self.assertContains(response, INVALID_CREDENTIALS_MESSAGE)
        self.assertNotIn(SESSION_KEY_STAFF_ID, self.client.session)

    def test_unknown_email_same_generic_message(self):
        """No account-enumeration: the message is identical."""
        response = self.client.post(
            reverse("core:login"),
            {"email": "ghost@test.local", "password": "Passw0rd!"},
        )
        self.assertContains(response, INVALID_CREDENTIALS_MESSAGE)

    def test_inactive_account_rejected(self):
        """Handle inactive or deleted staff accounts."""
        admin = self.make_admin(is_active=False)
        with self.assertRaises(AuthenticationError) as ctx:
            services.login(_Session(), admin.Email, "Passw0rd!")
        self.assertEqual(str(ctx.exception), INVALID_CREDENTIALS_MESSAGE)

    def test_empty_fields_rejected(self):
        """Validate empty login fields."""
        response = self.client.post(
            reverse("core:login"), {"email": "", "password": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY_STAFF_ID, self.client.session)

    def test_service_login_empty_email_raises(self):
        with self.assertRaises(AuthenticationError):
            services.login(_Session(), "   ", "Passw0rd!")

    def test_service_login_empty_password_raises(self):
        admin = self.make_admin()
        with self.assertRaises(AuthenticationError):
            services.login(_Session(), admin.Email, "")


class SessionContractTests(RoleTestCase):
    """The verify_session helper reflects the session state; logout invalidates it."""

    def test_verify_session_returns_identity(self):
        staff = self.make_admin()
        session = _Session()
        services.login(session, staff.Email, "Passw0rd!")
        identity = services.verify_session(session)
        self.assertEqual(
            identity,
            {
                "staff_id": staff.StaffID,
                "role_id": self.admin_role.RoleID,
                "role_name": "Administrator",
            },
        )

    def test_verify_session_without_login_raises(self):
        with self.assertRaises(AuthenticationError):
            services.verify_session(_Session())

    def test_logout_flushes_session(self):
        """Logout destroys the session so it cannot be reused."""
        staff = self.make_admin()
        session = _Session()
        services.login(session, staff.Email, "Passw0rd!")
        services.logout(session)
        with self.assertRaises(AuthenticationError):
            services.verify_session(session)

    def test_ui_logout_clears_session_and_redirects(self):
        admin = self.make_admin()
        self.client.post(
            reverse("core:login"),
            {"email": admin.Email, "password": "Passw0rd!"},
        )
        self.assertIn(SESSION_KEY_STAFF_ID, self.client.session)
        response = self.client.post(reverse("core:logout"))
        self.assertRedirects(response, reverse("core:login"))
        self.assertNotIn(SESSION_KEY_STAFF_ID, self.client.session)

    def test_logout_via_get_is_rejected(self):
        """Logout is a state change: POST only (CSRF-protected)."""
        admin = self.make_admin()
        self.client.post(
            reverse("core:login"),
            {"email": admin.Email, "password": "Passw0rd!"},
        )
        response = self.client.get(reverse("core:logout"))
        self.assertEqual(response.status_code, 405)
        self.assertIn(SESSION_KEY_STAFF_ID, self.client.session)