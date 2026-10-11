"""Record Cash on invoices that were settled before payment methods existed.

Cash was the only method the counter took, so every invoice already marked Paid
was paid in cash.
"""

from django.db import migrations


def backfill_cash(apps, schema_editor):
    Invoice = apps.get_model("core", "Invoice")
    Invoice.objects.filter(PaymentStatus="Paid", PaymentMethod="").update(
        PaymentMethod="Cash"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "payment_method"),
    ]

    operations = [
        migrations.RunPython(backfill_cash, migrations.RunPython.noop),
    ]
