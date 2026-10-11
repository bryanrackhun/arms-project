"""Smoke tests: every Timebox 2 screen renders for the roles allowed to see it.

These complement the behaviour tests by exercising the full request/response
cycle (template included) for each Timebox 2 page, so template syntax or
context errors surface even on screens the other tests only check access for.
"""

from datetime import time as dt_time, timedelta

from django.urls import reverse
from django.utils import timezone as dj_timezone

from core import services

from .base import RoleTestCase


class Timebox2ScreenSmokeTests(RoleTestCase):
    """Render every Timebox 2 screen as each role that may reach it."""

    def setUp(self):
        self.admin = self.make_admin()
        self.receptionist = self.make_staff(
            email="reception@test.local", role=self.receptionist_role,
            first_name="Rachel", last_name="Receptionist",
        )
        self.service = self.make_service()
        # Tomorrow guarantees the slot is in the future no matter when the
        # suite runs; 12:00 is inside opening hours (08:00-17:30).
        self.date = dj_timezone.localdate() + timedelta(days=1)
        self.slot = dt_time(12, 0)

    def _book(self, first="Sam", last="Customer"):
        return services.book_appointment(
            first, last, self.service.Service_code, self.date, self.slot,
            phone="0400000000", email="sam@example.com",
            car_model="Toyota Corolla", license_plate="SMOKEBK",
        )

    def _vehicle(self, plate="SMOKE-01", vin="SMKVN000000000001"):
        return services.create_vehicle(
            "Toyota", "Corolla", vin=vin, license_plate=plate,
            year=2018, colour="Silver",
        )

    def assert_ok(self, url):
        with self.subTest(url=url):
            self.assertEqual(self.client.get(url).status_code, 200)

    def assert_denied(self, url):
        """A role-blocked URL redirects to the 403 access-denied page."""
        with self.subTest(url=url):
            response = self.client.get(url, follow=True)
            self.assertRedirects(
                response,
                reverse("core:access_denied"),
                target_status_code=403,
            )
            self.assertContains(response, "Access denied", status_code=403)

    def test_receptionist_sees_every_booking_desk_screen(self):
        self.login_as(self.receptionist)
        appointment = self._book()
        vehicle = self._vehicle()
        # The booking already linked its own vehicle, so swapping in this one is
        # a correction and has to be confirmed explicitly.
        services.match_vehicle_to_appointment(
            appointment.AppointmentID, vehicle.VehicleID, confirm_replace=True
        )
        for url in (
            reverse("core:dashboard"),
            reverse("core:appointment_list"),
            reverse("core:appointment_create"),
            reverse("core:appointment_detail", args=[appointment.AppointmentID]),
            reverse("core:appointment_edit", args=[appointment.AppointmentID]),
            reverse("core:vehicle_list"),
            reverse("core:vehicle_create"),
            reverse("core:vehicle_detail", args=[vehicle.VehicleID]),
            reverse("core:vehicle_edit", args=[vehicle.VehicleID]),
            reverse("core:vehicle_check_in"),
        ):
            self.assert_ok(url)

    def test_administrator_sees_booking_desk_screens_too(self):
        self.login_as(self.admin)
        appointment = self._book()
        vehicle = self._vehicle()
        for url in (
            reverse("core:dashboard"),
            reverse("core:appointment_list"),
            reverse("core:appointment_detail", args=[appointment.AppointmentID]),
            reverse("core:vehicle_list"),
            reverse("core:vehicle_detail", args=[vehicle.VehicleID]),
            reverse("core:vehicle_check_in"),
        ):
            self.assert_ok(url)

    def test_technician_reaches_vehicle_detail_but_not_desk_actions(self):
        staff, _ = self.make_technician()
        self.login_as(staff)
        vehicle = self._vehicle()

        for url in (
            reverse("core:dashboard"),
            reverse("core:vehicle_detail", args=[vehicle.VehicleID]),
        ):
            self.assert_ok(url)

        for url in (
            reverse("core:appointment_list"),
            reverse("core:appointment_create"),
            reverse("core:vehicle_list"),
            reverse("core:vehicle_create"),
            reverse("core:vehicle_check_in"),
            reverse("core:vehicle_edit", args=[vehicle.VehicleID]),
        ):
            self.assert_denied(url)

    def test_anonymous_visitor_can_book_and_see_confirmation(self):
        self.assert_ok(reverse("core:booking"))
        response = self.client.post(
            reverse("core:booking"),
            {
                "first_name": "Sam",
                "last_name": "Customer",
                "phone": "0400000000",
                "email": "sam@example.com",
                "service": self.service.Service_code,
                "car_model": "Toyota Corolla",
                "license_plate": "SMOKEP1",
                "appointment_date": self.date.isoformat(),
                "appointment_time": self.slot.strftime("%H:%M"),
            },
            follow=True,
        )
        # The post redirects to the confirmation page exactly once; the
        # session reference is consumed there, so a second visit would
        # (correctly) bounce back to the booking form.
        self.assertRedirects(response, reverse("core:booking_success"))
        self.assertContains(response, "Sam")
        self.assertContains(response, "Customer")

    def test_logged_in_booking_staff_is_redirected_from_public_booking(self):
        self.login_as(self.receptionist)
        response = self.client.get(reverse("core:booking"))
        self.assertRedirects(response, reverse("core:appointment_list"))

    def test_booking_desk_dashboard_tiles_render_for_receptionist(self):
        self.login_as(self.receptionist)
        self._book()
        self._vehicle()
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Booking desk", content)
        self.assertIn("Vehicle records", content)
