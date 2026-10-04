"""Seed the database with the first ARMS data.

* ``Roles``:    Administrator, Receptionist and Technician.
* ``Staffs``:   one initial administrator account and one initial receptionist
                account, each with a hashed password, created here rather than
                through the UI.
* ``Services``: a handful of common services, so technician-service
                assignments can be made straight away.
* ``Vehicles`` and ``Appointments`` (Timebox 2): a few sample records so the
                booking desk, check-in and vehicle history screens have data
                to show immediately after setup.
* ``ServiceBay`` (Timebox 3): the workshop's bays. Their status is derived from
  the jobs they hold rather than set by hand, so after seeding the bays carrying
  active work read Occupied and the rest read Available.
* ``Repairs``, ``Inspections``, ``Parts``, ``Parts_usage`` and ``Invoices``
  (Timebox 3/4): sample records chosen so that every stage of a job's
  lifecycle - and every status the screens can report - already exists once
  the command finishes, so the system is demonstrable without any clicking.
  The seven demo appointments walk the public status page from Booked through
  to Completed (with a paid invoice) and Cancelled.

The command is idempotent: running it again leaves existing rows untouched and
only fills in what is missing, so it is safe to re-run on a database that
already has data. The demo appointments below carry fixed, memorable reference
codes (``DEMOBOOK``, ``DEMOPROG``, ...) so they can be typed straight into the
public "check repair status" page during a demonstration.

Run with::

    python manage.py seed_arms
    python manage.py seed_arms --admin-email admin@shop.local \
        --admin-password 'Secret123!'
"""

from datetime import time as dt_time, timedelta
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as dj_timezone

from core.models import (
    ACTIVE_REPAIR_STATUSES,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CHECKED_IN,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_PENDING,
    BAY_AVAILABLE,
    BAY_OCCUPIED,
    PAYMENT_PAID,
    PAYMENT_PENDING,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_NOT_STARTED,
    REPAIR_PRIORITY_ROUTINE,
    REPAIR_PRIORITY_URGENT,
    REFERENCE_CODE_MAX_ATTEMPTS,
    Appointment,
    Inspection,
    Invoice,
    Parts,
    PartsUsage,
    Repair,
    Role,
    Service,
    ServiceBay,
    Staff,
    Technician,
    TechnicianService,
    Vehicle,
    generate_reference_code,
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

        # Kept on the command instance: the sample-data helpers look the
        # receptionist up by email to attribute the seeded invoice to them.
        self.reception_email = reception_email

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
            # Quoted in Myanmar Kyat (MMK) at realistic local workshop prices.
            service_rows = [
                ("Oil Change", "35000.00", 30),
                ("Brake Service", "75000.00", 90),
                ("Engine Diagnostics", "120000.00", 60),
                ("Tyre Rotation", "40000.00", 20),
                ("Annual Service", "160000.00", 180),
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
            # Its vehicles and the receptionist account are handed back so the
            # Timebox 3/4 sample data can be built on the same rows rather than
            # re-querying them.
            billing = self._seed_timebox2(seeded_services)

            # Timebox 3: technician accounts, qualifications and the repair jobs
            # that put the bays to work.
            jobs = self._seed_technicians(roles[Role.TECHNICIAN], seeded_services)
            bays = self._seed_timebox3()
            repairs = self._seed_repair_jobs(jobs, bays)

            # Timebox 4: inspections, spare parts, the stock drawn against the
            # jobs and the invoice for the finished one.
            self._seed_inspections(repairs)
            parts = self._seed_parts()
            self._seed_parts_usage(repairs, parts, jobs)
            self._seed_invoices(repairs, jobs, billing["reception"])

        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def _seed_timebox2(self, seeded_services):
        """Add sample vehicles and appointments (Timebox 2).

        Dates are relative to today so the seeded diary always shows upcoming
        bookings, and the sample slots are distinct to respect the active-slot
        uniqueness constraint. Rows are matched by their natural keys (VIN for
        vehicles, date/time for appointments), so a re-run adds nothing.

        Returns a dictionary of the rows later sections need, so the caller is
        not left re-deriving them: the vehicles keyed by VIN and the
        receptionist account the sample invoice is attributed to.
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

        # Two extra vehicles for the Timebox 3/4 demo jobs: one for the repair
        # that is still in progress, one for the job that has been finished and
        # billed. Keeping them separate from the two above leaves the booking
        # desk's own sample appointments untouched.
        demo_vehicles = [
            ("3VWFE21C04M000123", "GHI-3456", "Volkswagen", "Golf", 2019, "White"),
            ("1FTFW1ET5DFC10312", "DEF-9012", "Ford", "Ranger", 2021, "Grey"),
        ]
        for vin, plate, make, model, year, colour in demo_vehicles:
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
            appointment, created = Appointment.objects.get_or_create(
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
                    # The column is unique and has no database default, so a
                    # row created directly must be given a code or it would sit
                    # on the placeholder empty string.
                    "ReferenceCode": self._unique_reference_code(),
                },
            )
            # A row seeded before codes were supplied may still hold the
            # placeholder; give it a real one so the lookup page can find it.
            if not appointment.ReferenceCode:
                appointment.ReferenceCode = self._unique_reference_code()
                appointment.save(update_fields=["ReferenceCode", "UpdatedAt"])
            if created:
                self.stdout.write(
                    f"  Created         appointment {first} {last} "
                    f"({appointment_date} {slot}, {status})"
                )

        # The demo bookings that carry a fixed reference code, so the public
        # status page can be demonstrated without hunting for a generated one.
        self._seed_demo_appointments(vehicles, seeded_services)

        # The receptionist account was created by ``handle`` before this ran, so
        # it is looked up again rather than passed in: the caller needs the row
        # and the password together to record who raised the sample invoice.
        reception = Staff.objects.get(Email=self.reception_email)

        return {
            "vehicles": vehicles,
            "reception": reception,
        }

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

        # Returned so the repair-job sample data can park a job in a specific
        # bay and have the occupancy rules demonstrated without manual setup.
        return {
            bay.BayName: bay for bay in ServiceBay.objects.order_by("BayID")
        }

    def _unique_reference_code(self) -> str:
        """Return a booking reference code no appointment is using yet.

        A booking taken through the UI gets its code from the service layer; a
        row created directly here would otherwise fall back to an empty string,
        and the column is unique, so the second such row would fail. Drawing a
        fresh code and checking it against the table keeps the seeded
        appointments as valid as any the workshop takes itself.
        """
        for _attempt in range(REFERENCE_CODE_MAX_ATTEMPTS * 4):
            code = generate_reference_code()
            if not Appointment.objects.filter(ReferenceCode=code).exists():
                return code
        raise CommandError(
            "Could not find an unused booking reference code; the appointments "
            "table may be holding placeholder rows."
        )

    def _ensure_appointment(
        self,
        vehicles,
        seeded_services,
        *,
        reference_code,
        first,
        last,
        phone,
        mail,
        service_name,
        days_ago,
        slot,
        status,
        vin=None,
        cancel_reason="",
        label=None,
    ):
        """Create or fetch one demo appointment keyed by its reference code.

        The reference code is the natural key: it is unique, it is the value the
        demonstration types into the public status page, and matching on it
        means a re-run can never raise a second copy of the same demo booking
        even if its date is recalculated. Everything else is set only when the
        row is first created, so a date that has since passed is not silently
        changed on a later run.
        """
        appointment, created = Appointment.objects.get_or_create(
            ReferenceCode=reference_code,
            defaults={
                "CustomerFirstName": first,
                "CustomerLastName": last,
                "CustomerPhone": phone,
                "CustomerEmail": mail,
                "ServiceCode": seeded_services[service_name],
                "AppointmentDate": dj_timezone.localdate() - timedelta(days=days_ago),
                "AppointmentTime": dt_time(*slot),
                "Status": status,
                "VehicleID": vehicles.get(vin) if vin else None,
                "CancelReason": cancel_reason,
            },
        )
        if created:
            self.stdout.write(
                f"  Created         appointment {reference_code} "
                f"({label or last}, {status})"
            )
        return appointment

    def _seed_demo_appointments(self, vehicles, seeded_services):
        """Add the fixed-reference demo appointments (Timebox 2/3).

        One booking is placed in each of the five appointment states, so the
        booking desk's queue, the check-in screen and the public status lookup
        all have a real row in every state they can report - in particular the
        two the booking desk cannot produce on its own: a cancelled booking and
        a completed one. The date and time are distinct and fixed (not relative
        to today) so the rows are stable under re-running.
        """
        for reference_code, first, last, phone, mail, service_name, days_ago, slot, status, vin, reason, label in (
            # The earliest stage - booked but not yet accepted.
            ("DEMOBOOK", "Dana", "Wu", "0400666777", "dana@example.com",
             "Oil Change", 1, (9, 0), APPOINTMENT_PENDING, None, "", None),
            # Checked in with a vehicle matched, and deliberately no repair job:
            # this is the "vehicle checked in, work not raised" state.
            ("DEMOCHKD", "Farid", "Osei", "0400777888", "farid@example.com",
             "Tyre Rotation", 1, (11, 15), APPOINTMENT_CHECKED_IN,
             "3VWFE21C04M000123", "", None),
            # Checked in, job raised and a technician on it, nothing inspected.
            ("DEMOJOBA", "Lena", "Fischer", "0400888999", "lena@example.com",
             "Oil Change", 1, (13, 30), APPOINTMENT_CHECKED_IN,
             "1HGBH41JXMN109186", "", None),
            # Checked in and an inspection recorded, still Not Started.
            ("DEMOINSP", "Tomas", "Silva", "0400999000", "tomas@example.com",
             "Brake Service", 2, (8, 45), APPOINTMENT_CHECKED_IN,
             "JH4KA7561PC008079", "", None),
            # Work under way on the second technician's Volkswagen.
            ("DEMOPROG", "Priya", "Anand", "0400111000", "priya@example.com",
             "Annual Service", 2, (10, 15), APPOINTMENT_CHECKED_IN,
             "3VWFE21C04M000123", "", None),
            # Finished and billed: the whole happy path in one row.
            ("DEMODONE", "Grace", "Liu", "0400222000", "grace@example.com",
             "Engine Diagnostics", 3, (15, 0), APPOINTMENT_COMPLETED,
             "1FTFW1ET5DFC10312", "", None),
            # Cancelled, with a reason, so the cancellation short-circuit can be
            # shown without cancelling a real booking live.
            ("DEMOCANX", "Marcus", "Bell", "0400333000", "marcus@example.com",
             "Engine Diagnostics", 5, (16, 30), APPOINTMENT_CANCELLED,
             None, "Customer rescheduled by phone", None),
        ):
            self._ensure_appointment(
                vehicles,
                seeded_services,
                reference_code=reference_code,
                first=first,
                last=last,
                phone=phone,
                mail=mail,
                service_name=service_name,
                days_ago=days_ago,
                slot=slot,
                status=status,
                vin=vin,
                cancel_reason=reason,
                label=f"{first} {last}",
            )

    def _seed_technicians(self, technician_role, seeded_services):
        """Add sample technician accounts and their service qualifications.

        Two fully-established technicians are created, plus one deliberately
        incomplete account that carries the Technician role but has **no**
        ``Technician`` row. That third account is what lets a payroll run show
        its "no hourly rate recorded, so excluded" path without anyone having to
        fabricate it by hand.

        Each established technician is left unqualified for at least one
        service, so a request to assign the wrong person can be refused live
        rather than only in a test.

        Returns a dictionary of ``(staff, technician)`` pairs keyed by the short
        name used in the sample repair data.
        """
        # Hourly rates in Myanmar Kyat (MMK), matching local workshop pay.
        qualifications = {
            "ravi": ("ravi@shop.local", "Ravi", "Khan", "5500.00",
                     ("Oil Change", "Tyre Rotation", "Annual Service")),
            "maria": ("maria@shop.local", "Maria", "Osei", "4000.00",
                      ("Brake Service", "Engine Diagnostics", "Annual Service")),
        }

        seeded = {}
        for key, (mail, first, last, rate, qualified) in qualifications.items():
            staff, created = Staff.objects.get_or_create(
                Email=mail,
                defaults={
                    "RoleID": technician_role,
                    "FirstName": first,
                    "LastName": last,
                    "Phone": "0400555010",
                    "Password": make_password("Tech1234!"),
                    "IsActive": True,
                },
            )
            if created:
                self.stdout.write(f"  Created         staff '{mail}' (Technician)")

            technician, tech_created = Technician.objects.get_or_create(
                StaffID=staff,
                defaults={"Hourly_rate": Decimal(rate)},
            )
            if tech_created:
                self.stdout.write(
                    f"  Created         technician record for {first} {last}"
                )

            for service_name in qualified:
                _, link_created = TechnicianService.objects.get_or_create(
                    TechnicianID=technician,
                    ServiceCode=seeded_services[service_name],
                )
                if link_created:
                    self.stdout.write(
                        f"  Created         {first} {last} qualified for "
                        f"'{service_name}'"
                    )

            seeded[key] = (staff, technician)

        # The deliberately incomplete account: right role, no technician record.
        incomplete, created = Staff.objects.get_or_create(
            Email="sam@shop.local",
            defaults={
                "RoleID": technician_role,
                "FirstName": "Sam",
                "LastName": "Patel",
                "Phone": "0400555011",
                "Password": make_password("Tech1234!"),
                "IsActive": True,
            },
        )
        if created:
            self.stdout.write(
                "  Created         staff 'sam@shop.local' (Technician, no pay rate)"
            )
        seeded["sam"] = (incomplete, None)

        return seeded

    def _ensure_repair(self, appointment, *, technician=None, bay=None,
                       priority=REPAIR_PRIORITY_ROUTINE,
                       status=REPAIR_NOT_STARTED):
        """Create or fetch one demo repair job keyed by its appointment.

        An appointment has at most one repair job, so the appointment is the
        natural key. The job's lifecycle status is only set on creation; a
        completed job's completion moment is stamped separately (see
        :meth:`_seed_invoices`) because it is what payroll reads.
        """
        repair, created = Repair.objects.get_or_create(
            AppointmentID=appointment,
            defaults={
                "TechnicianID": technician,
                "BayID": bay,
                "RepairPriority": priority,
                "Status": status,
            },
        )
        if created:
            label = appointment.ReferenceCode or f"appointment #{appointment.pk}"
            self.stdout.write(
                f"  Created         repair job #{repair.pk} for {label} ({status})"
            )
        return repair

    def _seed_repair_jobs(self, jobs, bays):
        """Raise the sample repair jobs that demonstrate the whole lifecycle.

        Five jobs are built against the demo appointments, covering: no job yet
        (``DEMOCHKD`` is deliberately left alone), a job with a technician but
        no inspection, a job with an inspection still Not Started, a job In
        Progress, and a job Completed in a released bay. Bay 1 and Bay 3 end up
        held by active work; Bay 2 is free, which is the spread the bay
        occupancy rules need to be shown.

        Returns the jobs keyed by reference code so the inspection, parts-usage
        and invoice sections can reference them.
        """
        ravi_staff, ravi = jobs["ravi"]
        maria_staff, maria = jobs["maria"]

        repairs = {}

        # DEMOJOBA: technician assigned, nothing inspected yet.
        repairs["DEMOJOBA"] = self._ensure_repair(
            Appointment.objects.get(ReferenceCode="DEMOJOBA"),
            technician=ravi,
            bay=bays["Bay 1"],
            status=REPAIR_NOT_STARTED,
        )

        # DEMOINSP: inspection recorded, work not started, raised as Urgent so
        # the job board shows an urgent row at the top.
        repairs["DEMOINSP"] = self._ensure_repair(
            Appointment.objects.get(ReferenceCode="DEMOINSP"),
            technician=maria,
            bay=bays["Bay 2"],
            priority=REPAIR_PRIORITY_URGENT,
            status=REPAIR_NOT_STARTED,
        )

        # DEMOPROG: work under way.
        repairs["DEMOPROG"] = self._ensure_repair(
            Appointment.objects.get(ReferenceCode="DEMOPROG"),
            technician=ravi,
            bay=bays["Bay 3"],
            status=REPAIR_IN_PROGRESS,
        )

        # DEMODONE: finished, and deliberately *not* parked in a bay - finishing
        # a job releases it, so the seeded state must match that or the bay
        # screen would lie.
        repairs["DEMODONE"] = self._ensure_repair(
            Appointment.objects.get(ReferenceCode="DEMODONE"),
            technician=maria,
            bay=None,
            status=REPAIR_COMPLETED,
        )

        # A bay's stored status is derived from the jobs it holds, never set
        # directly - the service layer recomputes it on every assignment. The
        # same derivation is applied here, so a seeded bay reports Occupied for
        # exactly the bays that carry active work and no other.
        for name, bay in bays.items():
            holds_active = bay.repairs.filter(
                Status__in=ACTIVE_REPAIR_STATUSES
            ).exists()
            wanted = BAY_OCCUPIED if holds_active else BAY_AVAILABLE
            if bay.Status != wanted:
                bay.Status = wanted
                bay.save(update_fields=["Status"])
                self.stdout.write(f"  Updated         bay '{name}' -> {wanted}")

        return repairs

    def _seed_inspections(self, repairs):
        """Record an inspection against each demo job that should have one.

        One inspection per job, linked back through ``Repairs.InspectionID``,
        with no photographs: the plan calls for the text of the inspection to be
        present and the image slots left empty, since attaching real files is
        not something a seed should invent. A re-run leaves an existing record
        unchanged rather than appending a second one - a job may only be
        inspected once.

        ``VehicleID`` is required, so it is taken from the job's appointment:
        an inspection records the condition of the car that was booked in.
        """
        descriptions = {
            "DEMOINSP": (
                "Pre-existing scuff on rear bumper, noted before work began. "
                "No other damage found."
            ),
            "DEMOPROG": (
                "Minor oil residue around the sump plug; otherwise vehicle is "
                "in good condition."
            ),
            "DEMODONE": "No pre-existing damage found at intake.",
        }

        for reference_code, description in descriptions.items():
            repair = repairs[reference_code]
            if repair.InspectionID_id is not None:
                continue

            inspection = Inspection.objects.create(
                VehicleID=repair.AppointmentID.VehicleID,
                Description=description,
            )
            repair.InspectionID = inspection
            repair.save(update_fields=["InspectionID", "UpdatedAt"])
            self.stdout.write(
                f"  Created         inspection for repair job #{repair.pk} "
                f"({reference_code})"
            )

    def _seed_parts(self):
        """Add the spare-parts catalogue with deliberately mixed stock levels.

        Matched by part name, so a re-run adds nothing. The levels are chosen so
        the low-stock page, the zero-stock refusal and the deactivated-but-used
        case are all visible straight after seeding:

        * Brake Pad Set sits at its reorder threshold, so it flags immediately;
        * Wiper Blade (pair) is out of stock and also below its threshold;
        * Old Air Filter (Discontinued) is deactivated and carries a little
          stock, so the usage below can be drawn against it and leave it with
          history - which is why deletion is not offered for a used part.

        Returns the parts keyed by name for the usage section to reference.
        """
        # Unit prices in Myanmar Kyat (MMK) at realistic local scale.
        part_rows = [
            ("Brake Pad Set", "180000.00", 2, 5, True),
            ("Oil Filter", "30000.00", 18, 5, True),
            ("Wiper Blade (pair)", "40000.00", 0, 3, True),
            ("Spark Plug", "20000.00", 24, 8, True),
            ("Old Air Filter (Discontinued)", "50000.00", 3, 0, False),
        ]

        parts = {}
        for name, price, stock, threshold, active in part_rows:
            part, created = Parts.objects.get_or_create(
                PartName=name,
                defaults={
                    "UnitPrice": Decimal(price),
                    "StockQty": stock,
                    "ReorderThreshold": threshold,
                    "IsActive": active,
                },
            )
            parts[name] = part
            if created:
                self.stdout.write(
                    f"  Created         part '{name}' (stock {stock}, "
                    f"threshold {threshold})"
                )

        return parts

    def _seed_parts_usage(self, repairs, parts, jobs):
        """Draw seeded stock against the demo jobs and record the lines.

        Each line mirrors what :func:`core.services.record_parts_usage` does:
        the quantity is checked against the stock actually held, the part's
        current unit price is copied onto the row, and ``StockQty`` is reduced
        by the quantity used. That keeps the seeded totals identical to the ones
        the service layer would produce, so an invoice generated afterwards
        matches what the demo already shows.

        Usage is only written for a job's stock when the part existed; the
        catalogue's stated stock is the *post*-usage figure, which is why the
        decrement happens here rather than at part creation. Lines are matched
        by job and part, so a re-run neither duplicates a line nor draws the
        same stock down twice.
        """
        # (repair reference, part name, quantity, who recorded it)
        usage_rows = [
            ("DEMOPROG", "Oil Filter", 1, jobs["ravi"][0]),
            ("DEMODONE", "Brake Pad Set", 2, jobs["maria"][0]),
            ("DEMODONE", "Old Air Filter (Discontinued)", 1, jobs["maria"][0]),
        ]

        for reference_code, part_name, quantity, recorded_by in usage_rows:
            repair = repairs[reference_code]
            part = parts[part_name]

            if PartsUsage.objects.filter(
                RepairID=repair, PartID=part
            ).exists():
                continue

            if quantity > part.StockQty:
                # Only reachable if someone edited the catalogue down before
                # seeding; reported rather than crashed, because the seed's job
                # is to describe a coherent shop, not to fight one.
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skipped         usage of {quantity} x {part_name} "
                        f"({part.StockQty} in stock)"
                    )
                )
                continue

            PartsUsage.objects.create(
                RepairID=repair,
                PartID=part,
                Qty_Used=quantity,
                # Copied, never referenced: this is what freezes the price and
                # matches the service layer's behaviour.
                Unit_Price=part.UnitPrice,
                RecordedBy=recorded_by,
            )
            Parts.objects.filter(pk=part.pk).update(
                StockQty=part.StockQty - quantity,
                UpdatedAt=dj_timezone.now(),
            )
            part.refresh_from_db()
            self.stdout.write(
                f"  Created         parts usage {quantity} x {part_name} on "
                f"repair job #{repair.pk}"
            )

        return parts

    def _seed_invoices(self, repairs, jobs, reception_staff):
        """Bill the finished demo job and mark it paid.

        This is the one seeded invoice: a full happy path, so the invoice list,
        the customer status page and a payroll run all have something real to
        show. Every other finished job is deliberately left unbilled so the
        "generate invoice" screen still has work to do during a demonstration.

        ``PaymentStatus`` is only set to Paid on creation; a re-run never
        re-stamps ``PaidAt``, so the collected-on date stays fixed.

        The completion moment is also stamped here, and moved onto the payroll
        window: a Completed job with no completion date would be invisible to a
        payroll run, which is the one thing a demo of that feature must not be.
        """
        repair = repairs["DEMODONE"]

        if not repair.CompletedAt:
            repair.CompletedAt = dj_timezone.now() - timedelta(days=3)
            repair.save(update_fields=["CompletedAt", "UpdatedAt"])
            self.stdout.write(
                f"  Stamped         completion date for repair job #{repair.pk}"
            )

        if Invoice.objects.filter(RepairID=repair).exists():
            self.stdout.write(
                f"  Already exists  invoice for repair job #{repair.pk}"
            )
            return

        # The same figures core.services.calculate_invoice_amounts produces:
        # the service's base cost as labour, the summed usage lines as parts,
        # and tax on their total.
        service = repair.AppointmentID.ServiceCode
        labour_cost = Decimal(service.BaseCost)
        parts_cost = sum(
            (line.Qty_Used * line.Unit_Price for line in repair.parts_usage.all()),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"))
        tax_rate = Decimal("10.00")
        tax_amount = ((labour_cost + parts_cost) * tax_rate / Decimal("100")).quantize(
            Decimal("0.01")
        )
        total_amount = labour_cost + parts_cost + tax_amount

        invoice = Invoice.objects.create(
            RepairID=repair,
            LabourCost=labour_cost,
            PartsCost=parts_cost,
            TaxRate=tax_rate,
            TaxAmount=tax_amount,
            TotalAmount=total_amount,
            PaymentStatus=PAYMENT_PENDING,
            GeneratedBy=reception_staff,
        )
        self.stdout.write(
            f"  Created         invoice #{invoice.pk} for repair job #{repair.pk} "
            f"({invoice.TotalAmount})"
        )

        # The customer has settled it: PaidAt is stamped once, now. ``UpdatedAt``
        # is auto_now, so it refreshes itself and must not be named here.
        invoice.PaymentStatus = PAYMENT_PAID
        if invoice.PaidAt is None:
            invoice.PaidAt = dj_timezone.now()
        invoice.save(update_fields=["PaymentStatus", "PaidAt"])
        self.stdout.write(f"  Marked paid     invoice #{invoice.pk}")
