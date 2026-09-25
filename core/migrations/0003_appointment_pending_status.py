# Appointment status: requests now start as "Pending".
#
# Two changes travel together, because the slot-uniqueness rule is expressed in
# terms of the status values:
#
# * ``Status`` gains "Pending" as its default, set at database level, so rows
#   created outside the application still start in the waiting state.
# * The active-slot uniqueness constraint is rebuilt to count "Pending" among
#   the statuses that hold a slot, so two people cannot request the same time
#   before either request has been reviewed.
#
# A row that still carries the old "Booked" value is folded into "Pending" by
# the migration below, so an existing diary lands in the review queue rather
# than in a state the new lifecycle no longer has a transition for.

from django.db import migrations, models


def booked_to_pending(apps, schema_editor):
    """Move any legacy "Booked" row onto the waiting status."""
    Appointment = apps.get_model("core", "Appointment")
    Appointment.objects.filter(Status="Booked").update(Status="Pending")


def pending_to_booked(apps, schema_editor):
    """Reverse: the waiting status was previously stored as "Booked"."""
    Appointment = apps.get_model("core", "Appointment")
    Appointment.objects.filter(Status="Pending").update(Status="Booked")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_vehicle_appointment'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='appointment',
            name='uq_appointments_active_slot',
        ),
        migrations.AlterField(
            model_name='appointment',
            name='Status',
            field=models.CharField(choices=[('Pending', 'Pending'), ('Confirmed', 'Confirmed'), ('Checked In', 'Checked In'), ('Completed', 'Completed'), ('Cancelled', 'Cancelled'), ('No Show', 'No Show')], db_column='Status', default='Pending', help_text='Where the appointment is in its lifecycle.', max_length=20),
        ),
        migrations.RunPython(booked_to_pending, pending_to_booked),
        migrations.AddConstraint(
            model_name='appointment',
            constraint=models.UniqueConstraint(condition=models.Q(('Status__in', ['Pending', 'Confirmed', 'Checked In', 'Completed'])), fields=('AppointmentDate', 'AppointmentTime'), name='uq_appointments_active_slot'),
        ),
    ]
