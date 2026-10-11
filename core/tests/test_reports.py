"""Business reports (FR15): figures aggregated from stored records, never typed in.

The report has no table of its own, so what these tests pin is the *rules* it
reads the existing tables with - the same rules payroll uses:

* a job counts in the period it was **completed** (``Repairs.CompletedAt``);
* revenue counts invoices **generated** in the period, payments by **PaidAt**;
* appointments count by **appointment date**, parts by **UsedAt**;
* money still owed is reported for all time, whatever the period.

Fixtures are mostly built through the real service calls. Where a test needs a
record to sit in another month, the timestamp is moved afterwards with a
queryset ``update``, which is the only way to put history in the past.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core import services
from core.models import (
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_PENDING,
    PAYMENT_PENDING,
    REPAIR_COMPLETED,
    REPAIR_PRIORITY_ROUTINE,
    REPAIR_PRIORITY_URGENT,
    Appointment,
    AuditLog,
    Inspection,
    Invoice,
    Parts,
    PartsUsage,
    Repair,
    Service,
    ServiceBay,
    Staff,
    Technician,
    Vehicle,
)
from core.services import ValidationError

from .test_timebox3 import RepairWorkflowTestCase


class ReportTestCase(RepairWorkflowTestCase):
    """A workshop with a qualified technician, plus helpers for finished work."""

    def setUp(self):
        super().setUp()
        self.today = dj_timezone.localdate()
        self.long_ago = dj_timezone.now() - timedelta(days=60)
        self.tech_staff, self.technician = self.qualified_technician()

    def report(self, start=None, end=None):
        return services.build_business_report(start or self.today, end or self.today)

    def completed_job(self, *, service=None, technician=None,
                      priority=REPAIR_PRIORITY_ROUTINE, completed_at=None):
        """A Completed repair job on a fresh booking for ``service``.

        Created directly rather than walked through every lifecycle step: these
        tests are about how finished work is *counted*, and the lifecycle itself
        is covered by the Timebox 3 suite.
        """
        service = service or self.service
        appointment = services.book_appointment(
            "Sam", "Customer", service.Service_code, self.date, self.next_slot(),
            phone="0400000000", car_model="Toyota Corolla",
            license_plate=f"RPT{self.next_index():04d}",
        )
        return Repair.objects.create(
            AppointmentID=appointment,
            TechnicianID=technician or self.technician,
            RepairPriority=priority,
            Status=REPAIR_COMPLETED,
            CompletedAt=completed_at or dj_timezone.now(),
        )

    def part(self, price="12.50", stock=50):
        return services.create_part(
            f"Part {self.next_index()}", price, stock, 2
        )

    def use_part(self, repair, part, quantity):
        """Record usage the way the service would, without the In Progress gate."""
        return PartsUsage.objects.create(
            RepairID=repair, PartID=part, Qty_Used=quantity,
            Unit_Price=part.UnitPrice, RecordedBy=self.tech_staff,
        )

    def invoiced_job(self, tax_rate="10", with_part=True):
        repair = self.completed_job()
        if with_part:
            self.use_part(repair, self.part(), 2)
        return services.generate_invoice(
            repair.pk, tax_rate, self.receptionist,
            confirm_no_parts=not with_part,
        )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
class ReportPageTests(ReportTestCase):
    url = reverse("core:reports")

    def test_admin_sees_the_current_month_by_default(self):  # 1
        self.login_as(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        start, end = services.default_payroll_range()
        report = response.context["report"]
        self.assertEqual(report["start_date"], start)
        self.assertEqual(report["end_date"], end)
        self.assertContains(response, "Business report")

    def test_receptionist_is_denied(self):  # 2
        self.login_as(self.receptionist)

        response = self.client.get(self.url, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_technician_is_denied(self):  # 3
        self.login_as(self.tech_staff)

        response = self.client.get(self.url, follow=True)

        self.assertRedirects(
            response, reverse("core:access_denied"), target_status_code=403
        )

    def test_start_after_end_is_a_form_error_and_no_report(self):  # 4
        self.login_as(self.admin)

        response = self.client.get(self.url, {
            "start_date": self.today.isoformat(),
            "end_date": (self.today - timedelta(days=1)).isoformat(),
        })

        self.assertIsNone(response.context["report"])
        self.assertContains(response, "The start date cannot be after the end date.")

    def test_missing_date_is_a_form_error_and_no_report(self):
        self.login_as(self.admin)

        response = self.client.get(self.url, {"start_date": self.today.isoformat()})

        self.assertIsNone(response.context["report"])
        self.assertIn("end_date", response.context["form"].errors)

    def test_chosen_period_is_recalculated(self):
        self.completed_job()
        self.login_as(self.admin)

        response = self.client.get(self.url, {
            "start_date": self.today.isoformat(),
            "end_date": self.today.isoformat(),
        })

        self.assertEqual(response.context["report"]["completed_jobs"], 1)
        self.assertContains(response, "Oil Change")

    def test_empty_period_renders_the_information_message(self):
        self.login_as(self.admin)

        response = self.client.get(self.url, {
            "start_date": "2001-01-01", "end_date": "2001-01-31",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No activity was recorded in this period.")

    def test_menu_link_shown_only_to_the_administrator(self):
        self.login_as(self.admin)
        self.assertContains(self.client.get(reverse("core:dashboard")), self.url)

        self.client.post(reverse("core:logout"))
        self.login_as(self.receptionist)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), self.url)


# ---------------------------------------------------------------------------
# The figures
# ---------------------------------------------------------------------------
class ReportFigureTests(ReportTestCase):
    def test_reversed_range_is_refused_by_the_service_too(self):
        with self.assertRaises(ValidationError):
            self.report(self.today, self.today - timedelta(days=1))

    def test_empty_period_is_all_zero_with_a_message(self):  # 5
        report = self.report()

        self.assertEqual(report["completed_jobs"], 0)
        self.assertEqual(report["invoice_count"], 0)
        self.assertEqual(report["invoice_total"], Decimal("0.00"))
        self.assertEqual(report["average_invoice"], Decimal("0.00"))
        self.assertEqual(report["received_total"], Decimal("0.00"))
        self.assertEqual(report["top_services"], [])
        self.assertEqual(report["technician_jobs"], [])
        self.assertEqual(report["top_parts"], [])
        self.assertFalse(report["has_data"])
        self.assertEqual(report["message"], "No activity was recorded in this period.")

    def test_jobs_count_in_the_period_they_were_completed(self):  # 6
        self.completed_job()
        self.completed_job()
        self.completed_job(completed_at=self.long_ago)

        self.assertEqual(self.report()["completed_jobs"], 2)

    def test_unfinished_jobs_are_not_counted(self):
        repair, *_ = self.assigned_repair()

        self.assertEqual(self.report()["completed_jobs"], 0)
        self.assertEqual(repair.CompletedAt, None)

    def test_urgent_and_routine_split(self):  # 7
        self.completed_job(priority=REPAIR_PRIORITY_URGENT)
        self.completed_job(priority=REPAIR_PRIORITY_ROUTINE)
        self.completed_job(priority=REPAIR_PRIORITY_ROUTINE)

        report = self.report()

        self.assertEqual(report["urgent_jobs"], 1)
        self.assertEqual(report["routine_jobs"], 2)

    def test_invoice_totals_and_their_split(self):  # 8
        first = self.invoiced_job(tax_rate="10")
        second = self.invoiced_job(tax_rate="5", with_part=False)

        report = self.report()

        self.assertEqual(report["invoice_count"], 2)
        self.assertEqual(
            report["invoice_total"], first.TotalAmount + second.TotalAmount
        )
        self.assertEqual(
            report["labour_total"] + report["parts_total"] + report["tax_total"],
            report["invoice_total"],
        )
        self.assertEqual(report["parts_total"], first.PartsCost)

    def test_average_invoice_is_rounded_to_two_places(self):  # 9
        self.invoiced_job(tax_rate="10")
        self.invoiced_job(tax_rate="7")
        self.invoiced_job(tax_rate="3", with_part=False)

        report = self.report()

        expected = services._money(report["invoice_total"] / 3)
        self.assertEqual(report["average_invoice"], expected)
        self.assertEqual(report["average_invoice"].as_tuple().exponent, -2)

    def test_payment_counts_by_the_date_it_was_received(self):  # 10
        invoice = self.invoiced_job()
        services.mark_invoice_paid(invoice.pk, self.receptionist)
        # Billed two months ago, but the money came in today.
        Invoice.objects.filter(pk=invoice.pk).update(GeneratedAt=self.long_ago)

        report = self.report()

        self.assertEqual(report["invoice_count"], 0)
        self.assertEqual(report["received_count"], 1)
        self.assertEqual(report["received_total"], invoice.TotalAmount)

    def test_outstanding_ignores_the_period(self):  # 11
        invoice = self.invoiced_job()
        Invoice.objects.filter(pk=invoice.pk).update(GeneratedAt=self.long_ago)

        report = self.report()

        self.assertEqual(invoice.PaymentStatus, PAYMENT_PENDING)
        self.assertEqual(report["invoice_count"], 0)
        self.assertEqual(report["unpaid_count"], 1)
        self.assertEqual(report["outstanding_total"], invoice.TotalAmount)

    def test_top_services_are_ordered_and_capped_at_five(self):  # 12
        brakes = self.make_service("Brake Service", "80.00", 60)
        self.completed_job(service=brakes)
        self.completed_job(service=brakes)
        self.completed_job(service=brakes)
        self.completed_job()  # Oil Change, once
        for index in range(5):
            self.completed_job(service=self.make_service(f"Extra {index}", "10", 30))

        top = self.report()["top_services"]

        self.assertEqual(len(top), services.REPORT_TOP_N)
        self.assertEqual(top[0], {"name": "Brake Service", "jobs": 3})
        self.assertEqual([row["jobs"] for row in top[1:]], [1, 1, 1, 1])

    def test_technician_workload(self):  # 13
        other_staff, other = self.qualified_technician()
        Staff.objects.filter(pk=other_staff.pk).update(FirstName="Uma")
        self.completed_job(technician=self.technician)
        self.completed_job(technician=self.technician)
        self.completed_job(technician=other)
        self.completed_job(technician=other, completed_at=self.long_ago)

        rows = self.report()["technician_jobs"]

        self.assertEqual(
            [(row["TechnicianID"], row["first"], row["jobs"]) for row in rows],
            [(self.technician.pk, "Tom", 2), (other.pk, "Uma", 1)],
        )

    def test_top_parts_use_the_frozen_unit_price(self):  # 14
        repair = self.completed_job()
        filter_part = self.part(price="12.50")
        plug_part = self.part(price="4.00")
        self.use_part(repair, filter_part, 2)
        self.use_part(repair, plug_part, 4)
        old_usage = self.use_part(repair, plug_part, 9)
        PartsUsage.objects.filter(pk=old_usage.pk).update(UsedAt=self.long_ago)
        # A later catalogue price rise must not re-price what was already used.
        Parts.objects.filter(pk=filter_part.pk).update(UnitPrice="99.00")

        top = self.report()["top_parts"]

        self.assertEqual(top[0]["name"], plug_part.PartName)
        self.assertEqual(top[0]["quantity"], 4)
        self.assertEqual(top[0]["value"], Decimal("16.00"))
        self.assertEqual(top[1]["name"], filter_part.PartName)
        self.assertEqual(top[1]["value"], Decimal("25.00"))

    def test_low_stock_count_matches_the_low_stock_page(self):
        self.part(stock=1)  # threshold is 2
        self.part(stock=50)

        self.assertEqual(
            self.report()["low_stock_count"], services.count_low_stock_parts()
        )
        self.assertEqual(self.report()["low_stock_count"], 1)

    def test_appointments_count_by_appointment_date(self):
        tomorrow = self.date
        accepted = self.book()
        services.accept_appointment(accepted.pk)
        self.book()

        report = self.report(tomorrow, tomorrow)

        by_status = {row["Status"]: row["count"] for row in report["appointments_by_status"]}
        self.assertEqual(by_status, {APPOINTMENT_CONFIRMED: 1, APPOINTMENT_PENDING: 1})
        self.assertEqual(report["appointments_total"], 2)
        self.assertEqual(self.report()["appointments_total"], 0)  # today: none

    def test_generating_the_report_changes_nothing(self):  # 15
        self.invoiced_job()
        models = (
            Appointment, AuditLog, Inspection, Invoice, Parts, PartsUsage, Repair,
            Service, ServiceBay, Staff, Technician, Vehicle,
        )
        before = {model: model.objects.count() for model in models}
        paid_before = list(Invoice.objects.values_list("PaymentStatus", flat=True))

        self.report()
        self.login_as(self.admin)
        self.client.get(reverse("core:reports"))

        self.assertEqual({model: model.objects.count() for model in models}, before)
        self.assertEqual(
            list(Invoice.objects.values_list("PaymentStatus", flat=True)), paid_before
        )

    def test_query_count_does_not_grow_with_the_data(self):
        """NFR4: aggregated in the database, so more rows means no more queries."""
        self.invoiced_job()
        with CaptureQueriesContext(connection) as small:
            self.report()

        for _ in range(4):
            self.invoiced_job()
        with CaptureQueriesContext(connection) as large:
            self.report()

        self.assertEqual(len(small), len(large))
