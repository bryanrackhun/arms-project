"""Customer invoice and cash-only payment choice, reached by booking reference.

Once a repair job is Completed and billed, the customer can open the itemised
invoice from the public status result and choose how to pay. Cash is the only
method accepted, and choosing it never settles the bill: only staff can mark an
invoice Paid, after taking the money at the counter.
"""

from decimal import Decimal

from django.urls import reverse

from core import services
from core.models import (
    PAYMENT_METHOD_CASH,
    PAYMENT_PAID,
    PAYMENT_PENDING,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    Invoice,
    Parts,
    PartsUsage,
    Repair,
)

from .test_reference_code import ReferenceCodeTestCase


class CustomerInvoiceTestCase(ReferenceCodeTestCase):
    """A booking walked to a Completed, billed repair with one part fitted."""

    def setUp(self):
        super().setUp()
        self.appointment = self._book()
        self.repair = Repair.objects.create(
            AppointmentID=self.appointment, Status=REPAIR_COMPLETED
        )
        part = Parts.objects.create(
            PartName="Oil filter", UnitPrice="12.50", StockQty=10, ReorderThreshold=2
        )
        PartsUsage.objects.create(
            RepairID=self.repair, PartID=part, Qty_Used=2, Unit_Price="12.50",
            RecordedBy=self.admin,
        )
        self.invoice = services.generate_invoice(
            self.repair.pk, "10", self.receptionist
        )
        self.ref = self.appointment.ReferenceCode

    def invoice_url(self, ref=None):
        return reverse("core:customer_invoice", args=[ref or self.ref])

    def payment_url(self, ref=None):
        return reverse("core:customer_payment", args=[ref or self.ref])


class CustomerInvoiceViewTests(CustomerInvoiceTestCase):
    def test_completed_job_shows_the_itemised_invoice(self):
        response = self.client.get(self.invoice_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Invoice #{self.invoice.pk}")
        self.assertContains(response, "Oil filter")
        self.assertContains(response, str(self.invoice.TotalAmount))
        self.assertContains(response, self.payment_url())

    def test_invoice_carries_no_staff_names(self):
        response = self.client.get(self.invoice_url())

        for name in (self.receptionist.FirstName, self.admin.FirstName):
            with self.subTest(name=name):
                self.assertNotContains(response, name)

    def test_lowercase_reference_still_finds_the_invoice(self):
        response = self.client.get(self.invoice_url(self.ref.lower()))

        self.assertEqual(response.status_code, 200)

    def test_unknown_reference_is_a_generic_404(self):
        response = self.client.get(self.invoice_url("ZZZZZZZZ"))

        self.assertEqual(response.status_code, 404)
        self.assertContains(
            response, services.CUSTOMER_INVOICE_NOT_FOUND, status_code=404
        )

    def test_unfinished_job_has_no_invoice_to_show(self):
        Repair.objects.filter(pk=self.repair.pk).update(Status=REPAIR_IN_PROGRESS)

        response = self.client.get(self.invoice_url())

        self.assertEqual(response.status_code, 404)

    def test_unbilled_completed_job_has_no_invoice_to_show(self):
        Invoice.objects.filter(pk=self.invoice.pk).delete()

        response = self.client.get(self.invoice_url())

        self.assertEqual(response.status_code, 404)

    def test_status_result_links_to_the_invoice_and_payment(self):
        response = self.client.post(
            reverse("core:repair_status_lookup"), {"reference_code": self.ref}
        )

        self.assertContains(response, self.invoice_url())
        self.assertContains(response, self.payment_url())


class CustomerPaymentTests(CustomerInvoiceTestCase):
    def test_payment_page_offers_cash_as_the_only_selectable_method(self):
        response = self.client.get(self.payment_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="payment_method"', count=1)
        self.assertContains(response, 'value="Cash"')

    def test_choosing_cash_records_it_but_leaves_the_invoice_pending(self):
        response = self.client.post(
            self.payment_url(), {"payment_method": PAYMENT_METHOD_CASH}
        )

        self.assertRedirects(response, self.invoice_url())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.PaymentMethod, PAYMENT_METHOD_CASH)
        self.assertEqual(self.invoice.PaymentStatus, PAYMENT_PENDING)
        self.assertIsNone(self.invoice.PaidAt)

    def test_any_method_other_than_cash_is_refused(self):
        for method in ("Card", "Online", ""):
            with self.subTest(method=method):
                response = self.client.post(
                    self.payment_url(), {"payment_method": method}
                )

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                self.invoice.refresh_from_db()
                self.assertEqual(self.invoice.PaymentMethod, "")

    def test_service_refuses_a_method_the_form_did_not_check(self):
        with self.assertRaises(services.ValidationError):
            services.choose_customer_payment_method(self.ref, "Card")

    def test_paid_invoice_sends_the_customer_back_to_the_invoice(self):
        services.mark_invoice_paid(self.invoice.pk, self.receptionist)

        get = self.client.get(self.payment_url())
        post = self.client.post(
            self.payment_url(), {"payment_method": PAYMENT_METHOD_CASH}
        )

        self.assertRedirects(get, self.invoice_url())
        self.assertRedirects(post, self.invoice_url())

    def test_service_refuses_to_change_a_paid_invoice(self):
        services.mark_invoice_paid(self.invoice.pk, self.receptionist)

        with self.assertRaises(services.ValidationError):
            services.choose_customer_payment_method(self.ref, PAYMENT_METHOD_CASH)

    def test_unknown_reference_cannot_reach_the_payment_form(self):
        response = self.client.get(self.payment_url("ZZZZZZZZ"))

        self.assertEqual(response.status_code, 404)


class StaffSettlementTests(CustomerInvoiceTestCase):
    def test_marking_paid_records_cash(self):
        invoice = services.mark_invoice_paid(self.invoice.pk, self.receptionist)

        self.assertEqual(invoice.PaymentStatus, PAYMENT_PAID)
        self.assertEqual(invoice.PaymentMethod, PAYMENT_METHOD_CASH)

    def test_customer_sees_the_bill_as_paid_in_cash(self):
        services.mark_invoice_paid(self.invoice.pk, self.receptionist)

        invoice = services.get_customer_invoice(self.ref)

        self.assertTrue(invoice["is_paid"])
        self.assertEqual(invoice["payment_method"], PAYMENT_METHOD_CASH)
        self.assertEqual(invoice["total_amount"], Decimal("82.49"))
