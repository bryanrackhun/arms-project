# Booking reference codes: every appointment gets a customer-facing code.
#
# ``ReferenceCode`` must end up unique and non-empty, but the Appointments table
# may already hold rows, and a unique column cannot be added with one shared
# default (every existing row would get "" and collide). So this runs in three
# steps:
#
# 1. add the column with a throwaway "" default and no unique constraint;
# 2. give every existing appointment its own random code;
# 3. only then make the column unique and indexed.
#
# The alphabet and length are deliberately copied here rather than imported from
# ``core.models``: a migration must keep doing what it did on the day it was
# written, even if the live constants change later.

import secrets

from django.db import migrations, models

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 8


def _new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def backfill_reference_codes(apps, schema_editor):
    """Give each appointment that has no code a unique one."""
    Appointment = apps.get_model("core", "Appointment")
    taken = set(
        Appointment.objects.exclude(ReferenceCode="").values_list(
            "ReferenceCode", flat=True
        )
    )
    for pk in Appointment.objects.filter(ReferenceCode="").values_list("pk", flat=True):
        code = _new_code()
        while code in taken:
            code = _new_code()
        taken.add(code)
        Appointment.objects.filter(pk=pk).update(ReferenceCode=code)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_servicebay_inspection_repair'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='ReferenceCode',
            field=models.CharField(db_column='ReferenceCode', default='', editable=False, max_length=8),
            preserve_default=False,
        ),
        # Reversing only needs to drop the column, which the AddField reverse
        # does, so there is nothing to undo for the backfill itself.
        migrations.RunPython(backfill_reference_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='appointment',
            name='ReferenceCode',
            field=models.CharField(db_column='ReferenceCode', db_index=True, editable=False, help_text='Customer-facing booking reference; generated at booking time.', max_length=8, unique=True),
        ),
    ]
