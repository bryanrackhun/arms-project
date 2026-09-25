"""Forms for the ARMS screens.

Each validation rule is implemented here so the error appears inline on the
form instead of as a server error. The same rules are re-checked in
:mod:`core.services`, so posting straight to a URL without the form cannot
write invalid data either.
"""

from datetime import date, datetime, time

from django import forms
from django.utils import timezone as dj_timezone

from .models import (
    APPOINTMENT_STATUSES,
    APPOINTMENT_TRANSITIONS,
    BAY_AVAILABLE,
    REPAIR_PRIORITIES,
    REPAIR_PRIORITY_ROUTINE,
    REPAIR_STATUSES,
    REPAIR_TRANSITIONS,
    Appointment,
    Inspection,
    Repair,
    Role,
    Service,
    ServiceBay,
    Staff,
    Technician,
    Vehicle,
)
from .services import (
    BOOKABLE_TIMES,
    LICENSE_PLATE_PATTERN,
    MAX_INSPECTION_IMAGES,
    MIN_PASSWORD_LENGTH,
    PHONE_PATTERN,
    VIN_PATTERN,
    list_appointments_eligible_for_repair,
)


class LoginForm(forms.Form):
    """Login page — single email/password form."""

    email = forms.EmailField(
        label="Email",
        max_length=50,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "you@workshop.com",
                "autocomplete": "username",
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "********",
                "autocomplete": "current-password",
            }
        ),
    )

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip()
        if not email:
            raise forms.ValidationError("Email is required.")
        return email

    def clean_password(self) -> str:
        if not self.cleaned_data.get("password"):
            raise forms.ValidationError("Password is required.")
        return self.cleaned_data["password"]


class StaffForm(forms.Form):
    """Staff creation and editing form with a role dropdown.

    ``role`` is a ``ModelChoiceField`` populated from ``Roles``; free-text role
    entry is therefore impossible.
    """

    first_name = forms.CharField(
        label="First name",
        max_length=50,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    last_name = forms.CharField(
        label="Last name",
        max_length=50,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    email = forms.EmailField(
        label="Email",
        max_length=50,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
        help_text="Used as the login identifier. Must be unique.",
    )
    phone = forms.CharField(
        label="Phone",
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "+1 555-0100"}
        ),
        help_text="Optional. Digits, optional +, spaces or hyphens.",
    )
    role = forms.ModelChoiceField(
        label="Role",
        queryset=Role.objects.all(),
        empty_label="— Select a role —",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    password = forms.CharField(
        label="Password",
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        ),
        help_text=f"Minimum {MIN_PASSWORD_LENGTH} characters.",
    )
    confirm_password = forms.CharField(
        label="Confirm password",
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        ),
    )

    def __init__(self, *args, staff: Staff | None = None, **kwargs):
        """``staff`` marks edit mode: the password fields become optional."""
        super().__init__(*args, **kwargs)
        self.staff = staff
        if staff is None:
            self.fields["password"].required = True
            self.fields["confirm_password"].required = True
        else:
            self.fields["password"].help_text = (
                "Leave blank to keep the current password."
            )
            self.fields["confirm_password"].help_text = "Repeat the new password."

    # First name / Last name: non-empty after trimming
    def clean_first_name(self) -> str:
        value = self.cleaned_data["first_name"].strip()
        if not value:
            raise forms.ValidationError("First name is required.")
        return value

    def clean_last_name(self) -> str:
        value = self.cleaned_data["last_name"].strip()
        if not value:
            raise forms.ValidationError("Last name is required.")
        return value

    # Phone: optional but pattern-checked
    def clean_phone(self) -> str:
        value = self.cleaned_data["phone"].strip()
        if value and not PHONE_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid phone number (digits, optional +, spaces or hyphens)."
            )
        return value

    # Email: unique across Staffs
    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip()
        if not email:
            raise forms.ValidationError("Email is required.")
        queryset = Staff.objects.filter(Email__iexact=email)
        if self.staff is not None:
            queryset = queryset.exclude(StaffID=self.staff.StaffID)
        if queryset.exists():
            raise forms.ValidationError("That email address is already in use.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("confirm_password")
        if password or confirm:
            if password and len(password) < MIN_PASSWORD_LENGTH:
                self.add_error(
                    "password",
                    f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.",
                )
            if password != confirm:
                self.add_error("confirm_password", "The two passwords do not match.")
        return cleaned

    @property
    def new_password(self) -> str | None:
        """The new plain-text password, or None when it should stay unchanged."""
        return self.cleaned_data.get("password") or None


class TechnicianForm(forms.Form):
    """Technician information form - linked staff account + hourly rate."""

    staff = forms.ModelChoiceField(
        label="Staff account",
        queryset=Staff.objects.none(),
        empty_label="— Select a Technician-role staff account —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Only active accounts holding the Technician role can be linked.",
    )
    hourly_rate = forms.DecimalField(
        label="Hourly rate",
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "step": "0.01", "min": "0.01"}
        ),
        help_text="Must be greater than zero.",
    )

    def __init__(self, *args, technician=None, **kwargs):
        """Restrict the dropdown to Technician-role staff."""
        super().__init__(*args, **kwargs)
        self.technician = technician
        queryset = Staff.objects.filter(
            IsActive=True, RoleID__RoleName=Role.TECHNICIAN
        ).select_related("RoleID")
        if technician is not None:
            # keep the already-linked account visible; it cannot be changed
            queryset = queryset | Staff.objects.filter(pk=technician.StaffID_id)
            self.fields["staff"].disabled = True
        else:
            queryset = queryset.filter(technician_record__isnull=True)
        self.fields["staff"].queryset = queryset

    def clean_hourly_rate(self):
        """Hourly_rate: required, numeric, greater than zero."""
        rate = self.cleaned_data["hourly_rate"]
        if rate is None:
            raise forms.ValidationError("Hourly rate is required.")
        if rate <= 0:
            raise forms.ValidationError("Hourly rate must be greater than zero.")
        return rate


class ServiceAssignmentForm(forms.Form):
    """Assign a service to a technician.

    Offers only services not yet assigned to this technician, so a duplicate
    pair cannot be submitted through the UI.
    """

    service = forms.ModelChoiceField(
        label="Service to assign",
        queryset=Service.objects.none(),
        empty_label="— Select a service —",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, technician=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.technician = technician
        queryset = Service.objects.all()
        if technician is not None:
            assigned = technician.service_assignments.values_list(
                "ServiceCode_id", flat=True
            )
            queryset = queryset.exclude(Service_code__in=list(assigned))
        self.fields["service"].queryset = queryset


class ServiceForm(forms.Form):
    """Services maintenance form (Administrator only)."""

    service_name = forms.CharField(
        label="Service name",
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    base_cost = forms.DecimalField(
        label="Base cost",
        max_digits=10,
        decimal_places=2,
        min_value=0,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "step": "0.01", "min": "0"}
        ),
        help_text="Must be ≥ 0.",
    )
    est_duration = forms.IntegerField(
        label="Estimated duration (minutes)",
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
    )

    def __init__(self, *args, service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service

    def clean_service_name(self) -> str:
        name = self.cleaned_data["service_name"].strip()
        if not name:
            raise forms.ValidationError("Service name is required.")
        queryset = Service.objects.filter(ServiceName__iexact=name)
        if self.service is not None:
            queryset = queryset.exclude(Service_code=self.service.Service_code)
        if queryset.exists():
            raise forms.ValidationError("That service name is already in use.")
        return name


# ===========================================================================
# Timebox 2 - appointment booking and vehicle check-in forms
# ===========================================================================
#: Statuses that still hold an appointment's slot; reused by the slot check.
_SLOT_HOLDING_STATUSES = [
    status for status, _label in APPOINTMENT_STATUSES
    if status not in ("Cancelled", "No Show")
]


class _SlotFormMixin(forms.Form):
    """Shared date/time field definitions and slot validation.

    Both the public booking form and the receptionist appointment form offer
    the same fixed list of half-hour slots inside opening hours, and reject a
    slot in the past or one already taken by an active appointment. Keeping
    the rules in one mixin means the two screens cannot drift apart.

    Inherits from ``forms.Form`` so Django's metaclass collects the declared
    slot fields here exactly as it does on the subclasses; a plain-object
    mixin would leave ``appointment_date``/``appointment_time`` unregistered
    and therefore absent from ``cleaned_data``.
    """

    #: Set by subclasses when editing, so the row's own slot is not a clash.
    exclude_appointment_id: int | None = None

    appointment_date = forms.DateField(
        label="Preferred date",
        widget=forms.DateInput(
            attrs={
                "class": "form-control",
                "type": "date",
                "min": date.today().isoformat(),
            }
        ),
        input_formats=["%Y-%m-%d"],
        help_text="The day you would like to bring the vehicle in.",
    )
    appointment_time = forms.ChoiceField(
        label="Preferred time",
        choices=[(t.strftime("%H:%M"), t.strftime("%H:%M")) for t in BOOKABLE_TIMES],
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Half-hour slots during opening hours (08:00-17:30).",
    )

    def clean_appointment_date(self):
        value = self.cleaned_data["appointment_date"]
        if value is None:
            raise forms.ValidationError("A preferred date is required.")
        return value

    def clean_appointment_time(self):
        raw = self.cleaned_data.get("appointment_time")
        if not raw:
            raise forms.ValidationError("A preferred time is required.")
        return datetime.strptime(raw, "%H:%M").time()

    def _validate_slot(self, cleaned):
        """Not-in-the-past and no-double-booking checks, shared by subclasses."""
        slot_date = cleaned.get("appointment_date")
        slot_time = cleaned.get("appointment_time")
        if slot_date is None or slot_time is None:
            return cleaned

        slot_dt = datetime.combine(slot_date, slot_time)
        if dj_timezone.is_naive(slot_dt):
            slot_dt = dj_timezone.make_aware(slot_dt)
        if slot_dt <= dj_timezone.localtime():
            self.add_error(
                "appointment_date",
                "The preferred appointment date and time must be in the future.",
            )
            return cleaned

        clash = Appointment.objects.filter(
            AppointmentDate=slot_date,
            AppointmentTime=slot_time,
            Status__in=_SLOT_HOLDING_STATUSES,
        )
        if self.exclude_appointment_id is not None:
            clash = clash.exclude(pk=self.exclude_appointment_id)
        if clash.exists():
            self.add_error(
                "appointment_time",
                "That date and time is already booked. Please choose another slot.",
            )
        return cleaned


class BookingForm(_SlotFormMixin, forms.Form):
    """Public repair-booking form - no login, and deliberately no vehicle data.

    Customers rarely have their VIN to hand when booking online, so the form
    captures only who they are, how to reach them, which service they want and
    when they would like to come in. The vehicle is recorded at check-in.
    """

    first_name = forms.CharField(
        label="First name",
        max_length=50,
        widget=forms.TextInput(
            attrs={"class": "form-control", "autocomplete": "given-name"}
        ),
    )
    last_name = forms.CharField(
        label="Last name",
        max_length=50,
        widget=forms.TextInput(
            attrs={"class": "form-control", "autocomplete": "family-name"}
        ),
    )
    phone = forms.CharField(
        label="Phone",
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "+1 555-0100",
                "autocomplete": "tel",
            }
        ),
        help_text="Provide a phone number or an email address (or both).",
    )
    email = forms.EmailField(
        label="Email",
        max_length=50,
        required=False,
        widget=forms.EmailInput(
            attrs={"class": "form-control", "autocomplete": "email"}
        ),
        help_text="Booking confirmations are sent here.",
    )
    service = forms.ModelChoiceField(
        label="Requested service",
        queryset=Service.objects.all(),
        empty_label="— Select a service —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Choose from the workshop's service catalogue.",
    )
    notes = forms.CharField(
        label="Notes",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": "Anything the workshop should know (optional).",
            }
        ),
    )

    def clean_first_name(self) -> str:
        value = self.cleaned_data["first_name"].strip()
        if not value:
            raise forms.ValidationError("First name is required.")
        return value

    def clean_last_name(self) -> str:
        value = self.cleaned_data["last_name"].strip()
        if not value:
            raise forms.ValidationError("Last name is required.")
        return value

    def clean_phone(self) -> str:
        value = (self.cleaned_data.get("phone") or "").strip()
        if value and not PHONE_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid phone number (digits, optional +, spaces or hyphens)."
            )
        return value

    def clean(self):
        cleaned = super().clean()
        # Contact information: at least one of phone or email is required.
        if not cleaned.get("phone") and not cleaned.get("email"):
            message = (
                "Contact information is required: provide a phone number or an "
                "email address."
            )
            self.add_error("phone", message)
            self.add_error("email", message)
        return self._validate_slot(cleaned)



class AppointmentForm(_SlotFormMixin, forms.Form):
    """Receptionist create/edit form for an appointment.

    Mirrors the booking form but adds the ability to link an already-recorded
    vehicle and to keep the appointment's status. Editing re-validates the slot
    while excluding the appointment's own row, so keeping the same time is not
    treated as a clash with itself.
    """

    first_name = forms.CharField(
        label="Customer first name",
        max_length=50,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    last_name = forms.CharField(
        label="Customer last name",
        max_length=50,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    phone = forms.CharField(
        label="Phone",
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "+1 555-0100"}
        ),
        help_text="Provide a phone number or an email address (or both).",
    )
    email = forms.EmailField(
        label="Email",
        max_length=50,
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    service = forms.ModelChoiceField(
        label="Requested service",
        queryset=Service.objects.all(),
        empty_label="— Select a service —",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    vehicle = forms.ModelChoiceField(
        label="Vehicle",
        queryset=Vehicle.objects.all(),
        required=False,
        empty_label="— Not linked yet —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Optional. Usually linked at check-in rather than when booking.",
    )
    notes = forms.CharField(
        label="Notes",
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
    )

    def __init__(self, *args, appointment: Appointment | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.appointment = appointment
        if appointment is not None:
            self.exclude_appointment_id = appointment.pk

    def clean_first_name(self) -> str:
        value = self.cleaned_data["first_name"].strip()
        if not value:
            raise forms.ValidationError("Customer first name is required.")
        return value

    def clean_last_name(self) -> str:
        value = self.cleaned_data["last_name"].strip()
        if not value:
            raise forms.ValidationError("Customer last name is required.")
        return value

    def clean_phone(self) -> str:
        value = (self.cleaned_data.get("phone") or "").strip()
        if value and not PHONE_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid phone number (digits, optional +, spaces or hyphens)."
            )
        return value

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("phone") and not cleaned.get("email"):
            message = (
                "Contact information is required: provide a phone number or an "
                "email address."
            )
            self.add_error("phone", message)
            self.add_error("email", message)
        return self._validate_slot(cleaned)


class AppointmentStatusForm(forms.Form):
    """Advance an appointment through its allowed lifecycle transitions.

    The status dropdown only lists transitions that are legal from the current
    status (per ``APPOINTMENT_TRANSITIONS``), so an invalid move cannot even be
    submitted through the UI; the service layer re-checks regardless.
    """

    status = forms.ChoiceField(
        label="New status",
        choices=[],
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, appointment: Appointment, **kwargs):
        super().__init__(*args, **kwargs)
        self.appointment = appointment
        allowed = APPOINTMENT_TRANSITIONS.get(appointment.Status, [])
        self.fields["status"].choices = [(s, s) for s in allowed]
        if not allowed:
            self.fields["status"].widget.attrs["disabled"] = True


class CancelAppointmentForm(forms.Form):
    """Cancel an appointment, capturing an optional reason."""

    reason = forms.CharField(
        label="Reason for cancellation",
        max_length=255,
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Why is this appointment being cancelled? (optional)",
            }
        ),
    )

    def clean_reason(self) -> str:
        return (self.cleaned_data.get("reason") or "").strip()


class MatchVehicleForm(forms.Form):
    """Match a recorded vehicle to an appointment ( 'match vehicle' action).

    Offers every vehicle so a receptionist can link one that has just been
    checked in. When the appointment already has a different vehicle, the
    ``confirm_replace`` checkbox must be ticked before the link is swapped.
    """

    vehicle = forms.ModelChoiceField(
        label="Vehicle",
        queryset=Vehicle.objects.all(),
        empty_label="— Select a vehicle —",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    confirm_replace = forms.BooleanField(
        label="Replace the currently linked vehicle",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check"}),
        help_text="Required only if this appointment is already linked to a "
        "different vehicle.",
    )

    def __init__(self, *args, appointment: Appointment | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.appointment = appointment



class VehicleForm(forms.Form):
    """Vehicle detail editing form (receptionist).

    VIN and registration are each optional, but at least one is required so the
    record can be found again at check-in. Format is checked here for inline
    errors; uniqueness against other vehicles is enforced in the service layer
    so the message stays consistent when posting without the form.
    """

    vin = forms.CharField(
        label="VIN",
        max_length=17,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "17-character chassis number",
            }
        ),
        help_text="17 characters; letters and digits (I, O and Q are not used).",
    )
    license_plate = forms.CharField(
        label="Registration number",
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. AB12 CDE"}
        ),
    )
    make = forms.CharField(
        label="Make",
        max_length=40,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    model = forms.CharField(
        label="Model",
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    year = forms.IntegerField(
        label="Year",
        required=False,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "1900",
                "placeholder": "e.g. 2019",
            }
        ),
    )
    colour = forms.CharField(
        label="Colour",
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    image = forms.ImageField(
        label="Photo",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
        help_text="Optional. Captured at check-in; replaces the current photo.",
    )

    def __init__(self, *args, vehicle: Vehicle | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vehicle = vehicle


    def clean_vin(self):
        value = (self.cleaned_data.get("vin") or "").strip().upper()
        if value and not VIN_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid 17-character VIN (letters and digits; I, O and Q "
                "are not allowed)."
            )
        return value or None

    def clean_license_plate(self):
        value = (self.cleaned_data.get("license_plate") or "").strip().upper()
        if value and not LICENSE_PLATE_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid registration number (2-15 letters, digits, "
                "spaces or hyphens)."
            )
        return value or None

    def clean_model(self) -> str:
        value = (self.cleaned_data.get("model") or "").strip()
        if not value:
            raise forms.ValidationError("The vehicle model is required.")
        return value

    def clean_year(self):
        value = self.cleaned_data.get("year")
        if value is None:
            return None
        if value < 1900 or value > date.today().year + 1:
            raise forms.ValidationError(
                f"Enter a four-digit model year between 1900 and "
                f"{date.today().year + 1}."
            )
        return value

    def clean(self):
        cleaned = super().clean()
        vin = cleaned.get("vin")
        plate = cleaned.get("license_plate")
        # When editing, fall back to the stored values for untouched fields.
        if self.vehicle is not None:
            if "vin" not in self.changed_data:
                vin = self.vehicle.VIN
            if "license_plate" not in self.changed_data:
                plate = self.vehicle.LicensePlate
        if not vin and not plate:
            message = "Provide at least a VIN or a registration number."
            self.add_error("vin", message)
            self.add_error("license_plate", message)
        return cleaned



class VehicleCheckInForm(forms.Form):
    """Check-in form: find an existing vehicle by VIN/registration or create one.

    Mirrors :class:`VehicleForm`'s fields but drives find-or-create behaviour
    rather than updating a known record. A photo is captured here because
    check-in is when the vehicle is physically present.
    """

    vin = forms.CharField(
        label="VIN",
        max_length=17,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "17-character chassis number",
            }
        ),
    )
    license_plate = forms.CharField(
        label="Registration number",
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. AB12 CDE"}
        ),
    )
    make = forms.CharField(
        label="Make",
        max_length=40,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    model = forms.CharField(
        label="Model",
        max_length=40,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    year = forms.IntegerField(
        label="Year",
        required=False,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "min": "1900",
                "placeholder": "e.g. 2019",
            }
        ),
    )
    colour = forms.CharField(
        label="Colour",
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    image = forms.ImageField(
        label="Vehicle photo",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
        help_text="Captured at check-in. Stored with the vehicle record.",
    )

    def clean_vin(self):
        value = (self.cleaned_data.get("vin") or "").strip().upper()
        if value and not VIN_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid 17-character VIN (letters and digits; I, O and Q "
                "are not allowed)."
            )
        return value or None

    def clean_license_plate(self):
        value = (self.cleaned_data.get("license_plate") or "").strip().upper()
        if value and not LICENSE_PLATE_PATTERN.match(value):
            raise forms.ValidationError(
                "Enter a valid registration number (2-15 letters, digits, "
                "spaces or hyphens)."
            )
        return value or None

    def clean_model(self) -> str:
        value = (self.cleaned_data.get("model") or "").strip()
        if not value:
            raise forms.ValidationError("The vehicle model is required.")
        return value

    def clean_year(self):
        value = self.cleaned_data.get("year")
        if value is None:
            return None
        if value < 1900 or value > date.today().year + 1:
            raise forms.ValidationError(
                f"Enter a four-digit model year between 1900 and "
                f"{date.today().year + 1}."
            )
        return value

    def clean(self):
        cleaned = super().clean()
        # Missing required vehicle field at check-in -> validation error, and
        # the record is not created. At least one identifier must be present.
        if not cleaned.get("vin") and not cleaned.get("license_plate"):
            message = (
                "Provide at least a VIN or a registration number so the vehicle "
                "can be identified."
            )
            self.add_error("vin", message)
            self.add_error("license_plate", message)
        return cleaned


class VehicleSearchForm(forms.Form):
    """Simple search box: registration number or VIN."""

    q = forms.CharField(
        label="Search",
        required=False,
        max_length=40,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Registration number or VIN",
            }
        ),
    )


class AppointmentSearchForm(forms.Form):
    """Simple search box: customer name or appointment date (YYYY-MM-DD)."""

    q = forms.CharField(
        label="Search",
        required=False,
        max_length=60,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Customer name or date (YYYY-MM-DD)",
            }
        ),
    )


# ===========================================================================
# Timebox 3 - repair jobs, inspections and service bays
# ===========================================================================
class RepairForm(forms.Form):
    """Create a repair job for a checked-in appointment (FR6).

    The appointment dropdown is limited to bookings that are Checked In, have a
    vehicle linked and do not already have a job, so the gate the service layer
    enforces is also visible in the UI: an administrator cannot even select a
    car that has not arrived.

    The technician dropdown is narrowed to those qualified for the chosen
    appointment's service whenever that appointment is known, which is the
    common case when the form is opened from the appointment page. When the
    appointment is picked in the same submission, qualification is checked in
    ``clean`` instead - and re-checked in :mod:`core.services` either way, so
    posting straight to the URL cannot assign an unqualified technician.
    """

    appointment = forms.ModelChoiceField(
        label="Appointment",
        queryset=Appointment.objects.none(),
        empty_label="— Select a checked-in appointment —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Only appointments that are checked in, have a vehicle linked "
        "and do not already have a repair job.",
    )
    technician = forms.ModelChoiceField(
        label="Technician",
        queryset=Technician.objects.none(),
        required=False,
        empty_label="— Assign later —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Must be qualified for the requested service. Optional: the "
        "job can be created first and staffed moments later.",
    )
    bay = forms.ModelChoiceField(
        label="Service bay",
        queryset=ServiceBay.objects.none(),
        required=False,
        empty_label="— Assign later —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Only Available bays are listed. Optional until one is free.",
    )
    priority = forms.ChoiceField(
        label="Repair priority",
        choices=REPAIR_PRIORITIES,
        initial=REPAIR_PRIORITY_ROUTINE,
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Set once, when the job is created.",
    )

    def __init__(self, *args, preset_appointment=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Held under a distinct name because the form also has a field called
        # "appointment"; shadowing it would make templates ambiguous.
        self.preset_appointment = preset_appointment

        eligible = list_appointments_eligible_for_repair()
        # The preset is honoured only when it is genuinely eligible. Checking the
        # *unmodified* list matters: an appointment that is Pending, has no
        # vehicle linked, or already has a job must not enter the dropdown, since
        # this form only ever creates a job and ``clean_appointment`` would have
        # to refuse it afterwards. A hand-edited ``?appointment=`` therefore
        # cannot preselect a choice the service layer rejects.
        preset_is_eligible = (
            preset_appointment is not None
            and preset_appointment.pk in {a.pk for a in eligible}
        )
        self.fields["appointment"].queryset = Appointment.objects.filter(
            pk__in=[a.pk for a in eligible]
        ).select_related("ServiceCode", "VehicleID")

        if preset_is_eligible:
            self.fields["appointment"].initial = preset_appointment

        self._scope_technicians(
            preset_appointment if preset_is_eligible else None
        )
        # An Occupied bay is never offered, so the UI cannot propose a move the
        # service layer would refuse.
        self.fields["bay"].queryset = ServiceBay.objects.filter(Status=BAY_AVAILABLE)

    def _scope_technicians(self, appointment) -> None:
        """Offer only technicians qualified for this appointment's service.

        With no appointment known yet every technician is offered, and the
        qualification rule is then enforced in ``clean``; this is the case where
        the appointment is being chosen in the same submission.
        """
        technicians = Technician.objects.select_related("StaffID")
        if appointment is None:
            self.fields["technician"].queryset = technicians
            return
        self.fields["technician"].queryset = technicians.filter(
            service_assignments__ServiceCode=appointment.ServiceCode
        )

    def clean_appointment(self) -> Appointment:
        appointment = self.cleaned_data["appointment"]
        if appointment is None:
            raise forms.ValidationError("Choose the appointment this job is for.")
        # Re-checked here so the message is inline rather than a page message:
        # a job for a car that has not arrived makes no sense.
        if appointment.Status != "Checked In":
            raise forms.ValidationError(
                "A repair job can only be created once the appointment is "
                f"checked in; this one is {appointment.Status.lower()}."
            )
        if appointment.VehicleID_id is None:
            raise forms.ValidationError(
                "Link a vehicle to the appointment before creating a repair job."
            )
        if Repair.objects.filter(AppointmentID=appointment).exists():
            raise forms.ValidationError("That appointment already has a repair job.")
        return appointment

    def clean_priority(self) -> str:
        priority = (self.cleaned_data.get("priority") or "").strip()
        valid = {value for value, _label in REPAIR_PRIORITIES}
        if priority not in valid:
            raise forms.ValidationError("Choose Urgent or Routine.")
        return priority

    def clean(self):
        cleaned = super().clean()
        appointment = cleaned.get("appointment")
        technician = cleaned.get("technician")
        # The qualification rule, applied inline when the appointment was chosen
        # in this same submission and so could not scope the dropdown.
        if appointment is not None and technician is not None:
            qualified = Technician.objects.filter(
                pk=technician.pk,
                service_assignments__ServiceCode=appointment.ServiceCode,
            ).exists()
            if not qualified:
                self.add_error(
                    "technician",
                    f"{technician.full_name} is not qualified to perform "
                    f"'{appointment.ServiceCode.ServiceName}'.",
                )
        return cleaned


class RepairAssignmentForm(forms.Form):
    """Update a repair job's technician and/or bay assignment (FR6).

    Both fields are optional so the administrator can fill in whichever became
    available after the job was created. The technician list is scoped to those
    qualified for the job's service; the bay list to those that are Available -
    plus, in each case, whatever the job already holds, so leaving one half
    untouched is a valid submission.
    """

    technician = forms.ModelChoiceField(
        label="Technician",
        queryset=Technician.objects.none(),
        required=False,
        empty_label="— Leave unchanged —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Only technicians qualified for this job's service are listed.",
    )
    bay = forms.ModelChoiceField(
        label="Service bay",
        queryset=ServiceBay.objects.none(),
        required=False,
        empty_label="— Leave unchanged —",
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text="Only Available bays (and the bay the job already holds) are listed.",
    )

    def __init__(self, *args, repair: Repair, **kwargs):
        super().__init__(*args, **kwargs)
        self.repair = repair

        service = repair.AppointmentID.ServiceCode
        self.fields["technician"].queryset = Technician.objects.filter(
            service_assignments__ServiceCode=service
        ).select_related("StaffID")

        # The bay the job already holds must stay selectable, even though it is
        # Occupied - otherwise the form could not represent "no change".
        bays = ServiceBay.objects.filter(Status=BAY_AVAILABLE)
        if repair.BayID_id is not None:
            bays = bays | ServiceBay.objects.filter(pk=repair.BayID_id)
        self.fields["bay"].queryset = bays.distinct()

    def clean(self):
        cleaned = super().clean()
        bay = cleaned.get("bay")
        # Re-assigning the bay the job already holds is a no-op, not an error,
        # but taking an Occupied bay from *another* job is refused inline.
        if bay is not None and not bay.is_available:
            if self.repair.BayID_id != bay.pk:
                self.add_error(
                    "bay",
                    f"{bay.BayName} is occupied by another repair job.",
                )
        return cleaned


class RepairStatusForm(forms.Form):
    """Advance a repair job through its allowed progress transitions (FR8).

    Mirrors ``AppointmentStatusForm``: the dropdown only lists transitions legal
    from the current status (per ``REPAIR_TRANSITIONS``), so an out-of-order
    move - Not Started straight to Completed - cannot even be submitted through
    the UI; the service layer re-checks regardless.
    """

    status = forms.ChoiceField(
        label="New status",
        choices=[],
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, repair: Repair, **kwargs):
        super().__init__(*args, **kwargs)
        self.repair = repair
        allowed = REPAIR_TRANSITIONS.get(repair.Status, [])
        self.fields["status"].choices = [(s, s) for s in allowed]
        if not allowed:
            self.fields["status"].widget.attrs["disabled"] = True


class InspectionForm(forms.Form):
    """Record or amend a vehicle condition inspection (FR7).

    Three separate image inputs rather than a multi-file field, because the
    schema holds exactly three columns and the limit is a hard rule: a technician
    sees three slots, so there is no fourth slot to be tempted into filling.

    When amending, only the slots still free are offered, and the count check
    counts photographs already stored plus the ones being added. That way
    uploading four images to an empty inspection is refused outright instead of
    silently keeping three and discarding the fourth.
    """

    description = forms.CharField(
        label="Condition description",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Describe the vehicle's condition, damage and "
                "anything the customer pointed out.",
            }
        ),
    )
    image_1 = forms.ImageField(
        label="Photograph 1",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
    )
    image_2 = forms.ImageField(
        label="Photograph 2",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
    )
    image_3 = forms.ImageField(
        label="Photograph 3",
        required=False,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
        help_text=f"Up to {MAX_INSPECTION_IMAGES} photographs per inspection.",
    )

    #: The field names in the order photographs fill the inspection's columns.
    IMAGE_FIELDS = ("image_1", "image_2", "image_3")

    def __init__(self, *args, repair: Repair, free_slots: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.repair = repair
        # How many of the three columns are still empty. On a new inspection
        # that is all three; when amending one it is whatever is left over.
        if free_slots is None:
            inspection = repair.InspectionID
            free_slots = (
                MAX_INSPECTION_IMAGES - inspection.image_count
                if inspection is not None
                else MAX_INSPECTION_IMAGES
            )
        self.free_slots = free_slots

        # Hide slots that are already full, so the form cannot invite an upload
        # that would have to be rejected.
        for index, field in enumerate(self.IMAGE_FIELDS):
            if index >= free_slots:
                self.fields[field].widget = forms.HiddenInput()

        # Amending an inspection: the stored description is the starting point,
        # and leaving it blank means "keep what is there".
        if repair.InspectionID is not None:
            self.fields["description"].required = False
            self.fields["description"].initial = repair.InspectionID.Description
            self.fields["description"].help_text = (
                "Leave blank to keep the existing description."
            )

    def clean_description(self) -> str:
        return (self.cleaned_data.get("description") or "").strip()

    def uploaded_images(self) -> list:
        """The photographs supplied, in slot order, with blanks dropped."""
        return [
            self.cleaned_data.get(field)
            for field in self.IMAGE_FIELDS
            if self.cleaned_data.get(field)
        ]

    def clean(self):
        cleaned = super().clean()
        uploads = [
            cleaned.get(field) for field in self.IMAGE_FIELDS if cleaned.get(field)
        ]
        if len(uploads) > self.free_slots:
            if self.free_slots >= MAX_INSPECTION_IMAGES:
                message = (
                    f"An inspection can hold at most {MAX_INSPECTION_IMAGES} "
                    f"images; {len(uploads)} were supplied."
                )
            else:
                message = (
                    f"An inspection can hold at most {MAX_INSPECTION_IMAGES} "
                    f"images; only {self.free_slots} more can be added."
                )
            # Attached to the last slot offered so the error sits next to the
            # upload that caused it rather than floating at the top of the form.
            self.add_error(self.IMAGE_FIELDS[max(self.free_slots - 1, 0)], message)
        return cleaned


class ServiceBayForm(forms.Form):
    """Create or rename a service bay (FR9).

    Only the name is editable. ``Status`` is deliberately absent: it is derived
    from whatever repair job the bay holds, and an administrator who could type
    "Available" over an Occupied bay could park two cars in one physical space.
    """

    bay_name = forms.CharField(
        label="Bay name",
        max_length=40,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. Bay 1"}
        ),
        help_text="Must be unique. Availability is set by the repair jobs "
        "assigned to the bay.",
    )

    def clean_bay_name(self) -> str:
        name = (self.cleaned_data.get("bay_name") or "").strip()
        if not name:
            raise forms.ValidationError("A service bay name is required.")
        return name


class RepairFilterForm(forms.Form):
    """Filter the repair job board by status, technician or priority (FR6).

    All three are optional and combine, so an administrator can ask for "urgent
    jobs still outstanding" without leaving the page. Choices come from the
    model constants, so a hand-edited query string cannot produce a confusing
    empty result.
    """

    status = forms.ChoiceField(
        label="Status",
        required=False,
        choices=[("", "All statuses")] + REPAIR_STATUSES,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    priority = forms.ChoiceField(
        label="Priority",
        required=False,
        choices=[("", "All priorities")] + REPAIR_PRIORITIES,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    technician = forms.ModelChoiceField(
        label="Technician",
        required=False,
        queryset=Technician.objects.select_related("StaffID"),
        empty_label="All technicians",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

