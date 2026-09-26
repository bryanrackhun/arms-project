"""Business rules for accounts, technicians and service assignments.

Views stay thin: they parse a request, call a function here, and turn the
result or the raised error into a message. That keeps the rules testable
without an HTTP request, and means the same guarantees hold for any future
caller (a management command, a bulk import) rather than only for the screens.

Every function that writes validates first and raises a :class:`ServiceError`
subclass instead of returning a status code, so a caller cannot accidentally
ignore a failure. Writes are wrapped in a transaction so a mid-way error leaves
no partial row behind.

Passwords are hashed with Django's PBKDF2-SHA256 hasher: a per-password salt,
plain text never stored, returned or logged. ``PASSWORD_HASHERS`` in settings
selects the algorithm, so moving to bcrypt or argon2 is a settings change and
needs no edit here.
"""

import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, F, Q, Sum
from django.utils import timezone as dj_timezone

from .models import (
    ACTIVE_APPOINTMENT_STATUSES,
    ACTIVE_REPAIR_STATUSES,
    APPOINTMENT_CANCELLED,
    APPOINTMENT_CHECKED_IN,
    APPOINTMENT_COMPLETED,
    APPOINTMENT_CONFIRMED,
    APPOINTMENT_NO_SHOW,
    APPOINTMENT_PENDING,
    APPOINTMENT_STATUSES,
    APPOINTMENT_TRANSITIONS,
    BAY_AVAILABLE,
    BAY_OCCUPIED,
    CUSTOMER_STAGE_BOOKED,
    CUSTOMER_STAGE_CANCELLED,
    CUSTOMER_STAGE_CHECKED_IN,
    CUSTOMER_STAGE_COMPLETED,
    CUSTOMER_STAGE_IN_PROGRESS,
    CUSTOMER_STAGE_INSPECTED,
    CUSTOMER_STAGE_JOB_ASSIGNED,
    CUSTOMER_STAGES,
    MAX_INSPECTION_IMAGES,
    PAYMENT_PAID,
    PAYMENT_PENDING,
    REFERENCE_CODE_MAX_ATTEMPTS,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_NOT_STARTED,
    REPAIR_PRIORITIES,
    REPAIR_PRIORITY_ROUTINE,
    REPAIR_STATUSES,
    REPAIR_TRANSITIONS,
    TAX_RATE_MAX,
    TAX_RATE_MIN,
    TERMINAL_APPOINTMENT_STATUSES,
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

logger = logging.getLogger(__name__)

# The session carries only these identifiers. Storing the role name too avoids
# a query on every request; the database stays authoritative via Staff.role_name
# if a role is changed mid-session.
SESSION_KEY_STAFF_ID = "arms_staff_id"
SESSION_KEY_ROLE_ID = "arms_role_id"
SESSION_KEY_ROLE_NAME = "arms_role_name"

#: Phone is optional, but when supplied must look like a phone number: digits
#: with an optional leading +, and spaces, hyphens or brackets as separators.
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s\-()]{4,19}$")

#: Enforced before hashing, since a hash tells you nothing about length later.
MIN_PASSWORD_LENGTH = 8

#: One message for every failed-login cause. Distinguishing "no such account"
#: from "wrong password" would let anyone enumerate valid staff email addresses
#: just by reading the response.
INVALID_CREDENTIALS_MESSAGE = "Invalid email or password."

#: Appointments may only be booked on the half hour, within the workshop's
#: opening window. These bounds are enforced by ``_validate_slot`` so the
#: public booking form and the receptionist screens share one rule.
OPENING_TIME = time(8, 0)
CLOSING_TIME = time(17, 30)
SLOT_MINUTES = 30

#: Allowed start times, generated from the window above: 08:00, 08:30 ... 17:30.
BOOKABLE_TIMES = [
    time(hour=8 + (SLOT_MINUTES * i) // 60, minute=(SLOT_MINUTES * i) % 60)
    for i in range(
        ((CLOSING_TIME.hour * 60 + CLOSING_TIME.minute)
         - (OPENING_TIME.hour * 60 + OPENING_TIME.minute)) // SLOT_MINUTES + 1
    )
]

#: A standard VIN is exactly 17 characters (ISO 3779); we accept letters and
#: digits and reject the easily-confused I, O and Q.
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

#: A registration plate: 2-15 characters of letters, digits, spaces or hyphens.
LICENSE_PLATE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,13}[A-Za-z0-9]$")

#: How far an inspection's condition notes are summarised on the public status
#: page. The customer is shown a sentence or two, not the technician's full
#: notes, so the truncation keeps internal detail off a page with no login.
CUSTOMER_INSPECTION_SUMMARY_LENGTH = 240

#: Every money figure the workshop handles is stored to two decimal places, so
#: every calculated amount is rounded to that before it is saved. Half-up is the
#: conventional billing rounding - a half cent goes to the customer's bill rather
#: than being truncated away - and using one rounding mode everywhere means the
#: invoice total always equals the sum of its own parts.
MONEY_QUANTUM = Decimal("0.01")


class ServiceError(Exception):
    """Base class for business-rule failures raised by this layer.

    Views catch this single type to cover every expected failure, so the more
    specific subclasses below exist for callers that want to react differently
    to, say, a duplicate than to a missing record.
    """


class ValidationError(ServiceError):
    """Input failed a validation rule; nothing was written."""


class AuthenticationError(ServiceError):
    """Login failed, or the session is missing/expired."""


class AuthorizationError(ServiceError):
    """The signed-in user's role does not permit the operation."""


class DuplicateError(ServiceError):
    """A unique constraint would be violated (email, service assignment)."""


class NotFoundError(ServiceError):
    """The requested record does not exist."""


# ---------------------------------------------------------------------------
# Authentication and sessions
# ---------------------------------------------------------------------------
def hash_password(plain_password: str) -> str:
    """Return a salted hash of ``plain_password``.

    The hasher generates a fresh salt per password, so two people who pick the
    same password get different stored values and a stolen table cannot be
    cracked with precomputed hashes. The plain text is never returned, stored
    or logged.
    """
    if not plain_password:
        raise ValidationError("Password is required.")
    if len(plain_password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    return make_password(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check ``plain_password`` against the stored ``hashed_password``.

    Returns False rather than raising, so a malformed stored hash and a wrong
    password both collapse into the same generic login failure. A corrupt hash
    is logged, because unlike a typo it is something an operator needs to know
    about.
    """
    if not plain_password or not hashed_password:
        return False
    try:
        return check_password(plain_password, hashed_password)
    except Exception:
        logger.exception("Password verification raised an unexpected error.")
        return False


def login(session, email: str, password: str) -> Staff:
    """Authenticate a staff account and create the session.

    Returns the authenticated :class:`Staff`. Raises
    :class:`AuthenticationError` with the generic message when either field is
    empty, no active account matches, or the password does not verify. The
    raw password is never logged.
    """
    email = (email or "").strip()
    password = password or ""

    # step 2 - validate that neither field is empty before any DB call.
    if not email or not password:
        raise AuthenticationError(INVALID_CREDENTIALS_MESSAGE)

    staff = Staff.objects.select_related("RoleID").filter(Email__iexact=email).first()

    # steps 4/5 - unknown account AND inactive account are identical, to
    # avoid account enumeration.
    if staff is None or not staff.IsActive:
        raise AuthenticationError(INVALID_CREDENTIALS_MESSAGE)

    # step 6 - verify against the stored hash.
    if not verify_password(password, staff.Password):
        raise AuthenticationError(INVALID_CREDENTIALS_MESSAGE)

    # - session scoped to StaffID and RoleID.
    session[SESSION_KEY_STAFF_ID] = staff.StaffID
    session[SESSION_KEY_ROLE_ID] = staff.RoleID_id
    session[SESSION_KEY_ROLE_NAME] = staff.RoleID.RoleName
    session.set_expiry(1800)  # 30 minutes of inactivity
    session.modified = True
    return staff


def logout(session) -> None:
    """Invalidate the session immediately so it cannot be reused."""
    session.flush()


def verify_session(session) -> dict:
    """Return the session identity ``{'staff_id','role_id','role_name'}``.

    Raises :class:`AuthenticationError` when no valid session exists, so every
    protected route can check for it before executing.
    """
    staff_id = session.get(SESSION_KEY_STAFF_ID)
    role_id = session.get(SESSION_KEY_ROLE_ID)
    if not staff_id or not role_id:
        raise AuthenticationError("Your session has expired. Please log in again.")
    return {
        "staff_id": staff_id,
        "role_id": role_id,
        "role_name": session.get(SESSION_KEY_ROLE_NAME, ""),
    }


# ---------------------------------------------------------------------------
# Role module
# ---------------------------------------------------------------------------
def list_roles() -> list[Role]:
    """Return all roles, ordered by RoleID ( 'view available user roles')."""
    return list(Role.objects.all())


def get_role(role_id: int) -> Role:
    """Return the role with ``role_id``, or raise :class:`NotFoundError`.

    Used to guarantee RoleID references an existing Roles row and that
    free-text role names are never accepted.
    """
    try:
        return Role.objects.get(pk=role_id)
    except Role.DoesNotExist as exc:
        raise NotFoundError("The selected role does not exist.") from exc


# ---------------------------------------------------------------------------
# Staff module
# ---------------------------------------------------------------------------
def _validate_names(first_name: str, last_name: str) -> tuple[str, str]:
    """First name / Last name: required, non-empty after trimming."""
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    if not first_name:
        raise ValidationError("First name is required.")
    if not last_name:
        raise ValidationError("Last name is required.")
    if len(first_name) > 50 or len(last_name) > 50:
        raise ValidationError("First and last names must be 50 characters or fewer.")
    return first_name, last_name


def _validate_email(email: str, exclude_staff_id: int | None = None) -> str:
    """Email: required, standard email pattern, unique across Staffs."""
    email = (email or "").strip()
    if not email:
        raise ValidationError("Email is required.")
    if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
        raise ValidationError("Enter a valid email address (e.g. name@example.com).")
    if len(email) > 50:
        raise ValidationError("Email must be 50 characters or fewer.")

    existing = Staff.objects.filter(Email__iexact=email)
    if exclude_staff_id is not None:
        existing = existing.exclude(StaffID=exclude_staff_id)
    if existing.exists():
        # duplicate email -> clear, specific message.
        raise DuplicateError("That email address is already in use.")
    return email


def _validate_phone(phone: str) -> str:
    """Phone: optional, but if provided must match an accepted pattern."""
    phone = (phone or "").strip()
    if phone and not PHONE_PATTERN.match(phone):
        raise ValidationError(
            "Enter a valid phone number (digits, optional +, spaces or hyphens)."
        )
    if len(phone) > 50:
        raise ValidationError("Phone must be 50 characters or fewer.")
    return phone


def create_staff(
    first_name: str,
    last_name: str,
    email: str,
    password: str,
    role_id: int,
    phone: str = "",
) -> Staff:
    """Create a staff account with a securely hashed password.

    Validates every field before anything is written; the role must exist.
    """
    first_name, last_name = _validate_names(first_name, last_name)
    email = _validate_email(email)
    phone = _validate_phone(phone)
    role = get_role(role_id)  # rejects free-text / non-existent roles

    try:
        with transaction.atomic():
            return Staff.objects.create(
                RoleID=role,
                FirstName=first_name,
                LastName=last_name,
                Phone=phone,
                Email=email,
                Password=hash_password(password),
                IsActive=True,
            )
    except ValidationError:
        raise
    except Exception:
        # database operation failure: transaction rolled back, generic
        # message to the user, real error logged server-side only.
        logger.exception("Staff account creation failed.")
        raise ServiceError("Something went wrong, please try again.")


def get_staff(staff_id: int) -> Staff:
    """Return the staff account with ``staff_id`` ( 'view staff accounts')."""
    try:
        return Staff.objects.select_related("RoleID").get(pk=staff_id)
    except Staff.DoesNotExist as exc:
        raise NotFoundError("That staff account does not exist.") from exc


def list_staff(include_inactive: bool = True) -> list[Staff]:
    """Return all staff accounts ( 'view staff accounts')."""
    queryset = Staff.objects.select_related("RoleID").all()
    if not include_inactive:
        queryset = queryset.filter(IsActive=True)
    return list(queryset)


def search_staff(query: str, include_inactive: bool = True) -> list[Staff]:
    """Search staff accounts by name or email ( 'search staff accounts')."""
    query = (query or "").strip()
    queryset = Staff.objects.select_related("RoleID")
    if not include_inactive:
        queryset = queryset.filter(IsActive=True)
    if not query:
        return list(queryset)
    return list(
        queryset.filter(
            Q(FirstName__icontains=query)
            | Q(LastName__icontains=query)
            | Q(Email__icontains=query)
        )
    )


def update_staff(
    staff_id: int,
    first_name: str,
    last_name: str,
    email: str,
    role_id: int,
    phone: str = "",
    new_password: str | None = None,
) -> Staff:
    """Update a staff account, including its role."""
    staff = get_staff(staff_id)
    first_name, last_name = _validate_names(first_name, last_name)
    email = _validate_email(email, exclude_staff_id=staff_id)
    phone = _validate_phone(phone)
    role = get_role(role_id)

    try:
        with transaction.atomic():
            staff.FirstName = first_name
            staff.LastName = last_name
            staff.Email = email
            staff.Phone = phone
            staff.RoleID = role
            if new_password:  # blank = keep the current password unchanged
                staff.Password = hash_password(new_password)
            staff.save()
        return staff
    except ValidationError:
        raise
    except Exception:
        logger.exception("Staff account update failed for StaffID=%s.", staff_id)
        raise ServiceError("Something went wrong, please try again.")


def deactivate_staff(staff_id: int) -> Staff:
    """Deactivate rather than delete ( 'staff deletion').

    Sets ``IsActive = 0`` so the account can no longer log in, while the row
    remains for referential integrity with any Technicians record.
    """
    staff = get_staff(staff_id)
    staff.IsActive = False
    staff.save(update_fields=["IsActive"])
    return staff


# ---------------------------------------------------------------------------
# Technician module
# ---------------------------------------------------------------------------
def _validate_hourly_rate(hourly_rate) -> object:
    """Hourly_rate: required, numeric, strictly greater than zero."""
    if hourly_rate is None or hourly_rate == "":
        raise ValidationError("Hourly rate is required.")
    try:
        rate = float(hourly_rate)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Hourly rate must be a number.") from exc
    if rate <= 0:
        raise ValidationError("Hourly rate must be greater than zero.")
    return rate


def create_technician(staff_id: int, hourly_rate) -> Technician:
    """Create a technician record linked to an existing staff account.

    The linked account must exist, be active and hold the Technician role —
    the application layer enforces this because the schema cannot.
    """
    staff = get_staff(staff_id)
    rate = _validate_hourly_rate(hourly_rate)

    if staff.RoleID.RoleName != Role.TECHNICIAN:
        raise ValidationError(
            "A technician record can only be created for a Technician-role account."
        )
    if Technician.objects.filter(StaffID=staff_id).exists():
        raise DuplicateError("That staff account already has a technician record.")

    try:
        with transaction.atomic():
            return Technician.objects.create(StaffID=staff, Hourly_rate=rate)
    except ServiceError:
        raise
    except Exception:
        logger.exception("Technician creation failed for StaffID=%s.", staff_id)
        raise ServiceError("Something went wrong, please try again.")


def get_technician(technician_id: int) -> Technician:
    """Return the technician with ``technician_id`` ( 'view technician')."""
    try:
        return Technician.objects.select_related("StaffID").get(pk=technician_id)
    except Technician.DoesNotExist as exc:
        raise NotFoundError("That technician does not exist.") from exc


def list_technicians() -> list[Technician]:
    """Return all technicians with their linked staff account."""
    return list(Technician.objects.select_related("StaffID").all())


def update_technician(technician_id: int, hourly_rate) -> Technician:
    """Update a technician's hourly rate."""
    technician = get_technician(technician_id)
    technician.Hourly_rate = _validate_hourly_rate(hourly_rate)
    technician.save(update_fields=["Hourly_rate"])
    return technician


def deactivate_technician(technician_id: int) -> Technician:
    """Deactivate a technician by deactivating the linked staff account."""
    technician = get_technician(technician_id)
    staff = technician.StaffID
    staff.IsActive = False
    staff.save(update_fields=["IsActive"])
    return technician


# ---------------------------------------------------------------------------
# TechnicianService module
# ---------------------------------------------------------------------------
def assign_service(technician_id: int, service_code: int) -> TechnicianService:
    """Assign a service to a technician.

    Duplicate (TechnicianID, ServiceCode) pairs are rejected first at the
    application level with a clear message, and again at the database
    level by the composite primary key.
    """
    try:
        technician = Technician.objects.get(pk=technician_id)
    except Technician.DoesNotExist as exc:
        raise NotFoundError("That technician does not exist.") from exc
    try:
        service = Service.objects.get(pk=service_code)
    except Service.DoesNotExist as exc:
        raise NotFoundError("That service does not exist.") from exc

    if TechnicianService.objects.filter(
        TechnicianID=technician, ServiceCode=service
    ).exists():
        raise DuplicateError(
            f"'{service.ServiceName}' is already assigned to {technician.full_name}."
        )

    try:
        with transaction.atomic():
            return TechnicianService.objects.create(
                TechnicianID=technician, ServiceCode=service
            )
    except ServiceError:
        raise
    except Exception:
        logger.exception(
            "Technician-service assignment failed (TechnicianID=%s, ServiceCode=%s).",
            technician_id,
            service_code,
        )
        raise ServiceError("Something went wrong, please try again.")


def remove_assignment(technician_id: int, service_code: int) -> None:
    """Remove a technician-service assignment ( 'remove a service assignment')."""
    deleted, _ = TechnicianService.objects.filter(
        TechnicianID_id=technician_id, ServiceCode_id=service_code
    ).delete()
    if not deleted:
        raise NotFoundError("That service is not assigned to that technician.")


def list_services_for_technician(technician_id: int) -> list[Service]:
    """Return the services assigned to a technician."""
    return list(
        Service.objects.filter(
            qualified_technicians__TechnicianID_id=technician_id
        ).order_by("ServiceName")
    )


def list_technicians_for_service(service_code: int) -> list[Technician]:
    """Return the technicians assigned to a service."""
    return list(
        Technician.objects.filter(
            service_assignments__ServiceCode_id=service_code
        ).select_related("StaffID")
    )


def list_services() -> list[Service]:
    """Return all services — used to populate the assignment form."""
    return list(Service.objects.all())




# ===========================================================================
# Timebox 2 - repair appointments and vehicle records
# ===========================================================================
# These follow exactly the same contract as the Timebox 1 functions above:
# validate first, write inside a transaction, and raise a ServiceError
# subclass (never return a status) so a caller cannot ignore a failure. Views
# stay thin and translate the raised error into a user-facing message.


def _validate_customer_name(first_name: str, last_name: str) -> tuple[str, str]:
    """Booking requires the customer's first and last name."""
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    if not first_name:
        raise ValidationError("Customer first name is required.")
    if not last_name:
        raise ValidationError("Customer last name is required.")
    if len(first_name) > 50 or len(last_name) > 50:
        raise ValidationError(
            "Customer first and last names must be 50 characters or fewer."
        )
    return first_name, last_name


def _validate_customer_contact(phone: str, email: str) -> tuple[str, str]:
    """At least one contact method, each validated when supplied."""
    phone = (phone or "").strip()
    email = (email or "").strip()
    if not phone and not email:
        raise ValidationError(
            "Contact information is required: provide a phone number or an "
            "email address."
        )
    if phone and not PHONE_PATTERN.match(phone):
        raise ValidationError(
            "Enter a valid phone number (digits, optional +, spaces or hyphens)."
        )
    if email and not re.match(
        r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email
    ):
        raise ValidationError("Enter a valid email address (e.g. name@example.com).")
    return phone, email



def _validate_slot(
    appointment_date,
    appointment_time,
    exclude_appointment_id: int | None = None,
) -> tuple:
    """Enforce the booking rules for a date/time slot.

    * the slot must be in the future (not in the past);
    * the time must be one of the half-hour slots inside opening hours;
    * no other *active* appointment may already hold that exact slot.

    Cancelled and no-show appointments are excluded from the clash check, so
    their freed slot can be rebooked.
    """
    if appointment_date is None or appointment_time is None:
        raise ValidationError("Both a preferred date and time are required.")

    now = dj_timezone.localtime()
    slot_dt = datetime.combine(appointment_date, appointment_time)
    if dj_timezone.is_naive(slot_dt):
        slot_dt = dj_timezone.make_aware(slot_dt)
    if slot_dt <= now:
        raise ValidationError(
            "The preferred appointment date and time must be in the future."
        )

    if appointment_time not in BOOKABLE_TIMES:
        raise ValidationError(
            "Choose an appointment time on the half hour between "
            f"{OPENING_TIME:%H:%M} and {CLOSING_TIME:%H:%M}."
        )

    clash = Appointment.objects.filter(
        AppointmentDate=appointment_date,
        AppointmentTime=appointment_time,
        Status__in=ACTIVE_APPOINTMENT_STATUSES,
    )
    if exclude_appointment_id is not None:
        clash = clash.exclude(pk=exclude_appointment_id)
    if clash.exists():
        raise ValidationError(
            "That date and time is already booked. Please choose another slot."
        )
    return appointment_date, appointment_time


def get_service(service_code: int) -> Service:
    """Return the catalogue service, or raise :class:`NotFoundError`.

    An appointment cannot exist without a linked service, so this is called on
    every booking to guarantee the ServiceCode references a real row.
    """
    try:
        return Service.objects.get(pk=service_code)
    except Service.DoesNotExist as exc:
        raise NotFoundError("The selected service does not exist.") from exc



def book_appointment(
    first_name: str,
    last_name: str,
    service_code: int,
    appointment_date,
    appointment_time,
    phone: str = "",
    email: str = "",
    notes: str = "",
) -> Appointment:
    """Create a booking request with ``VehicleID`` deliberately left unset.

    A booking captures who the customer is, which service they want and when
    they would like to come in - but never any vehicle data, because customers
    rarely have their VIN to hand when booking online. The vehicle is recorded
    and linked later, at check-in.

    The same entry point serves both routes into the diary: a customer booking
    online and a receptionist taking the request over the counter. Either way
    the request starts as ``Pending`` and only becomes ``Confirmed`` once the
    workshop accepts it, so no booking can occupy the diary unreviewed.

    The booking is also given its ``ReferenceCode`` here, in the same insert as
    the row itself, so an appointment can never exist without one. The code is
    never touched again, whatever the status later becomes. If a freshly drawn
    code happens to clash with an existing one, another is drawn, up to
    ``REFERENCE_CODE_MAX_ATTEMPTS`` times before a :class:`ServiceError`.
    """
    first_name, last_name = _validate_customer_name(first_name, last_name)
    phone, email = _validate_customer_contact(phone, email)
    service = get_service(service_code)  # cannot book without a valid service
    appointment_date, appointment_time = _validate_slot(
        appointment_date, appointment_time
    )
    notes = (notes or "").strip()

    try:
        with transaction.atomic():
            for _attempt in range(REFERENCE_CODE_MAX_ATTEMPTS):
                reference_code = generate_reference_code()
                try:
                    # Each attempt runs in its own savepoint: a failed INSERT
                    # would otherwise leave the surrounding transaction unusable
                    # and the retry could not run.
                    with transaction.atomic():
                        return Appointment.objects.create(
                            ReferenceCode=reference_code,
                            CustomerFirstName=first_name,
                            CustomerLastName=last_name,
                            CustomerPhone=phone,
                            CustomerEmail=email,
                            VehicleID=None,  # explicitly unset at booking time
                            ServiceCode=service,
                            AppointmentDate=appointment_date,
                            AppointmentTime=appointment_time,
                            Status=APPOINTMENT_PENDING,
                            Notes=notes,
                        )
                except IntegrityError:
                    # The insert can also fail for an unrelated constraint, such
                    # as another request winning the same slot a moment earlier.
                    # Only a clash on the code is worth retrying; anything else
                    # is re-raised for the handler below.
                    if Appointment.objects.filter(ReferenceCode=reference_code).exists():
                        continue
                    raise

            logger.error(
                "No unique reference code found after %s attempts.",
                REFERENCE_CODE_MAX_ATTEMPTS,
            )
            raise ServiceError(
                "We could not generate a booking reference. Please try again."
            )
    except ServiceError:
        raise
    except Exception:
        logger.exception("Appointment booking failed.")
        raise ServiceError("Something went wrong, please try again.")


def get_appointment(appointment_id: int) -> Appointment:
    """Return one appointment with its service and vehicle relations loaded."""
    try:
        return Appointment.objects.select_related(
            "ServiceCode", "VehicleID"
        ).get(pk=appointment_id)
    except Appointment.DoesNotExist as exc:
        raise NotFoundError("That appointment does not exist.") from exc


def get_appointment_by_reference(reference_code: str) -> Appointment:
    """Return the appointment with ``reference_code``, or raise NotFoundError.

    This, not the raw ``AppointmentID``, is how an appointment is found from the
    customer's side. Input is trimmed and upper-cased first, because the code
    is typed or read back by a person and the alphabet has no lower case.
    """
    # ``str()`` because the value can come from a session written before codes
    # existed, when it held an integer ID; that must read as "not found", not
    # crash.
    code = str(reference_code or "").strip().upper()
    if not code:
        raise NotFoundError("That booking reference was not found.")
    try:
        return Appointment.objects.select_related("ServiceCode", "VehicleID").get(
            ReferenceCode=code
        )
    except Appointment.DoesNotExist as exc:
        raise NotFoundError("That booking reference was not found.") from exc


def list_appointments() -> list[Appointment]:
    """Return all appointments, soonest first ( 'view list of appointments')."""
    return list(
        Appointment.objects.select_related("ServiceCode", "VehicleID").all()
    )


def list_pending_appointments() -> list[Appointment]:
    """Return the requests still waiting to be accepted, soonest first.

    Backs the review queue on the booking desk: both customer requests and
    over-the-counter bookings land here until a receptionist accepts them.
    """
    return list(
        Appointment.objects.select_related("ServiceCode", "VehicleID")
        .filter(Status=APPOINTMENT_PENDING)
        .order_by("AppointmentDate", "AppointmentTime")
    )


def search_appointments(query: str) -> list[Appointment]:
    """Search appointments by customer name, or by an ISO date (YYYY-MM-DD).

    Accepts a partial name in either the first or last name field, or a full
    date, so a receptionist can find a booking from the details the customer
    gives over the phone.
    """
    query = (query or "").strip()
    queryset = Appointment.objects.select_related("ServiceCode", "VehicleID")
    if not query:
        return list(queryset)

    name_filter = (
        Q(CustomerFirstName__icontains=query)
        | Q(CustomerLastName__icontains=query)
    )
    # A date search only makes sense when the query parses as a date.
    try:
        parsed_date = datetime.strptime(query, "%Y-%m-%d").date()
    except ValueError:
        return list(queryset.filter(name_filter))
    return list(queryset.filter(name_filter | Q(AppointmentDate=parsed_date)))



def update_appointment(
    appointment_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    service_code: int | None = None,
    appointment_date=None,
    appointment_time=None,
    phone: str | None = None,
    email: str | None = None,
    notes: str | None = None,
) -> Appointment:
    """Update an appointment's booking details ( 'update an appointment').

    Only the fields passed in are changed; omitted ones keep their current
    value. A terminal appointment (cancelled / no-show) is a finished record
    and may not be edited, and any slot change is re-validated so the booking
    cannot be moved into the past or onto an already-taken slot.
    """
    appointment = get_appointment(appointment_id)
    if appointment.Status in TERMINAL_APPOINTMENT_STATUSES:
        raise ValidationError(
            f"A {appointment.Status.lower()} appointment can no longer be edited."
        )

    if first_name is not None or last_name is not None:
        first_name, last_name = _validate_customer_name(
            first_name if first_name is not None else appointment.CustomerFirstName,
            last_name if last_name is not None else appointment.CustomerLastName,
        )
        appointment.CustomerFirstName = first_name
        appointment.CustomerLastName = last_name

    if phone is not None or email is not None:
        phone, email = _validate_customer_contact(
            phone if phone is not None else appointment.CustomerPhone,
            email if email is not None else appointment.CustomerEmail,
        )
        appointment.CustomerPhone = phone
        appointment.CustomerEmail = email

    if service_code is not None:
        appointment.ServiceCode = get_service(service_code)

    if appointment_date is not None or appointment_time is not None:
        new_date = appointment_date or appointment.AppointmentDate
        new_time = appointment_time or appointment.AppointmentTime
        _validate_slot(new_date, new_time, exclude_appointment_id=appointment.pk)
        appointment.AppointmentDate = new_date
        appointment.AppointmentTime = new_time

    if notes is not None:
        appointment.Notes = notes.strip()

    try:
        with transaction.atomic():
            appointment.save()
            return appointment
    except ValidationError:
        raise
    except Exception:
        logger.exception("Appointment update failed (AppointmentID=%s).", appointment_id)
        raise ServiceError("Something went wrong, please try again.")



def set_appointment_status(appointment_id: int, new_status: str) -> Appointment:
    """Move an appointment to ``new_status`` if the transition is legal.

    The allowed moves come from ``APPOINTMENT_TRANSITIONS`` in models, so the
    lifecycle cannot be bypassed: a cancelled appointment cannot be reopened,
    and a pending request cannot jump straight to completed. Moving to
    "Checked In" stamps ``CheckedInAt`` once, at the moment the vehicle is
    received.
    """
    appointment = get_appointment(appointment_id)

    valid_statuses = {value for value, _label in APPOINTMENT_STATUSES}
    if new_status not in valid_statuses:
        raise ValidationError(f"'{new_status}' is not a valid appointment status.")
    if new_status == appointment.Status:
        raise ValidationError(f"The appointment is already {new_status.lower()}.")

    allowed = APPOINTMENT_TRANSITIONS.get(appointment.Status, [])
    if new_status not in allowed:
        raise ValidationError(
            f"An appointment cannot change from {appointment.Status.lower()} "
            f"to {new_status.lower()}."
        )

    appointment.Status = new_status
    if new_status == APPOINTMENT_CHECKED_IN and appointment.CheckedInAt is None:
        appointment.CheckedInAt = dj_timezone.now()

    try:
        with transaction.atomic():
            appointment.save()
            return appointment
    except ValidationError:
        raise
    except Exception:
        logger.exception(
            "Appointment status change failed (AppointmentID=%s).", appointment_id
        )
        raise ServiceError("Something went wrong, please try again.")


def accept_appointment(appointment_id: int) -> Appointment:
    """Accept a pending booking request, confirming the slot.

    A receptionist (or an administrator) reviews the queued request and accepts
    it, which is the only way a request becomes ``Confirmed``. Rejecting one
    instead goes through :func:`cancel_appointment`.
    """
    return set_appointment_status(appointment_id, APPOINTMENT_CONFIRMED)


#: Kept so existing callers that speak of "confirming" keep working; accepting
#: and confirming are the same transition.
confirm_appointment = accept_appointment


def cancel_appointment(appointment_id: int, reason: str = "") -> Appointment:
    """Cancel an appointment, recording why, and releasing its slot.

    A reason is optional for the receptionist but always stored so the list
    screen can explain a gap in the diary. Cancelling a pending request is how
    staff decline it. Cancellation is final: the freed date/time becomes
    bookable again immediately.
    """
    appointment = get_appointment(appointment_id)
    if appointment.Status in TERMINAL_APPOINTMENT_STATUSES:
        raise ValidationError(
            f"A {appointment.Status.lower()} appointment cannot be cancelled again."
        )
    if appointment.Status in (APPOINTMENT_CHECKED_IN, APPOINTMENT_COMPLETED):
        raise ValidationError(
            "The vehicle has already been checked in; this appointment cannot "
            "be cancelled."
        )

    appointment.Status = APPOINTMENT_CANCELLED
    appointment.CancelReason = (reason or "").strip()
    try:
        with transaction.atomic():
            appointment.save()
            return appointment
    except ValidationError:
        raise
    except Exception:
        logger.exception(
            "Appointment cancellation failed (AppointmentID=%s).", appointment_id
        )
        raise ServiceError("Something went wrong, please try again.")



# ---------------------------------------------------------------------------
# Vehicle module (FR5)
# ---------------------------------------------------------------------------
def _validate_vin(vin: str, exclude_vehicle_id: int | None = None) -> str | None:
    """VIN: optional, but when supplied must be 17 valid characters and unique.

    An empty VIN is stored as NULL rather than "", so the partial unique
    constraint never fires between vehicles with an unknown VIN.
    """
    vin = (vin or "").strip().upper()
    if not vin:
        return None
    if not VIN_PATTERN.match(vin):
        raise ValidationError(
            "Enter a valid 17-character VIN (letters and digits; I, O and Q "
            "are not allowed)."
        )
    existing = Vehicle.objects.filter(VIN=vin)
    if exclude_vehicle_id is not None:
        existing = existing.exclude(pk=exclude_vehicle_id)
    if existing.exists():
        # A duplicate at check-in means "reuse the existing record"; the caller
        # decides, so this is a DuplicateError the caller can act on.
        raise DuplicateError(f"A vehicle with VIN {vin} is already recorded.")
    return vin


def _validate_license_plate(
    plate: str, exclude_vehicle_id: int | None = None
) -> str | None:
    """Registration plate: optional, pattern-checked and unique when supplied."""
    plate = (plate or "").strip().upper()
    if not plate:
        return None
    if not LICENSE_PLATE_PATTERN.match(plate):
        raise ValidationError(
            "Enter a valid registration number (2-15 letters, digits, spaces "
            "or hyphens)."
        )
    existing = Vehicle.objects.filter(LicensePlate=plate)
    if exclude_vehicle_id is not None:
        existing = existing.exclude(pk=exclude_vehicle_id)
    if existing.exists():
        raise DuplicateError(
            f"A vehicle with registration {plate} is already recorded."
        )
    return plate


def _validate_vehicle_details(make: str, model: str, year, colour: str):
    """Model/year are the descriptive fields recorded at check-in."""
    make = (make or "").strip()
    model = (model or "").strip()
    colour = (colour or "").strip()
    if not model:
        raise ValidationError("The vehicle model is required.")
    if len(make) > 40 or len(model) > 40 or len(colour) > 20:
        raise ValidationError(
            "Make, model and colour must be 40, 40 and 20 characters or fewer."
        )
    if year is not None and year != "":
        year = int(year)
        if year < 1900 or year > date.today().year + 1:
            raise ValidationError(
                f"Enter a four-digit model year between 1900 and {date.today().year + 1}."
            )
    else:
        year = None
    return make, model, year, colour



def create_vehicle(
    make: str,
    model: str,
    vin: str = "",
    license_plate: str = "",
    year=None,
    colour: str = "",
    image=None,
) -> Vehicle:
    """Record a new vehicle ( 'record a vehicle when it arrives').

    At least one identifier (VIN or registration) must be supplied so the
    record can later be found and matched to an appointment. Uniqueness is
    enforced here as well as by the database, to give a clear message rather
    than an opaque constraint error.
    """
    vin = _validate_vin(vin)
    license_plate = _validate_license_plate(license_plate)
    if not vin and not license_plate:
        raise ValidationError(
            "Provide at least a VIN or a registration number for the vehicle."
        )
    make, model, year, colour = _validate_vehicle_details(make, model, year, colour)

    try:
        with transaction.atomic():
            return Vehicle.objects.create(
                VIN=vin,
                LicensePlate=license_plate,
                Make=make,
                Model=model,
                Year=year,
                Colour=colour,
                Image=image,
            )
    except ValidationError:
        raise
    except Exception:
        logger.exception("Vehicle creation failed.")
        raise ServiceError("Something went wrong, please try again.")


def get_vehicle(vehicle_id: int) -> Vehicle:
    """Return one vehicle, or raise :class:`NotFoundError`."""
    try:
        return Vehicle.objects.get(pk=vehicle_id)
    except Vehicle.DoesNotExist as exc:
        raise NotFoundError("That vehicle does not exist.") from exc


def list_vehicles() -> list[Vehicle]:
    """Return all recorded vehicles ( 'view vehicle records')."""
    return list(Vehicle.objects.all())


def search_vehicles(query: str) -> list[Vehicle]:
    """Search vehicles by registration number or VIN ( 'search for a vehicle').

    Matching is case-insensitive and partial, so a receptionist can type part
    of a plate. An exact VIN or plate naturally comes back as a single row.
    """
    query = (query or "").strip()
    if not query:
        return list(Vehicle.objects.all())
    return list(
        Vehicle.objects.filter(
            Q(LicensePlate__icontains=query) | Q(VIN__icontains=query)
        )
    )


def update_vehicle(
    vehicle_id: int,
    make: str | None = None,
    model: str | None = None,
    vin: str | None = None,
    license_plate: str | None = None,
    year=None,
    colour: str | None = None,
    image=None,
    replace_image: bool = False,
) -> Vehicle:
    """Update a vehicle's details ( 'update a vehicle's information').

    Changing the VIN or plate is checked against every *other* vehicle, so an
    update that would collide with an existing record is rejected rather than
    silently creating a duplicate identity.
    """
    vehicle = get_vehicle(vehicle_id)

    if vin is not None:
        vehicle.VIN = _validate_vin(vin, exclude_vehicle_id=vehicle.pk)
    if license_plate is not None:
        vehicle.LicensePlate = _validate_license_plate(
            license_plate, exclude_vehicle_id=vehicle.pk
        )
    if not vehicle.VIN and not vehicle.LicensePlate:
        raise ValidationError(
            "A vehicle must keep at least a VIN or a registration number."
        )

    if make is not None or model is not None or year is not None or colour is not None:
        make, model, year, colour = _validate_vehicle_details(
            make if make is not None else vehicle.Make,
            model if model is not None else vehicle.Model,
            year if year is not None else vehicle.Year,
            colour if colour is not None else vehicle.Colour,
        )
        vehicle.Make = make
        vehicle.Model = model
        vehicle.Year = year
        vehicle.Colour = colour

    if replace_image:
        vehicle.Image = image

    try:
        with transaction.atomic():
            vehicle.save()
            return vehicle
    except ValidationError:
        raise
    except Exception:
        logger.exception("Vehicle update failed (VehicleID=%s).", vehicle_id)
        raise ServiceError("Something went wrong, please try again.")


def delete_vehicle(vehicle_id: int) -> None:
    """Delete a vehicle only when it has no appointment or repair history.

    History is referenced rather than deleted: an appointment or an inspection
    that already points at the vehicle must keep its record, so the delete is
    blocked and the caller is warned instead.
    """
    vehicle = get_vehicle(vehicle_id)
    if vehicle.appointments.exists():
        raise ValidationError(
            "This vehicle cannot be deleted because it has linked appointments."
        )
    # An inspection is PROTECTed too: a condition report is part of the repair
    # history Timebox 4 will invoice from, so it outlives the vehicle record.
    if vehicle.inspections.exists():
        raise ValidationError(
            "This vehicle cannot be deleted because it has inspection history."
        )
    try:
        with transaction.atomic():
            vehicle.delete()
    except Exception:
        logger.exception("Vehicle deletion failed (VehicleID=%s).", vehicle_id)
        raise ServiceError("Something went wrong, please try again.")



def find_vehicle(vin: str = "", license_plate: str = "") -> Vehicle | None:
    """Return an existing vehicle matching the VIN or plate, else ``None``.

    This is the lookup half of check-in: a match by *either* identifier reuses
    the existing record so no duplicate vehicle is ever created.
    """
    vin = (vin or "").strip().upper()
    license_plate = (license_plate or "").strip().upper()
    if not vin and not license_plate:
        return None
    condition = Q()
    if vin:
        condition |= Q(VIN=vin)
    if license_plate:
        condition |= Q(LicensePlate=license_plate)
    return Vehicle.objects.filter(condition).first()


def check_in_vehicle(
    make: str,
    model: str,
    vin: str = "",
    license_plate: str = "",
    year=None,
    colour: str = "",
    image=None,
) -> tuple[Vehicle, bool]:
    """Record a vehicle on arrival, reusing an existing record if one matches.

    Returns ``(vehicle, created)``. When a vehicle with the same VIN or
    registration already exists, that record is returned and ``created`` is
    False - the duplicate is *never* stored. Otherwise a new record is created.
    This is exactly the find-or-create behaviour FR5 requires at check-in.

    A reused vehicle also picks up any newly-supplied photo, so a car recorded
    earlier without an image gains one the first time it is checked in.
    """
    existing = find_vehicle(vin=vin, license_plate=license_plate)
    if existing is not None:
        if image and not existing.Image:
            existing.Image = image
            existing.save(update_fields=["Image"])
        return existing, False

    vehicle = create_vehicle(
        make=make,
        model=model,
        vin=vin,
        license_plate=license_plate,
        year=year,
        colour=colour,
        image=image,
    )
    return vehicle, True


def match_vehicle_to_appointment(
    appointment_id: int,
    vehicle_id: int,
    confirm_replace: bool = False,
) -> Appointment:
    """Link a recorded vehicle to an appointment ( 'match a vehicle').

    Two guards come straight from the validation rules:

    * a cancelled (or no-show) appointment cannot be matched - matching is
      blocked, because the vehicle never came in for it;
    * if the appointment already has a *different* vehicle linked, replacing
      it needs an explicit confirmation, so a mistaken check-in can be
      corrected without silently losing the original link.

    Re-linking the vehicle that is already attached is a harmless no-op.
    """
    appointment = get_appointment(appointment_id)
    vehicle = get_vehicle(vehicle_id)

    if appointment.Status in TERMINAL_APPOINTMENT_STATUSES:
        raise ValidationError(
            f"A {appointment.Status.lower()} appointment cannot be matched to a "
            "vehicle."
        )

    if appointment.VehicleID_id is not None:
        if appointment.VehicleID_id == vehicle.pk:
            return appointment  # already linked to this vehicle
        if not confirm_replace:
            raise ValidationError(
                "This appointment is already linked to a different vehicle. "
                "Confirm to replace the existing link."
            )

    appointment.VehicleID = vehicle
    try:
        with transaction.atomic():
            appointment.save(update_fields=["VehicleID", "UpdatedAt"])
            return appointment
    except Exception:
        logger.exception(
            "Vehicle-appointment match failed (AppointmentID=%s, VehicleID=%s).",
            appointment_id,
            vehicle_id,
        )
        raise ServiceError("Something went wrong, please try again.")


def list_appointments_for_vehicle(vehicle_id: int) -> list[Appointment]:
    """Return a vehicle's appointment history ( 'view linked appointments')."""
    vehicle = get_vehicle(vehicle_id)
    return list(
        Appointment.objects.filter(VehicleID=vehicle)
        .select_related("ServiceCode")
        .order_by("AppointmentDate", "AppointmentTime")
    )


# ===========================================================================
# Timebox 3 - repair jobs, inspections and service bays
# ===========================================================================
# Same contract as the Timebox 1 and 2 functions above: validate first, write
# inside a transaction, and raise a ServiceError subclass rather than returning
# a status, so a caller cannot silently ignore a failure.
#
# Three rules hold the whole stage together:
#   * a repair job needs a *Checked In* appointment - an accepted booking only
#     means the slot is reserved, not that the car has arrived;
#   * a technician must be qualified for the appointment's requested service,
#     checked against Technician_Service and never waived for convenience;
#   * a bay's status always reflects the job it holds - Occupied while that job
#     is active, released automatically the moment the job completes.


# ---------------------------------------------------------------------------
# Service bays (FR9)
# ---------------------------------------------------------------------------
def _validate_bay_name(bay_name: str, exclude_bay_id: int | None = None) -> str:
    """Return a trimmed, unique bay name, or raise.

    The name is the label technicians read on the workshop wall, so it must be
    present and must identify exactly one bay.
    """
    name = (bay_name or "").strip()
    if not name:
        raise ValidationError("A service bay name is required.")
    if len(name) > 40:
        raise ValidationError("The bay name must be 40 characters or fewer.")

    duplicate = ServiceBay.objects.filter(BayName__iexact=name)
    if exclude_bay_id is not None:
        duplicate = duplicate.exclude(pk=exclude_bay_id)
    if duplicate.exists():
        raise DuplicateError(f"A service bay named '{name}' already exists.")
    return name


def create_service_bay(bay_name: str) -> ServiceBay:
    """Add a service bay to the workshop ( 'create a bay').

    A new bay starts Available: it holds no job until one is assigned to it.
    """
    name = _validate_bay_name(bay_name)
    try:
        with transaction.atomic():
            return ServiceBay.objects.create(BayName=name, Status=BAY_AVAILABLE)
    except ServiceError:
        raise
    except Exception:
        logger.exception("Service bay creation failed (BayName=%s).", bay_name)
        raise ServiceError("Something went wrong, please try again.")


def get_service_bay(bay_id: int) -> ServiceBay:
    """Return one service bay, or raise :class:`NotFoundError`."""
    try:
        return ServiceBay.objects.get(pk=bay_id)
    except ServiceBay.DoesNotExist as exc:
        raise NotFoundError("That service bay does not exist.") from exc


def list_service_bays() -> list[ServiceBay]:
    """Return every bay, ordered by name ( 'view service bays')."""
    return list(ServiceBay.objects.all())


def list_available_bays() -> list[ServiceBay]:
    """Return only the bays free to take another repair job.

    Populates the bay dropdown on the repair forms, so an Occupied bay is never
    offered for selection in the first place.
    """
    return list(ServiceBay.objects.filter(Status=BAY_AVAILABLE))


def update_service_bay(bay_id: int, bay_name: str) -> ServiceBay:
    """Rename a service bay ( 'update a bay').

    Only the name is editable: ``Status`` is derived from whatever job the bay
    holds, so letting an administrator type "Available" over an Occupied bay
    would allow two jobs to be parked in one place.
    """
    bay = get_service_bay(bay_id)
    bay.BayName = _validate_bay_name(bay_name, exclude_bay_id=bay_id)
    try:
        with transaction.atomic():
            bay.save(update_fields=["BayName"])
            return bay
    except ServiceError:
        raise
    except Exception:
        logger.exception("Service bay update failed (BayID=%s).", bay_id)
        raise ServiceError("Something went wrong, please try again.")


def delete_service_bay(bay_id: int) -> None:
    """Retire a service bay ( 'delete a bay').

    A bay that currently holds an active job cannot be deleted - doing so would
    leave a repair job claiming to be in a bay that no longer exists. Completed
    jobs keep their history because ``Repair.BayID`` is nullable, so an old job
    simply stops naming the retired bay.
    """
    bay = get_service_bay(bay_id)
    if bay.repairs.filter(Status__in=ACTIVE_REPAIR_STATUSES).exists():
        raise ValidationError(
            f"{bay.BayName} cannot be deleted while it holds an active repair job."
        )
    try:
        with transaction.atomic():
            bay.delete()
    except Exception:
        logger.exception("Service bay deletion failed (BayID=%s).", bay_id)
        raise ServiceError("Something went wrong, please try again.")


def _set_bay_status(bay: ServiceBay | None, status: str) -> None:
    """Persist a bay's availability.

    Split out so every path that changes a bay's status - assignment,
    reassignment, completion - writes it the same way, and so a ``None`` bay
    (a job with no bay yet) is a no-op rather than a crash.
    """
    if bay is None:
        return
    bay.Status = status
    bay.save(update_fields=["Status"])


def _refresh_bay_status(bay_id: int | None) -> None:
    """Recompute a bay's status from the jobs currently assigned to it.

    Used after a repair is reassigned away from a bay: the old bay is Occupied
    if any active job still points at it, otherwise Available. Recomputing from
    the data means the stored status can never drift out of step with reality.
    """
    if bay_id is None:
        return
    bay = ServiceBay.objects.filter(pk=bay_id).first()
    if bay is None:
        return
    holds_active = bay.repairs.filter(Status__in=ACTIVE_REPAIR_STATUSES).exists()
    _set_bay_status(bay, BAY_OCCUPIED if holds_active else BAY_AVAILABLE)


# ---------------------------------------------------------------------------
# Repair jobs (FR6)
# ---------------------------------------------------------------------------
def _validate_repair_priority(priority: str) -> str:
    """Return a valid repair priority, or raise.

    Priority belongs to the job and is fixed at creation, so an unknown value
    here means the caller is guessing at the two options the workshop uses.
    """
    value = (priority or "").strip()
    valid = {stored for stored, _label in REPAIR_PRIORITIES}
    if value not in valid:
        raise ValidationError(
            f"'{value or 'None'}' is not a valid repair priority. "
            "Choose Urgent or Routine."
        )
    return value


def _validate_repair_technician(
    technician_id: int | None, service: Service
) -> Technician | None:
    """Check a technician is qualified for ``service``, returning them or None.

    This is the FR6 qualification rule: a technician can only be put on a repair
    for a service they hold a ``Technician_Service`` record for. Availability is
    irrelevant to the check - an unqualified technician is refused no matter how
    free they are, because the workshop cannot safely carry out work nobody on
    site is qualified to do.
    """
    if technician_id is None:
        return None

    try:
        technician = Technician.objects.select_related("StaffID").get(
            pk=technician_id
        )
    except Technician.DoesNotExist as exc:
        raise NotFoundError("That technician does not exist.") from exc

    qualified = TechnicianService.objects.filter(
        TechnicianID=technician, ServiceCode=service
    ).exists()
    if not qualified:
        raise ValidationError(
            f"{technician.full_name} is not qualified to perform "
            f"'{service.ServiceName}', so they cannot be assigned to this repair."
        )
    return technician


def _validate_repair_bay(
    bay_id: int | None, exclude_repair_id: int | None = None
) -> ServiceBay | None:
    """Check a bay is free to take a job, returning it or None.

    An Occupied bay already holds an active repair, so assigning it again would
    put two cars in one physical space. A job keeping the bay it already has is
    allowed, which is what ``exclude_repair_id`` is for.
    """
    if bay_id is None:
        return None

    try:
        bay = ServiceBay.objects.get(pk=bay_id)
    except ServiceBay.DoesNotExist as exc:
        raise NotFoundError("That service bay does not exist.") from exc

    if exclude_repair_id is not None:
        held_by_self = Repair.objects.filter(
            pk=exclude_repair_id, BayID=bay
        ).exists()
        if held_by_self:
            return bay

    if not bay.is_available:
        raise ValidationError(
            f"{bay.BayName} is currently occupied by another repair job and "
            "cannot be assigned."
        )
    return bay


def create_repair(
    appointment_id: int,
    priority: str = REPAIR_PRIORITY_ROUTINE,
    technician_id: int | None = None,
    bay_id: int | None = None,
) -> Repair:
    """Raise a repair job for a checked-in appointment (FR6).

    The gate is the appointment's status: only ``Checked In`` opens a job.
    Accepting a booking merely reserves the slot, and a pending request may never
    be accepted at all, so neither can justify work starting - the car is not at
    the workshop yet.

    A vehicle must also be linked, since the job exists to be carried out on a
    specific car and the inspection (FR7) records that car's condition.

    Technician and bay are both optional here. In practice a job is raised the
    moment the vehicle arrives, while a qualified technician or a free bay may
    only be confirmed moments later, so both can be filled in afterwards through
    :func:`assign_technician` and :func:`assign_bay`.
    """
    appointment = get_appointment(appointment_id)
    priority = _validate_repair_priority(priority)

    if appointment.Status != APPOINTMENT_CHECKED_IN:
        raise ValidationError(
            f"A repair job can only be created once the appointment is checked "
            f"in; appointment {appointment.pk} is {appointment.Status.lower()}."
        )

    if appointment.VehicleID_id is None:
        raise ValidationError(
            "Link the vehicle to the appointment before creating a repair job."
        )

    existing = Repair.objects.filter(AppointmentID=appointment).first()
    if existing is not None:
        raise DuplicateError(
            f"Appointment {appointment.pk} already has repair job #{existing.pk}."
        )

    technician = _validate_repair_technician(technician_id, appointment.ServiceCode)
    bay = _validate_repair_bay(bay_id)

    try:
        with transaction.atomic():
            repair = Repair.objects.create(
                AppointmentID=appointment,
                TechnicianID=technician,
                BayID=bay,
                RepairPriority=priority,
                Status=REPAIR_NOT_STARTED,
            )
            # The bay is Occupied from the moment it holds the job, including
            # while the job is still Not Started.
            _set_bay_status(bay, BAY_OCCUPIED)
            return repair
    except ServiceError:
        raise
    except Exception:
        logger.exception(
            "Repair job creation failed (AppointmentID=%s).", appointment_id
        )
        raise ServiceError("Something went wrong, please try again.")


#: How the repair listing is joined. Every job is shown alongside its
#: appointment, the service requested, the vehicle, the technician assigned and
#: the bay it sits in, so the list screen renders without a query per row.
_REPAIR_SELECT_RELATED = (
    "AppointmentID",
    "AppointmentID__ServiceCode",
    "AppointmentID__VehicleID",
    "TechnicianID",
    "TechnicianID__StaffID",
    "InspectionID",
    "BayID",
)


def get_repair(repair_id: int) -> Repair:
    """Return one repair job with its relations, or raise :class:`NotFoundError`."""
    try:
        return Repair.objects.select_related(*_REPAIR_SELECT_RELATED).get(
            pk=repair_id
        )
    except Repair.DoesNotExist as exc:
        raise NotFoundError("That repair job does not exist.") from exc


def list_repairs(
    status: str = "",
    technician_id: int | None = None,
    priority: str = "",
) -> list[Repair]:
    """Return repair jobs, optionally filtered by status, technician or priority.

    This is the administrator's job board (FR6): the same query serves the
    unfiltered listing and each filter, so a filtered view can never disagree
    with the full one. Urgent jobs float to the top, because that is the order a
    workshop reads its board in.
    """
    repairs = Repair.objects.select_related(*_REPAIR_SELECT_RELATED)

    if status:
        valid_statuses = {value for value, _label in REPAIR_STATUSES}
        if status not in valid_statuses:
            raise ValidationError(f"'{status}' is not a valid repair status.")
        repairs = repairs.filter(Status=status)

    if priority:
        valid_priorities = {value for value, _label in REPAIR_PRIORITIES}
        if priority not in valid_priorities:
            raise ValidationError(f"'{priority}' is not a valid repair priority.")
        repairs = repairs.filter(RepairPriority=priority)

    if technician_id is not None:
        repairs = repairs.filter(TechnicianID_id=technician_id)

    # Urgent first, then still-outstanding work before finished work, newest
    # first within that. Mirrors how the board is read on the workshop floor.
    return list(
        repairs.order_by("-RepairPriority", "Status", "-CreatedAt", "RepairID")
    )


def list_repairs_for_technician(technician_id: int) -> list[Repair]:
    """Return the jobs assigned to one technician ( 'My Repairs', FR8).

    A technician's whole view of this module. Scoped by the assignment itself,
    so the query cannot return someone else's job even if a caller passes a
    filter it should not have.
    """
    get_technician(technician_id)
    return list(
        Repair.objects.select_related(*_REPAIR_SELECT_RELATED)
        .filter(TechnicianID_id=technician_id)
        .order_by("-RepairPriority", "Status", "-CreatedAt", "RepairID")
    )


def get_repair_for_appointment(appointment_id: int) -> Repair | None:
    """Return the repair job raised for an appointment, or ``None``.

    Lets the appointment detail screen link straight to its job without
    repeating the lookup in every template.
    """
    return (
        Repair.objects.select_related(*_REPAIR_SELECT_RELATED)
        .filter(AppointmentID_id=appointment_id)
        .first()
    )


def list_appointments_eligible_for_repair() -> list[Appointment]:
    """Return checked-in appointments that can actually open a repair job.

    The creation form only offers these, so an administrator cannot pick a
    booking whose car has not arrived, or one that already has a job. All three
    conditions mirror the gate :func:`create_repair` enforces - Checked In, a
    linked vehicle, no existing job - so the dropdown can never propose a job the
    service layer would then refuse. The vehicle check is the easy one to miss
    because check-in and vehicle-matching are separate steps: an appointment can
    be Checked In with no car recorded yet, and offering it here would send the
    administrator down a dead end.
    """
    return list(
        Appointment.objects.filter(Status=APPOINTMENT_CHECKED_IN)
        .exclude(VehicleID__isnull=True)
        .exclude(repairs__isnull=False)
        .select_related("ServiceCode", "VehicleID")
        .order_by("AppointmentDate", "AppointmentTime")
    )


def assign_technician(repair_id: int, technician_id: int) -> Repair:
    """Assign a qualified technician to a repair job (FR6, step 2).

    The qualification check is the same one creation uses: the technician must
    hold a ``Technician_Service`` record for the service the appointment
    requested. Being free is not enough, so an unqualified technician is refused
    whatever their workload looks like.

    A job already Completed cannot be reassigned - the work is finished, and
    Timebox 4 will key payroll off whoever the job is attributed to.
    """
    repair = get_repair(repair_id)

    if repair.is_completed:
        raise ValidationError(
            "A completed repair job can no longer be reassigned to another "
            "technician."
        )

    technician = _validate_repair_technician(
        technician_id, repair.AppointmentID.ServiceCode
    )
    if technician is None:
        raise ValidationError("Choose a technician to assign.")

    if repair.TechnicianID_id == technician.pk:
        return repair  # already assigned to this technician

    try:
        with transaction.atomic():
            repair.TechnicianID = technician
            repair.save(update_fields=["TechnicianID", "UpdatedAt"])
            return repair
    except ServiceError:
        raise
    except Exception:
        logger.exception(
            "Technician assignment failed (RepairID=%s, TechnicianID=%s).",
            repair_id,
            technician_id,
        )
        raise ServiceError("Something went wrong, please try again.")


def assign_bay(repair_id: int, bay_id: int) -> Repair:
    """Assign an available service bay to a repair job (FR6 step 3, FR9).

    Only an Available bay can be taken. The bay becomes Occupied immediately -
    including while the job is still Not Started, because a car booked into a
    bay is physically in it whether or not work has begun.

    Moving a job *between* bays frees the old one, so a bay is never left
    Occupied by a job that has moved on.
    """
    repair = get_repair(repair_id)

    if repair.is_completed:
        raise ValidationError(
            "A completed repair job can no longer be moved to another bay."
        )

    bay = _validate_repair_bay(bay_id, exclude_repair_id=repair.pk)
    if bay is None:
        raise ValidationError("Choose a service bay to assign.")

    previous_bay_id = repair.BayID_id
    if previous_bay_id == bay.pk:
        return repair  # already in this bay

    try:
        with transaction.atomic():
            repair.BayID = bay
            repair.save(update_fields=["BayID", "UpdatedAt"])
            _set_bay_status(bay, BAY_OCCUPIED)
            # Release the bay the job has just left, unless another active job
            # is still holding it.
            _refresh_bay_status(previous_bay_id)
            return repair
    except ServiceError:
        raise
    except Exception:
        logger.exception(
            "Bay assignment failed (RepairID=%s, BayID=%s).", repair_id, bay_id
        )
        raise ServiceError("Something went wrong, please try again.")


def update_repair_assignment(
    repair_id: int,
    technician_id: int | None = None,
    bay_id: int | None = None,
) -> Repair:
    """Update a job's technician and/or bay in one step (FR6).

    Lets the administrator fill in whichever of the two became available after
    the job was created, or reassign both when a technician calls in sick. Each
    half is validated by the same rule as the dedicated assigner, so this is a
    convenience for the edit screen rather than a looser path.

    Passing ``None`` leaves that half untouched - clearing an assignment is not
    offered, because a job with work under way must be able to name someone.
    """
    repair = get_repair(repair_id)
    if technician_id is not None:
        repair = assign_technician(repair.pk, technician_id)
    if bay_id is not None:
        repair = assign_bay(repair.pk, bay_id)
    return repair


# ---------------------------------------------------------------------------
# Repair progress (FR8)
# ---------------------------------------------------------------------------
def can_manage_repair(repair: Repair, staff: Staff) -> bool:
    """True when ``staff`` may inspect or move this job's status.

    The scoping rule for a technician's access to this module: they act on their
    *own* jobs only. An administrator may act on any job so they can supervise
    and finish work when a technician is absent. Everyone else - including the
    receptionist, whose part ended at check-in - gets read-only visibility and
    is refused here.

    A technician is matched through their ``Technician`` record, not by comparing
    names, so renaming a staff account cannot change who owns a job.
    """
    if staff is None:
        return False
    if staff.role_name == Role.ADMINISTRATOR:
        return True
    if staff.role_name != Role.TECHNICIAN:
        return False
    technician = Technician.objects.filter(StaffID=staff).first()
    if technician is None:
        return False
    return repair.TechnicianID_id == technician.pk


def technician_for_staff(staff: Staff) -> Technician | None:
    """Return the ``Technician`` record behind a staff account, or ``None``.

    Bridges the session (which identifies a staff account) to the workshop data
    (which is keyed by technician), so a signed-in technician can be shown their
    own jobs without the caller knowing the record's primary key.
    """
    if staff is None:
        return None
    return Technician.objects.filter(StaffID=staff).first()


def set_repair_status(repair_id: int, new_status: str) -> Repair:
    """Move a repair job to ``new_status`` if the transition is legal (FR8).

    The allowed moves come from ``REPAIR_TRANSITIONS`` in models, so the
    progression cannot be bypassed: a job cannot jump from Not Started straight
    to Completed, and a Completed job cannot be reopened. Each step must happen
    in order, because "Completed" is the event that releases the service bay and
    that Timebox 4 reads as the trigger to generate an invoice.

    Completing a job releases its bay automatically - no separate step, so a bay
    can never be left Occupied by work that has finished.

    Authorisation is enforced by the view layer through
    :func:`can_manage_repair`; this function holds the lifecycle itself so the
    same rule applies to any future caller.
    """
    repair = get_repair(repair_id)

    valid_statuses = {value for value, _label in REPAIR_STATUSES}
    if new_status not in valid_statuses:
        raise ValidationError(f"'{new_status}' is not a valid repair status.")
    if new_status == repair.Status:
        raise ValidationError(f"The repair job is already {new_status.lower()}.")

    allowed = REPAIR_TRANSITIONS.get(repair.Status, [])
    if new_status not in allowed:
        raise ValidationError(
            f"A repair job cannot change from {repair.Status.lower()} to "
            f"{new_status.lower()}."
        )

    bay_id = repair.BayID_id
    repair.Status = new_status
    if new_status == REPAIR_COMPLETED:
        # T4-DB-01: stamp the completion moment once. Payroll groups jobs by when
        # the work finished rather than when it was raised, and the customer's
        # status page reports this as the point the car was ready. The lifecycle
        # is one-way, so this can only ever be set the first time.
        repair.CompletedAt = dj_timezone.now()

    try:
        with transaction.atomic():
            repair.save(update_fields=["Status", "CompletedAt", "UpdatedAt"])
            if new_status == REPAIR_COMPLETED:
                # The bay is free again the moment the work is done, unless
                # another active job is somehow still parked in it.
                _refresh_bay_status(bay_id)
            return repair
    except ServiceError:
        raise
    except Exception:
        logger.exception(
            "Repair status change failed (RepairID=%s, new_status=%s).",
            repair_id,
            new_status,
        )
        raise ServiceError("Something went wrong, please try again.")


def start_repair(repair_id: int) -> Repair:
    """Begin work on a job: Not Started -> In Progress."""
    return set_repair_status(repair_id, REPAIR_IN_PROGRESS)


def complete_repair(repair_id: int) -> Repair:
    """Finish a job: In Progress -> Completed, releasing its service bay."""
    return set_repair_status(repair_id, REPAIR_COMPLETED)


# ---------------------------------------------------------------------------
# Vehicle inspection (FR7)
# ---------------------------------------------------------------------------
#: The three image columns an inspection has, in the order photographs fill them.
_INSPECTION_IMAGE_FIELDS = ("Image1", "Image2", "Image3")


def _place_inspection_images(inspection: Inspection | None, images) -> dict:
    """Work out which image column each uploaded photograph goes into.

    Returns a mapping of ``{"Image1": file, ...}`` covering only the columns
    that change, so an edit that adds one photograph to a two-photo inspection
    leaves the existing pair untouched.

    The count check is what enforces the three-image rule at the service layer:
    photographs already stored plus the ones being added may not exceed
    ``MAX_INSPECTION_IMAGES``. Going over is an error rather than a silent drop,
    because a technician who uploads four pictures and sees three stored would
    reasonably assume the fourth was saved too.
    """
    uploads = [image for image in (images or []) if image]
    if not uploads:
        return {}

    existing_count = inspection.image_count if inspection is not None else 0
    allowed = MAX_INSPECTION_IMAGES - existing_count
    if len(uploads) > allowed:
        if existing_count:
            raise ValidationError(
                f"An inspection can hold at most {MAX_INSPECTION_IMAGES} images. "
                f"{existing_count} "
                f"{'is' if existing_count == 1 else 'are'} already stored, so "
                f"only {allowed} more can be added."
            )
        raise ValidationError(
            f"An inspection can hold at most {MAX_INSPECTION_IMAGES} images; "
            f"{len(uploads)} were supplied."
        )

    # Fill the empty columns in order, so photos keep the sequence they were
    # uploaded in and an existing one is never overwritten by accident.
    assignment = {}
    for field in _INSPECTION_IMAGE_FIELDS:
        if not uploads:
            break
        if inspection is not None and getattr(inspection, field):
            continue
        assignment[field] = uploads.pop(0)
    return assignment


def _require_repair_technician(repair: Repair, staff: Staff) -> None:
    """Refuse unless ``staff`` may act on ``repair`` (FR7/FR8 scoping).

    A technician may only inspect or progress a job assigned to them; an
    administrator may act on any job. Raising here rather than returning a flag
    means no caller can forget to check the result.
    """
    if can_manage_repair(repair, staff):
        return
    if staff is not None and staff.role_name == Role.TECHNICIAN:
        raise AuthorizationError("You can only work on repair jobs assigned to you.")
    raise AuthorizationError("Your role cannot modify this repair job.")


def create_inspection(
    repair_id: int, staff: Staff, description: str, images=()
) -> tuple[Repair, Inspection]:
    """Record the vehicle's condition for a repair job (FR7).

    Returns ``(repair, inspection)``: the inspection is written and then linked
    back onto the job via ``Repairs.InspectionID``, which is how the detail
    screen - and Timebox 4 later - finds it.

    Three conditions must hold before anything is written:

    * a technician must already be assigned, since the inspection is that
      technician's own documentation of the car and cannot be recorded for a job
      nobody owns;
    * the caller must be the assigned technician or an administrator;
    * the appointment must have a vehicle linked, because the inspection is *of*
      that vehicle.

    A job may only be inspected once; amending the record afterwards goes
    through :func:`update_inspection`.
    """
    repair = get_repair(repair_id)
    _require_repair_technician(repair, staff)

    text = (description or "").strip()
    if not text:
        raise ValidationError("An inspection description is required.")

    if repair.TechnicianID_id is None:
        raise ValidationError(
            "Assign a technician to the repair job before recording its inspection."
        )

    vehicle = repair.AppointmentID.VehicleID
    if vehicle is None:
        raise ValidationError(
            "The appointment has no vehicle linked, so there is nothing to inspect."
        )

    if repair.InspectionID_id is not None:
        raise DuplicateError(
            f"Repair job {repair.pk} already has an inspection recorded. Update "
            "it instead of creating another."
        )

    placement = _place_inspection_images(None, images)

    try:
        with transaction.atomic():
            inspection = Inspection.objects.create(
                VehicleID=vehicle,
                Description=text,
                **placement,
            )
            repair.InspectionID = inspection
            repair.save(update_fields=["InspectionID", "UpdatedAt"])
            return repair, inspection
    except ServiceError:
        raise
    except Exception:
        logger.exception("Inspection creation failed (RepairID=%s).", repair_id)
        raise ServiceError("Something went wrong, please try again.")


def update_inspection(
    repair_id: int, staff: Staff, description: str = "", images=()
) -> tuple[Repair, Inspection]:
    """Amend an existing inspection (FR7).

    Same authorisation as creation, so a technician can correct their own notes
    and add photographs but cannot touch anyone else's record. A blank
    description leaves the stored one alone - this amends rather than clears.
    """
    repair = get_repair(repair_id)
    _require_repair_technician(repair, staff)

    if repair.InspectionID_id is None:
        raise NotFoundError("That repair job has no inspection to update.")

    inspection = get_inspection(repair.InspectionID_id)

    text = (description or "").strip()
    if text:
        inspection.Description = text

    placement = _place_inspection_images(inspection, images)

    try:
        with transaction.atomic():
            for field, image in placement.items():
                setattr(inspection, field, image)
            inspection.save()
            return repair, inspection
    except ServiceError:
        raise
    except Exception:
        logger.exception("Inspection update failed (RepairID=%s).", repair_id)
        raise ServiceError("Something went wrong, please try again.")


def get_inspection(inspection_id: int) -> Inspection:
    """Return one inspection, or raise :class:`NotFoundError`."""
    try:
        return Inspection.objects.select_related("VehicleID").get(pk=inspection_id)
    except Inspection.DoesNotExist as exc:
        raise NotFoundError("That inspection does not exist.") from exc


# ---------------------------------------------------------------------------
# Spare-part inventory (FR10, Timebox 4)
# ---------------------------------------------------------------------------
#   * stock is the only live figure: recording usage reduces it in the same
#     transaction, so the two can never disagree (T4-INV-06);
#   * "low stock" is derived from stock versus threshold, never stored, so
#     restocking clears the flag with no second step (T4-INV-09/10);
#   * a part with usage history is deactivated rather than deleted, because
#     deleting it would erase the price an invoice was built from (T4-INV-04);
#   * prices are copied onto the usage row at the moment of use, so a later
#     catalogue change cannot re-price a repair that has already happened
#     (T4-INV-07).
# ---------------------------------------------------------------------------
def _money(value) -> Decimal:
    """Round ``value`` to two decimal places, half-up, ready for storage.

    Every calculated amount in this module goes through here, so an invoice total
    always equals the sum of its own stored parts rather than differing by a
    rounding cent when re-added.
    """
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _validate_part_name(part_name: str, exclude_part_id: int | None = None) -> str:
    """Return a trimmed, unique part name, or raise.

    Uniqueness is checked here as well as enforced by the schema, so the user gets
    "that name is already used" rather than a database error. Editing a part may
    keep its own name, which is what ``exclude_part_id`` is for. The lookup is
    case-insensitive: "Brake Pads" and "brake pads" are the same part to a
    technician at the shelf, and letting both through would split one item across
    two stock figures.
    """
    name = (part_name or "").strip()
    if not name:
        raise ValidationError("A part name is required.")
    if len(name) > 100:
        raise ValidationError("A part name must be 100 characters or fewer.")

    clash = Parts.objects.filter(PartName__iexact=name)
    if exclude_part_id is not None:
        clash = clash.exclude(pk=exclude_part_id)
    if clash.exists():
        raise DuplicateError(f"A part named '{name}' already exists.")
    return name


def _validate_non_negative(value, label: str, *, integer: bool = False):
    """Return ``value`` as an int (or Decimal) that is not negative, or raise.

    Shared by every numeric part field because they fail the same way: absent,
    non-numeric and negative all need a plain message rather than a stack trace.
    """
    if value is None or str(value).strip() == "":
        raise ValidationError(f"{label} is required.")
    try:
        number = int(value) if integer else Decimal(str(value).strip())
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError(f"{label} must be a number.") from exc
    if number < 0:
        raise ValidationError(f"{label} must not be negative.")
    return number


def _validate_unit_price(value) -> Decimal:
    """A unit price must be present, numeric and not negative (see T4-INV-01)."""
    return _validate_non_negative(value, "Unit price")


def _validate_stock_qty(value) -> int:
    """A stock quantity must be present, whole and not negative (see T4-INV-01)."""
    return _validate_non_negative(value, "Stock quantity", integer=True)


def _validate_reorder_threshold(value) -> int:
    """A reorder threshold must be present, whole and not negative (see T4-INV-01)."""
    return _validate_non_negative(value, "Reorder threshold", integer=True)


def create_part(part_name: str, unit_price, stock_qty, reorder_threshold) -> Parts:
    """Add a part to the catalogue (T4-INV-01).

    The new part starts active. If the administrator enters stock at or below the
    threshold they chose, the part is flagged low stock immediately - which is
    correct rather than a defect, since that is precisely the condition the
    threshold describes.
    """
    name = _validate_part_name(part_name)
    price = _money(_validate_unit_price(unit_price))
    stock = _validate_stock_qty(stock_qty)
    threshold = _validate_reorder_threshold(reorder_threshold)

    try:
        with transaction.atomic():
            return Parts.objects.create(
                PartName=name,
                UnitPrice=price,
                StockQty=stock,
                ReorderThreshold=threshold,
                IsActive=True,
            )
    except IntegrityError:
        # Lost a race with a concurrent insert of the same name; the unique
        # column is the last line of defence behind _validate_part_name.
        raise DuplicateError(f"A part named '{name}' already exists.")
    except ServiceError:
        raise
    except Exception:
        logger.exception("Part creation failed (name=%s).", name)
        raise ServiceError("Something went wrong, please try again.")


def get_part(part_id: int) -> Parts:
    """Return one part, or raise :class:`NotFoundError`."""
    try:
        return Parts.objects.get(pk=part_id)
    except Parts.DoesNotExist as exc:
        raise NotFoundError("That part does not exist.") from exc


def list_parts(include_inactive: bool = True) -> list[Parts]:
    """Return the parts catalogue, optionally hiding deactivated parts (FR10).

    ``include_inactive`` is False for the dropdown offered when recording usage: a
    deactivated part is retired, so it must not be selectable for new work even
    though it still appears in the administrator's full list and in old invoices.
    """
    parts = Parts.objects.all()
    if not include_inactive:
        parts = parts.filter(IsActive=True)
    return list(parts.order_by("PartName", "PartID"))


def search_parts(query: str, include_inactive: bool = True) -> list[Parts]:
    """Return parts whose name matches ``query``, for the catalogue search box."""
    text = (query or "").strip()
    if not text:
        return list_parts(include_inactive=include_inactive)
    parts = Parts.objects.filter(PartName__icontains=text)
    if not include_inactive:
        parts = parts.filter(IsActive=True)
    return list(parts.order_by("PartName", "PartID"))


def list_low_stock_parts() -> list[Parts]:
    """Return the active parts at or below their reorder threshold (T4-INV-09).

    The comparison is pushed into the database rather than done in Python, so the
    page, its counter and the navigation badge all read one query and cannot
    disagree. Only active parts qualify: a retired part cannot be restocked, so
    flagging it would be noise the administrator has to read past.
    """
    return list(
        Parts.objects.filter(IsActive=True, StockQty__lte=F("ReorderThreshold"))
        .order_by("PartName", "PartID")
    )


def count_low_stock_parts() -> int:
    """How many active parts need reordering, for the badge and dashboard."""
    return Parts.objects.filter(
        IsActive=True, StockQty__lte=F("ReorderThreshold")
    ).count()


def update_part(
    part_id: int,
    part_name: str = "",
    unit_price=None,
    stock_qty=None,
    reorder_threshold=None,
) -> Parts:
    """Amend a part's catalogue details (T4-INV-03).

    Every argument is optional so an administrator can correct a price without
    re-typing the stock figure. Changing ``UnitPrice`` here deliberately does not
    touch any existing ``Parts_usage`` row - those hold the price as it was at the
    time of use, which is the whole point of copying it (T4-INV-07).

    Updating stock is also how a part is restocked, and therefore what clears the
    low-stock flag (T4-INV-10). There is no separate "clear flag" action, because
    the flag is not stored.
    """
    part = get_part(part_id)

    fields: dict = {}
    if part_name:
        fields["PartName"] = _validate_part_name(part_name, exclude_part_id=part_id)
    if unit_price is not None:
        fields["UnitPrice"] = _money(_validate_unit_price(unit_price))
    if stock_qty is not None:
        fields["StockQty"] = _validate_stock_qty(stock_qty)
    if reorder_threshold is not None:
        fields["ReorderThreshold"] = _validate_reorder_threshold(reorder_threshold)

    if not fields:
        raise ValidationError("Nothing to update: supply a field to change.")

    try:
        with transaction.atomic():
            for field, value in fields.items():
                setattr(part, field, value)
            part.save(update_fields=[*fields.keys(), "UpdatedAt"])
            return part
    except IntegrityError:
        raise DuplicateError(f"A part named '{fields['PartName']}' already exists.")
    except ServiceError:
        raise
    except Exception:
        logger.exception("Part update failed (PartID=%s).", part_id)
        raise ServiceError("Something went wrong, please try again.")


def deactivate_part(part_id: int) -> Parts:
    """Retire a part without erasing its history (T4-INV-04).

    This is the retirement path for anything already used. Deleting a part that
    has appeared in ``Parts_usage`` would take the price an existing invoice was
    built from out of the database; deactivating it keeps every row - and every
    bill quoting it - intact while stopping it being offered for new repairs.
    """
    part = get_part(part_id)
    if not part.IsActive:
        raise ValidationError(f"'{part.PartName}' is already deactivated.")

    try:
        with transaction.atomic():
            part.IsActive = False
            part.save(update_fields=["IsActive", "UpdatedAt"])
            return part
    except ServiceError:
        raise
    except Exception:
        logger.exception("Part deactivation failed (PartID=%s).", part_id)
        raise ServiceError("Something went wrong, please try again.")


def reactivate_part(part_id: int) -> Parts:
    """Bring a deactivated part back into the catalogue.

    Offered because deactivation is retirement rather than deletion: a part the
    workshop stopped stocking may be sourced again, and reactivating it keeps its
    usage history, where creating a replacement row would start from nothing.
    """
    part = get_part(part_id)
    if part.IsActive:
        raise ValidationError(f"'{part.PartName}' is already active.")

    try:
        with transaction.atomic():
            part.IsActive = True
            part.save(update_fields=["IsActive", "UpdatedAt"])
            return part
    except ServiceError:
        raise
    except Exception:
        logger.exception("Part reactivation failed (PartID=%s).", part_id)
        raise ServiceError("Something went wrong, please try again.")


def part_has_usage(part_id: int) -> bool:
    """True once a part has been drawn against any repair job.

    The single question that decides whether the catalogue may offer "delete" or
    only "deactivate".
    """
    get_part(part_id)
    return PartsUsage.objects.filter(PartID_id=part_id).exists()


def delete_part(part_id: int) -> None:
    """Delete a part that has never been used (T4-INV-04).

    Refused the moment the part has usage history, with a message that names
    deactivation as the alternative, so the administrator is told what to do
    rather than only that they cannot do this.
    """
    part = get_part(part_id)
    if part_has_usage(part_id):
        raise ValidationError(
            f"'{part.PartName}' has been used on a repair job, so it cannot be "
            "deleted. Deactivate it instead to keep its history intact."
        )

    try:
        with transaction.atomic():
            part.delete()
    except ServiceError:
        raise
    except Exception:
        logger.exception("Part deletion failed (PartID=%s).", part_id)
        raise ServiceError("Something went wrong, please try again.")


def _require_repair_in_progress(repair: Repair) -> None:
    """Refuse unless ``repair`` is In Progress and not yet invoiced.

    The single gate on drawing stock: parts are consumed *while the work is being
    done* (the "Rules" note under T4-INV-05). A job that has not started has
    nothing fitted yet, and a completed job is closed to change because its bill is
    calculated from exactly these lines. An invoice is the stronger of the two
    stops, so it is reported separately - it names the reason the customer would
    care about.
    """
    if repair.Status != REPAIR_IN_PROGRESS:
        raise ValidationError(
            f"Parts can only be recorded while a repair job is In Progress; "
            f"job #{repair.pk} is {repair.Status.lower()}."
        )
    if Invoice.objects.filter(RepairID=repair).exists():
        raise ValidationError(
            f"Repair job #{repair.pk} has already been invoiced, so its parts "
            "usage cannot be changed."
        )


def _validate_usage_quantity(qty_used) -> int:
    """Return a positive whole quantity, or raise.

    Zero is refused rather than accepted as a harmless no-op: a technician
    recording "none of this part" has picked the wrong row, and writing an empty
    line would leave stock untouched while cluttering the job's parts list and
    adding a zero-value row to the invoice.
    """
    try:
        quantity = int(qty_used)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Quantity must be a whole number.") from exc
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")
    return quantity


def _adjust_stock(part_id: int, delta: int) -> None:
    """Move a part's stock by ``delta`` (negative draws, positive restores).

    Written as ``StockQty = StockQty + delta`` in the database rather than saving
    a value read into Python first. That matters when two technicians draw the
    same part at the same moment: with a read-modify-write, both would read the
    old figure and the second update would silently discard the first, leaving
    stock higher than the parts actually fitted. Doing the arithmetic in the
    database makes both decrements count.

    ``UpdatedAt`` is set explicitly because ``auto_now`` only fires on ``save()``,
    never on a queryset ``update()``.
    """
    Parts.objects.filter(pk=part_id).update(
        StockQty=F("StockQty") + delta,
        UpdatedAt=dj_timezone.now(),
    )


def record_parts_usage(
    repair_id: int, part_id: int, qty_used, staff: Staff
) -> tuple[PartsUsage, Parts]:
    """Draw stock against a repair job and record the line (T4-INV-05/06/07).

    Three things happen, all inside one transaction, so stock and usage can never
    disagree (T4-INV-06):

    1. the quantity is checked against the stock actually held;
    2. the part's *current* unit price is copied onto the usage row, so a later
       catalogue change cannot re-price this repair (T4-INV-07);
    3. ``StockQty`` is reduced by the quantity used.

    The part row is locked with ``select_for_update`` for the duration of the
    transaction, so the availability check and the decrement cannot be
    interleaved with another request drawing the same part. If the quantity
    exceeds the stock, nothing at all is written and the stock is unchanged -
    which is what TC4-02 checks.

    Authorisation follows the rule already used for inspections: the assigned
    technician or an Administrator, and nobody else (T4-INV-05). A technician
    posting against someone else's job is refused here, not only hidden in the
    template.
    """
    repair = get_repair(repair_id)
    _require_repair_technician(repair, staff)
    _require_repair_in_progress(repair)

    if staff is None:
        raise AuthorizationError("You must be signed in to record parts usage.")

    part = get_part(part_id)
    if not part.IsActive:
        raise ValidationError(
            f"'{part.PartName}' has been deactivated and cannot be used on new "
            "repairs."
        )

    quantity = _validate_usage_quantity(qty_used)

    try:
        with transaction.atomic():
            # Re-read under lock: the check must be made against the stock as it
            # stands now, not as it stood when this request began.
            locked = Parts.objects.select_for_update().get(pk=part.pk)
            if quantity > locked.StockQty:
                raise ValidationError(
                    f"Only {locked.StockQty} x {locked.PartName} in stock; "
                    f"{quantity} cannot be used on this repair."
                )

            usage = PartsUsage.objects.create(
                RepairID=repair,
                PartID=locked,
                Qty_Used=quantity,
                # Copied rather than referenced: this assignment is what freezes
                # the price against later catalogue changes.
                Unit_Price=locked.UnitPrice,
                RecordedBy=staff,
            )
            _adjust_stock(part.pk, -quantity)
            locked.refresh_from_db()
            return usage, locked
    except ServiceError:
        raise
    except Exception:
        logger.exception(
            "Parts usage recording failed (RepairID=%s, PartID=%s).",
            repair_id,
            part_id,
        )
        raise ServiceError("Something went wrong, please try again.")


def get_parts_usage(usage_id: int) -> PartsUsage:
    """Return one usage line with its part and job, or raise :class:`NotFoundError`."""
    try:
        return PartsUsage.objects.select_related(
            "PartID", "RepairID", "RecordedBy"
        ).get(pk=usage_id)
    except PartsUsage.DoesNotExist as exc:
        raise NotFoundError("That parts usage record does not exist.") from exc


def list_parts_usage(repair_id: int) -> list[PartsUsage]:
    """Return every parts line recorded against a repair job, oldest first."""
    get_repair(repair_id)
    return list(
        PartsUsage.objects.filter(RepairID_id=repair_id)
        .select_related("PartID", "RecordedBy")
        .order_by("UsedAt", "UsageID")
    )


def parts_cost_for_repair(repair_id: int) -> Decimal:
    """Sum of ``Qty_Used x Unit_Price`` over a job's parts lines (FR11).

    Calculated from the *copied* unit prices rather than from the current
    catalogue, so the figure quoted to the customer is the one that was true when
    the parts were fitted. Doing the multiplication in the database keeps the
    invoice preview and the stored invoice in step, since both call this.
    """
    get_repair(repair_id)
    total = PartsUsage.objects.filter(RepairID_id=repair_id).aggregate(
        total=Sum(F("Qty_Used") * F("Unit_Price"), output_field=DecimalField())
    )["total"]
    return _money(total or 0)


def remove_parts_usage(usage_id: int, staff: Staff) -> None:
    """Delete a usage line recorded in error and put the stock back (T4-INV-08).

    Administrator-only, and only while no invoice exists for the job. Once the
    customer has been billed, the lines that bill was built from are the record:
    restoring stock behind them would leave an invoice charging for parts the
    system now says were never fitted.

    The restore runs in the same transaction as the delete, so the two cannot be
    separated - a failure part-way leaves the line in place with stock untouched,
    rather than stock returned for a line that still exists.
    """
    if staff is None or staff.role_name != Role.ADMINISTRATOR:
        raise AuthorizationError(
            "Only an Administrator can remove a recorded parts usage entry."
        )

    usage = get_parts_usage(usage_id)
    repair = usage.RepairID
    if Invoice.objects.filter(RepairID=repair).exists():
        raise ValidationError(
            f"Repair job #{repair.pk} has already been invoiced, so its parts "
            "usage cannot be removed."
        )

    part_name = usage.PartID.PartName
    quantity = usage.Qty_Used
    part_id = usage.PartID_id

    try:
        with transaction.atomic():
            usage.delete()
            _adjust_stock(part_id, quantity)
    except ServiceError:
        raise
    except Exception:
        logger.exception("Parts usage removal failed (UsageID=%s).", usage_id)
        raise ServiceError("Something went wrong, please try again.")

    logger.info(
        "Parts usage %s removed by staff %s: %s x %s returned to stock.",
        usage_id,
        staff.pk,
        quantity,
        part_name,
    )


# ---------------------------------------------------------------------------
# Invoicing (FR11, Timebox 4)
# ---------------------------------------------------------------------------
#   * a job is billed once, and only after it is Completed (T4-INV-20);
#   * every amount is copied at generation, so neither the service catalogue nor
#     the parts catalogue can move an issued bill (T4-INV-21..23);
#   * a bill with no parts is legitimate, but the receptionist is made to confirm
#     it, because "no parts" is more often a forgotten record than a labour-only
#     job (T4-INV-25);
#   * marking Paid stamps ``PaidAt`` once and closes the invoice for editing
#     (T4-INV-26/27).
# ---------------------------------------------------------------------------
def _validate_tax_rate(tax_rate) -> Decimal:
    """Return a tax rate as a percentage, or raise.

    Required, because ``TaxAmount`` is calculated from it (T4-INV-23) and a
    missing rate is listed as a validation error rather than defaulted. Bounded at
    0-100: a negative rate would reduce the customer's bill below the cost of the
    work, and a rate above 100% would charge more in tax than for the repair
    itself. It is read as a percentage, so ``10`` means 10% - the same convention
    the form labels use.
    """
    if tax_rate is None or str(tax_rate).strip() == "":
        raise ValidationError("A tax rate is required.")
    try:
        rate = Decimal(str(tax_rate).strip())
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("The tax rate must be a number.") from exc
    if rate < TAX_RATE_MIN:
        raise ValidationError("The tax rate must not be negative.")
    if rate > TAX_RATE_MAX:
        raise ValidationError(
            f"The tax rate must be {TAX_RATE_MAX}% or less."
        )
    return rate


def calculate_invoice_amounts(labour_cost, parts_cost, tax_rate) -> dict:
    """Return the four amounts of a bill, calculated the same way everywhere.

    Split out so the preview shown before generation and the figures actually
    stored are produced by one function: if the two could differ, the
    receptionist would confirm one amount and issue another.

    Tax is charged on labour plus parts, and every amount is rounded to two
    decimal places half-up, so ``tax + labour + parts == total`` holds on the
    stored row rather than only in the calculation that produced it.
    """
    labour = _money(labour_cost)
    parts = _money(parts_cost)
    rate = _validate_tax_rate(tax_rate)

    subtotal = labour + parts
    tax = _money(subtotal * rate / Decimal("100"))
    return {
        "labour_cost": labour,
        "parts_cost": parts,
        "subtotal": _money(subtotal),
        "tax_rate": rate.quantize(MONEY_QUANTUM),
        "tax_amount": tax,
        "total_amount": _money(subtotal + tax),
    }


def invoice_for_repair(repair_id: int) -> Invoice | None:
    """Return a job's invoice, or ``None`` if it has not been billed yet."""
    get_repair(repair_id)
    return (
        Invoice.objects.select_related(
            "RepairID", "RepairID__AppointmentID", "GeneratedBy"
        )
        .filter(RepairID_id=repair_id)
        .first()
    )


def get_invoice_for_repair(repair_id: int) -> Invoice:
    """Return a job's invoice, or raise if the job has not been billed."""
    invoice = invoice_for_repair(repair_id)
    if invoice is None:
        raise NotFoundError("That repair job has no invoice yet.")
    return invoice


#: Who may raise a bill and take payment. The Receptionist runs the counter where
#: the customer settles up; the Administrator keeps full access so they can
#: supervise and correct. A Technician is deliberately absent - the workflow
#: document forbids them seeing invoices at all, since a bill shows what the
#: workshop charges rather than what the work involved.
INVOICE_STAFF_ROLES = frozenset({Role.RECEPTIONIST, Role.ADMINISTRATOR})

#: Fetched together whenever an invoice is shown, so a bill page renders without
#: a query per related row.
_INVOICE_SELECT_RELATED = (
    "RepairID",
    "RepairID__AppointmentID",
    "RepairID__AppointmentID__ServiceCode",
    "RepairID__TechnicianID__StaffID",
    "GeneratedBy",
)


def list_invoices(payment_status: str = "") -> list[Invoice]:
    """Return invoices, optionally filtered by payment status (T4-INV-28).

    One query serves the unfiltered list and each filter, exactly as the repair
    board does, so a filtered view can never disagree with the full one. An
    unrecognised status is refused rather than silently matching nothing, which
    would read as "no invoices" to a receptionist who mistyped a query string.
    """
    invoices = Invoice.objects.select_related(*_INVOICE_SELECT_RELATED)

    if payment_status:
        valid = {value for value, _label in PAYMENT_STATUSES}
        if payment_status not in valid:
            raise ValidationError(f"'{payment_status}' is not a valid payment status.")
        invoices = invoices.filter(PaymentStatus=payment_status)

    return list(invoices.order_by("-GeneratedAt", "InvoiceID"))


def get_invoice(invoice_id: int) -> Invoice:
    """Return one invoice with its job, or raise :class:`NotFoundError`."""
    try:
        return Invoice.objects.select_related(*_INVOICE_SELECT_RELATED).get(
            pk=invoice_id
        )
    except Invoice.DoesNotExist as exc:
        raise NotFoundError("That invoice does not exist.") from exc


def invoice_preview(repair_id: int, tax_rate) -> dict:
    """Return the amounts an invoice *would* carry, without writing anything.

    Lets the generation page show the receptionist labour, parts, tax and total
    before they commit, so the confirmation step - and the zero-parts warning - is
    about real figures rather than an estimate. It calls the same
    :func:`calculate_invoice_amounts` the write path does, which is what stops the
    preview and the stored invoice from ever disagreeing.
    """
    repair = get_repair(repair_id)
    service = repair.AppointmentID.ServiceCode
    parts_cost = parts_cost_for_repair(repair_id)
    amounts = calculate_invoice_amounts(service.BaseCost, parts_cost, tax_rate)
    amounts["service_name"] = service.ServiceName
    amounts["usage_count"] = repair.parts_usage.count()
    return amounts


def generate_invoice(
    repair_id: int, tax_rate, staff: Staff, confirm_no_parts: bool = False
) -> Invoice:
    """Raise the bill for a completed repair job (T4-INV-20..25).

    Three gates, in the order a receptionist meets them:

    * the job must be **Completed**. A bill for work still on the floor could not
      include parts a technician has yet to fit (T4-INV-20);
    * the job must not already have an invoice. One job is billed once, and a
      second bill would either double-charge the customer or contradict the first
      (T4-INV-20);
    * if no parts were recorded, ``confirm_no_parts`` must be set. A labour-only
      bill is legitimate, so this is a confirmation rather than a refusal - but
      "no parts" is more often a technician who forgot to record them than a
      genuinely parts-free job, so the receptionist has to say they mean it
      (T4-INV-25).

    Every amount is copied onto the row at generation (T4-INV-24): ``LabourCost``
    from the service's ``BaseCost``, ``PartsCost`` from the summed usage lines.
    Both are snapshots, so re-pricing either catalogue afterwards leaves this bill
    exactly as it was issued.

    Billing also finishes the booking the job came in on, since issuing the bill
    is the workshop's last step for that car. That happens in the same
    transaction, so an invoice can never exist for an appointment still marked
    Checked In. It is guarded on the appointment's own status, because a booking
    that was cancelled or never honoured has no completion to record.
    """
    repair = get_repair(repair_id)

    if staff is None or staff.role_name not in INVOICE_STAFF_ROLES:
        raise AuthorizationError(
            "Only a Receptionist or an Administrator can generate an invoice."
        )

    if not repair.is_completed:
        raise ValidationError(
            f"Repair job #{repair.pk} is {repair.Status.lower()}; an invoice can "
            "only be generated once the job is Completed."
        )

    if Invoice.objects.filter(RepairID=repair).exists():
        raise DuplicateError(
            f"Repair job #{repair.pk} has already been invoiced. A repair job can "
            "only have one invoice."
        )

    service = repair.AppointmentID.ServiceCode
    parts_cost = parts_cost_for_repair(repair_id)

    if parts_cost == 0 and not confirm_no_parts:
        raise ValidationError(
            f"No parts were recorded against repair job #{repair.pk}. Confirm to "
            "generate a labour-only invoice."
        )

    amounts = calculate_invoice_amounts(service.BaseCost, parts_cost, tax_rate)
    appointment = repair.AppointmentID

    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                RepairID=repair,
                LabourCost=amounts["labour_cost"],
                PartsCost=amounts["parts_cost"],
                TaxRate=amounts["tax_rate"],
                TaxAmount=amounts["tax_amount"],
                TotalAmount=amounts["total_amount"],
                PaymentStatus=PAYMENT_PENDING,
                GeneratedBy=staff,
            )
            if appointment.Status == APPOINTMENT_CHECKED_IN:
                appointment.Status = APPOINTMENT_COMPLETED
                appointment.save(update_fields=["Status", "UpdatedAt"])
            return invoice
    except IntegrityError:
        # The unique RepairID column is the last line of defence against two
        # receptionists billing the same job at the same moment.
        raise DuplicateError(
            f"Repair job #{repair.pk} has already been invoiced. A repair job can "
            "only have one invoice."
        )
    except ServiceError:
        raise
    except Exception:
        logger.exception("Invoice generation failed (RepairID=%s).", repair_id)
        raise ServiceError("Something went wrong, please try again.")


def mark_invoice_paid(invoice_id: int, staff: Staff) -> Invoice:
    """Record that the customer has settled the bill (T4-INV-26).

    ``PaidAt`` is stamped once, at the moment of payment, which is the evidence of
    when the money was collected. Settling an already-Paid invoice is refused
    rather than quietly ignored, so ``PaidAt`` can never be moved to a later date
    and rewrite when the customer actually paid. That refusal is also what makes a
    Paid invoice final (T4-INV-27): there is no code path back to Pending.
    """
    if staff is None or staff.role_name not in INVOICE_STAFF_ROLES:
        raise AuthorizationError(
            "Only a Receptionist or an Administrator can change an invoice's "
            "payment status."
        )

    invoice = get_invoice(invoice_id)
    if invoice.is_paid:
        raise ValidationError(
            f"Invoice #{invoice.pk} is already marked Paid. A paid invoice is "
            "final and cannot be changed."
        )

    try:
        with transaction.atomic():
            invoice.PaymentStatus = PAYMENT_PAID
            invoice.PaidAt = dj_timezone.now()
            invoice.save(update_fields=["PaymentStatus", "PaidAt"])
            return invoice
    except ServiceError:
        raise
    except Exception:
        logger.exception("Invoice payment failed (InvoiceID=%s).", invoice_id)
        raise ServiceError("Something went wrong, please try again.")


def list_invoices_for_appointments(appointment_ids) -> list[Invoice]:
    """Return the invoices for a set of appointments, newest first.

    Lets the appointment list show which bookings have already been billed without
    a query per row.
    """
    return list(
        Invoice.objects.filter(RepairID__AppointmentID_id__in=list(appointment_ids))
        .select_related("RepairID", "RepairID__AppointmentID")
        .order_by("-GeneratedAt", "InvoiceID")
    )


def list_repairs_eligible_for_invoice() -> list[Repair]:
    """Return completed jobs that have not been billed yet.

    The generation form only offers these, so a receptionist cannot pick a job the
    service layer would then refuse. Both halves of the gate mirror
    :func:`generate_invoice` exactly: the work must be finished, and no invoice
    may already exist for it.
    """
    return list(
        Repair.objects.select_related(*_REPAIR_SELECT_RELATED)
        .filter(Status=REPAIR_COMPLETED, invoice__isnull=True)
        .order_by("CompletedAt", "RepairID")
    )


def total_outstanding_invoices() -> Decimal:
    """Sum of ``TotalAmount`` over invoices still Pending.

    The "money owed to the workshop" figure on the invoice list, aggregated in the
    database rather than in Python so it stays correct as the invoice count grows.
    Only Pending invoices count: a Paid one is no longer outstanding.
    """
    total = Invoice.objects.filter(PaymentStatus=PAYMENT_PENDING).aggregate(
        total=Sum("TotalAmount", output_field=DecimalField())
    )["total"]
    return _money(total or 0)


# ---------------------------------------------------------------------------
# Technician payroll (FR12, Timebox 4)
# ---------------------------------------------------------------------------
#   * a job belongs to the period in which the work *finished*, keyed on
#     Repairs.CompletedAt, not on when the job was raised (T4-PAY-02);
#   * pay is estimated: service Est_Duration (minutes) / 60 x the technician's
#     Hourly_rate. There is no timesheet table, so this is the documented
#     simplification - actual hours are future work;
#   * a technician with no completed jobs in the range shows zero and an
#     information message, never an error (T4-PAY-05);
#   * a technician with no hourly rate is excluded with a validation message
#     rather than silently paid nothing (T4-PAY-06).
# ---------------------------------------------------------------------------
def default_payroll_range() -> tuple[date, date]:
    """Return ``(start, end)`` spanning the current calendar month.

    Payroll is run at month end, so opening the page on the last working day of a
    month should already be showing that month rather than an empty range. The end
    is the last day of the month, worked out from the calendar rather than assumed
    to be the 31st, so February and the 30-day months come out correctly.
    """
    today = dj_timezone.localdate()
    start = today.replace(day=1)
    if start.month == 12:
        first_of_next = start.replace(year=start.year + 1, month=1)
    else:
        first_of_next = start.replace(month=start.month + 1)
    return start, first_of_next - timedelta(days=1)


def _validate_payroll_range(start_date, end_date) -> tuple[date, date]:
    """Return a start and end date with the start not after the end, or raise.

    Both bounds are inclusive - a job completed on the end date belongs to that
    period - which is what lets "this month" cover its last day without the
    administrator adding one to it by hand. A reversed range is refused rather
    than silently returning nothing, since an empty payroll report looks like "no
    work was done" rather than "the dates were typed the wrong way round".
    """
    if start_date is None or end_date is None:
        raise ValidationError("A start date and an end date are both required.")

    try:
        start = (
            start_date
            if isinstance(start_date, date)
            else date.fromisoformat(str(start_date).strip())
        )
        end = (
            end_date
            if isinstance(end_date, date)
            else date.fromisoformat(str(end_date).strip())
        )
    except ValueError as exc:
        raise ValidationError("Enter the period as valid dates.") from exc

    if start > end:
        raise ValidationError("The start date cannot be after the end date.")
    return start, end


def _job_pay(estimated_minutes: int, hourly_rate) -> Decimal:
    """One job's pay: estimated hours x the technician's hourly rate (T4-PAY-03).

    The duration is stored in whole minutes and converted to hours here rather
    than rounded first: a 90-minute service is 1.5 hours, and rounding the minutes
    to the nearest hour before multiplying would pay a 50-minute job the same as a
    70-minute one. The result is rounded to two decimal places like every other
    money figure in this module.
    """
    hours = Decimal(estimated_minutes) / Decimal(60)
    return _money(hours * Decimal(hourly_rate))


def calculate_technician_payroll(
    technician: Technician, start_date: date, end_date: date
) -> dict:
    """Return one technician's pay for the period, with its job breakdown.

    The breakdown is built first and the total summed from it, so the figure shown
    is provably the sum of the lines beneath it (T4-PAY-04) rather than a separate
    calculation that could disagree with them.

    Only jobs with ``CompletedAt`` inside the range count (T4-PAY-02). The filter
    is on the completion timestamp, not on when the job was created, so a job
    started in one month and finished in the next is paid in the second - which is
    the rule the workflow document states explicitly. Jobs completed but missing a
    stamp (only possible on rows that pre-date the column) are skipped rather than
    guessed at.
    """
    jobs = (
        Repair.objects.select_related("AppointmentID__ServiceCode")
        .filter(
            TechnicianID=technician,
            Status=REPAIR_COMPLETED,
            CompletedAt__isnull=False,
            CompletedAt__date__gte=start_date,
            CompletedAt__date__lte=end_date,
        )
        .order_by("CompletedAt", "RepairID")
    )

    lines = []
    total = Decimal("0.00")
    for job in jobs:
        service = job.AppointmentID.ServiceCode
        amount = _job_pay(service.Est_Duration, technician.Hourly_rate)
        total += amount
        lines.append(
            {
                "repair_id": job.pk,
                "customer_name": job.customer_name,
                "service_name": service.ServiceName,
                "estimated_minutes": service.Est_Duration,
                "estimated_hours": _money(
                    Decimal(service.Est_Duration) / Decimal(60)
                ),
                "amount": amount,
                "completed_at": job.CompletedAt,
            }
        )

    return {
        "technician": technician,
        "technician_name": technician.full_name,
        "hourly_rate": technician.Hourly_rate,
        "job_count": len(lines),
        "lines": lines,
        "total": _money(total),
        # Reported rather than raised: no work in a period is a normal thing to
        # see, and the administrator needs the zero on the page to know the run
        # happened - not an error that reads as "the calculation failed".
        "message": (
            f"{technician.full_name} completed no repair jobs in this period."
            if not lines
            else ""
        ),
    }


def calculate_payroll(
    start_date, end_date, technician_id: int | None = None
) -> dict:
    """Run payroll for one technician or for all of them (T4-PAY-01).

    Returns a report dict rather than writing anything: payroll here is a
    calculation the administrator reviews, not a stored payslip, so there is no
    row to roll back and running it twice for the same period is harmless.

    Two kinds of technician are handled specially rather than being paid:

    * one with no hourly rate recorded is **excluded**, with a message naming them
      (T4-PAY-06). Silently showing zero would hide a data gap the administrator
      needs to fix - the rate is missing, not the work;
    * one with a rate but no completed jobs is **included** at zero (T4-PAY-05),
      because that is a genuine result: they were available and did nothing
      billable in the window.

    When ``technician_id`` is given the report covers that technician alone, which
    is what the single-technician selector produces; omitting it covers everyone,
    and the grand total spans whatever was included.
    """
    start, end = _validate_payroll_range(start_date, end_date)

    # The population is every staff account holding the Technician role, not just
    # those that already have a Technicians row. That distinction is what makes
    # T4-PAY-06 observable: Timebox 1 creates the account and its hourly rate as
    # two separate steps, so an account promoted to Technician without a rate yet
    # is exactly "no hourly rate recorded".
    staff_accounts = list(
        Staff.objects.filter(RoleID__RoleName=Role.TECHNICIAN).select_related(
            "RoleID", "technician_record"
        )
    )
    if technician_id is not None:
        staff_accounts = [
            account
            for account in staff_accounts
            if getattr(account, "technician_record", None) is not None
            and account.technician_record.pk == technician_id
        ]
        if not staff_accounts:
            raise NotFoundError("That technician does not exist.")

    entries = []
    excluded = []
    grand_total = Decimal("0.00")
    for account in sorted(
        staff_accounts, key=lambda item: (item.LastName, item.FirstName)
    ):
        technician = getattr(account, "technician_record", None)
        # Hourly_rate is a NOT NULL column with a positive check, so the rate can
        # only be absent when the record itself has not been created. Reported
        # rather than paid zero, because silently showing $0.00 would hide a data
        # gap the administrator needs to fix - the rate is missing, not the work.
        if technician is None or technician.Hourly_rate is None:
            excluded.append(
                {
                    "technician_name": account.full_name,
                    "reason": (
                        f"{account.full_name} has no hourly rate recorded and was "
                        "excluded from this payroll run."
                    ),
                }
            )
            continue
        entry = calculate_technician_payroll(technician, start, end)
        entries.append(entry)
        grand_total += entry["total"]

    return {
        "start_date": start,
        "end_date": end,
        "entries": entries,
        "excluded": excluded,
        "grand_total": _money(grand_total),
        "technician_count": len(entries),
        "job_count": sum(entry["job_count"] for entry in entries),
    }


# ---------------------------------------------------------------------------
# Customer repair status view (FR14, Timebox 4)
# ---------------------------------------------------------------------------
#   * nothing is stored: every stage is derived from data the workshop already
#     recorded, so the customer's view cannot drift out of step with the repair
#     record (T4-STS-02);
#   * the stages are built as an ordered walk, so "which stage has this reached?"
#     is the last one that applies rather than a chain of special cases;
#   * a cancelled booking reports Cancelled instead of a repair stage (T4-STS-09);
#   * staff names, internal notes and itemised costs never reach this page. The
#     customer sees progress, not who is doing it or what it costs - which is also
#     why the invoice is reported as a fact plus its payment status rather than as
#     a breakdown of the bill.
# ---------------------------------------------------------------------------
def _summarise_inspection(description: str) -> str:
    """Return the condition notes trimmed to a customer-safe summary (T4-STS-06).

    Truncated at a word boundary rather than mid-word, and only ever shortened:
    the customer gets the gist of what the technician found without the page
    carrying the full internal record. Whitespace is collapsed first so a
    multi-paragraph note does not leave ragged gaps in the summary.
    """
    text = " ".join((description or "").split())
    if len(text) <= CUSTOMER_INSPECTION_SUMMARY_LENGTH:
        return text

    cut = text[:CUSTOMER_INSPECTION_SUMMARY_LENGTH]
    # Back off to the last space so the summary never ends in half a word. The
    # halfway guard stops a note whose only space sits near the start from being
    # chopped down to almost nothing.
    last_space = cut.rfind(" ")
    if last_space > CUSTOMER_INSPECTION_SUMMARY_LENGTH // 2:
        cut = cut[:last_space]
    return f"{cut.rstrip()}..."


def _derive_customer_stages(appointment: Appointment, repair: Repair | None) -> list:
    """Return the stages this booking has reached, in lifecycle order.

    Each entry is marked reached or not, which is what lets the template draw a
    progress track instead of a single label: the customer sees how far along the
    car is, not only where it stopped. A stage counts as reached only when the
    workshop has actually recorded it, so a repair row with no technician yet does
    not claim "Job assigned".
    """
    checked_in = appointment.CheckedInAt is not None or appointment.Status in (
        APPOINTMENT_CHECKED_IN,
        APPOINTMENT_COMPLETED,
    )

    stages = [
        {
            "key": CUSTOMER_STAGE_BOOKED,
            "label": CUSTOMER_STAGE_BOOKED,
            "reached": True,
            "detail": "Your booking has been received.",
        },
        {
            "key": CUSTOMER_STAGE_CHECKED_IN,
            "label": CUSTOMER_STAGE_CHECKED_IN,
            "reached": checked_in,
            "detail": "Your vehicle is at the workshop.",
        },
    ]

    # No repair row yet: every workshop stage is still ahead, and none of them can
    # be claimed from an appointment alone (T4-STS-03/04).
    if repair is None:
        stages.extend(
            {"key": key, "label": key, "reached": False, "detail": ""}
            for key in (
                CUSTOMER_STAGE_JOB_ASSIGNED,
                CUSTOMER_STAGE_INSPECTED,
                CUSTOMER_STAGE_IN_PROGRESS,
                CUSTOMER_STAGE_COMPLETED,
            )
        )
        return stages

    assigned = repair.TechnicianID_id is not None
    inspected = repair.InspectionID_id is not None
    # A completed job has passed through In Progress, and the lifecycle is one-way,
    # so finishing counts as having been under way.
    in_progress = repair.Status in (REPAIR_IN_PROGRESS, REPAIR_COMPLETED)
    completed = repair.Status == REPAIR_COMPLETED

    stages.extend(
        [
            {
                "key": CUSTOMER_STAGE_JOB_ASSIGNED,
                "label": CUSTOMER_STAGE_JOB_ASSIGNED,
                "reached": assigned,
                # No technician name here: the customer is told work has been
                # allocated, not who it was allocated to.
                "detail": "A technician has been assigned to your vehicle.",
            },
            {
                "key": CUSTOMER_STAGE_INSPECTED,
                "label": CUSTOMER_STAGE_INSPECTED,
                "reached": inspected,
                "detail": (
                    _summarise_inspection(repair.InspectionID.Description)
                    if inspected
                    else ""
                ),
            },
            {
                "key": CUSTOMER_STAGE_IN_PROGRESS,
                "label": CUSTOMER_STAGE_IN_PROGRESS,
                "reached": in_progress,
                "detail": "Work is under way on your vehicle.",
            },
            {
                "key": CUSTOMER_STAGE_COMPLETED,
                "label": CUSTOMER_STAGE_COMPLETED,
                "reached": completed,
                "detail": "The work is finished and your vehicle is ready.",
            },
        ]
    )
    return stages


def get_customer_repair_status(reference_code: str) -> dict:
    """Derive the customer-facing status for a booking reference (FR14).

    Returns a dict the public template renders directly, and never raises for an
    unknown reference: it returns ``{'found': False, 'error': ...}`` instead, so the
    page shows one error message and no status (T4-STS-10) rather than a crash or a
    blank page the customer cannot interpret.

    The reference is trimmed and upper-cased before matching, because it is
    hand-written on a job card or read back over the phone. The alphabet already
    excludes the confusable characters, but a lowercase reading of a code should
    still find the booking.

    The same error is returned for an empty and for an unknown reference, so the
    page cannot be used to probe which codes exist. Nothing in the result exposes
    staff names, internal notes or itemised costs: the invoice is reported as the
    fact that it exists plus whether it is settled (T4-STS-08), which is what a
    customer needs in order to know whether to bring their card.
    """
    code = (reference_code or "").strip().upper()
    if not code:
        return {
            "found": False,
            "error": "Please enter the booking reference you were given.",
        }

    appointment = (
        Appointment.objects.select_related("ServiceCode", "VehicleID")
        .filter(ReferenceCode=code)
        .first()
    )
    if appointment is None:
        return {
            "found": False,
            "error": (
                "We could not find a booking for that reference. Please check it "
                "and try again."
            ),
        }

    repair = (
        Repair.objects.select_related("TechnicianID__StaffID", "InspectionID")
        .filter(AppointmentID=appointment)
        .first()
    )
    # Queried rather than read as ``repair.invoice``: that attribute access raises
    # when a completed job has not been billed yet, which is the normal case.
    invoice = (
        Invoice.objects.filter(RepairID=repair).first() if repair is not None else None
    )

    if appointment.is_cancelled:
        # Cancelled short-circuits the repair walk: whatever stage the job had
        # reached, the booking it came in on is over (T4-STS-09).
        stages = []
        current_stage = CUSTOMER_STAGE_CANCELLED
    else:
        stages = _derive_customer_stages(appointment, repair)
        reached = [stage for stage in stages if stage["reached"]]
        current_stage = reached[-1]["key"] if reached else CUSTOMER_STAGE_BOOKED

    return {
        "found": True,
        "error": "",
        "reference_code": appointment.ReferenceCode,
        "customer_name": appointment.customer_name,
        "service_name": appointment.ServiceCode.ServiceName,
        "appointment_date": appointment.AppointmentDate,
        "appointment_time": appointment.AppointmentTime,
        "appointment_status": appointment.Status,
        "is_cancelled": appointment.is_cancelled,
        "cancel_reason": appointment.CancelReason if appointment.is_cancelled else "",
        # Deliberately empty until a repair row exists: at "Booked" the customer
        # has no vehicle details to see and no job to follow (T4-STS-03).
        "has_repair": repair is not None,
        "stages": stages,
        "current_stage": current_stage,
        "has_invoice": invoice is not None,
        "invoice_payment_status": invoice.PaymentStatus if invoice else "",
        "invoice_is_paid": invoice.is_paid if invoice else False,
        "vehicle_license_plate": (
            appointment.VehicleID.LicensePlate
            if repair is not None and appointment.VehicleID_id is not None
            else ""
        ),
    }
