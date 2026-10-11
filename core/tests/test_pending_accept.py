"""Booking requests start as Pending and wait to be accepted.

Covers the three-part change: a booking lands in the review queue rather than
in the diary as a confirmed slot, staff decide what happens to each request
(accept or cancel), and the public landing page stays reachable by a customer
who has no account.

The service-layer tests pin the lifecycle itself — what the status becomes and
which moves are refused — while the view tests pin the HTTP contract around it:
who may accept, where the response lands, and what the screens say.
"""

import importlib
from datetime import time as dt_time, timedelta

from django.db import IntegrityError, migrations, transaction
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core import services
from core.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_PENDING,
    APPOINTMENT_TRANSITIONS,
    Appointment,
)

from .base import RoleTestCase


class PendingLifecycleTestCase(RoleTestCase):
    """Shared fixtures: a service, a receptionist and a future slot."""

    def setUp(self):
        self.admin = self.make_admin()
        self.receptionist = self.make_staff(
            email="reception@test.local", role=self.receptionist_role,
            first_name="Rachel", last_name="Receptionist",
        )
        self.service = self.make_service()
        # Tomorrow keeps the slot in the future whenever the suite runs; 12:00
        # sits inside opening hours (08:00-17:30) and on the half hour.
        self.date = dj_timezone.localdate() + timedelta(days=1)
        self.slot = dt_time(12, 0)

    def _book(self, first="Sam", last="Customer", slot=None):
        """Book through the real service call the public form uses."""
        return services.book_appointment(
            first, last, self.service.Service_code, self.date,
            slot or self.slot, phone="0400000000", email="sam@example.com",
            car_model="Toyota Corolla", license_plate="PEND123",
        )


class BookingCreatesPendingTests(PendingLifecycleTestCase):
    """A new booking is a request, not a confirmed slot."""

    def test_book_appointment_starts_as_pending(self):
        appointment = self._book()

        self.assertEqual(appointment.Status, APPOINTMENT_PENDING)
        self.assertTrue(appointment.is_pending)
        # The slot is reserved by the request itself, so nobody else can take it
        # while the request waits.
        self.assertTrue(appointment.is_active)

    def test_status_is_persisted_as_pending(self):
        """Re-read from the database, not just the in-memory instance."""
        appointment = self._book()
        fresh = Appointment.objects.get(pk=appointment.pk)

        self.assertEqual(fresh.Status, APPOINTMENT_PENDING)

    def test_status_default_is_pending(self):
        """The field default matches, so rows made outside the service agree."""
        field = Appointment._meta.get_field("Status")

        self.assertEqual(field.default, APPOINTMENT_PENDING)

    def test_pending_holds_the_slot_against_a_second_request(self):
        """Two customers cannot request the same time before either is reviewed."""
        self._book(first="Sam", last="Customer")

        with self.assertRaises(services.ValidationError):
            self._book(first="Alex", last="Other", slot=self.slot)

    def test_cancelling_a_request_frees_the_slot(self):
        appointment = self._book()
        services.cancel_appointment(appointment.pk, reason="Customer changed plans")

        # The freed time is offered to the next customer straight away.
        replacement = self._book(first="Alex", last="Other", slot=self.slot)
        self.assertEqual(replacement.Status, APPOINTMENT_PENDING)

    def test_pending_is_counted_as_slot_holding(self):
        """Guards the constant the uniqueness constraint is built from."""
        self.assertIn(APPOINTMENT_PENDING, ACTIVE_APPOINTMENT_STATUSES)

    def test_database_constraint_blocks_duplicate_slot(self):
        """The constraint holds even if the service-layer check is bypassed."""
        appointment = self._book()
        duplicate = Appointment(
            CustomerFirstName="Alex",
            CustomerLastName="Other",
            CustomerPhone="0400000000",
            CustomerEmail="alex@example.com",
            ServiceCode=self.service,
            AppointmentDate=appointment.AppointmentDate,
            AppointmentTime=appointment.AppointmentTime,
            Status=APPOINTMENT_PENDING,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                duplicate.save()


class AcceptAppointmentServiceTests(PendingLifecycleTestCase):
    """Accepting is the decision that turns a request into a booking."""

    def test_accept_moves_pending_to_confirmed(self):
        appointment = self._book()

        accepted = services.accept_appointment(appointment.pk)

        self.assertEqual(accepted.Status, APPOINTMENT_CONFIRMED)
        self.assertFalse(accepted.is_pending)
        self.assertTrue(accepted.is_active)

    def test_accept_persists_confirmed_status(self):
        appointment = self._book()
        services.accept_appointment(appointment.pk)

        self.assertEqual(
            Appointment.objects.get(pk=appointment.pk).Status,
            APPOINTMENT_CONFIRMED,
        )

    def test_accept_twice_is_refused(self):
        """The second attempt is an illegal transition, not a silent no-op."""
        appointment = self._book()
        services.accept_appointment(appointment.pk)

        with self.assertRaises(services.ValidationError):
            services.accept_appointment(appointment.pk)

    def test_accept_a_cancelled_request_is_refused(self):
        appointment = self._book()
        services.cancel_appointment(appointment.pk, reason="Declined")

        with self.assertRaises(services.ValidationError):
            services.accept_appointment(appointment.pk)

    def test_accept_unknown_appointment_raises(self):
        with self.assertRaises(services.NotFoundError):
            services.accept_appointment(999999)

    def test_confirm_appointment_is_the_same_transition(self):
        """The old name keeps working for callers that still speak 'confirm'."""
        appointment = self._book()

        confirmed = services.confirm_appointment(appointment.pk)

        self.assertEqual(confirmed.Status, APPOINTMENT_CONFIRMED)

    def test_confirmed_appointment_can_be_checked_in(self):
        """Accepting is a step along the lifecycle, not a dead end."""
        appointment = self._book()
        services.accept_appointment(appointment.pk)

        checked_in = services.set_appointment_status(
            appointment.pk, "Checked In"
        )

        self.assertEqual(checked_in.Status, "Checked In")
        self.assertIsNotNone(checked_in.CheckedInAt)


class CancelPendingRequestServiceTests(PendingLifecycleTestCase):
    """Declining a request cancels it and releases the slot."""

    def test_cancel_pending_request_records_reason(self):
        appointment = self._book()

        cancelled = services.cancel_appointment(appointment.pk, reason="Fully booked")

        self.assertEqual(cancelled.Status, APPOINTMENT_CANCELLED)
        self.assertEqual(cancelled.CancelReason, "Fully booked")
        self.assertTrue(cancelled.is_cancelled)
        self.assertFalse(cancelled.is_active)

    def test_pending_can_only_move_to_accept_cancel_or_no_show(self):
        """The lifecycle offered for a waiting request is the narrow one."""
        self.assertEqual(
            APPOINTMENT_TRANSITIONS[APPOINTMENT_PENDING],
            [APPOINTMENT_CONFIRMED, APPOINTMENT_CANCELLED, "No Show"],
        )

    def test_pending_request_cannot_jump_straight_to_completed(self):
        appointment = self._book()

        with self.assertRaises(services.ValidationError):
            services.set_appointment_status(appointment.pk, "Completed")

    def test_pending_request_cannot_be_checked_in_before_acceptance(self):
        """Checking in is only offered once the request has been accepted."""
        appointment = self._book()

        with self.assertRaises(services.ValidationError):
            services.set_appointment_status(appointment.pk, "Checked In")


class PendingQueueServiceTests(PendingLifecycleTestCase):
    """The review queue lists exactly the requests nobody has decided on."""

    def test_list_pending_returns_only_waiting_requests(self):
        waiting = self._book(first="Sam", last="Waiting", slot=dt_time(9, 0))
        decided = self._book(first="Alex", last="Decided", slot=dt_time(10, 0))
        services.accept_appointment(decided.pk)
        declined = self._book(first="Jo", last="Declined", slot=dt_time(11, 0))
        services.cancel_appointment(declined.pk, reason="Declined")

        pending = services.list_pending_appointments()

        self.assertEqual([a.pk for a in pending], [waiting.pk])

    def test_list_pending_is_soonest_first(self):
        later = self._book(first="Sam", last="Later", slot=dt_time(15, 0))
        earlier = self._book(first="Alex", last="Earlier", slot=dt_time(9, 0))

        pending = services.list_pending_appointments()

        self.assertEqual([a.pk for a in pending], [earlier.pk, later.pk])

    def test_list_pending_is_empty_when_nothing_awaits_review(self):
        appointment = self._book()
        services.accept_appointment(appointment.pk)

        self.assertEqual(services.list_pending_appointments(), [])


class HomeViewTests(PendingLifecycleTestCase):
    """The public landing page is the customer's way in."""

    def test_home_renders_for_an_anonymous_visitor(self):
        response = self.client.get(reverse("core:home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/home.html")
        self.assertIsNone(response.context["identity"])

    def test_home_offers_both_the_booking_form_and_staff_login(self):
        """One door for customers, one for staff — neither is hidden."""
        response = self.client.get(reverse("core:home"))

        self.assertContains(response, reverse("core:booking"))
        self.assertContains(response, reverse("core:login"))

    def test_home_lists_the_service_catalogue(self):
        response = self.client.get(reverse("core:home"))

        self.assertEqual(response.context["service_total"], 1)
        self.assertContains(response, self.service.ServiceName)

    def test_home_redirects_a_signed_in_user_to_the_dashboard(self):
        """Staff landing on ``/`` have nothing to book — send them in."""
        self.login_as(self.receptionist)

        response = self.client.get(reverse("core:home"))

        self.assertRedirects(response, reverse("core:dashboard"))

    def test_login_page_links_back_to_the_public_home(self):
        response = self.client.get(reverse("core:login"))

        self.assertContains(response, reverse("core:home"))

    def test_root_path_is_the_landing_page(self):
        self.assertEqual(reverse("core:home"), "/")
        self.assertEqual(reverse("core:login"), "/login/")


class PublicBookingPendingTests(PendingLifecycleTestCase):
    """What an online booking produces and what the customer is told."""

    def _post_booking(self, slot=None):
        return self.client.post(
            reverse("core:booking"),
            {
                "first_name": "Sam",
                "last_name": "Customer",
                "phone": "0400000000",
                "email": "sam@example.com",
                "service": self.service.Service_code,
                "car_model": "Toyota Corolla",
                "license_plate": "PEND123",
                "appointment_date": self.date.isoformat(),
                "appointment_time": (slot or self.slot).strftime("%H:%M"),
            },
            follow=True,
        )

    def test_online_booking_creates_a_pending_request(self):
        self._post_booking()

        appointment = Appointment.objects.get()
        self.assertEqual(appointment.Status, APPOINTMENT_PENDING)

    def test_success_page_tells_the_customer_the_request_is_pending(self):
        response = self._post_booking()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pending review")

    def test_booking_form_explains_that_acceptance_comes_later(self):
        response = self.client.get(reverse("core:booking"))

        self.assertContains(response, "pending")

    def test_online_booking_clash_is_reported_not_crashed(self):
        """A race for the same slot surfaces as a message, not a 500."""
        self._book(first="Alex", last="First")

        response = self._post_booking()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already booked")
        self.assertEqual(Appointment.objects.count(), 1)


class AcceptAppointmentViewTests(PendingLifecycleTestCase):
    """The HTTP contract of the accept action."""

    def setUp(self):
        super().setUp()
        self.appointment = self._book()
        self.accept_url = reverse(
            "core:appointment_accept", args=[self.appointment.pk]
        )

    def test_receptionist_accepts_from_the_detail_page(self):
        self.login_as(self.receptionist)

        response = self.client.post(self.accept_url)

        self.assertRedirects(
            response,
            reverse("core:appointment_detail", args=[self.appointment.pk]),
        )
        self.assertEqual(
            Appointment.objects.get(pk=self.appointment.pk).Status,
            APPOINTMENT_CONFIRMED,
        )

    def test_administrator_can_accept_too(self):
        self.login_as(self.admin)

        self.client.post(self.accept_url)

        self.assertEqual(
            Appointment.objects.get(pk=self.appointment.pk).Status,
            APPOINTMENT_CONFIRMED,
        )

    def test_accept_from_the_diary_returns_to_the_diary(self):
        """The hidden ``next`` field keeps staff working through the queue."""
        self.login_as(self.receptionist)

        response = self.client.post(
            self.accept_url, {"next": reverse("core:appointment_list")}
        )

        self.assertRedirects(response, reverse("core:appointment_list"))

    def test_accept_ignores_an_untrusted_next_target(self):
        """Only the diary is honoured, so ``next`` cannot redirect elsewhere."""
        self.login_as(self.receptionist)

        response = self.client.post(self.accept_url, {"next": "https://example.com"})

        self.assertRedirects(
            response,
            reverse("core:appointment_detail", args=[self.appointment.pk]),
        )

    def test_accept_get_is_not_allowed(self):
        """The action is state-changing, so it is POST-only."""
        self.login_as(self.receptionist)

        response = self.client.get(self.accept_url)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(
            Appointment.objects.get(pk=self.appointment.pk).Status,
            APPOINTMENT_PENDING,
        )

    def test_anonymous_visitor_is_sent_to_login(self):
        response = self.client.post(self.accept_url)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("core:login")))
        self.assertEqual(
            Appointment.objects.get(pk=self.appointment.pk).Status,
            APPOINTMENT_PENDING,
        )

    def test_technician_cannot_accept(self):
        staff, _ = self.make_technician()
        self.login_as(staff)

        response = self.client.post(self.accept_url, follow=True)

        self.assertRedirects(
            response,
            reverse("core:access_denied"),
            target_status_code=403,
        )
        self.assertEqual(
            Appointment.objects.get(pk=self.appointment.pk).Status,
            APPOINTMENT_PENDING,
        )

    def test_accepting_an_already_confirmed_appointment_shows_a_warning(self):
        """Refused by the service layer and reported, not silently accepted."""
        self.login_as(self.receptionist)
        services.accept_appointment(self.appointment.pk)

        response = self.client.post(self.accept_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already confirmed")

    def test_accept_missing_appointment_returns_to_the_diary(self):
        self.login_as(self.receptionist)
        missing_url = reverse("core:appointment_accept", args=[999999])

        response = self.client.post(missing_url)

        self.assertRedirects(response, reverse("core:appointment_list"))

    def test_accept_message_names_the_customer_and_slot(self):
        self.login_as(self.receptionist)

        response = self.client.post(self.accept_url, follow=True)

        self.assertContains(response, "Sam Customer")


class PendingQueueViewTests(PendingLifecycleTestCase):
    """The review queue and accept affordance on the staff screens."""

    def test_diary_lists_waiting_requests_with_an_accept_action(self):
        self.login_as(self.receptionist)
        appointment = self._book()

        response = self.client.get(reverse("core:appointment_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [a.pk for a in response.context["pending_appointments"]],
            [appointment.pk],
        )
        self.assertContains(response, "Awaiting acceptance")
        self.assertContains(
            response,
            reverse("core:appointment_accept", args=[appointment.pk]),
        )

    def test_diary_hides_the_queue_when_nothing_awaits_review(self):
        self.login_as(self.receptionist)
        appointment = self._book()
        services.accept_appointment(appointment.pk)

        response = self.client.get(reverse("core:appointment_list"))

        self.assertEqual(response.context["pending_appointments"], [])
        self.assertNotContains(response, "Awaiting acceptance")

    def test_diary_filters_to_pending_requests(self):
        self.login_as(self.receptionist)
        appointment = self._book()

        response = self.client.get(
            reverse("core:appointment_list") + "?status=Pending"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [a.pk for a in response.context["appointments"]], [appointment.pk]
        )

    def test_detail_page_offers_accept_for_a_pending_request(self):
        self.login_as(self.receptionist)
        appointment = self._book()

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertTrue(response.context["can_accept"])
        self.assertContains(response, "pending acceptance")
        self.assertContains(
            response,
            reverse("core:appointment_accept", args=[appointment.pk]),
        )

    def test_detail_page_withholds_accept_once_confirmed(self):
        self.login_as(self.receptionist)
        appointment = self._book()
        services.accept_appointment(appointment.pk)

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertFalse(response.context["can_accept"])
        self.assertNotContains(
            response,
            reverse("core:appointment_accept", args=[appointment.pk]),
        )

    def test_dashboard_shows_the_count_awaiting_acceptance(self):
        self.login_as(self.receptionist)
        self._book(first="Sam", last="One", slot=dt_time(9, 0))
        self._book(first="Alex", last="Two", slot=dt_time(10, 0))

        response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(response.context["appointment_pending"], 2)
        self.assertContains(response, "Awaiting acceptance")

    def test_receptionist_booking_at_the_counter_also_starts_pending(self):
        """Both routes into the diary land in the same review queue."""
        self.login_as(self.receptionist)

        response = self.client.post(
            reverse("core:appointment_create"),
            {
                "first_name": "Pat",
                "last_name": "Walker",
                "phone": "0400000001",
                "email": "pat@example.com",
                "service": self.service.Service_code,
                "car_model": "Toyota Corolla",
                "license_plate": "PENDK1",
                "appointment_date": self.date.isoformat(),
                "appointment_time": self.slot.strftime("%H:%M"),
            },
        )

        appointment = Appointment.objects.get()
        self.assertEqual(appointment.Status, APPOINTMENT_PENDING)
        self.assertRedirects(
            response,
            reverse("core:appointment_detail", args=[appointment.pk]),
        )

    def test_receptionist_is_told_the_new_booking_awaits_acceptance(self):
        self.login_as(self.receptionist)

        response = self.client.post(
            reverse("core:appointment_create"),
            {
                "first_name": "Pat",
                "last_name": "Walker",
                "phone": "0400000001",
                "email": "pat@example.com",
                "service": self.service.Service_code,
                "car_model": "Toyota Corolla",
                "license_plate": "PENDK2",
                "appointment_date": self.date.isoformat(),
                "appointment_time": self.slot.strftime("%H:%M"),
            },
            follow=True,
        )

        self.assertContains(response, "pending")


class _MigrationApps:
    """Minimal stand-in for the ``apps`` registry a data migration receives.

    Django hands a data migration an ``apps`` object whose ``get_model()``
    returns the historical model of that moment. Tests run against the live
    schema, so returning the real model is the closest equivalent — and it is
    what lets these tests drive the migration's own conversion functions rather
    than a copy of their logic.
    """

    @staticmethod
    def get_model(app_label, model_name):
        return Appointment


class LegacyBookedRowMigrationTests(PendingLifecycleTestCase):
    """Rows written before this change land in the review queue, not nowhere."""

    #: The migration module's name starts with a digit, so it cannot be written
    #: as an ``import`` statement; it has to be loaded by name instead.
    @staticmethod
    def _migration():
        return importlib.import_module(
            "core.migrations.booking_status"
        )

    def test_legacy_booked_row_is_folded_into_pending(self):
        booked_to_pending = self._migration().booked_to_pending
        appointment = self._book()
        # Simulate a row written before the status was renamed. The value goes
        # in directly because the application can no longer produce it.
        Appointment.objects.filter(pk=appointment.pk).update(Status="Booked")

        booked_to_pending(_MigrationApps(), None)

        self.assertEqual(
            Appointment.objects.get(pk=appointment.pk).Status,
            APPOINTMENT_PENDING,
        )

    def test_legacy_booked_row_joins_the_review_queue(self):
        """The converted row is picked up by the queue staff work from."""
        booked_to_pending = self._migration().booked_to_pending
        appointment = self._book()
        Appointment.objects.filter(pk=appointment.pk).update(Status="Booked")

        booked_to_pending(_MigrationApps(), None)

        self.assertEqual(
            [a.pk for a in services.list_pending_appointments()],
            [appointment.pk],
        )

    def test_forward_conversion_leaves_other_statuses_alone(self):
        """Only the obsolete value moves; confirmed bookings stay confirmed."""
        booked_to_pending = self._migration().booked_to_pending
        confirmed = self._book(first="Alex", last="Confirmed", slot=dt_time(9, 0))
        services.accept_appointment(confirmed.pk)

        booked_to_pending(_MigrationApps(), None)

        self.assertEqual(
            Appointment.objects.get(pk=confirmed.pk).Status,
            APPOINTMENT_CONFIRMED,
        )

    def test_reverse_moves_pending_back_to_booked(self):
        pending_to_booked = self._migration().pending_to_booked
        appointment = self._book()

        pending_to_booked(_MigrationApps(), None)

        self.assertEqual(
            Appointment.objects.get(pk=appointment.pk).Status, "Booked"
        )

    def test_migration_rebuilds_the_active_slot_constraint(self):
        """The constraint counts Pending among the slot-holding statuses."""
        migration = self._migration().Migration

        add_constraint = next(
            op for op in migration.operations
            if isinstance(op, migrations.AddConstraint)
        )
        condition_sql = str(add_constraint.constraint.condition)

        self.assertIn(APPOINTMENT_PENDING, condition_sql)
        self.assertNotIn("Booked", condition_sql)






