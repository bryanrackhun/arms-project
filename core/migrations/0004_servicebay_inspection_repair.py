# Timebox 3 - repair jobs, vehicle inspections and service bays.
#
# Three new tables, all pointing back at work Timebox 2 already built:
#
# * ``ServiceBay`` stores its own availability, so "which bays are free?" is a
#   single filtered read instead of a join over every active repair.
# * ``Inspections`` keeps three image columns rather than a child table, because
#   the three-photograph limit is a hard schema rule; the upload path groups the
#   files by inspection.
# * ``Repairs`` links a checked-in appointment to its technician, bay and
#   inspection. Three of those four keys are nullable - a job is raised when the
#   vehicle arrives, but a qualified technician and a free bay may follow
#   moments later. A unique constraint on ``AppointmentID`` means one repair job
#   per appointment, so the same checked-in car cannot be raised twice.
#
# Adding tables only, with no change to existing columns, so existing rows are
# untouched and the migration is reversible without data loss.

import core.models
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_appointment_pending_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='ServiceBay',
            fields=[
                ('BayID', models.AutoField(db_column='BayID', primary_key=True, serialize=False)),
                ('BayName', models.CharField(db_column='BayName', help_text="Must be unique, e.g. 'Bay 1'.", max_length=40, unique=True)),
                ('Status', models.CharField(choices=[('Available', 'Available'), ('Occupied', 'Occupied')], db_column='Status', default='Available', help_text='Occupied while an active repair job is assigned to the bay.', max_length=20)),
            ],
            options={
                'verbose_name': 'Service bay',
                'verbose_name_plural': 'Service bays',
                'db_table': 'ServiceBay',
                'ordering': ['BayName'],
            },
        ),
        migrations.CreateModel(
            name='Inspection',
            fields=[
                ('InspectionID', models.AutoField(db_column='InspectionID', primary_key=True, serialize=False)),
                ('Description', models.TextField(db_column='Description', help_text="Written record of the vehicle's condition.")),
                ('Image1', models.ImageField(blank=True, db_column='Image1', help_text='Optional photograph 1 of up to 3.', null=True, upload_to=core.models.inspection_image_path)),
                ('Image2', models.ImageField(blank=True, db_column='Image2', help_text='Optional photograph 2 of up to 3.', null=True, upload_to=core.models.inspection_image_path)),
                ('Image3', models.ImageField(blank=True, db_column='Image3', help_text='Optional photograph 3 of up to 3.', null=True, upload_to=core.models.inspection_image_path)),
                ('CreatedAt', models.DateTimeField(auto_now_add=True, db_column='CreatedAt')),
                ('UpdatedAt', models.DateTimeField(auto_now=True, db_column='UpdatedAt')),
                ('VehicleID', models.ForeignKey(db_column='VehicleID', help_text='The vehicle whose condition was recorded.', on_delete=django.db.models.deletion.PROTECT, related_name='inspections', to='core.vehicle')),
            ],
            options={
                'verbose_name': 'Inspection',
                'verbose_name_plural': 'Inspections',
                'db_table': 'Inspections',
                'ordering': ['-CreatedAt', 'InspectionID'],
            },
        ),
        migrations.CreateModel(
            name='Repair',
            fields=[
                ('RepairID', models.AutoField(db_column='RepairID', primary_key=True, serialize=False)),
                ('RepairPriority', models.CharField(choices=[('Urgent', 'Urgent'), ('Routine', 'Routine')], db_column='RepairPriority', default='Routine', help_text='Set once when the job is created.', max_length=20)),
                ('Status', models.CharField(choices=[('Not Started', 'Not Started'), ('In Progress', 'In Progress'), ('Completed', 'Completed')], db_column='Status', default='Not Started', help_text='Moves Not Started -> In Progress -> Completed.', max_length=20)),
                ('CreatedAt', models.DateTimeField(auto_now_add=True, db_column='CreatedAt')),
                ('UpdatedAt', models.DateTimeField(auto_now=True, db_column='UpdatedAt')),
                ('AppointmentID', models.ForeignKey(db_column='AppointmentID', help_text='The checked-in appointment this job was raised for.', on_delete=django.db.models.deletion.CASCADE, related_name='repairs', to='core.appointment')),
                ('InspectionID', models.ForeignKey(blank=True, db_column='InspectionID', help_text='Set once the assigned technician completes the inspection.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='repairs', to='core.inspection')),
                ('TechnicianID', models.ForeignKey(blank=True, db_column='TechnicianID', help_text='Optional until a qualified technician is assigned.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='repairs', to='core.technician')),
                ('BayID', models.ForeignKey(blank=True, db_column='BayID', help_text='Optional until a service bay is free.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='repairs', to='core.servicebay')),
            ],
            options={
                'verbose_name': 'Repair job',
                'verbose_name_plural': 'Repair jobs',
                'db_table': 'Repairs',
                'ordering': ['-CreatedAt', 'RepairID'],
                'constraints': [models.UniqueConstraint(fields=('AppointmentID',), name='uq_repairs_appointment')],
            },
        ),
    ]
