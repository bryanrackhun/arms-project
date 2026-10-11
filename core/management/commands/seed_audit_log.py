"""Backfill the audit trail (NFR2) from the records already in the database.

The audit log only starts filling once the application writes to it, so a
database seeded before Timebox 4 - or loaded by ``seed_arms`` /
``seed_sample_data`` - has vehicles, jobs and invoices but no history behind
them. This command reconstructs that history from what the records themselves
say, so the audit page has realistic entries for a demonstration:

* **who** - taken from the record wherever it is stored: the technician
  assigned to the job (status changes, inspection), ``Parts_usage.RecordedBy``
  and ``Invoices.GeneratedBy``. Desk work the schema does not attribute (vehicle
  check-in, linking the vehicle, raising the job) goes to an active
  Receptionist or Administrator, the only roles allowed to do it;
* **what** - one entry per change the live application would have logged:
  vehicle checked in, vehicle linked to the appointment, job created, each
  status step, inspection recorded, each part used, invoice generated and paid;
* **when** - each job's events are laid out in the order the workflow forces
  (check-in, job, start, inspection, parts, completion, invoice, payment). The
  stored completion, billing and payment times are kept as anchors; an earlier
  step whose stored time is missing, out of order or bunched with the next one
  (bulk-seeded rows are written in the same second) is placed a realistic gap
  before the step that follows it.

Only changes the application actually audits are reconstructed, and nothing is
invented that the data does not imply: no reassignments, no deletions.

Each entry is matched on (action, record type, record id, description), so
running the command twice adds nothing the second time.

Usage::

    python manage.py seed_audit_log
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone as dj_timezone

from core.models import (
    AUDIT_CHECKED_IN,
    AUDIT_CREATED,
    AUDIT_PAID,
    AUDIT_STATUS_CHANGED,
    AUDIT_UPDATED,
    CURRENCY_CODE,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_NOT_STARTED,
    AuditLog,
    Repair,
    Role,
    Staff,
)

#: The minimum gap placed *before* each kind of step when its stored time
#: cannot be used as-is. Rough figures for a small workshop: a few minutes at
#: the desk, longer on the floor.
GAP_BEFORE = {
    "link": timedelta(minutes=3),
    "repair": timedelta(minutes=12),
    "start": timedelta(minutes=20),
    "inspection": timedelta(minutes=15),
    "part": timedelta(minutes=10),
    "complete": timedelta(minutes=45),
    "invoice": timedelta(minutes=20),
    "paid": timedelta(minutes=8),
}

#: Stored times closer together than this were written by one bulk insert rather
#: than by a person working through the screens, so they are spaced out instead
#: of being trusted as the real order.
DISTINCT_STEP = timedelta(seconds=5)


@dataclass
class Event:
    kind: str
    staff: Staff | None
    action: str
    record_type: str
    record_id: int
    description: str
    preferred: datetime | None
    when: datetime | None = None


class Command(BaseCommand):
    help = "Backfill audit log entries from the vehicles, jobs and invoices on file."

    def handle(self, *args, **options):
        self.receptionists = self._active(Role.RECEPTIONIST)
        self.administrators = self._active(Role.ADMINISTRATOR)
        desk = self.receptionists + self.administrators
        if not desk:
            self.stdout.write(self.style.WARNING(
                "No active Receptionist or Administrator account - nothing to "
                "attribute desk work to. Run seed_arms or seed_sample_data first."
            ))
            return
        self.desk = desk

        events = []
        repairs = Repair.objects.select_related(
            "AppointmentID__VehicleID",
            "AppointmentID__ServiceCode",
            "TechnicianID__StaffID",
            "InspectionID",
            "invoice__GeneratedBy",
        ).prefetch_related("parts_usage__PartID", "parts_usage__RecordedBy")
        for repair in repairs.order_by("RepairID"):
            events.extend(self._job_timeline(repair))

        # Written oldest first, so AuditIDs ascend with time just as they would
        # had the entries been logged live.
        events.sort(key=lambda event: event.when)
        created = 0
        with transaction.atomic():
            for event in events:
                created += self._write(event)

        skipped = len(events) - created
        self.stdout.write(self.style.SUCCESS(
            f"Audit log backfilled: {created} entr{'y' if created == 1 else 'ies'} "
            f"added, {skipped} already present."
        ))

    # -- people ------------------------------------------------------------
    def _active(self, role_name):
        return list(
            Staff.objects.filter(RoleID__RoleName=role_name, IsActive=True)
            .order_by("StaffID")
        )

    def _desk_staff(self, repair):
        """Who handled the job at the counter.

        The receptionist who later billed the job is the most likely person to
        have checked the car in; failing that, the desk staff take turns by job
        number so the work is spread rather than all credited to one account.
        """
        invoice = getattr(repair, "invoice", None)
        if invoice is not None and invoice.GeneratedBy.role_name in (
            Role.RECEPTIONIST, Role.ADMINISTRATOR
        ):
            return invoice.GeneratedBy
        pool = self.receptionists or self.desk
        return pool[repair.pk % len(pool)]

    def _admin(self, repair):
        pool = self.administrators or self.desk
        return pool[repair.pk % len(pool)]

    # -- one job's history -------------------------------------------------
    def _job_timeline(self, repair):
        appointment = repair.AppointmentID
        vehicle = appointment.VehicleID
        technician = repair.TechnicianID.StaffID if repair.TechnicianID else None
        desk = self._desk_staff(repair)
        events = []

        if vehicle is not None:
            label = vehicle.display_name
            # Named by appointment: a returning vehicle is checked in once per
            # visit, and each visit is its own entry.
            events.append(Event(
                "checkin", desk, AUDIT_CHECKED_IN, "Vehicle", vehicle.pk,
                f"Existing vehicle {label} checked in for appointment "
                f"{appointment.pk}.",
                appointment.CheckedInAt,
            ))
            events.append(Event(
                "link", desk, AUDIT_UPDATED, "Appointment", appointment.pk,
                f"Vehicle {label} linked.", None,
            ))

        events.append(Event(
            "repair", self._admin(repair), AUDIT_CREATED, "Repair", repair.pk,
            f"Repair job created for appointment {appointment.pk} "
            f"({appointment.ServiceCode.ServiceName}, {repair.RepairPriority} "
            f"priority).",
            repair.CreatedAt,
        ))

        started = repair.Status in (REPAIR_IN_PROGRESS, REPAIR_COMPLETED)
        if started and technician is not None:
            events.append(Event(
                "start", technician, AUDIT_STATUS_CHANGED, "Repair", repair.pk,
                f"Status changed from {REPAIR_NOT_STARTED} to {REPAIR_IN_PROGRESS}.",
                None,
            ))

        inspection = repair.InspectionID
        if inspection is not None:
            events.append(Event(
                "inspection", technician or self._admin(repair), AUDIT_CREATED,
                "Inspection", inspection.pk,
                f"Inspection recorded for repair job #{repair.pk} "
                f"({inspection.image_count} image"
                f"{'' if inspection.image_count == 1 else 's'}).",
                inspection.CreatedAt,
            ))

        for usage in sorted(repair.parts_usage.all(), key=lambda u: u.pk):
            events.append(Event(
                "part", usage.RecordedBy, AUDIT_CREATED, "Parts usage", usage.pk,
                f"{usage.Qty_Used} x {usage.PartID.PartName} at {CURRENCY_CODE} "
                f"{usage.Unit_Price} recorded against repair job #{repair.pk}.",
                usage.UsedAt,
            ))

        if repair.Status == REPAIR_COMPLETED and technician is not None:
            events.append(Event(
                "complete", technician, AUDIT_STATUS_CHANGED, "Repair", repair.pk,
                f"Status changed from {REPAIR_IN_PROGRESS} to {REPAIR_COMPLETED}.",
                repair.CompletedAt,
            ))

        invoice = getattr(repair, "invoice", None)
        if invoice is not None:
            events.append(Event(
                "invoice", invoice.GeneratedBy, AUDIT_CREATED, "Invoice",
                invoice.pk,
                f"Invoice generated for repair job #{repair.pk}: labour "
                f"{invoice.LabourCost}, parts {invoice.PartsCost}, tax "
                f"{invoice.TaxRate}% ({invoice.TaxAmount}), total "
                f"{CURRENCY_CODE} {invoice.TotalAmount}.",
                invoice.GeneratedAt,
            ))
            if invoice.is_paid:
                events.append(Event(
                    "paid", invoice.GeneratedBy, AUDIT_PAID, "Invoice", invoice.pk,
                    f"Invoice marked Paid "
                    f"({invoice.PaymentMethod or 'method not recorded'}, "
                    f"total {CURRENCY_CODE} {invoice.TotalAmount}).",
                    invoice.PaidAt,
                ))

        self._schedule(events)
        return events

    def _schedule(self, events):
        """Give each event a time that respects the workflow's order.

        Walks backwards from the last event, so the anchors that matter most -
        when the job finished, was billed and was paid - keep their stored
        times. An earlier step keeps its own stored time whenever that time is
        genuinely before the next step, so a job worked through the live
        screens is logged exactly as it happened. Only a step with no stored
        time, or one out of order or written in the same instant as the next
        (bulk-seeded rows), is placed the usual gap before the step after it.
        """
        latest = max(
            (event.preferred for event in events if event.preferred),
            default=dj_timezone.now(),
        )
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if index == len(events) - 1:
                event.when = event.preferred or latest
                continue
            following = events[index + 1]
            if (
                event.preferred is not None
                and event.preferred <= following.when - DISTINCT_STEP
            ):
                event.when = event.preferred
                continue
            gap = GAP_BEFORE[following.kind]
            # A step with no stored time of its own (starting the job, linking
            # the vehicle) sits between its neighbours when the earlier one has a
            # usable time, rather than pushing that genuine time back.
            earlier = next(
                (e.preferred for e in reversed(events[:index]) if e.preferred),
                None,
            )
            if (
                event.preferred is None
                and earlier is not None
                and earlier <= following.when - 2 * DISTINCT_STEP
            ):
                gap = min(gap, (following.when - earlier) / 2)
            event.when = following.when - gap

    # -- writing -----------------------------------------------------------
    def _write(self, event) -> int:
        exists = AuditLog.objects.filter(
            Action=event.action,
            RecordType=event.record_type,
            RecordID=event.record_id,
            Description=event.description,
        ).exists()
        if exists:
            return 0

        entry = AuditLog.objects.create(
            StaffID=event.staff,
            StaffName=event.staff.full_name if event.staff else "System",
            Action=event.action,
            RecordType=event.record_type,
            RecordID=event.record_id,
            Description=event.description[:255],
        )
        # ``CreatedAt`` is stamped automatically and the public manager refuses
        # updates, which is right for the application. Backdating a
        # reconstructed entry to when the change happened is the one exception,
        # so it goes through the plain base manager, here and nowhere else.
        AuditLog._base_manager.filter(pk=entry.pk).update(CreatedAt=event.when)
        self.stdout.write(
            f"  Logged    {event.when:%Y-%m-%d %H:%M} {entry.StaffName}: "
            f"{event.action} {event.record_type} #{event.record_id}"
        )
        return 1
