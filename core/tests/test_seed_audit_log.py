"""``seed_audit_log``: rebuilding the audit trail from the records on file.

The command exists so a database loaded before NFR2 still has history on the
audit page. What matters is that the reconstruction is *faithful*: every entry
is attributed to whoever the records say did the work, steps appear in the
order the workflow forces, stored times are kept wherever they are usable, and
running it twice adds nothing.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command

from core import services
from core.models import (
    AUDIT_CHECKED_IN,
    AUDIT_CREATED,
    AUDIT_PAID,
    AUDIT_STATUS_CHANGED,
    AUDIT_UPDATED,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    AuditLog,
    Invoice,
    PartsUsage,
    Repair,
    Staff,
)

from .test_timebox3 import RepairWorkflowTestCase


class SeedAuditLogTests(RepairWorkflowTestCase):
    def setUp(self):
        super().setUp()
        # One job taken all the way: checked in, worked, billed and paid.
        self.repair, self.tech_staff, _technician, _bay = self.assigned_repair()
        services.set_repair_status(self.repair.pk, REPAIR_IN_PROGRESS)
        services.create_inspection(
            self.repair.pk, staff=self.tech_staff, description="Dent on bonnet."
        )
        part = services.create_part("Oil filter", "12.50", 10, 2)
        self.usage, _part = services.record_parts_usage(
            self.repair.pk, part.pk, 2, self.tech_staff
        )
        services.set_repair_status(self.repair.pk, REPAIR_COMPLETED)
        self.invoice = services.generate_invoice(
            self.repair.pk, Decimal("10"), self.receptionist
        )
        services.mark_invoice_paid(self.invoice.pk, self.receptionist)
        self.repair.refresh_from_db()
        self.invoice.refresh_from_db()

    def run_command(self):
        out = StringIO()
        call_command("seed_audit_log", stdout=out)
        return out.getvalue()

    def entries(self, **filters):
        return list(AuditLog.objects.filter(**filters).order_by("CreatedAt", "AuditID"))

    def test_rebuilds_one_entry_per_audited_step(self):
        self.run_command()

        steps = [(e.Action, e.RecordType) for e in self.entries()]
        self.assertEqual(steps, [
            (AUDIT_CHECKED_IN, "Vehicle"),
            (AUDIT_UPDATED, "Appointment"),
            (AUDIT_CREATED, "Repair"),
            (AUDIT_STATUS_CHANGED, "Repair"),
            (AUDIT_CREATED, "Inspection"),
            (AUDIT_CREATED, "Parts usage"),
            (AUDIT_STATUS_CHANGED, "Repair"),
            (AUDIT_CREATED, "Invoice"),
            (AUDIT_PAID, "Invoice"),
        ])

    def test_entries_are_attributed_from_the_records(self):
        self.run_command()

        by_step = {(e.Action, e.RecordType): e.StaffID for e in self.entries()}
        self.assertEqual(by_step[(AUDIT_CREATED, "Repair")], self.admin)
        self.assertEqual(by_step[(AUDIT_STATUS_CHANGED, "Repair")], self.tech_staff)
        self.assertEqual(by_step[(AUDIT_CREATED, "Inspection")], self.tech_staff)
        self.assertEqual(by_step[(AUDIT_CREATED, "Parts usage")], self.tech_staff)
        self.assertEqual(by_step[(AUDIT_CREATED, "Invoice")], self.receptionist)
        self.assertEqual(by_step[(AUDIT_PAID, "Invoice")], self.receptionist)
        # Check-in is desk work: credited to the receptionist who billed the job.
        self.assertEqual(by_step[(AUDIT_CHECKED_IN, "Vehicle")], self.receptionist)

    def test_stored_times_are_kept_when_they_are_usable(self):
        """A job worked through the screens is logged exactly when it happened."""
        base = self.repair.CreatedAt
        Repair.objects.filter(pk=self.repair.pk).update(
            CompletedAt=base + timedelta(hours=2)
        )
        PartsUsage.objects.filter(pk=self.usage.pk).update(
            UsedAt=base + timedelta(hours=1)
        )
        Invoice.objects.filter(pk=self.invoice.pk).update(
            GeneratedAt=base + timedelta(hours=3),
            PaidAt=base + timedelta(hours=4),
        )

        self.run_command()

        def when(action, record_type):
            return AuditLog.objects.get(
                Action=action, RecordType=record_type,
                **({"Description__endswith": "to Completed."}
                   if action == AUDIT_STATUS_CHANGED else {}),
            ).CreatedAt

        self.assertEqual(when(AUDIT_CREATED, "Parts usage"), base + timedelta(hours=1))
        self.assertEqual(when(AUDIT_STATUS_CHANGED, "Repair"), base + timedelta(hours=2))
        self.assertEqual(when(AUDIT_CREATED, "Invoice"), base + timedelta(hours=3))
        self.assertEqual(when(AUDIT_PAID, "Invoice"), base + timedelta(hours=4))

    def test_out_of_order_seed_times_are_put_in_workflow_order(self):
        """Seeded rows can claim a job finished before it was raised."""
        Repair.objects.filter(pk=self.repair.pk).update(
            CompletedAt=self.repair.CreatedAt - timedelta(days=2)
        )
        Invoice.objects.filter(pk=self.invoice.pk).update(
            GeneratedAt=self.repair.CreatedAt - timedelta(days=1),
            PaidAt=self.repair.CreatedAt - timedelta(days=1) + timedelta(hours=1),
        )

        self.run_command()

        times = [e.CreatedAt for e in AuditLog.objects.order_by("AuditID")]
        self.assertEqual(times, sorted(times))
        self.assertLess(
            AuditLog.objects.get(Action=AUDIT_CREATED, RecordType="Repair").CreatedAt,
            self.repair.CreatedAt - timedelta(days=2),
        )

    def test_running_twice_adds_nothing(self):
        self.run_command()
        first = AuditLog.objects.count()

        output = self.run_command()

        self.assertEqual(AuditLog.objects.count(), first)
        self.assertIn("0 entries added", output)

    def test_backfilled_entries_are_still_read_only(self):
        self.run_command()
        entry = AuditLog.objects.first()

        with self.assertRaises(ValueError):
            entry.delete()
        with self.assertRaises(ValueError):
            AuditLog.objects.update(Description="Rewritten.")

    def test_no_desk_staff_means_nothing_is_written(self):
        Staff.objects.filter(
            pk__in=[self.admin.pk, self.receptionist.pk]
        ).update(IsActive=False)

        output = self.run_command()

        self.assertIn("No active Receptionist or Administrator", output)
        self.assertEqual(AuditLog.objects.count(), 0)
