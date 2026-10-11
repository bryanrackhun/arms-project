"""Load an extra, realistic sample dataset into ARMS (at least 3 rows per table).

This command sits alongside ``seed_arms`` and does not change anything that
command creates. Every row here is matched on a natural key (email, service
name, VIN, reference code, part name, bay name), so running it twice adds
nothing the second time.

Tables covered, with the number of rows added:

* Roles ............. 3  (Administrator, Receptionist, Technician - reused if present)
* Staffs ............ 7  (1 administrator, 3 receptionists, 3 technicians)
* Technicians ....... 3  (hourly rates in MMK)
* Services .......... 4
* Technician_Service  8  qualification links
* Vehicles .......... 6
* Appointments ...... 13 (every status: Pending, Confirmed, Checked In,
                          Completed, Cancelled and No Show)
* ServiceBay ........ 3
* Inspections ....... 5
* Repairs ........... 6  (Not Started, In Progress and Completed)
* Parts ............. 6
* Parts_usage ....... 5
* Invoices .......... 3  (2 Paid, 1 Pending)
* Audit_log ......... the history behind every job on file, rebuilt by
                      ``seed_audit_log`` once the rows above exist

All sample reference codes start with ``SMP`` so they are easy to type into the
public "check repair status" page during a demonstration.

Usage::

    python manage.py migrate
    python manage.py seed_arms          # optional: the original seed data
    python manage.py seed_sample_data

Every sample staff account uses the password ``Sample123!``.
"""

from datetime import datetime, time as dt_time, timedelta
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as dj_timezone

from core.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    ACTIVE_REPAIR_STATUSES,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CHECKED_IN,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_NO_SHOW,
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
)
from core.services import calculate_invoice_amounts

SAMPLE_PASSWORD = "Sample123!"

# Myanmar commercial tax rate used on the sample invoices (percent).
SAMPLE_TAX_RATE = Decimal("5.00")

# Half-hour slots inside opening hours (08:00-17:30), matching the booking rules.
_SLOTS = [dt_time(8 + (30 * i) // 60, (30 * i) % 60) for i in range(20)]


# ---------------------------------------------------------------------------
# The sample data itself
# ---------------------------------------------------------------------------

# (role, first name, last name, phone, email)
STAFF = [
    (Role.ADMINISTRATOR, "Thida", "Aung", "09 421 110 001", "thida.aung@arms.sample"),
    (Role.RECEPTIONIST, "Su Mon", "Kyaw", "09 421 110 002", "sumon.kyaw@arms.sample"),
    (Role.RECEPTIONIST, "Nandar", "Hlaing", "09 421 110 003", "nandar.hlaing@arms.sample"),
    (Role.RECEPTIONIST, "Ei Phyu", "Win", "09 421 110 004", "eiphyu.win@arms.sample"),
    (Role.TECHNICIAN, "Aung Ko", "Ko", "09 421 110 005", "aungko.ko@arms.sample"),
    (Role.TECHNICIAN, "Min Thant", "Zaw", "09 421 110 006", "minthant.zaw@arms.sample"),
    (Role.TECHNICIAN, "Zaw Lin", "Htut", "09 421 110 007", "zawlin.htut@arms.sample"),
]

# (technician email, hourly rate MMK, services they are qualified for)
TECHNICIANS = [
    ("aungko.ko@arms.sample", "8000.00",
     ("Air Conditioning Service", "Battery Replacement", "Oil Change")),
    ("minthant.zaw@arms.sample", "7500.00",
     ("Wheel Alignment", "Transmission Service", "Brake Service")),
    ("zawlin.htut@arms.sample", "6500.00",
     ("Battery Replacement", "Oil Change")),
]

# (service name, base cost MMK, estimated minutes)
SERVICES = [
    ("Air Conditioning Service", "85000.00", 60),
    ("Battery Replacement", "30000.00", 30),
    ("Wheel Alignment", "50000.00", 45),
    ("Transmission Service", "180000.00", 120),
]

# Services the qualification list relies on that come from ``seed_arms``.
# Created here too (same cost as that command) if the original seed was not run.
FALLBACK_SERVICES = [
    ("Oil Change", "35000.00", 30),
    ("Brake Service", "75000.00", 90),
]

# (VIN, plate, make, model, year, colour)
VEHICLES = [
    ("MR0FZ22G501234567", "YGN 3B-4521", "Toyota", "Hilux", 2017, "White"),
    ("JTDBR32E530045678", "MDY 2C-7788", "Toyota", "Vios", 2019, "Silver"),
    ("KMHCT41BAEU123456", "YGN 4K-1190", "Hyundai", "Accent", 2020, "Red"),
    ("JN1TANT31U0001234", "NPT 1D-3302", "Nissan", "X-Trail", 2018, "Black"),
    ("MHFXR42G880012345", "YGN 7F-6612", "Toyota", "Fortuner", 2021, "Grey"),
    ("MMBJNKB40HD012345", "YGN 5E-2087", "Mitsubishi", "Pajero", 2016, "Blue"),
]

# (reference, first, last, phone, email, service, day offset from today,
#  preferred slot, status, VIN or None, notes, cancel reason)
# Negative offsets are in the past (the car has already been in).
APPOINTMENTS = [
    # Waiting for the workshop to accept them.
    ("SMPPEND2", "Hnin", "Wai", "09 250 300 101", "hnin.wai@example.com",
     "Air Conditioning Service", 3, (9, 0), APPOINTMENT_PENDING, None,
     "A/C blowing warm air.", ""),
    ("SMPPEND3", "Ko", "Naing", "09 250 300 102", "",
     "Battery Replacement", 4, (10, 30), APPOINTMENT_PENDING, None,
     "Car struggles to start in the morning.", ""),
    ("SMPPEND4", "May", "Thu", "", "may.thu@example.com",
     "Wheel Alignment", 5, (14, 0), APPOINTMENT_PENDING, None,
     "Steering pulls to the left.", ""),
    # Accepted, car not here yet.
    ("SMPCNFM2", "Tun", "Lwin", "09 250 300 104", "tun.lwin@example.com",
     "Oil Change", 2, (8, 30), APPOINTMENT_CONFIRMED, None, "", ""),
    ("SMPCNFM3", "Yadanar", "Oo", "09 250 300 105", "yadanar.oo@example.com",
     "Transmission Service", 3, (13, 0), APPOINTMENT_CONFIRMED, None,
     "Gear change feels rough.", ""),
    # Car has arrived; repair jobs are raised against these.
    ("SMPCHKD2", "Kyaw", "Soe", "09 250 300 106", "kyaw.soe@example.com",
     "Battery Replacement", 0, (9, 30), APPOINTMENT_CHECKED_IN,
     "MMBJNKB40HD012345", "", ""),
    ("SMPCHKD3", "Phyo", "Min", "09 250 300 107", "phyo.min@example.com",
     "Brake Service", 0, (11, 0), APPOINTMENT_CHECKED_IN,
     "JN1TANT31U0001234", "Squealing noise when braking.", ""),
    ("SMPCHKD4", "Aye", "Chan", "09 250 300 108", "aye.chan@example.com",
     "Air Conditioning Service", -1, (10, 0), APPOINTMENT_CHECKED_IN,
     "MHFXR42G880012345", "", ""),
    # Finished and billed.
    ("SMPCMPL2", "Zin", "Mar", "09 250 300 109", "zin.mar@example.com",
     "Oil Change", -3, (9, 0), APPOINTMENT_COMPLETED,
     "MR0FZ22G501234567", "", ""),
    ("SMPCMPL3", "Htet", "Aung", "09 250 300 110", "htet.aung@example.com",
     "Transmission Service", -2, (13, 30), APPOINTMENT_COMPLETED,
     "JTDBR32E530045678", "", ""),
    ("SMPCMPL4", "Thiri", "San", "09 250 300 111", "thiri.san@example.com",
     "Battery Replacement", -2, (15, 0), APPOINTMENT_COMPLETED,
     "KMHCT41BAEU123456", "", ""),
    # Did not go ahead.
    ("SMPCANX2", "Moe", "Kyaw", "09 250 300 112", "moe.kyaw@example.com",
     "Wheel Alignment", -1, (16, 0), APPOINTMENT_CANCELLED, None, "",
     "Customer travelling, will rebook next month."),
    ("SMPNSHW2", "Wai", "Yan", "09 250 300 113", "wai.yan@example.com",
     "Oil Change", -4, (8, 0), APPOINTMENT_NO_SHOW, None, "", ""),
]

BAYS = ["Bay 4", "Bay 5", "Bay 6"]

# (appointment reference, technician email, bay or None, priority, status,
#  days ago it was completed or None)
REPAIRS = [
    ("SMPCHKD2", "zawlin.htut@arms.sample", "Bay 4",
     REPAIR_PRIORITY_ROUTINE, REPAIR_NOT_STARTED, None),
    ("SMPCHKD3", "minthant.zaw@arms.sample", "Bay 5",
     REPAIR_PRIORITY_URGENT, REPAIR_NOT_STARTED, None),
    ("SMPCHKD4", "aungko.ko@arms.sample", "Bay 6",
     REPAIR_PRIORITY_ROUTINE, REPAIR_IN_PROGRESS, None),
    ("SMPCMPL2", "zawlin.htut@arms.sample", None,
     REPAIR_PRIORITY_ROUTINE, REPAIR_COMPLETED, 3),
    ("SMPCMPL3", "minthant.zaw@arms.sample", None,
     REPAIR_PRIORITY_URGENT, REPAIR_COMPLETED, 2),
    ("SMPCMPL4", "aungko.ko@arms.sample", None,
     REPAIR_PRIORITY_ROUTINE, REPAIR_COMPLETED, 2),
]

# (appointment reference, inspection text)
INSPECTIONS = [
    ("SMPCHKD3", "Front pads worn to about 2 mm. Small dent on left rear door, "
                 "pre-existing and pointed out to the customer."),
    ("SMPCHKD4", "A/C gas pressure low; no visible leak at the condenser. "
                 "Bodywork in good condition."),
    ("SMPCMPL2", "Engine oil dark and below minimum. No body damage found."),
    ("SMPCMPL3", "Transmission fluid burnt smell. Scratch on front bumper, "
                 "pre-existing."),
    ("SMPCMPL4", "Battery terminals corroded, battery tested weak. "
                 "No other issues."),
]

# (part name, unit price MMK, stock AFTER the sample usage, reorder threshold)
# Car Battery and A/C Refrigerant end at or below their threshold, so the
# low-stock page has sample entries straight away.
PARTS = [
    ("Engine Oil 5W-30 (4L)", "48000.00", 19, 5),
    ("Oil Filter (Toyota)", "9500.00", 24, 6),
    ("Brake Fluid DOT4 (1L)", "15000.00", 12, 4),
    ("Car Battery 12V 60Ah", "185000.00", 2, 2),
    ("A/C Refrigerant R134a (500g)", "25000.00", 1, 2),
    ("ATF Transmission Fluid (1L)", "22000.00", 20, 8),
]

# (appointment reference, part name, quantity, recorded by technician email)
PARTS_USAGE = [
    ("SMPCHKD4", "A/C Refrigerant R134a (500g)", 1, "aungko.ko@arms.sample"),
    ("SMPCMPL2", "Engine Oil 5W-30 (4L)", 1, "zawlin.htut@arms.sample"),
    ("SMPCMPL2", "Oil Filter (Toyota)", 1, "zawlin.htut@arms.sample"),
    ("SMPCMPL3", "ATF Transmission Fluid (1L)", 4, "minthant.zaw@arms.sample"),
    ("SMPCMPL4", "Car Battery 12V 60Ah", 1, "aungko.ko@arms.sample"),
]

# (appointment reference, receptionist email, paid?)
INVOICES = [
    ("SMPCMPL2", "sumon.kyaw@arms.sample", True),
    ("SMPCMPL3", "nandar.hlaing@arms.sample", True),
    ("SMPCMPL4", "eiphyu.win@arms.sample", False),
]


class Command(BaseCommand):
    help = "Load an extra ARMS sample dataset with at least three rows per table."

    def handle(self, *args, **options):
        self.today = dj_timezone.localdate()
        with transaction.atomic():
            roles = self._roles()
            staff = self._staff(roles)
            services = self._services()
            technicians = self._technicians(staff, services)
            vehicles = self._vehicles()
            appointments = self._appointments(services, vehicles)
            bays = self._bays()
            repairs = self._repairs(appointments, technicians, bays)
            self._inspections(repairs)
            parts = self._parts()
            self._parts_usage(repairs, parts, staff)
            self._invoices(repairs, staff)
            self._refresh_bays()
        # The audit trail (NFR2) is reconstructed from the rows just written, so
        # the audit page has history to show without anyone clicking through.
        call_command("seed_audit_log", stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(
            f"Sample data loaded. Staff password for every sample account: "
            f"{SAMPLE_PASSWORD}"
        ))

    # -- helpers -----------------------------------------------------------
    def _log(self, created, text):
        if created:
            self.stdout.write(f"  Created   {text}")

    def _free_slot(self, day, preferred, exclude_pk=None):
        """Return ``preferred`` if the slot is free that day, else the next one.

        The Appointments table refuses two active bookings in the same slot, and
        the diary may already hold real bookings, so the sample data steps
        around them instead of failing.
        """
        start = _SLOTS.index(dt_time(*preferred))
        for slot in _SLOTS[start:] + _SLOTS[:start]:
            taken = Appointment.objects.filter(
                AppointmentDate=day,
                AppointmentTime=slot,
                Status__in=ACTIVE_APPOINTMENT_STATUSES,
            )
            if exclude_pk:
                taken = taken.exclude(pk=exclude_pk)
            if not taken.exists():
                return slot
        raise CommandError(f"No free appointment slot left on {day}.")

    # -- tables ------------------------------------------------------------
    def _roles(self):
        roles = {}
        for name in (Role.ADMINISTRATOR, Role.RECEPTIONIST, Role.TECHNICIAN):
            role, created = Role.objects.get_or_create(RoleName=name)
            self._log(created, f"role '{name}'")
            roles[name] = role
        return roles

    def _staff(self, roles):
        staff = {}
        password = make_password(SAMPLE_PASSWORD)
        for role, first, last, phone, email in STAFF:
            account, created = Staff.objects.get_or_create(
                Email=email,
                defaults={
                    "RoleID": roles[role],
                    "FirstName": first,
                    "LastName": last,
                    "Phone": phone,
                    "Password": password,
                    "IsActive": True,
                },
            )
            self._log(created, f"staff '{email}' ({role})")
            staff[email] = account
        return staff

    def _services(self):
        services = {}
        for name, cost, minutes in SERVICES + FALLBACK_SERVICES:
            service, created = Service.objects.get_or_create(
                ServiceName=name,
                defaults={"BaseCost": Decimal(cost), "Est_Duration": minutes},
            )
            self._log(created, f"service '{name}'")
            services[name] = service
        return services

    def _technicians(self, staff, services):
        technicians = {}
        for email, rate, qualified in TECHNICIANS:
            technician, created = Technician.objects.get_or_create(
                StaffID=staff[email],
                defaults={"Hourly_rate": Decimal(rate)},
            )
            self._log(created, f"technician record for {email} (MMK {rate}/hr)")
            for service_name in qualified:
                _, link_created = TechnicianService.objects.get_or_create(
                    TechnicianID=technician,
                    ServiceCode=services[service_name],
                )
                self._log(link_created, f"qualification {email} -> '{service_name}'")
            technicians[email] = technician
        return technicians

    def _vehicles(self):
        vehicles = {}
        for vin, plate, make, model, year, colour in VEHICLES:
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
            self._log(created, f"vehicle '{plate}' ({make} {model})")
            vehicles[vin] = vehicle
        return vehicles

    def _appointments(self, services, vehicles):
        appointments = {}
        for (ref, first, last, phone, email, service_name, offset, slot, status,
             vin, notes, reason) in APPOINTMENTS:
            existing = Appointment.objects.filter(ReferenceCode=ref).first()
            if existing:
                appointments[ref] = existing
                continue

            day = self.today + timedelta(days=offset)
            # Cancelled / no-show rows do not hold a slot, so they keep theirs.
            if status in ACTIVE_APPOINTMENT_STATUSES:
                slot_time = self._free_slot(day, slot)
            else:
                slot_time = dt_time(*slot)

            checked_in_at = None
            if status in (APPOINTMENT_CHECKED_IN, APPOINTMENT_COMPLETED):
                checked_in_at = dj_timezone.make_aware(
                    datetime.combine(day, slot_time)
                )

            appointment = Appointment.objects.create(
                ReferenceCode=ref,
                CustomerFirstName=first,
                CustomerLastName=last,
                CustomerPhone=phone,
                CustomerEmail=email,
                ServiceCode=services[service_name],
                AppointmentDate=day,
                AppointmentTime=slot_time,
                Status=status,
                VehicleID=vehicles[vin] if vin else None,
                Notes=notes,
                CancelReason=reason,
                CheckedInAt=checked_in_at,
            )
            self._log(True, f"appointment {ref} ({first} {last}, {status}, "
                            f"{day} {slot_time:%H:%M})")
            appointments[ref] = appointment
        return appointments

    def _bays(self):
        bays = {}
        for name in BAYS:
            bay, created = ServiceBay.objects.get_or_create(
                BayName=name, defaults={"Status": BAY_AVAILABLE}
            )
            self._log(created, f"service bay '{name}'")
            bays[name] = bay
        return bays

    def _repairs(self, appointments, technicians, bays):
        repairs = {}
        now = dj_timezone.now()
        for ref, tech_email, bay_name, priority, status, done_days_ago in REPAIRS:
            completed_at = None
            if done_days_ago is not None:
                completed_at = now - timedelta(days=done_days_ago)
                # Keep payroll demonstrable: stay inside the current month.
                if completed_at.date().month != self.today.month:
                    completed_at = now
            repair, created = Repair.objects.get_or_create(
                AppointmentID=appointments[ref],
                defaults={
                    "TechnicianID": technicians[tech_email],
                    "BayID": bays[bay_name] if bay_name else None,
                    "RepairPriority": priority,
                    "Status": status,
                    "CompletedAt": completed_at,
                },
            )
            self._log(created, f"repair job #{repair.pk} for {ref} ({status})")
            repairs[ref] = repair
        return repairs

    def _inspections(self, repairs):
        for ref, text in INSPECTIONS:
            repair = repairs[ref]
            if repair.InspectionID_id is not None:
                continue
            inspection = Inspection.objects.create(
                VehicleID=repair.AppointmentID.VehicleID,
                Description=text,
            )
            repair.InspectionID = inspection
            repair.save(update_fields=["InspectionID", "UpdatedAt"])
            self._log(True, f"inspection for repair job #{repair.pk} ({ref})")

    def _parts(self):
        # Stock is created at the post-usage figure plus what the sample usage
        # draws, so after ``_parts_usage`` runs it lands on the listed value.
        drawn = {}
        for _ref, part_name, qty, _by in PARTS_USAGE:
            drawn[part_name] = drawn.get(part_name, 0) + qty

        parts = {}
        for name, price, final_stock, threshold in PARTS:
            part, created = Parts.objects.get_or_create(
                PartName=name,
                defaults={
                    "UnitPrice": Decimal(price),
                    "StockQty": final_stock + drawn.get(name, 0),
                    "ReorderThreshold": threshold,
                    "IsActive": True,
                },
            )
            self._log(created, f"part '{name}'")
            parts[name] = part
        return parts

    def _parts_usage(self, repairs, parts, staff):
        for ref, part_name, qty, by_email in PARTS_USAGE:
            repair = repairs[ref]
            part = parts[part_name]
            if PartsUsage.objects.filter(RepairID=repair, PartID=part).exists():
                continue
            part.refresh_from_db()
            if qty > part.StockQty:
                self.stdout.write(self.style.WARNING(
                    f"  Skipped   {qty} x {part_name} ({part.StockQty} in stock)"
                ))
                continue
            PartsUsage.objects.create(
                RepairID=repair,
                PartID=part,
                Qty_Used=qty,
                Unit_Price=part.UnitPrice,  # price frozen at time of use
                RecordedBy=staff[by_email],
            )
            part.StockQty -= qty
            part.save(update_fields=["StockQty", "UpdatedAt"])
            self._log(True, f"parts usage {qty} x {part_name} on job #{repair.pk}")

    def _invoices(self, repairs, staff):
        for ref, receptionist_email, paid in INVOICES:
            repair = repairs[ref]
            if Invoice.objects.filter(RepairID=repair).exists():
                continue
            parts_cost = sum(
                (line.Qty_Used * line.Unit_Price for line in repair.parts_usage.all()),
                Decimal("0.00"),
            )
            amounts = calculate_invoice_amounts(
                repair.AppointmentID.ServiceCode.BaseCost, parts_cost, SAMPLE_TAX_RATE
            )
            invoice = Invoice.objects.create(
                RepairID=repair,
                LabourCost=amounts["labour_cost"],
                PartsCost=amounts["parts_cost"],
                TaxRate=amounts["tax_rate"],
                TaxAmount=amounts["tax_amount"],
                TotalAmount=amounts["total_amount"],
                PaymentStatus=PAYMENT_PAID if paid else PAYMENT_PENDING,
                GeneratedBy=staff[receptionist_email],
                PaidAt=dj_timezone.now() if paid else None,
            )
            self._log(True, f"invoice #{invoice.pk} for {ref} "
                            f"(MMK {invoice.TotalAmount}, "
                            f"{'Paid' if paid else 'Pending'})")

    def _refresh_bays(self):
        """Set each bay's status from the jobs it holds, as the service layer does."""
        for bay in ServiceBay.objects.all():
            busy = bay.repairs.filter(Status__in=ACTIVE_REPAIR_STATUSES).exists()
            wanted = BAY_OCCUPIED if busy else BAY_AVAILABLE
            if bay.Status != wanted:
                bay.Status = wanted
                bay.save(update_fields=["Status"])
