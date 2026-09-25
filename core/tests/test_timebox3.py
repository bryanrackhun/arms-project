"""Timebox 3: repair jobs, vehicle inspections and service bays (FR6-FR9).

The workshop story these tests pin down runs in one direction:

    appointment checked in -> repair job raised -> technician and bay assigned
    -> inspection recorded -> progress moved Not Started / In Progress /
    Completed, which releases the bay.

Each step is tested twice over. The service-layer tests assert the rule itself -
what is refused and why - because that is where the rule lives and every caller
goes through it. The view tests then assert the HTTP contract around it: which
role may reach each screen, where a POST lands, and what the technician's own
board is scoped to. Testing both layers matters here specifically because the
same rule is deliberately enforced in two places (form, service), and a test on
one alone would pass while the other drifted.

Bays get their own attention because ``ServiceBay.Status`` is *stored* rather
than derived, so it can drift if a code path forgets to update it. Every test
that changes what a bay holds therefore re-reads the bay from the database and
asserts on the stored value, not on an in-memory instance that the service
happened to mutate.
"""

import io
import tempfile
from datetime import time as dt_time, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.forms import HiddenInput
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone
from PIL import Image as PILImage

from core import services
from core.forms import ServiceBayForm
from core.models import (
    ACTIVE_REPAIR_STATUSES,
    APPOINTMENT_CHECKED_IN,
    BAY_AVAILABLE,
    BAY_OCCUPIED,
    MAX_INSPECTION_IMAGES,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_NOT_STARTED,
    REPAIR_PRIORITIES,
    REPAIR_PRIORITY_ROUTINE,
    REPAIR_PRIORITY_URGENT,
    REPAIR_STATUSES,
    REPAIR_TRANSITIONS,
    Inspection,
    Repair,
    ServiceBay,
)
from core.services import (
    AuthorizationError,
    DuplicateError,
    NotFoundError,
    ValidationError,
)

from .base import RoleTestCase


def make_png(name="photo.png", colour=(200, 30, 30)):
    """Build a tiny but genuinely valid PNG upload.

    Real bytes rather than a stub, so ``ImageField`` validation actually runs and
    a test asserting "the photograph was stored" is asserting something true.
    """
    buffer = io.BytesIO()
    PILImage.new("RGB", (4, 4), colour).save(buffer, format="PNG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/png")


class TempMediaMixin:
    """Point ``MEDIA_ROOT`` at a throwaway directory for one test.

    Inspection photographs are written to disk, so without this every image test
    would leave files behind in the project's real ``media/`` folder - and a
    second run could then behave differently from the first.
    """

    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        patcher = override_settings(MEDIA_ROOT=self._media.name)
        patcher.enable()
        self.addCleanup(patcher.disable)



class RepairWorkflowTestCase(RoleTestCase):
    """Fixtures for the whole Timebox 3 flow: people, a booking, a bay.

    The appointment starts life as a booking request and is walked through the
    real lifecycle (accept -> match vehicle -> check in) rather than being set to
    "Checked In" directly. Going through the same calls the UI uses means these
    tests fail if the handoff between Timebox 2 and Timebox 3 breaks, which is
    the seam most likely to regress.
    """

    def setUp(self):
        self.admin = self.make_admin()
        self.receptionist = self.make_staff(
            email="reception@test.local", role=self.receptionist_role,
            first_name="Rachel", last_name="Receptionist",
        )
        self.service = self.make_service()
        # Tomorrow keeps every slot in the future whenever the suite runs.
        self.date = dj_timezone.localdate() + timedelta(days=1)
        self.bay = services.create_service_bay("Bay 1")
        # Several tests need two appointments at once (a second technician's job,
        # a bay to move into). Slots are a uniqueness constraint, so each booking
        # takes the next half-hour rather than reusing one and colliding.
        # ``datetime.time`` cannot be added to a ``timedelta``, so the slots are
        # built from minutes-since-midnight inside opening hours (08:00-17:00).
        self._slots = [
            dt_time((480 + 30 * i) // 60, (480 + 30 * i) % 60) for i in range(19)
        ]
        self._slot_index = 0
        # A single counter behind every "give me a unique thing" helper, so a
        # test can ask for two vehicles, or two technicians, or both, without any
        # of them colliding on a unique column.
        self._index = 0

    def next_index(self) -> int:
        """The next unused counter value, for unique names/plates/emails."""
        self._index += 1
        return self._index

    # ---- booking helpers -------------------------------------------------
    def next_slot(self):
        """The next unused bookable slot (08:00-17:00, half-hourly)."""
        slot = self._slots[self._slot_index % len(self._slots)]
        self._slot_index += 1
        return slot

    def book(self, *, first="Sam", last="Customer", slot=None):
        """Book through the real service call the public form uses."""
        return services.book_appointment(
            first, last, self.service.Service_code, self.date,
            slot or self.next_slot(), phone="0400000000",
            email="sam@example.com",
        )

    def vehicle(self, *, plate="", vin="", suffix=None):
        """A distinct vehicle record.

        Plates and VINs are both unique, so each call allocates its own suffix by
        default; tests that need a second car get one without having to think
        about it, instead of tripping the duplicate check that check-in exists to
        enforce. The VIN is padded to exactly 17 characters because that is what
        ``VIN_PATTERN`` requires - a wrong length would fail for a reason that has
        nothing to do with what the test is checking.
        """
        if suffix is None:
            suffix = self.next_index()
        return services.create_vehicle(
            "Toyota", "Corolla",
            vin=vin or f"TB3VN{suffix:011d}1",
            license_plate=plate or f"TB3-{suffix:04d}",
            year=2018, colour="Silver",
        )

    def checked_in_appointment(self, *, slot=None, first="Sam", last="Customer",
                               plate="", vin="", suffix=None, with_vehicle=True):
        """Walk a booking to Checked In, optionally linking a vehicle.

        ``with_vehicle=False`` gives the one state that is checked in but not
        repair-ready, which the creation gate has to refuse.
        """
        appointment = self.book(first=first, last=last, slot=slot)
        services.accept_appointment(appointment.pk)
        if with_vehicle:
            car = self.vehicle(plate=plate, vin=vin, suffix=suffix)
            services.match_vehicle_to_appointment(appointment.pk, car.VehicleID)
        return services.set_appointment_status(
            appointment.pk, APPOINTMENT_CHECKED_IN
        )

    # ---- workshop helpers ------------------------------------------------
    def qualified_technician(self, *, email="", rate="35.00"):
        """A technician holding a ``Technician_Service`` record for the service.

        Qualification is the FR6 gate on assignment, so most tests need a
        technician who genuinely has it rather than one who merely exists. The
        email is allocated per call because it is unique, and a test may need two
        qualified technicians (one to swap a job onto). ``RoleTestCase`` fixes the
        names itself, so they are not passed through here.
        """
        email = email or f"tech{self.next_index()}@test.local"
        staff, technician = self.make_technician(email=email, rate=rate)
        services.assign_service(technician.pk, self.service.Service_code)
        return staff, technician

    def unqualified_technician(self, *, email=""):
        """A technician with a record but no qualification for this service."""
        email = email or f"other{self.next_index()}@test.local"
        return self.make_technician(email=email)

    def repair(self, **kwargs):
        """Create a repair job for a freshly checked-in appointment."""
        appointment = kwargs.pop("appointment", None) or self.checked_in_appointment()
        return services.create_repair(appointment.pk, **kwargs)

    def assigned_repair(self, **kwargs):
        """A repair job with both a qualified technician and a bay on it."""
        appointment = kwargs.pop("appointment", None) or self.checked_in_appointment()
        pair = kwargs.pop("technician", None) or self.qualified_technician()
        tech_staff, technician = pair
        bay = kwargs.pop("bay", None) or self.bay
        repair = services.create_repair(
            appointment.pk, technician_id=technician.pk, bay_id=bay.pk, **kwargs
        )
        return repair, tech_staff, technician, bay


# ---------------------------------------------------------------------------
# Repair job creation (FR6)
# ---------------------------------------------------------------------------
class RepairCreationServiceTests(RepairWorkflowTestCase):
    """A job may only be raised for a checked-in car that is on site."""

    def test_checked_in_appointment_with_vehicle_can_open_a_job(self):
        """The happy path, and the one the whole timebox hangs off."""
        appointment = self.checked_in_appointment()

        repair = services.create_repair(appointment.pk)

        self.assertIsNotNone(repair.pk)
        self.assertEqual(repair.AppointmentID_id, appointment.pk)
        # A new job has not been started, whatever the appointment's own status.
        self.assertEqual(repair.Status, REPAIR_NOT_STARTED)
        self.assertEqual(repair.RepairPriority, REPAIR_PRIORITY_ROUTINE)
        # Both halves of the assignment are optional at creation.
        self.assertIsNone(repair.TechnicianID_id)
        self.assertIsNone(repair.BayID_id)

    def test_created_repair_is_persisted_and_reachable(self):
        """Re-read from the database, and findable via its appointment."""
        appointment = self.checked_in_appointment()
        created = services.create_repair(appointment.pk)

        stored = Repair.objects.get(pk=created.pk)
        found = services.get_repair_for_appointment(appointment.pk)

        self.assertEqual(stored.Status, REPAIR_NOT_STARTED)
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, created.pk)

    def test_job_carries_the_appointments_service_and_vehicle(self):
        """The job reads its facts through the appointment, so nothing is copied."""
        appointment = self.checked_in_appointment()
        repair = services.create_repair(appointment.pk)

        self.assertEqual(repair.service.pk, self.service.pk)
        self.assertEqual(repair.vehicle.pk, appointment.VehicleID_id)
        self.assertEqual(repair.customer_name, "Sam Customer")

    def test_urgent_priority_is_stored(self):
        """Priority is set once, at creation, and is one of the two choices."""
        appointment = self.checked_in_appointment()

        repair = services.create_repair(
            appointment.pk, priority=REPAIR_PRIORITY_URGENT
        )

        self.assertEqual(repair.RepairPriority, REPAIR_PRIORITY_URGENT)

    def test_pending_booking_cannot_open_a_job(self):
        """A request nobody has accepted yet has no car at the workshop."""
        appointment = self.book()

        with self.assertRaises(ValidationError):
            services.create_repair(appointment.pk)
        self.assertFalse(Repair.objects.exists())

    def test_confirmed_booking_cannot_open_a_job(self):
        """Confirming reserves the slot; it is not the same as the car arriving."""
        appointment = self.book()
        services.accept_appointment(appointment.pk)

        with self.assertRaises(ValidationError):
            services.create_repair(appointment.pk)
        self.assertFalse(Repair.objects.exists())

    def test_cancelled_appointment_cannot_open_a_job(self):
        appointment = self.book()
        services.cancel_appointment(appointment.pk, reason="Customer called")

        with self.assertRaises(ValidationError):
            services.create_repair(appointment.pk)
        self.assertFalse(Repair.objects.exists())

    def test_checked_in_without_a_linked_vehicle_cannot_open_a_job(self):
        """Check-in alone is not enough: there must be a car to work on.

        This is the case the appointment page has to hide its button for, so the
        rule is worth pinning on its own rather than only as part of eligibility.
        """
        appointment = self.checked_in_appointment(with_vehicle=False)

        with self.assertRaises(ValidationError):
            services.create_repair(appointment.pk)
        self.assertFalse(Repair.objects.exists())


    def test_second_job_for_the_same_appointment_is_refused(self):
        """One job per appointment, so a double-click cannot raise two."""
        appointment = self.checked_in_appointment()
        first = services.create_repair(appointment.pk)

        with self.assertRaises(DuplicateError):
            services.create_repair(appointment.pk)

        self.assertEqual(Repair.objects.count(), 1)
        self.assertEqual(
            services.get_repair_for_appointment(appointment.pk).pk, first.pk
        )

    def test_uniqueness_is_also_enforced_by_the_database(self):
        """The application check is a courtesy; the constraint is the guarantee.

        Asserting the constraint directly proves that a code path which forgets
        the duplicate check still cannot create two jobs for one appointment.
        """
        appointment = self.checked_in_appointment()
        services.create_repair(appointment.pk)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Repair.objects.create(
                    AppointmentID=appointment,
                    RepairPriority=REPAIR_PRIORITY_URGENT,
                    Status=REPAIR_NOT_STARTED,
                )

    def test_unknown_appointment_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            services.create_repair(999999)

    def test_invalid_priority_is_refused(self):
        """Priority is a closed choice, not free text."""
        appointment = self.checked_in_appointment()

        with self.assertRaises(ValidationError):
            services.create_repair(appointment.pk, priority="Whenever")
        self.assertFalse(Repair.objects.exists())

    def test_eligible_list_excludes_bookings_that_cannot_open_a_job(self):
        """The creation dropdown's source cannot offer a job that would fail."""
        eligible = self.checked_in_appointment()
        pending = self.book()
        confirmed_only = self.book()
        services.accept_appointment(confirmed_only.pk)
        checked_in_no_vehicle = self.checked_in_appointment(with_vehicle=False)

        listed_ids = {
            a.pk for a in services.list_appointments_eligible_for_repair()
        }

        self.assertIn(eligible.pk, listed_ids)
        for appointment in (pending, confirmed_only, checked_in_no_vehicle):
            self.assertNotIn(appointment.pk, listed_ids)

    def test_eligible_list_drops_an_appointment_once_it_has_a_job(self):
        """Otherwise the dropdown would offer the one thing that must be refused."""
        appointment = self.checked_in_appointment()
        self.assertIn(
            appointment.pk,
            {a.pk for a in services.list_appointments_eligible_for_repair()},
        )

        services.create_repair(appointment.pk)

        self.assertNotIn(
            appointment.pk,
            {a.pk for a in services.list_appointments_eligible_for_repair()},
        )

    def test_get_repair_for_appointment_without_one_returns_none(self):
        """A missing job is a normal state, not an error to raise."""
        appointment = self.checked_in_appointment()

        self.assertIsNone(services.get_repair_for_appointment(appointment.pk))

    def test_get_repair_unknown_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            services.get_repair(999999)


# ---------------------------------------------------------------------------
# Technician and bay assignment (FR6)
# ---------------------------------------------------------------------------
class RepairAssignmentServiceTests(RepairWorkflowTestCase):
    """Who may be put on a job, and what that does to the bay it sits in."""

    def test_qualified_technician_can_be_assigned_at_creation(self):
        _, technician = self.qualified_technician()
        appointment = self.checked_in_appointment()

        repair = services.create_repair(
            appointment.pk, technician_id=technician.pk
        )

        self.assertEqual(repair.TechnicianID_id, technician.pk)

    def test_unqualified_technician_is_refused_at_creation(self):
        """The FR6 qualification gate: free is not the same as qualified."""
        _, technician = self.unqualified_technician()
        appointment = self.checked_in_appointment()

        with self.assertRaises(ValidationError):
            services.create_repair(appointment.pk, technician_id=technician.pk)

        # Nothing was written, so the job cannot be half-created.
        self.assertFalse(Repair.objects.exists())

    def test_unqualified_technician_is_refused_when_assigning_later(self):
        """The same gate applies to the detail page's assignment form."""
        repair = self.repair()
        _, technician = self.unqualified_technician()

        with self.assertRaises(ValidationError):
            services.assign_technician(repair.pk, technician.pk)

        self.assertIsNone(Repair.objects.get(pk=repair.pk).TechnicianID_id)

    def test_qualified_technician_can_be_assigned_after_creation(self):
        """A job is raised on arrival and staffed once someone is confirmed free."""
        repair = self.repair()
        _, technician = self.qualified_technician()

        services.assign_technician(repair.pk, technician.pk)

        self.assertEqual(
            Repair.objects.get(pk=repair.pk).TechnicianID_id, technician.pk
        )

    def test_reassigning_the_same_technician_is_a_no_op(self):
        """Not an error: the form's "leave unchanged" posts the current value."""
        repair, _staff, technician, _bay = self.assigned_repair()

        result = services.assign_technician(repair.pk, technician.pk)

        self.assertEqual(result.TechnicianID_id, technician.pk)

    def test_unknown_technician_raises_not_found(self):
        repair = self.repair()

        with self.assertRaises(NotFoundError):
            services.assign_technician(repair.pk, 999999)

    def test_bay_becomes_occupied_when_assigned_at_creation(self):
        """Stored status, re-read from the database - not the instance handed back."""
        appointment = self.checked_in_appointment()
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_AVAILABLE
        )

        services.create_repair(appointment.pk, bay_id=self.bay.pk)

        bay = ServiceBay.objects.get(pk=self.bay.pk)
        self.assertEqual(bay.Status, BAY_OCCUPIED)
        self.assertFalse(bay.is_available)

    def test_bay_becomes_occupied_when_assigned_later(self):
        repair = self.repair()

        services.assign_bay(repair.pk, self.bay.pk)

        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED
        )

    def test_occupied_bay_cannot_hold_a_second_job(self):
        """Two cars cannot physically be in one bay."""
        first = self.repair()
        services.assign_bay(first.pk, self.bay.pk)
        second_appointment = self.checked_in_appointment()

        with self.assertRaises(ValidationError):
            services.create_repair(second_appointment.pk, bay_id=self.bay.pk)

        second = self.repair()
        with self.assertRaises(ValidationError):
            services.assign_bay(second.pk, self.bay.pk)

    def test_occupied_bays_are_left_out_of_the_available_list(self):
        """The dropdown's source never offers a bay that is already taken."""
        repair = self.repair()
        services.assign_bay(repair.pk, self.bay.pk)
        services.create_service_bay("Bay 2")

        available = services.list_available_bays()

        self.assertEqual([bay.BayName for bay in available], ["Bay 2"])

    def test_moving_a_job_to_another_bay_releases_the_old_one(self):
        """Otherwise a bay stays Occupied forever once a job leaves it."""
        second_bay = services.create_service_bay("Bay 2")
        repair = self.repair()
        services.assign_bay(repair.pk, self.bay.pk)

        services.assign_bay(repair.pk, second_bay.pk)

        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_AVAILABLE
        )
        self.assertEqual(
            ServiceBay.objects.get(pk=second_bay.pk).Status, BAY_OCCUPIED
        )

    def test_reassigning_the_same_bay_keeps_it_occupied(self):
        """The exclusion for "no change" must not release the bay it still holds."""
        repair = self.repair()
        services.assign_bay(repair.pk, self.bay.pk)

        services.assign_bay(repair.pk, self.bay.pk)

        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED
        )

    def test_both_halves_can_be_assigned_in_one_call(self):
        """The detail page's assignment form posts technician and bay together."""
        repair = self.repair()
        _, technician = self.qualified_technician()

        services.update_repair_assignment(
            repair.pk, technician_id=technician.pk, bay_id=self.bay.pk
        )

        stored = Repair.objects.get(pk=repair.pk)
        self.assertEqual(stored.TechnicianID_id, technician.pk)
        self.assertEqual(stored.BayID_id, self.bay.pk)
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED
        )

    def test_update_assignment_leaves_a_half_alone_when_not_given(self):
        """``None`` means "untouched", not "clear it"."""
        repair, _staff, technician, _bay = self.assigned_repair()
        other_bay = services.create_service_bay("Bay 2")

        services.update_repair_assignment(repair.pk, bay_id=other_bay.pk)

        stored = Repair.objects.get(pk=repair.pk)
        self.assertEqual(stored.TechnicianID_id, technician.pk)
        self.assertEqual(stored.BayID_id, other_bay.pk)

    def test_unknown_bay_raises_not_found(self):
        repair = self.repair()

        with self.assertRaises(NotFoundError):
            services.assign_bay(repair.pk, 999999)


# ---------------------------------------------------------------------------
# Repair progress (FR8)
# ---------------------------------------------------------------------------
class RepairProgressServiceTests(RepairWorkflowTestCase):
    """The lifecycle moves forward one step at a time, and never backwards."""

    def test_a_new_job_starts_at_not_started(self):
        repair = self.repair()

        self.assertEqual(repair.Status, REPAIR_NOT_STARTED)
        self.assertTrue(repair.is_active)
        self.assertFalse(repair.is_completed)

    def test_not_started_can_move_to_in_progress(self):
        repair = self.repair()

        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)

        self.assertEqual(
            Repair.objects.get(pk=repair.pk).Status, REPAIR_IN_PROGRESS
        )

    def test_in_progress_can_move_to_completed(self):
        repair = self.repair()
        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)

        services.set_repair_status(repair.pk, REPAIR_COMPLETED)

        stored = Repair.objects.get(pk=repair.pk)
        self.assertEqual(stored.Status, REPAIR_COMPLETED)
        self.assertTrue(stored.is_completed)
        self.assertFalse(stored.is_active)

    def test_a_job_cannot_skip_straight_to_completed(self):
        """The step the transition table exists to prevent."""
        repair = self.repair()

        with self.assertRaises(ValidationError):
            services.set_repair_status(repair.pk, REPAIR_COMPLETED)

        self.assertEqual(
            Repair.objects.get(pk=repair.pk).Status, REPAIR_NOT_STARTED
        )

    def test_a_completed_job_cannot_be_reopened(self):
        """Completed is terminal: Timebox 4 invoices and pays against it."""
        repair = self.repair()
        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)
        services.set_repair_status(repair.pk, REPAIR_COMPLETED)

        for status in (REPAIR_NOT_STARTED, REPAIR_IN_PROGRESS):
            with self.subTest(status=status):
                with self.assertRaises(ValidationError):
                    services.set_repair_status(repair.pk, status)

        self.assertEqual(Repair.objects.get(pk=repair.pk).Status, REPAIR_COMPLETED)

    def test_repeating_the_current_status_is_refused(self):
        """A no-op that would look like progress is refused, not silently accepted."""
        repair = self.repair()

        with self.assertRaises(ValidationError):
            services.set_repair_status(repair.pk, REPAIR_NOT_STARTED)

    def test_an_unknown_status_is_refused(self):
        repair = self.repair()

        with self.assertRaises(ValidationError):
            services.set_repair_status(repair.pk, "On Hold")


    def test_completing_a_job_releases_its_bay(self):
        """The automatic release: no separate step, so a bay cannot be forgotten.

        Asserted on a re-read of the bay, because ``Status`` is a stored column
        and it is the stored value the next job assignment will check.
        """
        repair, _staff, _technician, bay = self.assigned_repair()
        self.assertEqual(ServiceBay.objects.get(pk=bay.pk).Status, BAY_OCCUPIED)
        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)

        services.set_repair_status(repair.pk, REPAIR_COMPLETED)

        released = ServiceBay.objects.get(pk=bay.pk)
        self.assertEqual(released.Status, BAY_AVAILABLE)
        self.assertTrue(released.is_available)
        self.assertIn(released, services.list_available_bays())

    def test_starting_a_job_does_not_release_its_bay(self):
        """Only completion frees a bay; work under way still occupies it."""
        repair, _staff, _technician, bay = self.assigned_repair()

        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)

        self.assertEqual(
            ServiceBay.objects.get(pk=bay.pk).Status, BAY_OCCUPIED
        )

    def test_a_freed_bay_can_take_the_next_job(self):
        """The point of releasing it: the workshop can keep booking cars in."""
        first, _staff, _tech, bay = self.assigned_repair()
        services.set_repair_status(first.pk, REPAIR_IN_PROGRESS)
        services.set_repair_status(first.pk, REPAIR_COMPLETED)

        second = self.repair()
        services.assign_bay(second.pk, bay.pk)

        self.assertEqual(Repair.objects.get(pk=second.pk).BayID_id, bay.pk)
        self.assertEqual(
            ServiceBay.objects.get(pk=bay.pk).Status, BAY_OCCUPIED
        )

    def test_a_completed_job_cannot_be_reassigned(self):
        """Its technician and bay are the payroll record, so they are frozen."""
        repair, _staff, assigned_tech, assigned_bay = self.assigned_repair()
        _staff2, technician2 = self.qualified_technician()
        bay2 = services.create_service_bay("Bay 2")
        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)
        services.set_repair_status(repair.pk, REPAIR_COMPLETED)

        with self.assertRaises(ValidationError):
            services.assign_technician(repair.pk, technician2.pk)
        with self.assertRaises(ValidationError):
            services.assign_bay(repair.pk, bay2.pk)

        stored = Repair.objects.get(pk=repair.pk)
        self.assertEqual(stored.TechnicianID_id, assigned_tech.pk)
        self.assertEqual(stored.BayID_id, assigned_bay.pk)

    def test_unknown_repair_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            services.set_repair_status(999999, REPAIR_IN_PROGRESS)

    def test_transition_table_covers_every_status_exactly_once(self):
        """Guard the table itself, since every rule above reads from it.

        A status missing from ``REPAIR_TRANSITIONS`` would be silently
        un-progressable; an extra one would offer a move the UI cannot render.
        """
        statuses = {value for value, _label in REPAIR_STATUSES}

        self.assertEqual(set(REPAIR_TRANSITIONS), statuses)
        for current, allowed in REPAIR_TRANSITIONS.items():
            self.assertTrue(set(allowed) <= statuses, current)
        self.assertEqual(REPAIR_TRANSITIONS[REPAIR_COMPLETED], [])
        self.assertEqual(
            ACTIVE_REPAIR_STATUSES, [REPAIR_NOT_STARTED, REPAIR_IN_PROGRESS]
        )


# ---------------------------------------------------------------------------
# Vehicle inspection (FR7)
# ---------------------------------------------------------------------------
class InspectionServiceTests(TempMediaMixin, RepairWorkflowTestCase):
    """The assigned technician's record of the car, with a hard three-photo limit.

    ``TempMediaMixin`` comes first so its ``setUp`` runs before the fixtures and
    photographs land in a throwaway directory rather than the project's ``media/``.
    """

    def setUp(self):
        super().setUp()
        # Most inspection tests need a job that is already staffed, because an
        # inspection cannot be recorded for a job nobody owns.
        self.job, self.tech_staff, self.technician, self.bay = (
            self.assigned_repair()
        )

    def test_assigned_technician_can_record_an_inspection(self):
        repair, inspection = services.create_inspection(
            self.job.pk, staff=self.tech_staff,
            description="Scratch on the near-side rear door.",
        )

        self.assertEqual(inspection.Description, "Scratch on the near-side rear door.")
        self.assertEqual(inspection.VehicleID_id, self.job.AppointmentID.VehicleID_id)
        # Linked back onto the job, which is how the detail screen finds it.
        self.assertEqual(Repair.objects.get(pk=self.job.pk).InspectionID_id,
                         inspection.pk)
        self.assertTrue(repair.is_inspected)

    def test_administrator_can_record_an_inspection(self):
        """An administrator may act on any job, to cover an absent technician."""
        _repair, inspection = services.create_inspection(
            self.job.pk, staff=self.admin, description="Admin recorded on arrival.",
        )

        self.assertEqual(inspection.Description, "Admin recorded on arrival.")

    def test_unassigned_technician_is_refused(self):
        """The scoping rule: a technician works only on jobs assigned to them."""
        other_staff, _other = self.qualified_technician()

        with self.assertRaises(AuthorizationError):
            services.create_inspection(
                self.job.pk, staff=other_staff, description="Not my job.",
            )
        self.assertFalse(Inspection.objects.exists())

    def test_receptionist_is_refused(self):
        """The booking desk's part of the story ended at check-in."""
        with self.assertRaises(AuthorizationError):
            services.create_inspection(
                self.job.pk, staff=self.receptionist, description="Counter note.",
            )
        self.assertFalse(Inspection.objects.exists())

    def test_a_job_with_no_technician_cannot_be_inspected(self):
        """Nobody owns it yet, so there is no technician to document the car."""
        unstaffed = self.repair()

        with self.assertRaises(ValidationError):
            services.create_inspection(
                unstaffed.pk, staff=self.admin, description="No owner.",
            )
        self.assertFalse(Inspection.objects.exists())

    def test_a_blank_description_is_refused(self):
        with self.assertRaises(ValidationError):
            services.create_inspection(
                self.job.pk, staff=self.tech_staff, description="   ",
            )
        self.assertFalse(Inspection.objects.exists())

    def test_a_second_inspection_for_one_job_is_refused(self):
        """One record per job; amending it goes through ``update_inspection``."""
        services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="First note.",
        )

        with self.assertRaises(DuplicateError):
            services.create_inspection(
                self.job.pk, staff=self.tech_staff, description="Second note.",
            )

        self.assertEqual(Inspection.objects.count(), 1)

    def test_up_to_three_photographs_are_stored(self):
        """The schema's three columns, filled in upload order."""
        _repair, inspection = services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="Three photos.",
            images=[make_png("a.png"), make_png("b.png"), make_png("c.png")],
        )

        self.assertEqual(inspection.image_count, MAX_INSPECTION_IMAGES)
        self.assertFalse(inspection.has_free_image_slot)
        # Each file really reached the (temporary) media directory.
        for image in inspection.images:
            self.assertTrue(image.storage.exists(image.name))

    def test_a_fourth_photograph_is_refused(self):
        """Over the limit is an error, not a silent drop of the extra one.

        A technician who uploads four and sees three stored would reasonably
        assume the fourth was saved too, so the whole submission is rejected.
        """
        with self.assertRaises(ValidationError):
            services.create_inspection(
                self.job.pk, staff=self.tech_staff, description="Four photos.",
                images=[make_png(f"{i}.png") for i in range(4)],
            )
        self.assertFalse(Inspection.objects.exists())

    def test_an_inspection_can_be_recorded_with_no_photographs(self):
        """Images are optional; the written description is the record."""
        _repair, inspection = services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="No photos needed.",
        )

        self.assertEqual(inspection.image_count, 0)
        self.assertTrue(inspection.has_free_image_slot)

    def test_amending_adds_only_into_the_slots_still_free(self):
        """An edit must not discard the photographs already stored."""
        _repair, inspection = services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="One photo.",
            images=[make_png("kept.png")],
        )
        first_name = inspection.Image1.name

        _repair, updated = services.update_inspection(
            self.job.pk, staff=self.tech_staff, images=[make_png("added.png")],
        )

        self.assertEqual(updated.Image1.name, first_name)
        self.assertIsNotNone(updated.Image2)
        self.assertEqual(updated.image_count, 2)

    def test_amending_keeps_the_description_when_left_blank(self):
        """Blank means "keep what is there", so a photo edit cannot wipe the note."""
        _repair, inspection = services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="Original note.",
        )

        _repair, updated = services.update_inspection(
            self.job.pk, staff=self.tech_staff, description="",
        )

        self.assertEqual(updated.Description, "Original note.")
        self.assertEqual(Inspection.objects.get(pk=inspection.pk).Description,
                         "Original note.")

    def test_amending_cannot_exceed_the_three_image_ceiling(self):
        """The count is stored plus uploaded, so the ceiling holds across edits."""
        services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="Two photos.",
            images=[make_png("a.png"), make_png("b.png")],
        )

        with self.assertRaises(ValidationError):
            services.update_inspection(
                self.job.pk, staff=self.tech_staff,
                images=[make_png("c.png"), make_png("d.png")],
            )

        # Re-read the job: ``self.job`` was fetched before the inspection was
        # linked, so its cached ``InspectionID_id`` is still None.
        stored_repair = Repair.objects.get(pk=self.job.pk)
        self.assertEqual(
            Inspection.objects.get(pk=stored_repair.InspectionID_id).image_count, 2
        )

    def test_amending_without_an_inspection_raises_not_found(self):
        """There is nothing to amend, which is different from "not allowed"."""
        unstaffed = self.repair()
        services.assign_technician(unstaffed.pk, self.technician.pk)

        with self.assertRaises(NotFoundError):
            services.update_inspection(
                unstaffed.pk, staff=self.tech_staff, description="Nothing to amend.",
            )

    def test_an_unassigned_technician_cannot_amend_someone_elses_inspection(self):
        services.create_inspection(
            self.job.pk, staff=self.tech_staff, description="Tom's note.",
        )
        other_staff, _other = self.qualified_technician()

        with self.assertRaises(AuthorizationError):
            services.update_inspection(
                self.job.pk, staff=other_staff, description="Overwritten.",
            )

        # Re-read the job, whose cached ``InspectionID_id`` predates the link.
        stored_repair = Repair.objects.get(pk=self.job.pk)
        self.assertEqual(
            Inspection.objects.get(pk=stored_repair.InspectionID_id).Description,
            "Tom's note.",
        )

    def test_unknown_repair_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            services.create_inspection(
                999999, staff=self.tech_staff, description="No such job.",
            )


# ---------------------------------------------------------------------------
# View layer: who may reach each screen (FR6-FR9)
# ---------------------------------------------------------------------------
class RepairScreenAccessTests(RepairWorkflowTestCase):
    """Role-based access, asserted through the real HTTP responses.

    The service layer holds the business rules; the decorators hold who may reach
    a screen at all. Both need testing, because a rule enforced only in the
    service layer is bypassed by any caller that forgets to go through it, and a
    rule enforced only by a decorator is bypassed by any POST that skips the form.
    """

    def setUp(self):
        super().setUp()
        self.job, self.tech_staff, self.technician, _bay = self.assigned_repair()
        self.other_tech_staff, _other = self.qualified_technician()

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

    def test_administrator_reaches_every_repair_screen(self):
        self.login_as(self.admin)

        for url in (
            reverse("core:dashboard"),
            reverse("core:repair_list"),
            reverse("core:repair_create"),
            reverse("core:repair_detail", args=[self.job.pk]),
            reverse("core:bay_list"),
        ):
            self.assert_ok(url)

    def test_receptionist_reads_jobs_and_bays_but_cannot_create_one(self):
        """Counter staff can answer "how is my car coming along?" - and no more."""
        self.login_as(self.receptionist)

        for url in (
            reverse("core:repair_list"),
            reverse("core:repair_detail", args=[self.job.pk]),
        ):
            self.assert_ok(url)

        for url in (
            reverse("core:repair_create"),
            reverse("core:bay_list"),
            reverse("core:my_repairs"),
        ):
            self.assert_denied(url)

    def test_receptionist_detail_page_is_read_only(self):
        """No status or assignment control is rendered for a read-only viewer."""
        self.login_as(self.receptionist)

        response = self.client.get(
            reverse("core:repair_detail", args=[self.job.pk])
        )

        self.assertFalse(response.context["can_manage"])
        self.assertFalse(response.context["is_admin"])
        self.assertNotContains(response, reverse("core:repair_assign",
                                                args=[self.job.pk]))

    def test_technician_is_redirected_from_the_full_board_to_their_own(self):
        """The scoping rule is a redirect, not a filtered-down full list."""
        self.login_as(self.tech_staff)

        response = self.client.get(reverse("core:repair_list"))

        self.assertRedirects(response, reverse("core:my_repairs"))

    def test_assigned_technician_may_open_their_own_job(self):
        self.login_as(self.tech_staff)

        response = self.client.get(
            reverse("core:repair_detail", args=[self.job.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_manage"])
        # Re-assignment is an Administrator function, not a technician one.
        self.assertFalse(response.context["is_admin"])
        self.assertNotIn("assignment_form", response.context)

    def test_unassigned_technician_cannot_open_someone_elses_job(self):
        self.login_as(self.other_tech_staff)

        self.assert_denied(reverse("core:repair_detail", args=[self.job.pk]))

    def test_technician_cannot_reach_administrator_screens(self):
        self.login_as(self.tech_staff)

        for url in (
            reverse("core:repair_create"),
            reverse("core:bay_list"),
            reverse("core:appointment_list"),
        ):
            self.assert_denied(url)

    def test_anonymous_visitor_is_sent_to_login(self):
        """Unauthenticated access redirects to login, keeping the target to
        return to afterwards - so a signed-out technician is not left stranded."""
        response = self.client.get(reverse("core:repair_list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("core:login")))
        self.assertIn("next=", response.url)


# ---------------------------------------------------------------------------
# My repairs: a technician's board is scoped to their own jobs (FR8)
# ---------------------------------------------------------------------------
class MyRepairsScopingTests(RepairWorkflowTestCase):
    """A technician sees exactly their own jobs, and never anyone else's.

    Scoping is tested with a second technician holding a second job, because an
    empty board and a board containing only your own work look identical when
    there is nothing else in the database to be excluded.
    """

    def setUp(self):
        super().setUp()
        self.mine, self.my_staff, self.my_tech, _bay = self.assigned_repair()
        # A second technician with their own checked-in car and their own job, in
        # a second bay - the first is Occupied by now, and an Occupied bay cannot
        # take another job.
        self.bay2 = services.create_service_bay("Bay 2")
        self.theirs, self.other_staff, self.other_tech, _bay2 = (
            self.assigned_repair(bay=self.bay2)
        )

    def test_board_lists_only_the_signed_in_technicians_jobs(self):
        self.login_as(self.my_staff)

        response = self.client.get(reverse("core:my_repairs"))

        self.assertEqual(response.status_code, 200)
        listed = [repair.pk for repair in response.context["repairs"]]
        self.assertEqual(listed, [self.mine.pk])
        self.assertNotIn(self.theirs.pk, listed)

    def test_the_other_technician_sees_their_own_job_instead(self):
        """Proves the scoping follows the signed-in identity, not the URL."""
        self.login_as(self.other_staff)

        response = self.client.get(reverse("core:my_repairs"))

        self.assertEqual(
            [repair.pk for repair in response.context["repairs"]],
            [self.theirs.pk],
        )

    def test_counts_describe_only_their_own_work(self):
        self.login_as(self.my_staff)
        services.set_repair_status(self.mine.pk, REPAIR_IN_PROGRESS)

        response = self.client.get(reverse("core:my_repairs"))

        self.assertEqual(response.context["in_progress"], 1)
        self.assertEqual(response.context["not_started"], 0)
        self.assertEqual(response.context["completed"], 0)

    def test_an_empty_board_is_rendered_rather_than_erroring(self):
        """A technician with nothing assigned still gets a usable page."""
        idle_staff, _idle = self.qualified_technician()
        self.login_as(idle_staff)

        response = self.client.get(reverse("core:my_repairs"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["repairs"], [])

    def test_a_technician_role_account_without_a_record_is_redirected(self):
        """The error is stated, rather than showing a board that implies
        everything is finished when in fact no jobs could ever be assigned."""
        staff = self.make_staff(
            email="norecord@test.local", role=self.technician_role,
            first_name="Nick", last_name="NoRecord",
        )
        self.login_as(staff)

        response = self.client.get(reverse("core:my_repairs"))

        self.assertRedirects(response, reverse("core:profile"))

    def test_an_administrator_cannot_open_the_technician_board(self):
        """The board is a technician screen; an administrator uses the full list."""
        self.login_as(self.admin)

        response = self.client.get(reverse("core:my_repairs"), follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )


# ---------------------------------------------------------------------------
# Appointment -> repair handoff (FR6, step 1)
# ---------------------------------------------------------------------------
class AppointmentRepairHandoffTests(RepairWorkflowTestCase):
    """The appointment page offers a job exactly when one could be created.

    The button's condition is deliberately a copy of the service layer's gate, so
    the two can drift apart. These tests pin both halves: the button appears for
    every appointment ``create_repair`` would accept, and stays hidden for every
    one it would refuse - otherwise an administrator clicks it and gets an error
    for a state the UI itself proposed.
    """

    def test_checked_in_with_a_vehicle_offers_repair_creation(self):
        appointment = self.checked_in_appointment()
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertTrue(response.context["can_create_repair"])
        self.assertIsNone(response.context["repair"])
        self.assertContains(
            response,
            f"{reverse('core:repair_create')}?appointment={appointment.pk}",
        )

    def test_the_link_preselects_that_appointment(self):
        """The handoff lands on a form already pointed at the right booking."""
        appointment = self.checked_in_appointment()
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:repair_create"), {"appointment": appointment.pk}
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.fields["appointment"].initial.pk, appointment.pk)

    def test_a_preselection_of_an_ineligible_appointment_is_ignored(self):
        """Hand-editing the query string cannot preselect a job that would fail.

        Asserted on the dropdown as well as on ``initial``: an appointment that
        cannot open a job must not be *selectable* either, otherwise the form
        offers a choice the service layer would then have to refuse.
        """
        appointment = self.book()  # still Pending
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:repair_create"), {"appointment": appointment.pk}
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIsNone(form.fields["appointment"].initial)
        self.assertNotIn(
            appointment.pk,
            set(form.fields["appointment"].queryset.values_list("pk", flat=True)),
        )

    def test_an_unparseable_preselection_does_not_break_the_form(self):
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:repair_create"), {"appointment": "not-a-number"}
        )

        self.assertEqual(response.status_code, 200)

    def test_button_is_hidden_while_the_appointment_is_not_checked_in(self):
        appointment = self.book()
        services.accept_appointment(appointment.pk)
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertFalse(response.context["can_create_repair"])
        self.assertNotContains(response, reverse("core:repair_create"))

    def test_button_is_hidden_when_no_vehicle_is_linked(self):
        appointment = self.checked_in_appointment(with_vehicle=False)
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertFalse(response.context["can_create_repair"])

    def test_button_is_hidden_once_a_job_exists(self):
        """Otherwise the page would offer the one action that must be refused."""
        appointment = self.checked_in_appointment()
        repair = services.create_repair(appointment.pk)
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertFalse(response.context["can_create_repair"])
        self.assertEqual(response.context["repair"].pk, repair.pk)
        self.assertContains(
            response, reverse("core:repair_detail", args=[repair.pk])
        )

    def test_a_receptionist_is_never_offered_repair_creation(self):
        """Their part of the story ended at check-in."""
        appointment = self.checked_in_appointment()
        self.login_as(self.receptionist)

        response = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )

        self.assertFalse(response.context["can_create_repair"])
        self.assertNotContains(response, reverse("core:repair_create"))


# ---------------------------------------------------------------------------
# POST flows: the screens actually change the data (FR6-FR9)
# ---------------------------------------------------------------------------
class RepairPostFlowTests(RepairWorkflowTestCase):
    """Every write goes through the real HTTP cycle.

    A POST endpoint is the path a hand-edited request takes, so these assert the
    response contract as well as the database effect: where the user lands, and
    that a GET cannot perform a state change.
    """

    def setUp(self):
        super().setUp()
        self.job, self.tech_staff, self.technician, _bay = self.assigned_repair()

    def test_creating_a_job_through_the_form_redirects_to_its_detail_page(self):
        self.login_as(self.admin)
        appointment = self.checked_in_appointment()

        response = self.client.post(reverse("core:repair_create"), {
            "appointment": appointment.pk,
            "priority": REPAIR_PRIORITY_URGENT,
        })

        repair = Repair.objects.get(AppointmentID=appointment)
        self.assertRedirects(
            response, reverse("core:repair_detail", args=[repair.pk])
        )
        self.assertEqual(repair.RepairPriority, REPAIR_PRIORITY_URGENT)
        self.assertEqual(repair.Status, REPAIR_NOT_STARTED)

    def test_creating_a_job_can_assign_both_halves_at_once(self):
        self.login_as(self.admin)
        appointment = self.checked_in_appointment()
        _staff, technician = self.qualified_technician()
        bay = services.create_service_bay("Bay 2")

        self.client.post(reverse("core:repair_create"), {
            "appointment": appointment.pk,
            "technician": technician.pk,
            "bay": bay.pk,
            "priority": REPAIR_PRIORITY_ROUTINE,
        })

        repair = Repair.objects.get(AppointmentID=appointment)
        self.assertEqual(repair.TechnicianID_id, technician.pk)
        self.assertEqual(repair.BayID_id, bay.pk)
        self.assertEqual(ServiceBay.objects.get(pk=bay.pk).Status, BAY_OCCUPIED)

    def test_posting_an_unqualified_technician_is_refused_inline(self):
        """The refusal happens in the form, so no half-created job is left behind."""
        self.login_as(self.admin)
        appointment = self.checked_in_appointment()
        _staff, technician = self.unqualified_technician()

        response = self.client.post(reverse("core:repair_create"), {
            "appointment": appointment.pk,
            "technician": technician.pk,
            "priority": REPAIR_PRIORITY_ROUTINE,
        })

        self.assertEqual(response.status_code, 200)  # form re-rendered
        # Asserted per-appointment rather than on ``exists()``: the fixture in
        # setUp already holds one job, so "nothing was created" has to be scoped.
        self.assertFalse(Repair.objects.filter(AppointmentID=appointment).exists())
        self.assertFalse(response.context["form"].is_valid())
        self.assertIn("technician", response.context["form"].errors)

    def test_posting_an_occupied_bay_is_refused(self):
        """The dropdown excludes Occupied bays, so the posted pk is not a choice."""
        self.login_as(self.admin)
        appointment = self.checked_in_appointment()

        response = self.client.post(reverse("core:repair_create"), {
            "appointment": appointment.pk,
            "bay": self.bay.pk,  # already held by self.job
            "priority": REPAIR_PRIORITY_ROUTINE,
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Repair.objects.filter(AppointmentID=appointment).exists())
        self.assertIn("bay", response.context["form"].errors)

    def test_creating_a_job_is_refused_for_a_role_that_may_not(self):
        self.login_as(self.receptionist)
        appointment = self.checked_in_appointment()

        response = self.client.post(reverse("core:repair_create"), {
            "appointment": appointment.pk,
            "priority": REPAIR_PRIORITY_ROUTINE,
        }, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )
        self.assertFalse(Repair.objects.filter(AppointmentID=appointment).exists())

    def test_the_state_change_urls_refuse_a_get(self):
        """POST-only, so a prefetch or a crawler cannot progress somebody's work."""
        self.login_as(self.admin)

        for url in (
            reverse("core:repair_status", args=[self.job.pk]),
            reverse("core:repair_assign", args=[self.job.pk]),
            reverse("core:bay_create"),
            reverse("core:bay_edit", args=[self.bay.pk]),
            reverse("core:bay_delete", args=[self.bay.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)


# ---------------------------------------------------------------------------
# Progress and assignment through the UI (FR6, FR8)
# ---------------------------------------------------------------------------
class RepairProgressPostTests(RepairWorkflowTestCase):
    """Moving a job along through the screens, including who lands where after.

    The redirect target is part of the contract, not a detail: a technician
    ticking a job off should land back on their board rather than on a detail
    page they then have to navigate away from.
    """

    def setUp(self):
        super().setUp()
        self.job, self.tech_staff, self.technician, self.bay = (
            self.assigned_repair()
        )
        self.status_url = reverse("core:repair_status", args=[self.job.pk])

    def test_assigned_technician_starts_work_from_their_own_board(self):
        self.login_as(self.tech_staff)

        response = self.client.post(self.status_url, {
            "status": REPAIR_IN_PROGRESS,
            "next": reverse("core:my_repairs"),
        })

        self.assertRedirects(response, reverse("core:my_repairs"))
        self.assertEqual(
            Repair.objects.get(pk=self.job.pk).Status, REPAIR_IN_PROGRESS
        )

    def test_completing_from_the_detail_page_stays_on_the_detail_page(self):
        self.login_as(self.admin)
        services.set_repair_status(self.job.pk, REPAIR_IN_PROGRESS)

        response = self.client.post(self.status_url, {"status": REPAIR_COMPLETED})

        self.assertRedirects(
            response, reverse("core:repair_detail", args=[self.job.pk])
        )
        # The release the message promises actually happened in the database.
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_AVAILABLE
        )

    def test_the_completion_message_names_the_freed_bay(self):
        """Naming the bay is what makes the automatic release visible to staff.

        Followed through to the final page, because the message is flashed on the
        redirect and would otherwise be consumed before it could be read.
        """
        self.login_as(self.admin)
        services.set_repair_status(self.job.pk, REPAIR_IN_PROGRESS)

        response = self.client.post(
            self.status_url, {"status": REPAIR_COMPLETED}, follow=True
        )

        self.assertContains(response, "is available again")
        self.assertContains(response, self.bay.BayName)


    def test_skipping_a_step_is_refused_by_the_form(self):
        """The dropdown only offers legal moves, so the skip is not even a choice."""
        self.login_as(self.admin)

        response = self.client.post(self.status_url, {
            "status": REPAIR_COMPLETED,
        }, follow=True)

        self.assertContains(response, "Choose a valid status transition.")
        self.assertEqual(
            Repair.objects.get(pk=self.job.pk).Status, REPAIR_NOT_STARTED
        )

    def test_an_unassigned_technician_cannot_progress_the_job(self):
        other_staff, _other = self.qualified_technician()
        self.login_as(other_staff)

        response = self.client.post(self.status_url, {
            "status": REPAIR_IN_PROGRESS,
        }, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )
        self.assertEqual(
            Repair.objects.get(pk=self.job.pk).Status, REPAIR_NOT_STARTED
        )

    def test_a_receptionist_cannot_progress_the_job(self):
        """Read-only visibility at the counter, even on a job they checked in."""
        self.login_as(self.receptionist)

        response = self.client.post(self.status_url, {
            "status": REPAIR_IN_PROGRESS,
        }, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )
        self.assertEqual(
            Repair.objects.get(pk=self.job.pk).Status, REPAIR_NOT_STARTED
        )

    def test_an_unknown_job_redirects_to_the_board(self):
        self.login_as(self.admin)

        response = self.client.post(
            reverse("core:repair_status", args=[999999]),
            {"status": REPAIR_IN_PROGRESS},
        )

        self.assertRedirects(response, reverse("core:repair_list"))



class RepairAssignmentPostTests(RepairWorkflowTestCase):
    """Re-assignment from the detail page, which only an administrator reaches."""

    def setUp(self):
        super().setUp()
        self.job, self.tech_staff, self.technician, self.bay = (
            self.assigned_repair()
        )
        self.assign_url = reverse("core:repair_assign", args=[self.job.pk])

    def test_administrator_can_change_both_halves_at_once(self):
        self.login_as(self.admin)
        _staff, other_tech = self.qualified_technician()
        other_bay = services.create_service_bay("Bay 2")

        response = self.client.post(self.assign_url, {
            "technician": other_tech.pk,
            "bay": other_bay.pk,
        })

        self.assertRedirects(
            response, reverse("core:repair_detail", args=[self.job.pk])
        )
        stored = Repair.objects.get(pk=self.job.pk)
        self.assertEqual(stored.TechnicianID_id, other_tech.pk)
        self.assertEqual(stored.BayID_id, other_bay.pk)
        # Old bay released, new bay taken - both re-read from the database.
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_AVAILABLE
        )
        self.assertEqual(
            ServiceBay.objects.get(pk=other_bay.pk).Status, BAY_OCCUPIED
        )

    def test_leaving_both_halves_unchanged_changes_nothing(self):
        """The form's "leave unchanged" is a valid, inert submission."""
        self.login_as(self.admin)

        response = self.client.post(self.assign_url, {})

        self.assertRedirects(
            response, reverse("core:repair_detail", args=[self.job.pk])
        )
        stored = Repair.objects.get(pk=self.job.pk)
        self.assertEqual(stored.TechnicianID_id, self.technician.pk)
        self.assertEqual(stored.BayID_id, self.bay.pk)

    def test_posting_an_unqualified_technician_is_refused(self):
        """The dropdown excludes them, so the pk is not an available choice."""
        self.login_as(self.admin)
        _staff, unqualified = self.unqualified_technician()

        self.client.post(self.assign_url, {"technician": unqualified.pk})

        self.assertEqual(
            Repair.objects.get(pk=self.job.pk).TechnicianID_id,
            self.technician.pk,
        )

    def test_posting_an_occupied_bay_from_another_job_is_refused(self):
        self.login_as(self.admin)
        second_bay = services.create_service_bay("Bay 2")
        other_job = self.repair()
        services.assign_bay(other_job.pk, second_bay.pk)

        self.client.post(self.assign_url, {"bay": second_bay.pk})

        self.assertEqual(Repair.objects.get(pk=self.job.pk).BayID_id, self.bay.pk)

    def test_a_receptionist_cannot_reassign(self):
        self.login_as(self.receptionist)

        response = self.client.post(self.assign_url, {}, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_a_technician_cannot_reassign_even_their_own_job(self):
        """Re-assignment is a workshop-management decision, not the technician's."""
        self.login_as(self.tech_staff)

        response = self.client.post(self.assign_url, {}, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_a_completed_job_is_closed_to_reassignment_on_the_page(self):
        """The form is not rendered at all, so there is nothing to post to."""
        self.login_as(self.admin)
        services.set_repair_status(self.job.pk, REPAIR_IN_PROGRESS)
        services.set_repair_status(self.job.pk, REPAIR_COMPLETED)

        response = self.client.get(
            reverse("core:repair_detail", args=[self.job.pk])
        )

        self.assertNotContains(response, self.assign_url)



class InspectionPostTests(TempMediaMixin, RepairWorkflowTestCase):
    """Recording an inspection through the screen, photographs and all.

    These post real multipart data, so the ``ImageField`` validation and the
    three-slot layout are exercised end to end rather than only at the service
    layer. ``TempMediaMixin`` keeps the uploaded files out of the project's
    ``media/`` directory.
    """

    def setUp(self):
        super().setUp()
        self.job, self.tech_staff, self.technician, _bay = self.assigned_repair()
        self.create_url = reverse("core:inspection_create", args=[self.job.pk])
        self.edit_url = reverse("core:inspection_edit", args=[self.job.pk])

    def test_assigned_technician_records_an_inspection_with_a_photograph(self):
        self.login_as(self.tech_staff)

        response = self.client.post(self.create_url, {
            "description": "Dent on the offside front wing.",
            "image_1": make_png("dent.png"),
        })

        self.assertRedirects(
            response, reverse("core:repair_detail", args=[self.job.pk])
        )
        stored = Repair.objects.get(pk=self.job.pk)
        self.assertTrue(stored.is_inspected)
        self.assertEqual(stored.InspectionID.image_count, 1)

    def test_all_three_slots_can_be_filled_in_one_submission(self):
        self.login_as(self.tech_staff)

        self.client.post(self.create_url, {
            "description": "Full photographic record.",
            "image_1": make_png("a.png"),
            "image_2": make_png("b.png"),
            "image_3": make_png("c.png"),
        })

        stored = Repair.objects.get(pk=self.job.pk)
        self.assertEqual(stored.InspectionID.image_count, MAX_INSPECTION_IMAGES)

    def test_a_blank_description_is_refused_by_the_form(self):
        self.login_as(self.tech_staff)

        response = self.client.post(self.create_url, {"description": "  "})

        self.assertEqual(response.status_code, 200)  # form re-rendered
        self.assertIn("description", response.context["form"].errors)
        self.assertFalse(Inspection.objects.exists())

    def test_an_unassigned_technician_cannot_open_the_inspection_form(self):
        other_staff, _other = self.qualified_technician()
        self.login_as(other_staff)

        response = self.client.get(self.create_url, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_a_receptionist_cannot_record_an_inspection(self):
        """The booking desk's part of the story ended at check-in."""
        self.login_as(self.receptionist)

        response = self.client.post(
            self.create_url, {"description": "Counter note."}, follow=True
        )

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )
        self.assertFalse(Inspection.objects.exists())

    def test_opening_the_create_url_twice_edits_rather_than_duplicates(self):
        """A second visit is redirected to the edit screen, not refused."""
        self.login_as(self.tech_staff)
        self.client.post(self.create_url, {"description": "First note."})

        response = self.client.get(self.create_url)

        self.assertRedirects(response, self.edit_url)
        self.assertEqual(Inspection.objects.count(), 1)

    def test_opening_the_edit_url_with_no_inspection_records_one_instead(self):
        self.login_as(self.tech_staff)

        response = self.client.get(self.edit_url)

        self.assertRedirects(response, self.create_url)

    def test_editing_adds_a_photograph_into_the_slot_still_free(self):
        self.login_as(self.tech_staff)
        self.client.post(self.create_url, {
            "description": "One photo.", "image_1": make_png("kept.png"),
        })
        stored = Repair.objects.get(pk=self.job.pk)
        first_name = stored.InspectionID.Image1.name

        self.client.post(self.edit_url, {
            "description": "",  # blank keeps the stored description
            "image_2": make_png("added.png"),
        })

        updated = Repair.objects.get(pk=self.job.pk).InspectionID
        self.assertEqual(updated.Image1.name, first_name)
        self.assertEqual(updated.image_count, 2)
        self.assertEqual(updated.Description, "One photo.")

    def test_edit_screen_hides_the_slots_that_are_already_full(self):
        """So the form cannot invite an upload it would have to refuse."""
        self.login_as(self.tech_staff)
        self.client.post(self.create_url, {
            "description": "Three photos.",
            "image_1": make_png("a.png"),
            "image_2": make_png("b.png"),
            "image_3": make_png("c.png"),
        })

        response = self.client.get(self.edit_url)

        form = response.context["form"]
        self.assertEqual(form.free_slots, 0)
        for field in form.IMAGE_FIELDS:
            self.assertIsInstance(form.fields[field].widget, HiddenInput)

    def test_a_job_with_no_technician_offers_no_inspection_action(self):
        """The detail page says why, rather than showing a button that fails."""
        unstaffed = self.repair()
        self.login_as(self.admin)

        response = self.client.get(
            reverse("core:repair_detail", args=[unstaffed.pk])
        )

        self.assertNotContains(
            response, reverse("core:inspection_create", args=[unstaffed.pk])
        )
        self.assertContains(response, "must be assigned")



class ServiceBayManagementTests(RepairWorkflowTestCase):
    """Bays are workshop infrastructure, managed by the Administrator alone (FR9).

    Two rules carry the weight here. ``Status`` is derived from whatever job the
    bay holds, so it is never editable directly - otherwise two jobs could be
    parked in one physical space. And a bay holding active work cannot be
    retired, because that would leave a job pointing at a bay that no longer
    exists.
    """

    def setUp(self):
        super().setUp()
        self.list_url = reverse("core:bay_list")
        self.create_url = reverse("core:bay_create")

    def test_a_new_bay_starts_available(self):
        self.login_as(self.admin)

        self.client.post(self.create_url, {"bay_name": "Bay 2"})

        bay = ServiceBay.objects.get(BayName="Bay 2")
        self.assertEqual(bay.Status, BAY_AVAILABLE)
        self.assertTrue(bay.is_available)

    def test_the_bay_page_shows_what_is_holding_each_bay(self):
        """Naming the job is what makes an Occupied bay actionable."""
        self.login_as(self.admin)
        services.assign_bay(self.repair().pk, self.bay.pk)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["occupied_count"], 1)
        self.assertEqual(response.context["available_count"], 0)
        self.assertIsNotNone(response.context["bays"][0].active_repair)

    def test_a_duplicate_name_is_refused(self):
        self.login_as(self.admin)
        self.client.post(self.create_url, {"bay_name": "Bay 1"})

        self.assertEqual(ServiceBay.objects.filter(BayName="Bay 1").count(), 1)

    def test_a_duplicate_name_ignoring_case_is_refused(self):
        """The check is case-insensitive, matching the service layer's rule."""
        self.login_as(self.admin)

        self.client.post(self.create_url, {"bay_name": "bay 1"})

        self.assertFalse(ServiceBay.objects.filter(BayName="bay 1").exists())

    def test_renaming_a_bay_leaves_its_status_alone(self):
        """The name is the only editable field; availability is derived."""
        self.login_as(self.admin)
        job = self.repair()
        services.assign_bay(job.pk, self.bay.pk)
        self.assertEqual(ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED)

        self.client.post(
            reverse("core:bay_edit", args=[self.bay.pk]), {"bay_name": "Bay 1A"}
        )

        renamed = ServiceBay.objects.get(pk=self.bay.pk)
        self.assertEqual(renamed.BayName, "Bay 1A")
        self.assertEqual(renamed.Status, BAY_OCCUPIED)

    def test_a_bay_holding_active_work_cannot_be_deleted(self):
        self.login_as(self.admin)
        services.assign_bay(self.repair().pk, self.bay.pk)

        self.client.post(reverse("core:bay_delete", args=[self.bay.pk]))

        self.assertTrue(ServiceBay.objects.filter(pk=self.bay.pk).exists())

    def test_a_bay_is_retirable_once_its_job_is_completed(self):
        """Completion releases it, and a released bay can be retired."""
        self.login_as(self.admin)
        job = self.repair()
        services.assign_bay(job.pk, self.bay.pk)
        services.set_repair_status(job.pk, REPAIR_IN_PROGRESS)
        services.set_repair_status(job.pk, REPAIR_COMPLETED)

        self.client.post(reverse("core:bay_delete", args=[self.bay.pk]))

        self.assertFalse(ServiceBay.objects.filter(pk=self.bay.pk).exists())
        # The completed job keeps its history: the bay column is nullable, so
        # retiring the bay does not delete the record of work carried out in it.
        self.assertIsNone(Repair.objects.get(pk=job.pk).BayID_id)

    def test_the_status_field_cannot_be_posted_at_all(self):
        """The form has no status field, so a crafted POST cannot set one.

        Asserted on the form's fields rather than only on the outcome: if a
        status field were ever added here, this test names the regression.
        """
        self.assertNotIn("Status", ServiceBayForm().fields)
        self.assertNotIn("status", ServiceBayForm().fields)
        self.assertEqual(list(ServiceBayForm().fields), ["bay_name"])

    def test_only_an_administrator_reaches_the_bay_pages(self):
        for staff in (self.receptionist, self.make_technician()[0]):
            with self.subTest(role=staff.role_name):
                self.login_as(staff)
                for url in (
                    self.list_url,
                    reverse("core:bay_edit", args=[self.bay.pk]),
                ):
                    response = self.client.get(url, follow=True)
                    self.assertRedirects(
                        response,
                        reverse("core:access_denied"),
                        target_status_code=403,
                    )


# ---------------------------------------------------------------------------
# The whole story, end to end
# ---------------------------------------------------------------------------
class RepairLifecycleEndToEndTests(TempMediaMixin, RepairWorkflowTestCase):
    """One continuous flow through the UI, in the order the workshop works in.

    The unit tests above each prove one rule in isolation; this one proves the
    rules compose. It is the test that fails when a handoff breaks - a context
    variable renamed, a redirect pointed at the wrong page, a bay left Occupied
    by a job that has finished - which no single-rule test would catch.

    Deliberately driven through ``self.client`` rather than the service layer, so
    the templates and views are on the path too.
    """

    def switch_user(self, staff):
        """Log out, then log in as ``staff``.

        ``login_view`` sends an already-authenticated session to the dashboard
        rather than swapping the user, so a role change has to pass through
        logout first - exactly what someone sharing a workstation would do. Doing
        this in a helper means a switch can never silently fail and leave the
        previous role signed in.
        """
        self.client.post(reverse("core:logout"))
        return self.login_as(staff)

    def test_appointment_through_to_completed_job_and_released_bay(self):
        # --- 1. A customer books, the request is accepted, the car arrives. ---
        appointment = self.book(first="Dana", last="Driver")
        services.accept_appointment(appointment.pk)
        car = self.vehicle()
        services.match_vehicle_to_appointment(appointment.pk, car.VehicleID)
        services.set_appointment_status(appointment.pk, APPOINTMENT_CHECKED_IN)

        # --- 2. The administrator opens the appointment and raises a job. ---
        self.login_as(self.admin)
        detail = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )
        # The handoff is offered, and links straight to the preselected form.
        self.assertTrue(detail.context["can_create_repair"])
        self.assertContains(
            detail,
            f"{reverse('core:repair_create')}?appointment={appointment.pk}",
        )

        # --- 3. The job is created with a qualified technician and a bay. ---
        _staff, technician = self.qualified_technician()
        response = self.client.post(
            f"{reverse('core:repair_create')}?appointment={appointment.pk}",
            {
                "appointment": appointment.pk,
                "technician": technician.pk,
                "bay": self.bay.pk,
                "priority": REPAIR_PRIORITY_URGENT,
            },
        )
        job = Repair.objects.get(AppointmentID=appointment)
        self.assertRedirects(
            response, reverse("core:repair_detail", args=[job.pk])
        )
        self.assertEqual(job.Status, REPAIR_NOT_STARTED)
        self.assertEqual(job.RepairPriority, REPAIR_PRIORITY_URGENT)
        # The bay is taken from the moment the car is parked in it.
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED
        )
        # The appointment now names its job, and no longer offers to create one.
        appointment_page = self.client.get(
            reverse("core:appointment_detail", args=[appointment.pk])
        )
        self.assertEqual(appointment_page.context["repair"].pk, job.pk)
        self.assertFalse(appointment_page.context["can_create_repair"])

        # --- 4. The assigned technician records the vehicle's condition. ---
        self.switch_user(technician.StaffID)
        response = self.client.post(
            reverse("core:inspection_create", args=[job.pk]),
            {
                "description": "Rear bumper scuffed; customer advised.",
                "image_1": make_png("bumper.png"),
            },
        )
        self.assertRedirects(
            response, reverse("core:repair_detail", args=[job.pk])
        )
        job.refresh_from_db()
        self.assertTrue(job.is_inspected)
        self.assertEqual(job.InspectionID.image_count, 1)
        self.assertEqual(job.InspectionID.VehicleID_id, car.VehicleID)

        # --- 5. Work starts, from the technician's own board. ---
        board = self.client.get(reverse("core:my_repairs"))
        self.assertEqual(
            [repair.pk for repair in board.context["repairs"]], [job.pk]
        )
        self.assertEqual(board.context["not_started"], 1)

        self.client.post(
            reverse("core:repair_status", args=[job.pk]),
            {"status": REPAIR_IN_PROGRESS, "next": reverse("core:my_repairs")},
        )
        job.refresh_from_db()
        self.assertEqual(job.Status, REPAIR_IN_PROGRESS)
        # Under way still holds the bay.
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED
        )

        # --- 6. Work finishes, and the bay is released without a second step. --
        self.client.post(
            reverse("core:repair_status", args=[job.pk]),
            {"status": REPAIR_COMPLETED},
        )
        job.refresh_from_db()
        self.assertEqual(job.Status, REPAIR_COMPLETED)
        self.assertTrue(job.is_completed)
        released = ServiceBay.objects.get(pk=self.bay.pk)
        self.assertEqual(released.Status, BAY_AVAILABLE)
        self.assertIn(released, services.list_available_bays())

        # The technician's board now counts it as done.
        board = self.client.get(reverse("core:my_repairs"))
        self.assertEqual(board.context["completed"], 1)
        self.assertEqual(board.context["in_progress"], 0)

        # --- 7. The freed bay takes the next car in. ---
        self.switch_user(self.admin)
        next_appointment = self.checked_in_appointment(first="Ned", last="Next")
        next_job = services.create_repair(
            next_appointment.pk, bay_id=self.bay.pk
        )
        self.assertEqual(
            ServiceBay.objects.get(pk=self.bay.pk).Status, BAY_OCCUPIED
        )
        # And the finished job keeps its own history untouched.
        self.assertEqual(Repair.objects.get(pk=job.pk).Status, REPAIR_COMPLETED)
        self.assertEqual(
            Repair.objects.get(pk=next_job.pk).Status, REPAIR_NOT_STARTED
        )


