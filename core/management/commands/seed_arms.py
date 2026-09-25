"""Seed the database with the initial ARMS data.

* ``Roles``:    Administrator, Receptionist and Technician.
* ``Staffs``:   one initial administrator account and one initial receptionist
                account, each with a hashed password, created here rather than
                through the UI.
* ``Services``: a handful of common services, so technician-service
                assignments can be made straight away.
* ``Vehicles`` and ``Appointments`` (Timebox 2): a few sample records so the
                booking desk, check-in and vehicle history screens have data
                to show immediately after setup.
* ``ServiceBay`` (Timebox 3): the workshop's bays, all starting Available, so a
  repair job has somewhere to be assigned straight after setup.

The command is idempotent: running it again leaves existing rows untouched and
only fills in what is missing, so it is safe to re-run on a database that
already has data.

Run with::

    python manage.py seed_arms
    python manage.py seed_arms --admin-email admin@shop.local \
        --admin-password 'Secret123!'
"""

from datetime import time as dt_time, timedelta

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as dj_timezone

from core.models import (
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_PENDING,
    BAY_AVAILABLE,
    Appointment,
    Role,
    Service,
    ServiceBay,
    Staff,
    Vehicle,
)


class Command(BaseCommand):
    help = "Insert the ARMS seed data: roles, services and the initial administrator account."

    def add_arguments(self, parser):
        parser.add_argument("--admin-email", default="admin@shop.local")
        parser.add_argument("--admin-password", default="Admin123!")
        parser.add_argument("--admin-first-name", default="System")
        parser.add_argument("--admin-last-name", default="Administrator")
        parser.add_argument("--reception-email", default="reception@shop.local")
        parser.add_argument("--reception-password", default="Reception123!")
        parser.add_argument("--reception-first-name", default="Rachel")
        parser.add_argument("--reception-last-name", default="Receptionist")

    def handle(self, *args, **options):
        email = options["admin_email"].strip().lower()
        password = options["admin_password"]
        if len(password) < 8:
            raise CommandError("The administrator password must be at least 8 characters.")
        reception_email = options["reception_email"].strip().lower()
        reception_password = options["reception_password"]
        if len(reception_password) < 8:
            raise CommandError("The receptionist password must be at least 8 characters.")

        with transaction.atomic():
            # Roles: the three system roles, idempotent.
            roles = {}
            for name in (Role.ADMINISTRATOR, Role.RECEPTIONIST, Role.TECHNICIAN):
                role, created = Role.objects.get_or_create(RoleName=name)
                roles[name] = role
                action = "Created" if created else "Already exists"
                self.stdout.write(f"  {action:<15} role '{name}' (RoleID={role.RoleID})")

            # Services: a sensible starting set so assignments can be made.
            # Kept in a name-keyed dict so the Timebox 2 sample appointments
            # below can reference them without extra queries.
            seeded_services = {}
            service_rows = [
                ("Oil Change", "49.99", 30),
                ("Brake Service", "149.99", 90),
                ("Engine Diagnostics", "89.99", 60),
                ("Tyre Rotation", "29.99", 20),
                ("Annual Service", "249.99", 180),
            ]
            for name, cost, minutes in service_rows:
                service, created = Service.objects.get_or_create(
                    ServiceName=name,
                    defaults={"BaseCost": cost, "Est_Duration": minutes},
                )
                seeded_services[name] = service
                if created:
                    self.stdout.write(f"  Created         service '{name}'")

            # Staffs: one initial administrator with a hashed password.
            admin_role = roles[Role.ADMINISTRATOR]
            admin_account, created = Staff.objects.get_or_create(
                Email=email,
                defaults={
                    "RoleID": admin_role,
                    "FirstName": options["admin_first_name"],
                    "LastName": options["admin_last_name"],
                    "Password": make_password(password),  # salted hash
                    "IsActive": True,
                },
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Created         administrator account '{email}'"
                    )
                )
            else:
                self.stdout.write(
                    f"  Already exists  administrator account '{email}' (unchanged)"
                )

            # Staffs: one initial receptionist so the booking desk (Timebox 2)
            # can be exercised without first creating the account in the UI.
            reception_account, created = Staff.objects.get_or_create(
                Email=reception_email,
                defaults={
                    "RoleID": roles[Role.RECEPTIONIST],
                    "FirstName": options["reception_first_name"],
                    "LastName": options["reception_last_name"],
                    "Password": make_password(reception_password),  # salted hash
                    "IsActive": True,
                },
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Created         receptionist account '{reception_email}'"
                    )
                )
            else:
                self.stdout.write(
                    f"  Already exists  receptionist account '{reception_email}' (unchanged)"
                )

            # Timebox 2: sample vehicles and appointments for the booking desk.
            self._seed_timebox2(seeded_services)

        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def _seed_timebox2(self, seeded_services):
        """Add sample vehicles and appointments (Timebox 2).

        Dates are relative to today so the seeded diary always shows upcoming
        bookings, and the sample slots are distinct to respect the active-slot
        uniqueness constraint. Rows are matched by their natural keys (VIN for
        vehicles, date/time for appointments), so a re-run adds nothing.
        """
        today = dj_timezone.localdate()

        vehicles = {}
        vehicle_rows = [
            ("1HGBH41JXMN109186", "ABC-1234", "Toyota", "Corolla", 2018, "Silver"),
            ("JH4KA7561PC008079", "XYZ-5678", "Honda", "Civic", 2020, "Blue"),
        ]
        for vin, plate, make, model, year, colour in vehicle_rows:
            vehicle, created = Vehicle.objects.get_or_create(
                VIN=vin,
                defaults={
                    "LicensePlate": plate,
                    "Make": make,
                    "Model": model,
                    "Year": year,
                    "Colour": colour,
                },
            )
            vehicles[vin] = vehicle
            if created:
                self.stdout.write(f"  Created         vehicle '{plate}' ({make} {model})")

        # (first, last, phone, email, service, day offset, slot, status, VIN)
        # The first two rows are the state a booking actually starts in, so the
        # review queue has something to accept straight after seeding; the third
        # shows a request already accepted by the workshop.
        appointment_rows = [
            ("Alice", "Nguyen", "0400111222", "alice@example.com",
             "Oil Change", 0, "09:00", APPOINTMENT_PENDING, None),
            ("Ben", "Okafor", "0400333444", "ben@example.com",
             "Brake Service", 1, "10:30", APPOINTMENT_PENDING, "1HGBH41JXMN109186"),
            ("Chloe", "Martin", "0400555666", "chloe@example.com",
             "Engine Diagnostics", 2, "14:00", APPOINTMENT_CONFIRMED, "JH4KA7561PC008079"),
        ]
        for first, last, phone, mail, name, offset, slot, status, vin in appointment_rows:
            hour, minute = (int(part) for part in slot.split(":"))
            appointment_date = today + timedelta(days=offset)
            _, created = Appointment.objects.get_or_create(
                AppointmentDate=appointment_date,
                AppointmentTime=dt_time(hour, minute),
                defaults={
                    "CustomerFirstName": first,
                    "CustomerLastName": last,
                    "CustomerPhone": phone,
                    "CustomerEmail": mail,
                    "ServiceCode": seeded_services[name],
                    "Status": status,
                    "VehicleID": vehicles.get(vin) if vin else None,
                },
            )
            if created:
                self.stdout.write(
                    f"  Created         appointment {first} {last} "
                    f"({appointment_date} {slot}, {status})"
                )

        self._seed_timebox3()

    def _seed_timebox3(self):
        """Add the workshop's service bays (Timebox 3).

        Bays are matched by name, so a re-run adds nothing. They are created
        Available and stay that way until a repair job is assigned to one -
        availability is derived from the job a bay holds, never seeded directly,
        which is what keeps the stored status honest.
        """
        for name in ("Bay 1", "Bay 2", "Bay 3"):
            _, created = ServiceBay.objects.get_or_create(
                BayName=name,
                defaults={"Status": BAY_AVAILABLE},
            )
            if created:
                self.stdout.write(f"  Created         service bay '{name}'")
