"""Booking collects the car's model and registration, and links a Vehicle.

The public booking form now asks for the car model and registration number, and
:func:`core.services.book_appointment` resolves those two facts into a real
``Vehicles`` row *inside the same transaction* as the appointment. These tests
pin the four promises that matter:

* a plate that is new creates a vehicle holding only the plate and the model;
* a plate that already exists - in any formatting - reuses that record and never
  overwrites the fuller details captured at check-in;
* both fields are required, and a missing one is refused before anything is
  written;
* if the booking fails, the vehicle it would have created is rolled back too, so
  no orphan car is left on file.

Check-in is covered as well: the receptionist can correct a vehicle's details
and re-link the appointment to a different one.
"""

from datetime import time as dt_time, timedelta
from unittest import mock

from django.utils import timezone as dj_timezone

from core import services
from core.forms import BookingForm
from core.models import (
    REFERENCE_CODE_MAX_ATTEMPTS,
    Appointment,
    Vehicle,
)

from .base import RoleTestCase


class BookingVehicleTestCase(RoleTestCase):
    """Shared fixtures: a service and a future slot for the public form."""

    def setUp(self):
        self.service = self.make_service()
        # Tomorrow keeps the slot in the future whenever the suite runs.
        self.date = dj_timezone.localdate() + timedelta(days=1)

    def _book(self, *, slot=dt_time(12, 0), first="Sam", last="Customer",
              car_model="Toyota Corolla", license_plate="MMK1234"):
        return services.book_appointment(
            first, last, self.service.Service_code, self.date, slot,
            phone="0400000000", email="sam@example.com",
            car_model=car_model, license_plate=license_plate,
        )


# ---------------------------------------------------------------------------
# A new registration plate creates the vehicle
# ---------------------------------------------------------------------------
class NewPlateTests(BookingVehicleTestCase):

    def test_booking_links_a_vehicle(self):
        appointment = self._book()

        self.assertIsNotNone(appointment.VehicleID_id)
        self.assertTrue(Vehicle.objects.filter(pk=appointment.VehicleID_id).exists())

    def test_new_plate_creates_a_vehicle_with_only_plate_and_model(self):
        """The booking knows two facts, so the row holds two facts - no more."""
        appointment = self._book(car_model="Honda Civic", license_plate="NEW123")
        vehicle = Vehicle.objects.get(pk=appointment.VehicleID_id)

        self.assertEqual(vehicle.LicensePlate, "NEW123")
        self.assertEqual(vehicle.Model, "Honda Civic")
        # Everything the online form cannot know stays empty rather than guessed.
        self.assertEqual(vehicle.Make, "")
        self.assertIsNone(vehicle.VIN)
        self.assertIsNone(vehicle.Year)
        self.assertEqual(vehicle.Colour, "")

    def test_only_one_vehicle_is_created(self):
        self._book()

        self.assertEqual(Vehicle.objects.count(), 1)

    def test_the_appointment_is_persisted_with_the_link(self):
        appointment = self._book()

        stored = Appointment.objects.get(pk=appointment.pk)
        self.assertEqual(stored.VehicleID_id, appointment.VehicleID_id)
# ---------------------------------------------------------------------------
# An existing plate is reused, in any format
# ---------------------------------------------------------------------------
class ExistingPlateTests(BookingVehicleTestCase):

    def test_existing_plate_is_reused_not_duplicated(self):
        existing = services.create_vehicle(
            "Toyota", "Corolla", license_plate="OLD123", year=2015,
            colour="Blue",
        )

        appointment = self._book(license_plate="OLD123")

        self.assertEqual(appointment.VehicleID_id, existing.VehicleID)
        self.assertEqual(Vehicle.objects.count(), 1)

    def test_a_differently_formatted_plate_still_matches(self):
        """``ab12 cde``, ``AB12-CDE`` and ``AB12CDE`` are the same registration."""
        existing = services.create_vehicle(
            "Toyota", "Corolla", license_plate="AB12CDE", year=2015,
        )

        appointment = self._book(license_plate="ab12 cde")

        self.assertEqual(appointment.VehicleID_id, existing.VehicleID)
        self.assertEqual(Vehicle.objects.count(), 1)

    def test_hyphenated_plate_still_matches_the_stored_one(self):
        existing = services.create_vehicle(
            "Toyota", "Corolla", license_plate="AB12CDE", year=2015,
        )

        appointment = self._book(license_plate="AB12-CDE")

        self.assertEqual(appointment.VehicleID_id, existing.VehicleID)
        self.assertEqual(Vehicle.objects.count(), 1)

    def test_the_stored_plate_is_the_normalised_value(self):
        appointment = self._book(license_plate="mmk 99-88")
        vehicle = Vehicle.objects.get(pk=appointment.VehicleID_id)

        self.assertEqual(vehicle.LicensePlate, "MMK9988")

    def test_existing_vehicle_details_are_never_overwritten(self):
        """A booking typed on a phone must not blank the check-in record."""
        existing = services.create_vehicle(
            "Toyota", "Corolla", license_plate="KEEP123",
            year=2012, colour="Green",
        )

        self._book(car_model="Something Else", license_plate="KEEP123")

        vehicle = Vehicle.objects.get(pk=existing.VehicleID)
        self.assertEqual(vehicle.Make, "Toyota")
        self.assertEqual(vehicle.Model, "Corolla")
        self.assertEqual(vehicle.Year, 2012)
        self.assertEqual(vehicle.Colour, "Green")

    def test_check_in_finds_the_vehicle_a_booking_created(self):
        """The desk typing the plate in a different format finds the same car."""
        appointment = self._book(car_model="Mazda 3", license_plate="FB1000")

        vehicle, created = services.check_in_vehicle(
            make="Mazda", model="3", license_plate="fb 1000",
        )

        self.assertFalse(created)
        self.assertEqual(vehicle.VehicleID, appointment.VehicleID_id)
        self.assertEqual(Vehicle.objects.count(), 1)
# ---------------------------------------------------------------------------
# Both fields are required
# ---------------------------------------------------------------------------
class MissingFieldsTests(BookingVehicleTestCase):

    def test_missing_car_model_is_refused(self):
        with self.assertRaises(services.ValidationError):
            self._book(car_model="")

        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(Vehicle.objects.exists())

    def test_missing_registration_is_refused(self):
        with self.assertRaises(services.ValidationError):
            self._book(license_plate="")

        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(Vehicle.objects.exists())

    def test_a_malformed_registration_is_refused(self):
        with self.assertRaises(services.ValidationError):
            self._book(license_plate="!")

        self.assertFalse(Vehicle.objects.exists())

    def test_a_too_long_car_model_is_refused(self):
        with self.assertRaises(services.ValidationError):
            self._book(car_model="X" * 41)

        self.assertFalse(Vehicle.objects.exists())

    def test_whitespace_only_values_are_treated_as_missing(self):
        with self.assertRaises(services.ValidationError):
            self._book(car_model="   ", license_plate="   ")

        self.assertFalse(Vehicle.objects.exists())


# ---------------------------------------------------------------------------
# Rollback: a failed booking leaves no vehicle behind
# ---------------------------------------------------------------------------
class RollbackTests(BookingVehicleTestCase):

    def test_a_clashing_slot_leaves_no_vehicle_behind(self):
        """The vehicle insert rolls back with the appointment it belonged to."""
        self._book(slot=dt_time(9, 0), license_plate="FIRST1")

        with self.assertRaises(services.ValidationError):
            self._book(slot=dt_time(9, 0), license_plate="SECOND2", first="Alex")

        self.assertEqual(Appointment.objects.count(), 1)
        self.assertEqual(Vehicle.objects.count(), 1)
        self.assertFalse(Vehicle.objects.filter(LicensePlate="SECOND2").exists())

    def test_a_failed_reference_code_leaves_no_vehicle_behind(self):
        """Even after the retry loop gives up, the new car is rolled back."""
        # Force every attempt to collide so the booking ultimately fails.
        with mock.patch(
            "core.services.generate_reference_code",
            return_value="CLASH999",
        ):
            self._book(slot=dt_time(9, 0), license_plate="CLASH1")

            with self.assertRaises(services.ServiceError):
                self._book(slot=dt_time(10, 0), license_plate="CLASH2", first="Alex")

        self.assertEqual(Appointment.objects.count(), 1)
        # The first booking's vehicle survives; the failed one's does not.
        self.assertFalse(Vehicle.objects.filter(LicensePlate="CLASH2").exists())
        self.assertTrue(Vehicle.objects.filter(LicensePlate="CLASH1").exists())
        self.assertGreaterEqual(REFERENCE_CODE_MAX_ATTEMPTS, 1)

    def test_reusing_a_vehicle_after_a_failure_is_clean(self):
        """Rollback does not leave the shared vehicle row damaged."""
        existing = services.create_vehicle(
            "Toyota", "Corolla", license_plate="SHARED1", year=2011,
        )
        self._book(slot=dt_time(9, 0), license_plate="SHARED1")
        with self.assertRaises(services.ValidationError):
            self._book(slot=dt_time(9, 0), license_plate="SHARED1", first="Alex")

        vehicle = Vehicle.objects.get(pk=existing.VehicleID)
        self.assertEqual(vehicle.Year, 2011)
        self.assertEqual(Vehicle.objects.count(), 1)
# ---------------------------------------------------------------------------
# Check-in corrections still work
# ---------------------------------------------------------------------------
class CheckInCorrectionTests(BookingVehicleTestCase):

    def test_receptionist_can_complete_a_bookings_partial_vehicle(self):
        """The record a booking created can be filled in when the car arrives."""
        appointment = self._book(car_model="Toyota Corolla", license_plate="FILL12")

        vehicle = services.update_vehicle(
            appointment.VehicleID_id,
            make="Toyota", model="Corolla", license_plate="FILL12",
            year=2019, colour="White",
        )

        self.assertEqual(vehicle.Make, "Toyota")
        self.assertEqual(vehicle.Year, 2019)
        self.assertEqual(vehicle.Colour, "White")

    def test_appointment_can_be_re_linked_to_a_different_vehicle(self):
        """Correcting the link needs confirm_replace, because one is attached."""
        appointment = self._book(license_plate="WRONG1")
        correct = services.create_vehicle(
            "Honda", "Civic", license_plate="RIGHT1", year=2020,
        )

        with self.assertRaises(services.ValidationError):
            services.match_vehicle_to_appointment(
                appointment.pk, correct.VehicleID
            )

        services.match_vehicle_to_appointment(
            appointment.pk, correct.VehicleID, confirm_replace=True
        )

        self.assertEqual(
            Appointment.objects.get(pk=appointment.pk).VehicleID_id,
            correct.VehicleID,
        )

    def test_re_linking_the_same_vehicle_is_a_harmless_no_op(self):
        appointment = self._book(license_plate="SAME123")

        returned = services.match_vehicle_to_appointment(
            appointment.pk, appointment.VehicleID_id
        )

        self.assertEqual(returned.VehicleID_id, appointment.VehicleID_id)

    def test_editing_the_plate_moves_the_link_to_the_new_vehicle(self):
        """Correcting a mistyped plate re-points the appointment."""
        appointment = self._book(car_model="Toyota Corolla", license_plate="TYPO12")

        updated = services.update_appointment(
            appointment.pk,
            car_model="Toyota Corolla", license_plate="FIXED1",
        )

        vehicle = Vehicle.objects.get(pk=updated.VehicleID_id)
        self.assertEqual(vehicle.LicensePlate, "FIXED1")
# ---------------------------------------------------------------------------
# The form refuses the same cases before touching the database
# ---------------------------------------------------------------------------
class BookingFormVehicleTests(BookingVehicleTestCase):

    def _payload(self, **overrides):
        data = {
            "first_name": "Sam",
            "last_name": "Customer",
            "phone": "0400000000",
            "email": "sam@example.com",
            "service": self.service.Service_code,
            "car_model": "Toyota Corolla",
            "license_plate": "FORM123",
            "appointment_date": self.date.isoformat(),
            "appointment_time": "12:00",
        }
        data.update(overrides)
        return data

    def test_car_model_and_registration_are_required_fields(self):
        form = BookingForm(data=self._payload(car_model="", license_plate=""))

        self.assertFalse(form.is_valid())
        self.assertIn("car_model", form.errors)
        self.assertIn("license_plate", form.errors)

    def test_the_form_normalises_the_plate_it_accepts(self):
        form = BookingForm(data=self._payload(license_plate="ab 12-cde"))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["license_plate"], "AB12CDE")

    def test_the_form_rejects_a_malformed_plate(self):
        form = BookingForm(data=self._payload(license_plate="!"))

        self.assertFalse(form.is_valid())
        self.assertIn("license_plate", form.errors)

    def test_the_form_trims_the_car_model(self):
        form = BookingForm(data=self._payload(car_model="  Toyota Corolla  "))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["car_model"], "Toyota Corolla")


# ---------------------------------------------------------------------------
# The public form is announced in the page itself
# ---------------------------------------------------------------------------
class BookingPageTests(BookingVehicleTestCase):

    def test_the_booking_page_asks_for_the_car(self):
        response = self.client.get("/book/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Car model")
        self.assertContains(response, "Registration number")
class BookingViewVehicleTests(BookingVehicleTestCase):
    """The end-to-end POST, through the real view, creates and links a vehicle."""

    def test_posting_the_form_creates_and_links_a_vehicle(self):
        response = self.client.post(
            "/book/",
            {
                "first_name": "Sam",
                "last_name": "Customer",
                "phone": "0400000000",
                "email": "sam@example.com",
                "service": self.service.Service_code,
                "car_model": "Toyota Corolla",
                "license_plate": "view 123",
                "appointment_date": self.date.isoformat(),
                "appointment_time": "12:00",
            },
        )

        self.assertEqual(response.status_code, 302)
        appointment = Appointment.objects.get()
        vehicle = Vehicle.objects.get(pk=appointment.VehicleID_id)
        self.assertEqual(vehicle.LicensePlate, "VIEW123")
        self.assertEqual(vehicle.Model, "Toyota Corolla")

    def test_posting_without_the_car_is_refused_by_the_form(self):
        response = self.client.post(
            "/book/",
            {
                "first_name": "Sam",
                "last_name": "Customer",
                "phone": "0400000000",
                "email": "sam@example.com",
                "service": self.service.Service_code,
                "appointment_date": self.date.isoformat(),
                "appointment_time": "12:00",
            },
        )

        # The form redisplays with inline errors rather than booking anything.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(Vehicle.objects.exists())