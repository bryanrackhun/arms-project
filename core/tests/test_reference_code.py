"""Booking reference codes: generation, storage, uniqueness and durability.

Every booking gets an 8-character code the customer can keep. These tests pin
the four promises that matter:

* the code is generated with :mod:`secrets` from the agreed alphabet;
* it is stored on the row at booking time, unique, for every route into the
  diary (a customer online, a receptionist at the counter);
* a clash is retried a limited number of times, and only a clash on the code
  itself is retried;
* nothing that happens to the appointment afterwards can change it.

The confirmation screen is covered too, including that it finds the booking by
its code rather than by the sequential ``AppointmentID``.
"""

import re
from datetime import time as dt_time, timedelta
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core import services
from core.models import (
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CHECKED_IN,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_NO_SHOW,
    REFERENCE_CODE_ALPHABET,
    REFERENCE_CODE_LENGTH,
    REFERENCE_CODE_MAX_ATTEMPTS,
    Appointment,
    generate_reference_code,
)

from .base import RoleTestCase

CODE_PATTERN = re.compile(rf"^[{REFERENCE_CODE_ALPHABET}]{{{REFERENCE_CODE_LENGTH}}}$")


class ReferenceCodeTestCase(RoleTestCase):
    """Shared fixtures: a service, a receptionist and a future slot."""

    def setUp(self):
        self.admin = self.make_admin()
        self.receptionist = self.make_staff(
            email="reception@test.local", role=self.receptionist_role,
            first_name="Rachel", last_name="Receptionist",
        )
        self.service = self.make_service()
        # Tomorrow keeps the slot in the future whenever the suite runs.
        self.date = dj_timezone.localdate() + timedelta(days=1)

    def _book(self, slot=dt_time(12, 0), first="Sam", last="Customer"):
        return services.book_appointment(
            first, last, self.service.Service_code, self.date, slot,
            phone="0400000000", email="sam@example.com",
        )

    def _form_data(self, slot="12:00"):
        return {
            "first_name": "Sam",
            "last_name": "Customer",
            "phone": "0400000000",
            "email": "sam@example.com",
            "service": self.service.Service_code,
            "appointment_date": self.date.isoformat(),
            "appointment_time": slot,
        }


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------
class GenerateReferenceCodeTests(ReferenceCodeTestCase):

    def test_alphabet_is_the_one_the_requirements_specify(self):
        self.assertEqual(REFERENCE_CODE_ALPHABET, "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")

    def test_alphabet_has_no_confusable_zero_one_o_or_i(self):
        for symbol in "01OI":
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, REFERENCE_CODE_ALPHABET)

    def test_code_is_eight_characters_from_the_alphabet(self):
        for _ in range(500):
            code = generate_reference_code()
            self.assertRegex(code, CODE_PATTERN)

    def test_codes_vary(self):
        codes = {generate_reference_code() for _ in range(200)}

        # 200 draws from ~1.1 trillion possibilities: any repeat would mean the
        # generator is not random.
        self.assertEqual(len(codes), 200)

    def test_every_character_is_drawn_with_secrets_choice(self):
        """``secrets`` is the source of randomness, one draw per character."""
        with mock.patch("core.models.secrets.choice", return_value="A") as choice:
            code = generate_reference_code()

        self.assertEqual(code, "A" * REFERENCE_CODE_LENGTH)
        self.assertEqual(choice.call_count, REFERENCE_CODE_LENGTH)
        choice.assert_called_with(REFERENCE_CODE_ALPHABET)

    def test_random_module_is_not_used(self):
        import core.models as models_module

        self.assertFalse(hasattr(models_module, "random"))


class ReferenceCodeFieldTests(ReferenceCodeTestCase):

    def test_field_is_an_indexed_unique_eight_character_column(self):
        field = Appointment._meta.get_field("ReferenceCode")

        self.assertEqual(field.max_length, 8)
        self.assertTrue(field.unique)
        self.assertTrue(field.db_index)
        self.assertFalse(field.editable)

    def test_database_refuses_a_duplicate_code(self):
        """Uniqueness is enforced by the database, not just by the retry loop."""
        first = self._book(slot=dt_time(9, 0))

        with self.assertRaises(IntegrityError), transaction.atomic():
            Appointment.objects.create(
                ReferenceCode=first.ReferenceCode,
                CustomerFirstName="Alex",
                CustomerLastName="Other",
                CustomerPhone="0400000001",
                ServiceCode=self.service,
                AppointmentDate=self.date,
                AppointmentTime=dt_time(10, 0),
            )


# ---------------------------------------------------------------------------
# Booking: generated, stored, unique
# ---------------------------------------------------------------------------
class BookingStoresCodeTests(ReferenceCodeTestCase):

    def test_booking_stores_a_valid_code(self):
        appointment = self._book()

        # Re-read from the database, not just the in-memory instance.
        stored = Appointment.objects.get(pk=appointment.pk)
        self.assertRegex(stored.ReferenceCode, CODE_PATTERN)
        self.assertEqual(stored.ReferenceCode, appointment.ReferenceCode)

    def test_each_booking_gets_its_own_code(self):
        slots = [dt_time(8, 0), dt_time(9, 0), dt_time(10, 0), dt_time(11, 0)]
        codes = [self._book(slot=slot).ReferenceCode for slot in slots]

        self.assertEqual(len(set(codes)), len(slots))

    def test_code_is_the_generators_output(self):
        with mock.patch(
            "core.services.generate_reference_code", return_value="ABCD2345"
        ):
            appointment = self._book()

        self.assertEqual(appointment.ReferenceCode, "ABCD2345")

    def test_code_is_not_derived_from_the_appointment_id(self):
        appointment = self._book()

        self.assertNotEqual(appointment.ReferenceCode, str(appointment.pk))


# ---------------------------------------------------------------------------
# Booking: retry on a collision
# ---------------------------------------------------------------------------
class CollisionRetryTests(ReferenceCodeTestCase):

    def setUp(self):
        super().setUp()
        self.existing = self._book(slot=dt_time(8, 0))

    def test_a_clash_is_retried_with_a_fresh_code(self):
        clash = self.existing.ReferenceCode
        with mock.patch(
            "core.services.generate_reference_code",
            side_effect=[clash, "FRESH234"],
        ) as generate:
            appointment = self._book(slot=dt_time(9, 0))

        self.assertEqual(appointment.ReferenceCode, "FRESH234")
        self.assertEqual(generate.call_count, 2)
        # The original booking is untouched and both rows exist.
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.ReferenceCode, clash)
        self.assertEqual(Appointment.objects.count(), 2)

    def test_the_last_permitted_attempt_can_still_succeed(self):
        clash = self.existing.ReferenceCode
        attempts = [clash] * (REFERENCE_CODE_MAX_ATTEMPTS - 1) + ["LASTTRY2"]
        with mock.patch(
            "core.services.generate_reference_code", side_effect=attempts
        ) as generate:
            appointment = self._book(slot=dt_time(9, 0))

        self.assertEqual(appointment.ReferenceCode, "LASTTRY2")
        self.assertEqual(generate.call_count, REFERENCE_CODE_MAX_ATTEMPTS)

    def test_gives_up_after_five_attempts_with_a_service_error(self):
        self.assertEqual(REFERENCE_CODE_MAX_ATTEMPTS, 5)
        clash = self.existing.ReferenceCode
        with mock.patch(
            "core.services.generate_reference_code", return_value=clash
        ) as generate:
            with self.assertRaises(services.ServiceError):
                self._book(slot=dt_time(9, 0))

        self.assertEqual(generate.call_count, 5)

    def test_a_failed_booking_leaves_nothing_behind(self):
        clash = self.existing.ReferenceCode
        with mock.patch(
            "core.services.generate_reference_code", return_value=clash
        ):
            with self.assertRaises(services.ServiceError):
                self._book(slot=dt_time(9, 0))

        self.assertEqual(Appointment.objects.count(), 1)

    def test_the_transaction_is_usable_after_retries(self):
        """A failed INSERT must not poison the enclosing transaction."""
        clash = self.existing.ReferenceCode
        with mock.patch(
            "core.services.generate_reference_code",
            side_effect=[clash, clash, "OKAY2345"],
        ):
            appointment = self._book(slot=dt_time(9, 0))

        self.assertTrue(Appointment.objects.filter(pk=appointment.pk).exists())

    def test_a_slot_clash_is_not_mistaken_for_a_code_clash(self):
        """Only a clash on the code is retried; other failures surface at once."""
        # Skip the up-front slot check so the *database* rejects the slot, as it
        # would if another request won the same slot a moment earlier.
        with mock.patch(
            "core.services._validate_slot",
            return_value=(self.date, dt_time(8, 0)),
        ), mock.patch(
            "core.services.generate_reference_code",
            side_effect=lambda: generate_reference_code(),
        ) as generate:
            with self.assertRaises(services.ServiceError):
                self._book(slot=dt_time(8, 0))

        self.assertEqual(generate.call_count, 1)
        self.assertEqual(Appointment.objects.count(), 1)


# ---------------------------------------------------------------------------
# Every route into the diary
# ---------------------------------------------------------------------------
class BookingSourceTests(ReferenceCodeTestCase):

    def test_online_booking_stores_a_code(self):
        response = self.client.post(reverse("core:booking"), self._form_data())

        self.assertRedirects(response, reverse("core:booking_success"))
        appointment = Appointment.objects.get()
        self.assertRegex(appointment.ReferenceCode, CODE_PATTERN)

    def test_walk_in_booking_stores_a_code(self):
        self.login_as(self.receptionist)

        self.client.post(reverse("core:appointment_create"), self._form_data())

        appointment = Appointment.objects.get()
        self.assertRegex(appointment.ReferenceCode, CODE_PATTERN)

    def test_online_and_walk_in_bookings_get_different_codes(self):
        self.client.post(reverse("core:booking"), self._form_data("09:00"))
        self.login_as(self.receptionist)
        self.client.post(reverse("core:appointment_create"), self._form_data("10:00"))

        codes = list(Appointment.objects.values_list("ReferenceCode", flat=True))
        self.assertEqual(len(codes), 2)
        self.assertEqual(len(set(codes)), 2)

    def test_a_booking_with_a_vehicle_already_linked_keeps_its_code(self):
        """The walk-in form can link a vehicle straight after booking."""
        vehicle = services.create_vehicle(
            "Toyota", "Corolla", vin="", license_plate="REF-001", year=2018,
        )
        self.login_as(self.receptionist)

        self.client.post(
            reverse("core:appointment_create"),
            {**self._form_data(), "vehicle": vehicle.VehicleID},
        )

        appointment = Appointment.objects.get()
        self.assertEqual(appointment.VehicleID_id, vehicle.VehicleID)
        self.assertRegex(appointment.ReferenceCode, CODE_PATTERN)


# ---------------------------------------------------------------------------
# The code never changes
# ---------------------------------------------------------------------------
class CodeSurvivesLifecycleTests(ReferenceCodeTestCase):

    def _code(self, appointment):
        """The stored code, read straight from the database."""
        return Appointment.objects.get(pk=appointment.pk).ReferenceCode

    def test_accepting_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode

        services.accept_appointment(appointment.pk)

        self.assertEqual(self._code(appointment), original)

    def test_cancelling_a_pending_request_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode

        cancelled = services.cancel_appointment(appointment.pk, reason="Declined")

        self.assertEqual(cancelled.Status, APPOINTMENT_CANCELLED)
        self.assertEqual(self._code(appointment), original)

    def test_cancelling_a_confirmed_appointment_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode
        services.accept_appointment(appointment.pk)

        services.cancel_appointment(appointment.pk, reason="Customer changed mind")

        self.assertEqual(self._code(appointment), original)

    def test_checking_in_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode
        services.accept_appointment(appointment.pk)

        checked_in = services.set_appointment_status(
            appointment.pk, APPOINTMENT_CHECKED_IN
        )

        self.assertEqual(checked_in.Status, APPOINTMENT_CHECKED_IN)
        self.assertEqual(self._code(appointment), original)

    def test_completing_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode
        services.accept_appointment(appointment.pk)
        services.set_appointment_status(appointment.pk, APPOINTMENT_CHECKED_IN)

        services.set_appointment_status(appointment.pk, APPOINTMENT_COMPLETED)

        self.assertEqual(self._code(appointment), original)

    def test_no_show_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode
        services.accept_appointment(appointment.pk)

        services.set_appointment_status(appointment.pk, APPOINTMENT_NO_SHOW)

        self.assertEqual(self._code(appointment), original)

    def test_the_code_is_unchanged_at_every_step_of_a_full_lifecycle(self):
        appointment = self._book()
        original = appointment.ReferenceCode

        for status in (
            APPOINTMENT_CONFIRMED,
            APPOINTMENT_CHECKED_IN,
            APPOINTMENT_COMPLETED,
        ):
            with self.subTest(status=status):
                updated = services.set_appointment_status(appointment.pk, status)
                self.assertEqual(updated.Status, status)
                self.assertEqual(updated.ReferenceCode, original)
                self.assertEqual(self._code(appointment), original)

    def test_editing_the_booking_details_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode

        services.update_appointment(
            appointment.pk, first_name="Samuel", appointment_time=dt_time(14, 0),
            notes="Moved later",
        )

        self.assertEqual(self._code(appointment), original)

    def test_linking_a_vehicle_keeps_the_code(self):
        appointment = self._book()
        original = appointment.ReferenceCode
        vehicle = services.create_vehicle(
            "Toyota", "Corolla", license_plate="REF-002", year=2018
        )

        services.match_vehicle_to_appointment(appointment.pk, vehicle.VehicleID)

        self.assertEqual(self._code(appointment), original)

    def test_a_cancelled_code_is_never_handed_to_a_later_booking(self):
        """Cancelling frees the *slot*, not the code."""
        appointment = self._book()
        original = appointment.ReferenceCode
        services.cancel_appointment(appointment.pk)

        rebooked = self._book()  # same slot, now free again

        self.assertNotEqual(rebooked.ReferenceCode, original)
        self.assertEqual(self._code(appointment), original)

    def test_reference_code_is_not_editable_through_the_admin_form(self):
        from django.contrib import admin as django_admin

        model_admin = django_admin.site._registry[Appointment]

        self.assertIn("ReferenceCode", model_admin.readonly_fields)


# ---------------------------------------------------------------------------
# Lookup: by code, not by AppointmentID
# ---------------------------------------------------------------------------
class LookupByReferenceTests(ReferenceCodeTestCase):

    def test_finds_the_appointment_by_its_code(self):
        appointment = self._book()

        found = services.get_appointment_by_reference(appointment.ReferenceCode)

        self.assertEqual(found.pk, appointment.pk)

    def test_lookup_ignores_case_and_surrounding_spaces(self):
        appointment = self._book()
        typed = f"  {appointment.ReferenceCode.lower()} "

        found = services.get_appointment_by_reference(typed)

        self.assertEqual(found.pk, appointment.pk)

    def test_unknown_code_is_not_found(self):
        self._book()

        with self.assertRaises(services.NotFoundError):
            services.get_appointment_by_reference("ZZZZZZZZ")

    def test_blank_code_is_not_found(self):
        self._book()

        for blank in ("", "   ", None):
            with self.subTest(blank=blank), self.assertRaises(services.NotFoundError):
                services.get_appointment_by_reference(blank)

    def test_the_raw_appointment_id_is_not_a_valid_reference(self):
        appointment = self._book()

        with self.assertRaises(services.NotFoundError):
            services.get_appointment_by_reference(str(appointment.pk))


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------
class ConfirmationScreenTests(ReferenceCodeTestCase):

    def test_confirmation_screen_shows_the_code(self):
        response = self.client.post(
            reverse("core:booking"), self._form_data(), follow=True
        )

        appointment = Appointment.objects.get()
        self.assertRedirects(response, reverse("core:booking_success"))
        self.assertContains(response, appointment.ReferenceCode)

    def test_confirmation_screen_no_longer_presents_the_row_id_as_the_reference(self):
        response = self.client.post(
            reverse("core:booking"), self._form_data(), follow=True
        )

        appointment = Appointment.objects.get()
        self.assertNotContains(response, f"reference #{appointment.pk}")

    def test_the_session_hand_off_carries_the_code_not_the_id(self):
        self.client.post(reverse("core:booking"), self._form_data())

        appointment = Appointment.objects.get()
        self.assertEqual(
            self.client.session["booking_reference"], appointment.ReferenceCode
        )

    def test_the_confirmation_screen_looks_the_booking_up_by_code(self):
        """A bare AppointmentID in the session no longer opens a booking."""
        appointment = self._book()
        session = self.client.session
        session["booking_reference"] = appointment.pk
        session.save()

        response = self.client.get(reverse("core:booking_success"))

        self.assertRedirects(response, reverse("core:booking"))

    def test_a_code_in_the_session_opens_the_booking(self):
        appointment = self._book()
        session = self.client.session
        session["booking_reference"] = appointment.ReferenceCode
        session.save()

        response = self.client.get(reverse("core:booking_success"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, appointment.ReferenceCode)

    def test_the_code_is_shown_for_the_booking_once_only(self):
        self.client.post(reverse("core:booking"), self._form_data())
        self.client.get(reverse("core:booking_success"))

        second = self.client.get(reverse("core:booking_success"))

        self.assertRedirects(second, reverse("core:booking"))


class ReceptionistScreenTests(ReferenceCodeTestCase):

    def test_walk_in_confirmation_message_shows_the_code(self):
        self.login_as(self.receptionist)

        response = self.client.post(
            reverse("core:appointment_create"), self._form_data(), follow=True
        )

        appointment = Appointment.objects.get()
        self.assertRedirects(
            response,
            reverse("core:appointment_detail", args=[appointment.pk]),
        )
        self.assertContains(response, appointment.ReferenceCode)

    def test_appointment_detail_shows_the_code_in_every_status(self):
        self.login_as(self.receptionist)
        appointment = self._book()
        url = reverse("core:appointment_detail", args=[appointment.pk])

        self.assertContains(self.client.get(url), appointment.ReferenceCode)
        services.cancel_appointment(appointment.pk)
        self.assertContains(self.client.get(url), appointment.ReferenceCode)


# ---------------------------------------------------------------------------
# Migration: appointments that already exist get codes
# ---------------------------------------------------------------------------
class BackfillMigrationTests(TransactionTestCase):
    """Rows made before this feature must each end up with their own code."""

    migrate_from = [("core", "0004_servicebay_inspection_repair")]
    migrate_to = [("core", "0005_appointment_referencecode")]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Leave the schema fully migrated for whatever test runs next.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_appointments_each_receive_a_unique_valid_code(self):
        old_apps = self._migrate(self.migrate_from)
        Service = old_apps.get_model("core", "Service")
        OldAppointment = old_apps.get_model("core", "Appointment")
        service = Service.objects.create(
            ServiceName="Legacy", BaseCost="10.00", Est_Duration=30
        )
        day = dj_timezone.localdate() + timedelta(days=1)
        for hour in (8, 9, 10):
            OldAppointment.objects.create(
                CustomerFirstName="Old",
                CustomerLastName=f"Booking{hour}",
                CustomerPhone="0400000000",
                ServiceCode=service,
                AppointmentDate=day,
                AppointmentTime=dt_time(hour, 0),
                Status="Pending",
            )

        new_apps = self._migrate(self.migrate_to)
        NewAppointment = new_apps.get_model("core", "Appointment")
        codes = list(NewAppointment.objects.values_list("ReferenceCode", flat=True))

        self.assertEqual(len(codes), 3)
        self.assertEqual(len(set(codes)), 3)
        for code in codes:
            self.assertRegex(code, CODE_PATTERN)
