"""Database models for the workshop's staff, technician and service data.

Field names and ``db_column`` values are written in PascalCase to match the
column names already used by the workshop's existing SQL schema, so the Django
ORM maps straight onto those tables instead of Django's usual snake_case
defaults. Python-side attribute access therefore uses the same spelling as the
columns (``staff.Email``), which keeps queries and raw SQL interchangeable.

Passwords are stored as salted hashes, never as plain text, and accounts are
deactivated rather than deleted so that technician records and other rows that
reference them stay intact.
"""

import secrets

from django.core.validators import MinValueValidator
from django.db import models


class Role(models.Model):
    """A named system role: Administrator, Receptionist or Technician.

    Roles are fixed reference data seeded once at setup time; nothing in the
    application creates or edits them afterwards.
    """

    ADMINISTRATOR = "Administrator"
    RECEPTIONIST = "Receptionist"
    TECHNICIAN = "Technician"

    RoleID = models.AutoField(primary_key=True, db_column="RoleID")
    RoleName = models.CharField(
        max_length=20,
        db_column="RoleName",
        unique=True,
        help_text="Must be unique.",
    )

    class Meta:
        db_table = "Roles"
        verbose_name = "Role"
        verbose_name_plural = "Roles"
        ordering = ["RoleID"]

    def __str__(self) -> str:
        return self.RoleName


class Staff(models.Model):
    """A system user account. Every person who logs in has exactly one row.

    The role is a foreign key rather than free text so an account can never
    claim a role that does not exist. ``PROTECT`` is used instead of
    ``CASCADE`` because silently deleting every account attached to a role
    would be far worse than refusing the delete.
    """

    StaffID = models.AutoField(primary_key=True, db_column="StaffID")
    RoleID = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        db_column="RoleID",
        related_name="staff_accounts",
        help_text="A role in use cannot be deleted.",
    )
    FirstName = models.CharField(max_length=50, db_column="FirstName")
    LastName = models.CharField(max_length=50, db_column="LastName")
    Phone = models.CharField(
        max_length=50,
        db_column="Phone",
        blank=True,
        help_text="Optional.",
    )
    Email = models.EmailField(
        max_length=50,
        db_column="Email",
        unique=True,
        help_text="Used as the login identifier; must be unique.",
    )
    Password = models.CharField(
        max_length=255,
        db_column="Password",
        help_text="Salted hash only — never plain text.",
    )
    IsActive = models.BooleanField(
        default=True,
        db_column="IsActive",
        help_text="Deactivated accounts cannot log in but keep their row.",
    )

    class Meta:
        db_table = "Staffs"
        verbose_name = "Staff account"
        verbose_name_plural = "Staff accounts"
        ordering = ["LastName", "FirstName"]
        constraints = [
            models.UniqueConstraint(fields=["Email"], name="uq_staffs_email"),
        ]

    def __str__(self) -> str:
        return f"{self.FirstName} {self.LastName} <{self.Email}>"

    @property
    def full_name(self) -> str:
        return f"{self.FirstName} {self.LastName}"

    @property
    def role_name(self) -> str:
        """Read the role from the relation, not from the session.

        The session keeps a copy of the role name for convenience, but the
        database is authoritative: if an administrator changes someone's role
        while they are logged in, this property reflects the new value.
        """
        return self.RoleID.RoleName


class Technician(models.Model):
    """Workshop-specific data for a staff account holding the Technician role.

    Kept in a separate one-to-one table rather than as extra columns on
    ``Staff`` because only a minority of accounts are technicians, and an
    hourly rate is meaningless for a receptionist or an administrator.
    """

    TechnicianID = models.AutoField(primary_key=True, db_column="TechnicianID")
    StaffID = models.OneToOneField(
        Staff,
        on_delete=models.CASCADE,
        db_column="StaffID",
        related_name="technician_record",
        help_text="One technician record per staff account.",
    )
    Hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        db_column="Hourly_rate",
        validators=[MinValueValidator(0.01)],
        help_text="Must be greater than zero.",
    )

    class Meta:
        db_table = "Technicians"
        verbose_name = "Technician"
        verbose_name_plural = "Technicians"
        ordering = ["StaffID__LastName", "StaffID__FirstName"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(Hourly_rate__gt=0),
                name="ck_technicians_hourly_rate_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.StaffID.full_name} (${self.Hourly_rate}/hr)"

    @property
    def full_name(self) -> str:
        return self.StaffID.full_name


class Service(models.Model):
    """A repair service the workshop offers, such as an oil change.

    A technician can only perform a service once an assignment row links the
    two, so this catalogue exists to give those assignments something to point
    at.
    """

    Service_code = models.AutoField(primary_key=True, db_column="Service_code")
    ServiceName = models.CharField(
        max_length=40,
        db_column="ServiceName",
        unique=True,
        help_text="Must be unique.",
    )
    BaseCost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        db_column="BaseCost",
        validators=[MinValueValidator(0)],
        help_text="Must not be negative.",
    )
    Est_Duration = models.IntegerField(
        db_column="Est_Duration",
        validators=[MinValueValidator(1)],
        help_text="Estimated duration in minutes.",
    )

    class Meta:
        db_table = "Services"
        verbose_name = "Service"
        verbose_name_plural = "Services"
        ordering = ["ServiceName"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(BaseCost__gte=0),
                name="ck_services_base_cost_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ServiceName} (${self.BaseCost})"


class TechnicianService(models.Model):
    """Which services a technician is qualified to perform.

    The composite primary key makes a duplicate assignment impossible at the
    database level, so the check in ``core.services.assign_service`` exists to
    give a friendly message before the database would have to reject it.
    """

    pk = models.CompositePrimaryKey("TechnicianID", "ServiceCode")
    TechnicianID = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        db_column="TechnicianID",
        related_name="service_assignments",
    )
    ServiceCode = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        db_column="ServiceCode",
        related_name="qualified_technicians",
    )

    class Meta:
        db_table = "Technician_Service"
        verbose_name = "Technician service assignment"
        verbose_name_plural = "Technician service assignments"
        ordering = ["TechnicianID", "ServiceCode"]

    def __str__(self) -> str:
        return f"{self.TechnicianID} — {self.ServiceCode}"


# ---------------------------------------------------------------------------
# Appointment lifecycle (Timebox 2)
# ---------------------------------------------------------------------------
# A booking - whether a customer requested it online or a receptionist took it
# over the counter - starts as "Pending" and waits for the workshop to accept
# it. Accepting moves it to "Confirmed"; reception then checks the vehicle in on
# arrival and finally records the outcome. "Cancelled" and "No Show" are
# terminal: they release the appointment's date/time slot so it can be offered
# to another customer again.
APPOINTMENT_PENDING = "Pending"
APPOINTMENT_CONFIRMED = "Confirmed"
APPOINTMENT_CHECKED_IN = "Checked In"
APPOINTMENT_COMPLETED = "Completed"
APPOINTMENT_CANCELLED = "Cancelled"
APPOINTMENT_NO_SHOW = "No Show"

#: ``choices`` for ``Appointment.Status`` (stored as the label text).
APPOINTMENT_STATUSES = [
    (APPOINTMENT_PENDING, "Pending"),
    (APPOINTMENT_CONFIRMED, "Confirmed"),
    (APPOINTMENT_CHECKED_IN, "Checked In"),
    (APPOINTMENT_COMPLETED, "Completed"),
    (APPOINTMENT_CANCELLED, "Cancelled"),
    (APPOINTMENT_NO_SHOW, "No Show"),
]

#: Statuses that still hold an appointment's slot. A double-booking check only
#: counts these, so cancelling or a no-show frees the time for someone else.
#: A request awaiting acceptance holds its slot too, otherwise the same time
#: could be requested twice before staff have reviewed either request.
ACTIVE_APPOINTMENT_STATUSES = [
    APPOINTMENT_PENDING,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_CHECKED_IN,
    APPOINTMENT_COMPLETED,
]

#: Statuses an appointment may no longer leave once reached.
TERMINAL_APPOINTMENT_STATUSES = [
    APPOINTMENT_CANCELLED,
    APPOINTMENT_NO_SHOW,
]

#: Which status changes are legal, keyed by the current status. An empty list
#: means the status is terminal. Reception staff pick from these only, so an
#: appointment can never jump from "Cancelled" back to "Pending".
#:
#: "Pending" is the waiting state a request sits in until the workshop accepts
#: it: staff either accept it (Confirmed) or decline it (Cancelled).
APPOINTMENT_TRANSITIONS = {
    APPOINTMENT_PENDING: [
        APPOINTMENT_CONFIRMED,
        APPOINTMENT_CANCELLED,
        APPOINTMENT_NO_SHOW,
    ],
    APPOINTMENT_CONFIRMED: [
        APPOINTMENT_CHECKED_IN,
        APPOINTMENT_CANCELLED,
        APPOINTMENT_NO_SHOW,
    ],
    # Once the vehicle is physically at the workshop the appointment can only
    # be completed; cancelling a checked-in car would misrepresent what happened.
    APPOINTMENT_CHECKED_IN: [
        APPOINTMENT_COMPLETED,
    ],
    APPOINTMENT_COMPLETED: [],
    APPOINTMENT_CANCELLED: [],
    APPOINTMENT_NO_SHOW: [],
}

# A booking's reference code is what the customer keeps and quotes back to the
# workshop, so it is the public handle for an appointment. The raw
# ``AppointmentID`` is a sequential integer: anyone could count upwards from
# their own booking and read a stranger's, so it is never shown for lookup.
#
# The alphabet is fixed by the requirements: no 0 (vs O) and no 1 (vs I) so a
# code read aloud or handwritten is not misread. 32 symbols over 8 positions
# gives about 1.1 trillion codes, far more than the workshop will ever book.
REFERENCE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERENCE_CODE_LENGTH = 8

#: How many fresh codes ``book_appointment`` tries before giving up. A clash is
#: astronomically unlikely, so more than a handful of misses means something is
#: wrong and the caller should be told rather than left looping.
REFERENCE_CODE_MAX_ATTEMPTS = 5


def generate_reference_code() -> str:
    """Return a random 8-character booking reference code.

    Uses :mod:`secrets` rather than :mod:`random`: ``random`` is a predictable
    generator, and a customer who could work out the next code could guess
    another customer's reference. This only *proposes* a code; uniqueness is
    settled by the database when the appointment is saved.
    """
    return "".join(
        secrets.choice(REFERENCE_CODE_ALPHABET) for _ in range(REFERENCE_CODE_LENGTH)
    )


def vehicle_photo_path(instance, filename):
    """Store check-in photos under ``vehicles/<pk>/<filename>``.

    Grouping by primary key keeps one folder per vehicle instead of dropping
    every image into a single flat directory.
    """
    return f"vehicles/{instance.pk}/{filename}"


class Vehicle(models.Model):
    """A customer vehicle, identified by VIN or registration plate.

    Either identifier may be used to find an existing record at check-in, so
    both carry a unique constraint (blank values are allowed to repeat, which
    is why ``unique=True`` rather than a table-wide constraint is used). The
    optional ``Image`` holds a check-in photograph.
    """

    VehicleID = models.AutoField(primary_key=True, db_column="VehicleID")
    # NULL (not "") when unknown, so the partial unique constraints below only
    # apply to rows that actually carry a value.
    VIN = models.CharField(
        max_length=17,
        db_column="VIN",
        null=True,
        blank=True,
        help_text="Chassis number; 17 characters. Optional.",
    )
    LicensePlate = models.CharField(
        max_length=20,
        db_column="LicensePlate",
        null=True,
        blank=True,
        help_text="Registration plate. Optional.",
    )
    Make = models.CharField(max_length=40, db_column="Make", blank=True)
    Model = models.CharField(max_length=40, db_column="Model", blank=True)
    Year = models.IntegerField(
        db_column="Year",
        null=True,
        blank=True,
        validators=[MinValueValidator(1900)],
        help_text="Four-digit model year. Optional.",
    )
    Colour = models.CharField(max_length=20, db_column="Colour", blank=True)
    Image = models.ImageField(
        upload_to=vehicle_photo_path,
        db_column="Image",
        blank=True,
        null=True,
        help_text="Optional check-in photograph.",
    )

    class Meta:
        db_table = "Vehicles"
        verbose_name = "Vehicle"
        verbose_name_plural = "Vehicles"
        ordering = ["LicensePlate", "VIN"]
        constraints = [
            # Uniqueness applies only to rows that actually carry a value, so
            # several vehicles with an unknown VIN or plate are still allowed.
            models.UniqueConstraint(
                fields=["VIN"],
                condition=models.Q(VIN__isnull=False),
                name="uq_vehicles_vin",
            ),
            models.UniqueConstraint(
                fields=["LicensePlate"],
                condition=models.Q(LicensePlate__isnull=False),
                name="uq_vehicles_license_plate",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        """Best available label: plate, else VIN, else a make/model summary."""
        if self.LicensePlate:
            return self.LicensePlate
        if self.VIN:
            return self.VIN
        summary = " ".join(part for part in (self.Make, self.Model) if part)
        return summary or f"Vehicle #{self.pk}"


class Appointment(models.Model):
    """A booked service slot for a customer, optionally linked to a vehicle.

    The customer's contact details are stored on the appointment itself so a
    booking can exist before the vehicle is known; ``VehicleID`` is therefore
    nullable and is filled in when the vehicle arrives and is checked in. The
    ``ServiceCode`` link is required and ``PROTECT``ed, because deleting a
    service that bookings reference would corrupt appointment history.

    ``CheckedInAt`` records when the vehicle was received and is set only once,
    so the check-in time reflects the first arrival rather than a later edit.
    """

    AppointmentID = models.AutoField(primary_key=True, db_column="AppointmentID")
    # Set once, when the booking is made, and never rewritten: the customer
    # holds this code, so it must survive every later status change (accepted,
    # cancelled, checked in...). ``editable=False`` keeps it out of forms and
    # the admin edit page for the same reason.
    ReferenceCode = models.CharField(
        max_length=REFERENCE_CODE_LENGTH,
        db_column="ReferenceCode",
        unique=True,
        db_index=True,
        editable=False,
        help_text="Customer-facing booking reference; generated at booking time.",
    )
    # The customer's own details live on the appointment: Timebox 2 has no
    # separate customer table, and a booking is taken before any vehicle is
    # known, so the appointment is the only place they can be recorded.
    CustomerFirstName = models.CharField(
        max_length=50, db_column="CustomerFirstName"
    )
    CustomerLastName = models.CharField(max_length=50, db_column="CustomerLastName")
    CustomerPhone = models.CharField(
        max_length=50,
        db_column="CustomerPhone",
        blank=True,
        help_text="At least one of phone or email is required when booking.",
    )
    CustomerEmail = models.EmailField(
        max_length=50,
        db_column="CustomerEmail",
        blank=True,
        help_text="At least one of phone or email is required when booking.",
    )
    # Nullable on purpose: a booking may be taken before the vehicle is known.
    VehicleID = models.ForeignKey(
        Vehicle,
        on_delete=models.SET_NULL,
        db_column="VehicleID",
        related_name="appointments",
        null=True,
        blank=True,
        help_text="Optional until the vehicle is checked in.",
    )
    ServiceCode = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        db_column="ServiceCode",
        related_name="appointments",
        help_text="The service this booking is for.",
    )
    AppointmentDate = models.DateField(
        db_column="AppointmentDate",
        help_text="The calendar day of the booking.",
    )
    AppointmentTime = models.TimeField(
        db_column="AppointmentTime",
        help_text="Start time, on the half hour between 08:00 and 17:30.",
    )
    Status = models.CharField(
        max_length=20,
        db_column="Status",
        choices=APPOINTMENT_STATUSES,
        default=APPOINTMENT_PENDING,
        help_text="Where the appointment is in its lifecycle.",
    )
    Notes = models.TextField(
        db_column="Notes",
        blank=True,
        help_text="Free-text customer request or reception notes.",
    )
    CancelReason = models.CharField(
        max_length=255,
        db_column="CancelReason",
        blank=True,
        help_text="Recorded when the appointment is cancelled.",
    )
    CheckedInAt = models.DateTimeField(
        db_column="CheckedInAt",
        null=True,
        blank=True,
        help_text="Timestamp when the vehicle was received.",
    )
    CreatedAt = models.DateTimeField(auto_now_add=True, db_column="CreatedAt")
    UpdatedAt = models.DateTimeField(auto_now=True, db_column="UpdatedAt")

    class Meta:
        db_table = "Appointments"
        verbose_name = "Appointment"
        verbose_name_plural = "Appointments"
        ordering = ["AppointmentDate", "AppointmentTime"]
        constraints = [
            # No two appointments that still hold their slot may share the
            # same day and time, so the workshop cannot be double-booked. A
            # request waiting to be accepted counts as holding its slot too,
            # otherwise two customers could ask for the same time before staff
            # had reviewed either request. Cancelled/no-show rows are excluded,
            # which frees their slot for re-booking.
            models.UniqueConstraint(
                fields=["AppointmentDate", "AppointmentTime"],
                condition=models.Q(Status__in=ACTIVE_APPOINTMENT_STATUSES),
                name="uq_appointments_active_slot",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"#{self.pk} {self.customer_name} "
            f"{self.AppointmentDate} {self.AppointmentTime:%H:%M} ({self.Status})"
        )

    @property
    def customer_name(self) -> str:
        """Full customer name, assembled from the stored first/last parts."""
        return f"{self.CustomerFirstName} {self.CustomerLastName}".strip()

    @property
    def slot(self) -> str:
        """Human-readable ``date at time`` label used across the UI."""
        return f"{self.AppointmentDate:%d %b %Y} at {self.AppointmentTime:%H:%M}"

    @property
    def is_pending(self) -> bool:
        """True while the request is waiting for the workshop to accept it."""
        return self.Status == APPOINTMENT_PENDING

    @property
    def is_active(self) -> bool:
        """True while the appointment still holds its slot."""
        return self.Status in ACTIVE_APPOINTMENT_STATUSES

    @property
    def is_cancelled(self) -> bool:
        return self.Status in (APPOINTMENT_CANCELLED, APPOINTMENT_NO_SHOW)


# ---------------------------------------------------------------------------
# Repair job lifecycle (Timebox 3)
# ---------------------------------------------------------------------------
# A repair job only comes into existence once the vehicle is physically at the
# workshop: the linked appointment must have reached "Checked In". Accepting a
# booking merely reserves the slot, so a Confirmed appointment is *not* enough -
# there is nothing to repair until the car arrives.
#
# Work then moves forward in one direction only: Not Started -> In Progress ->
# Completed. Skipping a step is refused, because "Completed" is what releases
# the service bay and what Timebox 4 keys invoicing and payroll off.
REPAIR_NOT_STARTED = "Not Started"
REPAIR_IN_PROGRESS = "In Progress"
REPAIR_COMPLETED = "Completed"

#: ``choices`` for ``Repair.Status`` (stored as the label text).
REPAIR_STATUSES = [
    (REPAIR_NOT_STARTED, "Not Started"),
    (REPAIR_IN_PROGRESS, "In Progress"),
    (REPAIR_COMPLETED, "Completed"),
]

#: Statuses that keep a job - and therefore its service bay - active. A bay
#: holding one of these is Occupied; once the job reaches Completed it is free.
ACTIVE_REPAIR_STATUSES = [
    REPAIR_NOT_STARTED,
    REPAIR_IN_PROGRESS,
]

#: Which repair status changes are legal, keyed by the current status. An empty
#: list means the job is finished, so it can never skip straight to Completed.
REPAIR_TRANSITIONS = {
    REPAIR_NOT_STARTED: [REPAIR_IN_PROGRESS],
    REPAIR_IN_PROGRESS: [REPAIR_COMPLETED],
    REPAIR_COMPLETED: [],
}

#: Priority is a property of the *job*, not of the inspection: it is set once
#: when the administrator creates the job and does not change based on what the
#: technician later finds under the bonnet.
REPAIR_PRIORITY_URGENT = "Urgent"
REPAIR_PRIORITY_ROUTINE = "Routine"

#: ``choices`` for ``Repair.RepairPriority``.
REPAIR_PRIORITIES = [
    (REPAIR_PRIORITY_URGENT, "Urgent"),
    (REPAIR_PRIORITY_ROUTINE, "Routine"),
]

# A service bay is either free to take a job or busy with one. The status is a
# stored column rather than something derived on every query, so "which bays are
# free?" is a single filtered read instead of a join over every active repair.
BAY_AVAILABLE = "Available"
BAY_OCCUPIED = "Occupied"

#: ``choices`` for ``ServiceBay.Status``.
BAY_STATUSES = [
    (BAY_AVAILABLE, "Available"),
    (BAY_OCCUPIED, "Occupied"),
]

#: An inspection carries at most three photographs of the vehicle's condition.
#: The limit is fixed by the schema (three columns) and re-checked by the form
#: and the service layer so an extra upload is rejected rather than dropped.
MAX_INSPECTION_IMAGES = 3


def inspection_image_path(instance, filename):
    """Store inspection photos under ``inspections/<pk>/<filename>``.

    Grouped by inspection primary key so the three images belonging to one
    record sit together instead of sharing a flat directory with every other
    inspection the workshop has ever taken.
    """
    return f"inspections/{instance.pk}/{filename}"


class ServiceBay(models.Model):
    """A physical work bay a repair job can be carried out in (FR9).

    ``Status`` mirrors whether the bay currently holds an active job, and is
    kept in step by the service layer whenever a job is assigned, reassigned or
    completed. Storing it rather than deriving it means an administrator asking
    "which bays are free?" reads one column instead of joining every active
    repair each time.
    """

    BayID = models.AutoField(primary_key=True, db_column="BayID")
    BayName = models.CharField(
        max_length=40,
        db_column="BayName",
        unique=True,
        help_text="Must be unique, e.g. 'Bay 1'.",
    )
    Status = models.CharField(
        max_length=20,
        db_column="Status",
        choices=BAY_STATUSES,
        default=BAY_AVAILABLE,
        help_text="Occupied while an active repair job is assigned to the bay.",
    )

    class Meta:
        db_table = "ServiceBay"
        verbose_name = "Service bay"
        verbose_name_plural = "Service bays"
        ordering = ["BayName"]

    def __str__(self) -> str:
        return f"{self.BayName} ({self.Status})"

    @property
    def is_available(self) -> bool:
        """True when the bay is free to take another repair job."""
        return self.Status == BAY_AVAILABLE


class Inspection(models.Model):
    """The technician's record of a vehicle's condition once work starts (FR7).

    Deliberately *not* an intake document: the receptionist's part of the story
    ends at check-in (Timebox 2), and this inspection is written by the
    technician assigned to the repair job. It holds a written description plus
    up to three photographs, stored as three columns so the limit is enforced by
    the schema itself rather than only by application code.

    ``VehicleID`` is ``PROTECT``ed - a vehicle with inspection history cannot be
    deleted, mirroring how appointments already guard vehicle records.
    """

    InspectionID = models.AutoField(primary_key=True, db_column="InspectionID")
    VehicleID = models.ForeignKey(
        Vehicle,
        on_delete=models.PROTECT,
        db_column="VehicleID",
        related_name="inspections",
        help_text="The vehicle whose condition was recorded.",
    )
    Description = models.TextField(
        db_column="Description",
        help_text="Written record of the vehicle's condition.",
    )
    Image1 = models.ImageField(
        upload_to=inspection_image_path,
        db_column="Image1",
        blank=True,
        null=True,
        help_text="Optional photograph 1 of up to 3.",
    )
    Image2 = models.ImageField(
        upload_to=inspection_image_path,
        db_column="Image2",
        blank=True,
        null=True,
        help_text="Optional photograph 2 of up to 3.",
    )
    Image3 = models.ImageField(
        upload_to=inspection_image_path,
        db_column="Image3",
        blank=True,
        null=True,
        help_text="Optional photograph 3 of up to 3.",
    )
    CreatedAt = models.DateTimeField(auto_now_add=True, db_column="CreatedAt")
    UpdatedAt = models.DateTimeField(auto_now=True, db_column="UpdatedAt")

    class Meta:
        db_table = "Inspections"
        verbose_name = "Inspection"
        verbose_name_plural = "Inspections"
        ordering = ["-CreatedAt", "InspectionID"]

    def __str__(self) -> str:
        return f"Inspection #{self.pk} for {self.VehicleID}"

    @property
    def images(self) -> list:
        """The photographs attached to this inspection, in order, blanks dropped."""
        return [image for image in (self.Image1, self.Image2, self.Image3) if image]

    @property
    def image_count(self) -> int:
        """How many of the three allowed photographs are in use."""
        return len(self.images)

    @property
    def has_free_image_slot(self) -> bool:
        """True while fewer than ``MAX_INSPECTION_IMAGES`` photos are attached."""
        return self.image_count < MAX_INSPECTION_IMAGES


class Repair(models.Model):
    """A repair job raised against a checked-in appointment (FR6).

    Three of the four foreign keys are nullable on purpose, because a job is
    created the moment the vehicle arrives while a technician and a bay may not
    be free yet:

    * ``TechnicianID`` - filled in once a *qualified* technician is confirmed;
    * ``BayID`` - filled in once a bay becomes Available;
    * ``InspectionID`` - filled in once the assigned technician records the
      vehicle's condition.

    ``AppointmentID`` carries a unique constraint: an appointment produces one
    repair job, so a second job for the same checked-in car cannot be created by
    accident. The link is ``PROTECT``ed on the technician and inspection, since
    deleting a technician who has work history would erase payroll evidence
    (Timebox 4), and a bay is ``SET_NULL`` because retiring a bay should not
    delete the jobs that were carried out in it.
    """

    RepairID = models.AutoField(primary_key=True, db_column="RepairID")
    AppointmentID = models.ForeignKey(
        Appointment,
        on_delete=models.CASCADE,
        db_column="AppointmentID",
        related_name="repairs",
        help_text="The checked-in appointment this job was raised for.",
    )
    TechnicianID = models.ForeignKey(
        Technician,
        on_delete=models.PROTECT,
        db_column="TechnicianID",
        related_name="repairs",
        null=True,
        blank=True,
        help_text="Optional until a qualified technician is assigned.",
    )
    InspectionID = models.ForeignKey(
        Inspection,
        on_delete=models.PROTECT,
        db_column="InspectionID",
        related_name="repairs",
        null=True,
        blank=True,
        help_text="Set once the assigned technician completes the inspection.",
    )
    BayID = models.ForeignKey(
        ServiceBay,
        on_delete=models.SET_NULL,
        db_column="BayID",
        related_name="repairs",
        null=True,
        blank=True,
        help_text="Optional until a service bay is free.",
    )
    RepairPriority = models.CharField(
        max_length=20,
        db_column="RepairPriority",
        choices=REPAIR_PRIORITIES,
        default=REPAIR_PRIORITY_ROUTINE,
        help_text="Set once when the job is created.",
    )
    Status = models.CharField(
        max_length=20,
        db_column="Status",
        choices=REPAIR_STATUSES,
        default=REPAIR_NOT_STARTED,
        help_text="Moves Not Started -> In Progress -> Completed.",
    )
    CreatedAt = models.DateTimeField(auto_now_add=True, db_column="CreatedAt")
    UpdatedAt = models.DateTimeField(auto_now=True, db_column="UpdatedAt")

    class Meta:
        db_table = "Repairs"
        verbose_name = "Repair job"
        verbose_name_plural = "Repair jobs"
        # Newest first: the job board is read as "what is on the floor now".
        ordering = ["-CreatedAt", "RepairID"]
        constraints = [
            # One repair job per appointment, so the same checked-in vehicle
            # cannot be raised twice.
            models.UniqueConstraint(
                fields=["AppointmentID"],
                name="uq_repairs_appointment",
            ),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.AppointmentID.ServiceCode} ({self.Status})"

    @property
    def is_active(self) -> bool:
        """True while work is outstanding, which is what holds a bay."""
        return self.Status in ACTIVE_REPAIR_STATUSES

    @property
    def is_completed(self) -> bool:
        return self.Status == REPAIR_COMPLETED

    @property
    def is_inspected(self) -> bool:
        """True once the technician has recorded the vehicle's condition."""
        return self.InspectionID_id is not None

    @property
    def vehicle(self) -> Vehicle | None:
        """The vehicle being repaired, reached through its appointment."""
        return self.AppointmentID.VehicleID

    @property
    def service(self) -> Service:
        """The requested service - what a technician must be qualified for."""
        return self.AppointmentID.ServiceCode

    @property
    def customer_name(self) -> str:
        """The customer's name, carried through from the appointment."""
        return self.AppointmentID.customer_name

