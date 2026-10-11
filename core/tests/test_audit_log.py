"""Audit trail (NFR2): who changed which record, when - and that it stays put.

Three promises are pinned here:

* the entry itself - staff, time, action, record and description - is written by
  ``record_audit`` and cannot afterwards be edited or deleted, through the
  instance *or* through a bulk queryset call;
* only the Administrator can open the audit page, and its record-type filter
  falls back to "All records" for anything it does not recognise;
* each of the 14 screens that change a vehicle, appointment-vehicle link, repair
  job, inspection, parts usage line or invoice writes exactly one entry on
  success, attributed to the signed-in account - and none when the change is
  refused.

The integration tests drive the real views through the test client, because the
audit call lives in the view's success branch and a service-level test alone
would not notice it being moved or dropped.
"""

from decimal import Decimal
from unittest import mock

from django.db import DatabaseError
from django.urls import reverse

from core import services
from core.models import (
    AUDIT_CHECKED_IN,
    AUDIT_CREATED,
    AUDIT_DELETED,
    AUDIT_PAID,
    AUDIT_REASSIGNED,
    AUDIT_STATUS_CHANGED,
    AUDIT_UPDATED,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_NOT_STARTED,
    AuditLog,
    Vehicle,
)

from .base import RoleTestCase
from .test_timebox3 import RepairWorkflowTestCase


# ---------------------------------------------------------------------------
# The entry itself
# ---------------------------------------------------------------------------
class AuditEntryTests(RoleTestCase):
    def setUp(self):
        self.admin = self.make_admin()

    def _entry(self, **overrides):
        values = dict(
            staff=self.admin, action=AUDIT_UPDATED, record_type="Vehicle",
            record_id=7, description="Vehicle details updated.",
        )
        values.update(overrides)
        services.record_audit(**values)
        return AuditLog.objects.get()

    def test_record_audit_creates_entry_with_user_and_time(self):
        entry = self._entry()

        self.assertEqual(entry.StaffID, self.admin)
        self.assertEqual(entry.StaffName, "Alice Admin")
        self.assertEqual(entry.Action, AUDIT_UPDATED)
        self.assertEqual(entry.RecordType, "Vehicle")
        self.assertEqual(entry.RecordID, 7)
        self.assertEqual(entry.Description, "Vehicle details updated.")
        self.assertIsNotNone(entry.CreatedAt)

    def test_staff_name_is_a_snapshot(self):
        """Renaming the account later does not rewrite who made the change."""
        entry = self._entry()
        self.admin.FirstName = "Renamed"
        self.admin.save()

        entry.refresh_from_db()
        self.assertEqual(entry.StaffName, "Alice Admin")

    def test_long_description_is_truncated_not_refused(self):
        entry = self._entry(description="x" * 400)

        self.assertEqual(len(entry.Description), 255)

    def test_entry_cannot_be_edited(self):
        entry = self._entry()
        entry.Description = "Quietly rewritten."

        with self.assertRaises(ValueError):
            entry.save()
        self.assertEqual(
            AuditLog.objects.get().Description, "Vehicle details updated."
        )

    def test_entry_cannot_be_deleted(self):
        entry = self._entry()

        with self.assertRaises(ValueError):
            entry.delete()
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_bulk_update_and_delete_are_refused(self):
        """The queryset paths skip ``save``/``delete``, so they are closed too."""
        self._entry()

        with self.assertRaises(ValueError):
            AuditLog.objects.update(Description="Rewritten.")
        with self.assertRaises(ValueError):
            AuditLog.objects.all().delete()
        self.assertEqual(
            AuditLog.objects.get().Description, "Vehicle details updated."
        )

    def test_database_failure_is_logged_not_raised(self):
        """A broken audit table must never stop the staff member's own action."""
        with mock.patch.object(
            AuditLog.objects, "create", side_effect=DatabaseError("disk full")
        ), self.assertLogs("core.services", level="ERROR") as logs:
            services.record_audit(self.admin, AUDIT_PAID, "Invoice", 3, "Paid.")

        self.assertIn("Could not write audit entry for Invoice #3", logs.output[0])
        self.assertEqual(AuditLog.objects.count(), 0)


# ---------------------------------------------------------------------------
# The audit log page
# ---------------------------------------------------------------------------
class AuditLogPageTests(RoleTestCase):
    def setUp(self):
        self.admin = self.make_admin()
        self.receptionist = self.make_staff(
            email="reception@test.local", role=self.receptionist_role,
            first_name="Rachel", last_name="Receptionist",
        )
        self.tech_staff, _technician = self.make_technician()
        services.record_audit(
            self.receptionist, AUDIT_PAID, "Invoice", 11, "Invoice marked Paid."
        )
        services.record_audit(
            self.tech_staff, AUDIT_CREATED, "Inspection", 22, "Scratch recorded."
        )
        self.url = reverse("core:audit_log")

    def test_admin_can_view_audit_log(self):
        self.login_as(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invoice marked Paid.")
        self.assertContains(response, "Rachel Receptionist")
        self.assertEqual(len(response.context["entries"]), 2)

    def test_receptionist_cannot_view_audit_log(self):
        self.login_as(self.receptionist)

        response = self.client.get(self.url, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_technician_cannot_view_audit_log(self):
        self.login_as(self.tech_staff)

        response = self.client.get(self.url, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_filter_by_record_type(self):
        self.login_as(self.admin)

        response = self.client.get(self.url, {"type": "Inspection"})

        entries = response.context["entries"]
        self.assertEqual([e.RecordType for e in entries], ["Inspection"])
        self.assertNotContains(response, "Invoice marked Paid.")

    def test_unknown_record_type_shows_all_records(self):
        self.login_as(self.admin)

        response = self.client.get(self.url, {"type": "Staff"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_type"], "")
        self.assertEqual(len(response.context["entries"]), 2)

    def test_page_offers_no_edit_or_delete_action(self):
        self.login_as(self.admin)

        response = self.client.get(self.url)

        # The only POST form on the page is the shared logout button.
        self.assertEqual(response.content.decode().count('method="post"'), 1)
        self.assertNotContains(response, "Delete")

    def test_menu_link_shown_only_to_the_administrator(self):
        self.login_as(self.admin)
        self.assertContains(self.client.get(reverse("core:dashboard")), self.url)

        self.client.post(reverse("core:logout"))
        self.login_as(self.receptionist)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), self.url)


# ---------------------------------------------------------------------------
# The 14 screens that write an entry
# ---------------------------------------------------------------------------
class AuditedViewTests(RepairWorkflowTestCase):
    """One successful change per screen -> exactly one entry, by the right person."""

    def assert_single_entry(self, staff, action, record_type, record_id=None):
        entries = list(AuditLog.objects.all())
        self.assertEqual(len(entries), 1, [str(e) for e in entries])
        entry = entries[0]
        self.assertEqual(entry.StaffID, staff)
        self.assertEqual(entry.StaffName, staff.full_name)
        self.assertEqual(entry.Action, action)
        self.assertEqual(entry.RecordType, record_type)
        if record_id is not None:
            self.assertEqual(entry.RecordID, record_id)
        return entry

    def in_progress_repair(self):
        repair, tech_staff, technician, bay = self.assigned_repair()
        services.set_repair_status(repair.pk, REPAIR_IN_PROGRESS)
        return repair, tech_staff

    def part(self, stock=10):
        return services.create_part(
            f"Oil filter {self.next_index()}", "12.50", stock, 2
        )

    def completed_repair_with_parts(self):
        repair, tech_staff = self.in_progress_repair()
        services.record_parts_usage(repair.pk, self.part().pk, 2, tech_staff)
        services.set_repair_status(repair.pk, REPAIR_COMPLETED)
        return repair

    # ---- vehicles ---------------------------------------------------------
    def test_vehicle_create(self):
        self.login_as(self.receptionist)

        self.client.post(reverse("core:vehicle_create"), {
            "make": "Toyota", "model": "Corolla", "license_plate": "AUD-001",
        })

        vehicle = Vehicle.objects.get(LicensePlate="AUD001")
        self.assert_single_entry(
            self.receptionist, AUDIT_CREATED, "Vehicle", vehicle.pk
        )

    def test_vehicle_check_in(self):
        self.login_as(self.receptionist)

        self.client.post(reverse("core:vehicle_check_in"), {
            "make": "Honda", "model": "Civic", "license_plate": "AUD-002",
        })

        vehicle = Vehicle.objects.get(LicensePlate="AUD002")
        self.assert_single_entry(
            self.receptionist, AUDIT_CHECKED_IN, "Vehicle", vehicle.pk
        )

    def test_vehicle_edit(self):
        car = self.vehicle()
        self.login_as(self.receptionist)

        self.client.post(reverse("core:vehicle_edit", args=[car.pk]), {
            "make": "Toyota", "model": "Corolla", "license_plate": car.LicensePlate,
            "vin": car.VIN, "year": 2018, "colour": "Red",
        })

        self.assert_single_entry(self.receptionist, AUDIT_UPDATED, "Vehicle", car.pk)

    def test_vehicle_delete_keeps_its_entry_after_the_row_is_gone(self):
        car = self.vehicle()
        label = car.display_name
        self.login_as(self.receptionist)

        self.client.post(reverse("core:vehicle_delete", args=[car.pk]))

        self.assertFalse(Vehicle.objects.filter(pk=car.pk).exists())
        entry = self.assert_single_entry(
            self.receptionist, AUDIT_DELETED, "Vehicle", car.pk
        )
        self.assertIn(label, entry.Description)

    def test_refused_vehicle_delete_writes_nothing(self):
        appointment = self.book()  # links a vehicle, which blocks deletion
        self.login_as(self.receptionist)

        self.client.post(
            reverse("core:vehicle_delete", args=[appointment.VehicleID_id])
        )

        self.assertEqual(AuditLog.objects.count(), 0)

    # ---- appointment-vehicle link -----------------------------------------
    def test_appointment_match_vehicle(self):
        appointment = self.book()
        car = self.vehicle()
        self.login_as(self.receptionist)

        self.client.post(
            reverse("core:appointment_match_vehicle", args=[appointment.pk]),
            {"vehicle": car.pk, "confirm_replace": "on"},
        )

        entry = self.assert_single_entry(
            self.receptionist, AUDIT_UPDATED, "Appointment", appointment.pk
        )
        self.assertIn(car.display_name, entry.Description)

    # ---- repair jobs ------------------------------------------------------
    def test_repair_create(self):
        appointment = self.checked_in_appointment()
        self.login_as(self.admin)

        self.client.post(reverse("core:repair_create"), {
            "appointment": appointment.pk, "priority": "Urgent",
        })

        repair = services.get_repair_for_appointment(appointment.pk)
        self.assert_single_entry(self.admin, AUDIT_CREATED, "Repair", repair.pk)

    def test_repair_assign(self):
        repair = self.repair()
        _staff, technician = self.qualified_technician()
        self.login_as(self.admin)

        self.client.post(
            reverse("core:repair_assign", args=[repair.pk]),
            {"technician": technician.pk},
        )

        entry = self.assert_single_entry(
            self.admin, AUDIT_REASSIGNED, "Repair", repair.pk
        )
        self.assertIn("from unassigned to Tom Tech", entry.Description)

    def test_repair_status_change_is_attributed_to_the_technician(self):
        repair, tech_staff, _technician, _bay = self.assigned_repair()
        self.login_as(tech_staff)

        self.client.post(
            reverse("core:repair_status", args=[repair.pk]),
            {"status": REPAIR_IN_PROGRESS},
        )

        entry = self.assert_single_entry(
            tech_staff, AUDIT_STATUS_CHANGED, "Repair", repair.pk
        )
        self.assertEqual(
            entry.Description,
            f"Status changed from {REPAIR_NOT_STARTED} to {REPAIR_IN_PROGRESS}.",
        )

    def test_illegal_status_change_writes_nothing(self):
        repair, tech_staff, _technician, _bay = self.assigned_repair()
        self.login_as(tech_staff)

        self.client.post(
            reverse("core:repair_status", args=[repair.pk]),
            {"status": REPAIR_COMPLETED},  # cannot skip In Progress
        )

        self.assertEqual(AuditLog.objects.count(), 0)

    # ---- inspections ------------------------------------------------------
    def test_inspection_create(self):
        repair, tech_staff, _technician, _bay = self.assigned_repair()
        self.login_as(tech_staff)

        self.client.post(
            reverse("core:inspection_create", args=[repair.pk]),
            {"description": "Scratch on the rear passenger door."},
        )

        repair.refresh_from_db()
        self.assert_single_entry(
            tech_staff, AUDIT_CREATED, "Inspection", repair.InspectionID_id
        )

    def test_inspection_edit(self):
        repair, tech_staff, _technician, _bay = self.assigned_repair()
        _repair, inspection = services.create_inspection(
            repair.pk, staff=tech_staff, description="Initial notes."
        )
        self.login_as(tech_staff)

        self.client.post(
            reverse("core:inspection_edit", args=[repair.pk]),
            {"description": "Initial notes, plus a dent on the bonnet."},
        )

        self.assert_single_entry(
            tech_staff, AUDIT_UPDATED, "Inspection", inspection.pk
        )

    # ---- parts usage ------------------------------------------------------
    def test_parts_usage_recorded(self):
        repair, tech_staff = self.in_progress_repair()
        part = self.part()
        self.login_as(tech_staff)

        self.client.post(
            reverse("core:repair_parts_record", args=[repair.pk]),
            {"part": part.pk, "qty_used": 2},
        )

        usage = repair.parts_usage.get()
        self.assert_single_entry(tech_staff, AUDIT_CREATED, "Parts usage", usage.pk)

    def test_parts_usage_removed(self):
        repair, tech_staff = self.in_progress_repair()
        part = self.part()
        usage, _part = services.record_parts_usage(repair.pk, part.pk, 3, tech_staff)
        self.login_as(self.admin)

        self.client.post(
            reverse("core:repair_parts_remove", args=[repair.pk, usage.pk])
        )

        entry = self.assert_single_entry(
            self.admin, AUDIT_DELETED, "Parts usage", usage.pk
        )
        self.assertIn(f"3 x {part.PartName}", entry.Description)

    # ---- invoices ---------------------------------------------------------
    def test_invoice_generate(self):
        repair = self.completed_repair_with_parts()
        self.login_as(self.receptionist)

        self.client.post(
            reverse("core:invoice_generate", args=[repair.pk]), {"tax_rate": "10"}
        )

        invoice = services.invoice_for_repair(repair.pk)
        entry = self.assert_single_entry(
            self.receptionist, AUDIT_CREATED, "Invoice", invoice.pk
        )
        self.assertIn(str(invoice.TotalAmount), entry.Description)

    def test_invoice_paid(self):
        repair = self.completed_repair_with_parts()
        invoice = services.generate_invoice(repair.pk, Decimal("10"), self.receptionist)
        self.login_as(self.receptionist)

        self.client.post(reverse("core:invoice_pay", args=[invoice.pk]))

        self.assert_single_entry(self.receptionist, AUDIT_PAID, "Invoice", invoice.pk)

    def test_paying_an_already_paid_invoice_writes_nothing_more(self):
        repair = self.completed_repair_with_parts()
        invoice = services.generate_invoice(repair.pk, Decimal("10"), self.receptionist)
        self.login_as(self.receptionist)
        self.client.post(reverse("core:invoice_pay", args=[invoice.pk]))

        self.client.post(reverse("core:invoice_pay", args=[invoice.pk]))

        self.assertEqual(AuditLog.objects.filter(Action=AUDIT_PAID).count(), 1)

    def test_change_still_completes_when_the_audit_write_fails(self):
        car = self.vehicle()
        self.login_as(self.receptionist)

        with mock.patch.object(
            AuditLog.objects, "create", side_effect=DatabaseError("disk full")
        ), self.assertLogs("core.services", level="ERROR"):
            self.client.post(reverse("core:vehicle_delete", args=[car.pk]))

        self.assertFalse(Vehicle.objects.filter(pk=car.pk).exists())
        self.assertEqual(AuditLog.objects.count(), 0)
