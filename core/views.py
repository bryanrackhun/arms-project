"""Views for the ARMS web interface.

Every view is thin: it validates the request, calls into :mod:`core.services`,
and renders a template or redirects. No query or business rule lives here, so
the rules can be tested without an HTTP request.

Screens implemented:
    * login and logout
    * dashboard, with navigation limited to what the role may use
    * staff account listing, creation and editing
    * technician listing and detail form, including service assignment
    * the service catalogue and the role listing
    * public online booking, plus reception appointment management
    * vehicle records: listing, detail, check-in, editing and deletion

Business errors surface as Django messages or inline form errors. Unexpected
database failures show a generic message to the user while the real error is
logged server-side, so a stack trace is never rendered into a page.
"""

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone as dj_timezone
from django.views.decorators.http import require_POST

from . import services
from .decorators import (
    admin_only,
    bay_manager,
    booking_staff,
    inventory_manager,
    invoice_staff,
    parts_viewer,
    payroll_manager,
    repair_manager,
    repair_viewer,
    require_auth,
    require_role,
    vehicle_viewer,
)
from .forms import (
    AppointmentForm,
    AppointmentSearchForm,
    AppointmentStatusForm,
    BookingForm,
    CancelAppointmentForm,
    CustomerPaymentForm,
    InspectionForm,
    InvoiceFilterForm,
    InvoiceGenerateForm,
    LoginForm,
    MatchVehicleForm,
    PartForm,
    PartRestockForm,
    PartsUsageForm,
    PayrollForm,
    RepairAssignmentForm,
    RepairFilterForm,
    RepairForm,
    RepairStatusForm,
    RepairStatusLookupForm,
    ServiceAssignmentForm,
    ServiceBayForm,
    ServiceForm,
    StaffForm,
    TechnicianForm,
    VehicleCheckInForm,
    VehicleForm,
    VehicleSearchForm,
)
from .models import (
    ACTIVE_APPOINTMENT_STATUSES,
    ACTIVE_REPAIR_STATUSES,
    APPOINTMENT_CHECKED_IN,
    APPOINTMENT_PENDING,
    APPOINTMENT_STATUSES,
    APPOINTMENT_TRANSITIONS,
    BAY_AVAILABLE,
    BAY_OCCUPIED,
    CURRENCY_CODE,
    MAX_INSPECTION_IMAGES,
    PAYMENT_PAID,
    PAYMENT_PENDING,
    PAYMENT_STATUSES,
    REPAIR_COMPLETED,
    REPAIR_IN_PROGRESS,
    REPAIR_NOT_STARTED,
    REPAIR_STATUSES,
    REPAIR_TRANSITIONS,
    Appointment,
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
from .services import (
    AuthorizationError,
    DuplicateError,
    NotFoundError,
    ServiceError,
    ValidationError,
)


def _service_error(request, error: ServiceError) -> None:
    """Translate a business error into a user-facing message."""
    level = messages.WARNING if isinstance(error, ValidationError) else messages.ERROR
    messages.add_message(request, level, str(error))


# ---------------------------------------------------------------------------
# Public landing page
# ---------------------------------------------------------------------------
def home_view(request):
    """Public entry point at ``/``.

    Two audiences land here and each needs a different door: a customer wants to
    book without an account, while staff want the login form. Rather than send a
    visitor to the staff login by default - which hides the booking service
    entirely - this page offers both. Signed-in staff go straight to the
    dashboard, because the landing page would only be a detour for them.

    The landing page also carries the repair-progress lookup, so a returning
    customer who holds a booking reference does not have to find a second screen.
    It runs the same service call as the dedicated status page and renders the
    same shared partial, so the two can never disagree. Nothing extra is exposed
    here: the lookup asks only for the reference, exactly as the dedicated page
    does.
    """
    if request.session.get(services.SESSION_KEY_STAFF_ID):
        return redirect("core:dashboard")

    status_form = RepairStatusLookupForm()
    status = None

    # A booking confirmation can hand the reference over in the query string;
    # when it does, the lookup runs straight away rather than making the
    # customer retype a code they have just been given.
    preset = request.GET.get("ref", "").strip()
    if preset and request.method == "GET":
        status_form = RepairStatusLookupForm(initial={"reference_code": preset})
        status = services.get_customer_repair_status(preset)

    if request.method == "POST":
        status_form = RepairStatusLookupForm(request.POST)
        if status_form.is_valid():
            status = services.get_customer_repair_status(
                status_form.cleaned_data["reference_code"]
            )
        # An invalid form falls through with status None: the inline field error
        # (empty reference) is the explanation, and no result block is rendered.

    return render(
        request,
        "core/home.html",
        {
            "identity": None,
            # Shown so a customer can see what the workshop actually offers
            # before opening the booking form.
            "services": Service.objects.order_by("ServiceName"),
            "service_total": Service.objects.count(),
            # "Popular" is measured by bookings; ties (and a new workshop with
            # no bookings yet) fall back to alphabetical order.
            "popular_services": Service.objects.annotate(
                booking_count=Count("appointments")
            ).order_by("-booking_count", "ServiceName")[:4],
            "status_form": status_form,
            "status": status,
        },
    )


# ---------------------------------------------------------------------------
# Public static content pages (about / contact / privacy / terms)
# ---------------------------------------------------------------------------
#: Static pages served from the public site. Each entry maps a URL name to its
#: template, so one view handles every page and a new page is a two-line change
#: (a template plus an entry here and in ``urls.py``).
STATIC_PAGES = {
    "about": "core/about.html",
    "contact": "core/contact.html",
    "privacy": "core/privacy.html",
    "terms": "core/terms.html",
}


def static_page_view(request, page):
    """Render one of the public static content pages.

    The pages carry no database content - they are workshop copy - so a single
    view with a small name-to-template map is enough. An unknown ``page``
    resolves to no template and raises ``Http404`` rather than guessing, so the
    URL space stays honest. Signed-in staff can still read these pages; unlike
    the landing page they are not a detour, because they hold no dashboard
    content.
    """
    template = STATIC_PAGES.get(page)
    if template is None:
        raise Http404("No such page")
    return render(request, template, {"identity": None})


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------
def login_view(request):
    """Render the login form; create the session on success."""
    if request.session.get(services.SESSION_KEY_STAFF_ID):
        return redirect("core:dashboard")

    form = LoginForm()
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            try:
                staff = services.login(
                    request.session,
                    form.cleaned_data["email"],
                    form.cleaned_data["password"],
                )
            except ServiceError as error:
                # generic invalid-credentials message; no session created.
                messages.error(request, str(error))
            else:
                messages.success(request, f"Welcome back, {staff.FirstName}.")
                next_url = request.GET.get("next")
                if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect("core:dashboard")
        else:
            messages.error(request, "Please correct the errors below.")

    return render(request, "core/login.html", {"form": form})


@require_POST
def logout_view(request):
    """Invalidate the session immediately and return to login."""
    services.logout(request.session)
    messages.success(request, "You have been logged out.")
    return redirect("core:login")


def access_denied_view(request):
    """Access-denied page for authenticated users with the wrong role."""
    identity = _identity(request)
    return render(request, "core/access_denied.html", {"identity": identity}, status=403)


def _identity(request) -> dict | None:
    """Session identity for templates; enriched with the display name."""
    try:
        identity = services.verify_session(request.session)
    except ServiceError:
        return None
    identity["staff_name"] = Staff.objects.filter(
        pk=identity["staff_id"]
    ).values_list("FirstName", "LastName").first()
    first, last = identity["staff_name"] or ("", "")
    identity["staff_name"] = f"{first} {last}".strip()
    return identity


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------
@require_auth
def dashboard_view(request):
    """Landing page after login, with navigation matched to the user's role.

    Administrators see management links and the staff/technician/service
    counts. Every other role sees only their own profile, so the page never
    offers a link that would immediately bounce to the access-denied screen.

    Modules from later phases are listed but disabled, because their tables do
    not exist yet.
    """
    identity = _identity(request)
    role_name = identity["role_name"] if identity else ""

    is_booking_staff = role_name in (Role.RECEPTIONIST, Role.ADMINISTRATOR)
    upcoming_appointments = Appointment.objects.filter(
        Status__in=ACTIVE_APPOINTMENT_STATUSES,
        AppointmentDate__gte=dj_timezone.localdate(),
    )

    context = {
        "identity": identity,
        "role_name": role_name,
        "is_admin": role_name == Role.ADMINISTRATOR,
        "is_booking_staff": is_booking_staff,
        "staff_total": Staff.objects.count(),
        "staff_active": Staff.objects.filter(IsActive=True).count(),
        "technician_total": Technician.objects.count(),
        "service_total": Service.objects.count(),
        "assignment_total": TechnicianService.objects.count(),
        # Timebox 2 counters for the booking desk (Receptionist, Administrator).
        "appointment_upcoming": upcoming_appointments.count(),
        "appointment_today": upcoming_appointments.filter(
            AppointmentDate=dj_timezone.localdate()
        ).count(),
        # Requests waiting to be accepted. Surfaced on the landing page so
        # reception sees the queue as soon as they sign in.
        "appointment_pending": Appointment.objects.filter(
            Status=APPOINTMENT_PENDING
        ).count(),
        "vehicle_total": Vehicle.objects.count(),
        # Timebox 3 counters: the job board and bay availability at a glance.
        "repair_total": Repair.objects.count(),
        "repair_active": Repair.objects.filter(
            Status__in=ACTIVE_REPAIR_STATUSES
        ).count(),
        "repair_completed": Repair.objects.filter(Status=REPAIR_COMPLETED).count(),
        "bay_total": ServiceBay.objects.count(),
        "bay_available": ServiceBay.objects.filter(Status=BAY_AVAILABLE).count(),
        # Timebox 4 counters: stock, billing and the money still owed.
        "part_total": Parts.objects.filter(IsActive=True).count(),
        # Surfaced for every role that can open the parts list, so a technician
        # sees the shortage before starting a job that needs the part.
        "part_low_stock": services.count_low_stock_parts(),
        "invoice_pending": Invoice.objects.filter(
            PaymentStatus=PAYMENT_PENDING
        ).count(),
        "invoice_paid": Invoice.objects.filter(PaymentStatus=PAYMENT_PAID).count(),
        # Completed jobs waiting to be billed: the receptionist's work queue.
        "repair_to_invoice": len(services.list_repairs_eligible_for_invoice()),
        "role_counts": (
            Staff.objects.values("RoleID__RoleName")
            .annotate(total=Count("StaffID"))
            .order_by("RoleID__RoleName")
        ),
        # Everything with a requirement and a table has a screen now: Timebox 4
        # added the parts catalogue, invoices and payroll. What is left is the
        # work that has no screen at all - FR13 (multi-branch) is out of scope,
        # and reporting has no requirement in Chapter 5, so it would need an
        # FR15 before it could be built.
        "future_modules": [
            ("Multi-branch", "Out of scope"),
            ("Reports", "Needs FR15"),
        ],
    }
    return render(request, "core/dashboard.html", context)


@require_auth
def profile_view(request):
    """'view own staff/technician profile' - allowed for every role."""
    identity = _identity(request)
    if identity is None:
        return redirect("core:login")
    try:
        staff = services.get_staff(identity["staff_id"])
    except NotFoundError:
        messages.error(request, "Your account could not be found. Please log in again.")
        services.logout(request.session)
        return redirect("core:login")

    technician = (
        Technician.objects.filter(StaffID=staff).select_related("StaffID").first()
    )
    assigned_services = (
        services.list_services_for_technician(technician.TechnicianID)
        if technician
        else []
    )
    return render(
        request,
        "core/profile.html",
        {
            "identity": identity,
            "staff": staff,
            "technician": technician,
            "assigned_services": assigned_services,
        },
    )


# ---------------------------------------------------------------------------
# Staff listing, creation and editing (Administrator only)
# ---------------------------------------------------------------------------
@require_auth
@admin_only
def staff_list_view(request):
    """Searchable table of staff accounts: name, role, status, actions."""
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")

    staff_rows = services.search_staff(query) if query else services.list_staff()
    if status == "active":
        staff_rows = [s for s in staff_rows if s.IsActive]
    elif status == "inactive":
        staff_rows = [s for s in staff_rows if not s.IsActive]

    return render(
        request,
        "core/staff_list.html",
        {
            "identity": _identity(request),
            "staff_rows": staff_rows,
            "query": query,
            "status_filter": status,
            "result_count": len(staff_rows),
        },
    )


@require_auth
@admin_only
def staff_create_view(request):
    """'Add staff' form with the role dropdown."""
    form = StaffForm()
    if request.method == "POST":
        form = StaffForm(request.POST)
        if form.is_valid():
            try:
                staff = services.create_staff(
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    email=form.cleaned_data["email"],
                    password=form.cleaned_data["password"],
                    role_id=form.cleaned_data["role"].RoleID,
                    phone=form.cleaned_data.get("phone", ""),
                )
            except DuplicateError as error:
                # duplicate email -> blocked with a clear message
                form.add_error("email", str(error))
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Staff account created for {staff.full_name}.")
                return redirect("core:staff_list")

    return render(
        request,
        "core/staff_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "create",
            "roles": services.list_roles(),
        },
    )


@require_auth
@admin_only
def staff_edit_view(request, staff_id: int):
    """Edit form reusing the same fields as creation."""
    try:
        staff = services.get_staff(staff_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:staff_list")

    if request.method == "POST":
        form = StaffForm(request.POST, staff=staff)
        if form.is_valid():
            try:
                services.update_staff(
                    staff_id=staff.StaffID,
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    email=form.cleaned_data["email"],
                    role_id=form.cleaned_data["role"].RoleID,
                    phone=form.cleaned_data.get("phone", ""),
                    new_password=form.new_password,
                )
            except DuplicateError as error:
                form.add_error("email", str(error))
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Staff account {staff.full_name} updated.")
                return redirect("core:staff_list")
    else:
        form = StaffForm(
            initial={
                "first_name": staff.FirstName,
                "last_name": staff.LastName,
                "email": staff.Email,
                "phone": staff.Phone,
                "role": staff.RoleID_id,
            },
            staff=staff,
        )

    return render(
        request,
        "core/staff_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "staff": staff,
            "mode": "edit",
            "roles": services.list_roles(),
        },
    )

@require_auth
@admin_only
def staff_deactivate_view(request, staff_id: int):
    """Deactivate (IsActive = 0) rather than hard-delete."""
    if request.method != "POST":
        return redirect("core:staff_list")
    try:
        staff = services.get_staff(staff_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:staff_list")

    identity = _identity(request)
    if identity and identity["staff_id"] == staff.StaffID:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("core:staff_list")

    try:
        services.deactivate_staff(staff.StaffID)
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(
            request, f"{staff.full_name} was deactivated and can no longer log in."
        )
    return redirect("core:staff_list")


@require_auth
@admin_only
def staff_reactivate_view(request, staff_id: int):
    """Re-activate a previously deactivated account."""
    if request.method != "POST":
        return redirect("core:staff_list")
    try:
        staff = services.get_staff(staff_id)
        staff.IsActive = True
        staff.save(update_fields=["IsActive"])
    except NotFoundError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"{staff.full_name} was re-activated.")
    return redirect("core:staff_list")


@require_auth
@admin_only
def role_list_view(request):
    """Read-only listing of the available user roles."""
    roles = Role.objects.annotate(
        staff_count=Count("staff_accounts")
    ).order_by("RoleID")
    return render(
        request,
        "core/role_list.html",
        {"identity": _identity(request), "roles": roles},
    )


# ---------------------------------------------------------------------------
# Technician listing and information form (Administrator only)
# ---------------------------------------------------------------------------
@require_auth
@admin_only
def technician_list_view(request):
    """Table of technicians: linked staff name, hourly rate, service count."""
    technicians = (
        Technician.objects.select_related("StaffID")
        .annotate(service_count=Count("service_assignments"))
        .order_by("StaffID__LastName")
    )
    # Staff accounts holding the Technician role without a technician record yet.
    available_staff = Staff.objects.filter(
        IsActive=True,
        RoleID__RoleName=Role.TECHNICIAN,
        technician_record__isnull=True,
    ).order_by("LastName")

    return render(
        request,
        "core/technician_list.html",
        {
            "identity": _identity(request),
            "technicians": technicians,
            "available_staff": available_staff,
            "available_count": available_staff.count(),
        },
    )


@require_auth
@admin_only
def technician_create_view(request):
    """Technician information form: linked staff account + hourly rate."""
    form = TechnicianForm()
    if request.method == "POST":
        form = TechnicianForm(request.POST)
        if form.is_valid():
            try:
                technician = services.create_technician(
                    staff_id=form.cleaned_data["staff"].StaffID,
                    hourly_rate=form.cleaned_data["hourly_rate"],
                )
            except DuplicateError as error:
                form.add_error("staff", str(error))
            except ValidationError as error:
                _service_error(request, error)
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    f"Technician record created for {technician.full_name}.",
                )
                return redirect("core:technician_list")

    return render(
        request,
        "core/technician_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "create",
            "available_services": services.list_services(),
        },
    )


@require_auth
@admin_only
def technician_edit_view(request, technician_id: int):
    """Edit a technician record and manage its service assignments."""
    try:
        technician = services.get_technician(technician_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:technician_list")

    if request.method == "POST":
        # `staff` is a disabled field on edit, so Django reads it from initial
        # rather than the submitted data - it must be supplied here too.
        form = TechnicianForm(
            request.POST,
            initial={"staff": technician.StaffID_id},
            technician=technician,
        )
        if form.is_valid():
            try:
                services.update_technician(
                    technician.TechnicianID, form.cleaned_data["hourly_rate"]
                )
            except ValidationError as error:
                _service_error(request, error)
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request, f"Technician {technician.full_name} updated."
                )
                return redirect("core:technician_list")
    else:
        form = TechnicianForm(
            initial={
                "staff": technician.StaffID_id,
                "hourly_rate": technician.Hourly_rate,
            },
            technician=technician,
        )

    return render(
        request,
        "core/technician_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "edit",
            "technician": technician,
            "assigned_services": services.list_services_for_technician(
                technician.TechnicianID
            ),
            "assignment_form": ServiceAssignmentForm(technician=technician),
            "all_services": services.list_services(),
        },
    )


# ---------------------------------------------------------------------------
# Technician-service assignment (Administrator only)
# ---------------------------------------------------------------------------
@require_auth
@admin_only
def technician_assign_service_view(request, technician_id: int):
    """Assign a service to a technician."""
    if request.method != "POST":
        return redirect("core:technician_edit", technician_id=technician_id)
    try:
        technician = services.get_technician(technician_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:technician_list")

    form = ServiceAssignmentForm(request.POST, technician=technician)
    if form.is_valid():
        try:
            services.assign_service(
                technician.TechnicianID, form.cleaned_data["service"].Service_code
            )
        except DuplicateError as error:
            # duplicate assignment -> blocked with a clear message
            messages.warning(request, str(error))
        except ServiceError as error:
            _service_error(request, error)
        else:
            messages.success(
                request,
                f"Service assigned to {technician.full_name}.",
            )
    else:
        messages.error(request, "Please select a service to assign.")
    return redirect("core:technician_edit", technician_id=technician_id)


@require_auth
@admin_only
def technician_remove_service_view(request, technician_id: int, service_code: int):
    """Remove a technician-service assignment."""
    if request.method != "POST":
        return redirect("core:technician_edit", technician_id=technician_id)
    try:
        technician = services.get_technician(technician_id)
        services.remove_assignment(technician.TechnicianID, service_code)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(request, "Service assignment removed.")
    return redirect("core:technician_edit", technician_id=technician_id)


@require_auth
@admin_only
def technician_deactivate_view(request, technician_id: int):
    """Deactivate a technician via its linked staff account."""
    if request.method != "POST":
        return redirect("core:technician_list")
    try:
        technician = services.deactivate_technician(technician_id)
    except NotFoundError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request, f"Technician {technician.full_name} was deactivated."
        )
    return redirect("core:technician_list")


# ---------------------------------------------------------------------------
# Services (Administrator only)
# ---------------------------------------------------------------------------
@require_auth
@admin_only
def service_list_view(request):
    """List all services with the count of qualified technicians."""
    services_qs = (
        Service.objects.annotate(technician_count=Count("qualified_technicians"))
        .order_by("ServiceName")
    )
    return render(
        request,
        "core/service_list.html",
        {"identity": _identity(request), "services": services_qs},
    )


@require_auth
@admin_only
def service_create_view(request):
    """Create a service (needed so technicians can be qualified for it)."""
    form = ServiceForm()
    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            try:
                service = services.create_service(
                    service_name=form.cleaned_data["service_name"],
                    base_cost=form.cleaned_data["base_cost"],
                    est_duration=form.cleaned_data["est_duration"],
                )
            except DuplicateError as error:
                form.add_error("service_name", str(error))
            except ValidationError as error:
                _service_error(request, error)
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Service '{service.ServiceName}' created.")
                return redirect("core:service_list")
    return render(
        request,
        "core/service_form.html",
        {"identity": _identity(request), "form": form, "mode": "create"},
    )


@require_auth
@admin_only
def service_edit_view(request, service_code: int):
    """Edit a catalogue service's name, base cost and estimated duration.

    The service is fetched up front so an unknown code becomes a message back on
    the list rather than a 404 on a screen the administrator reached from that
    same list. Only an Administrator may get here - the same guard as every
    other catalogue screen - because these values feed every later quote.
    """
    try:
        service = services.get_service(service_code)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:service_list")

    if request.method == "POST":
        # `service` is passed so the duplicate-name check ignores this row's own
        # name; without it, re-saving the form unchanged would look like a clash.
        form = ServiceForm(request.POST, service=service)
        if form.is_valid():
            try:
                service = services.update_service(
                    service.Service_code,
                    service_name=form.cleaned_data["service_name"],
                    base_cost=form.cleaned_data["base_cost"],
                    est_duration=form.cleaned_data["est_duration"],
                )
            except DuplicateError as error:
                form.add_error("service_name", str(error))
            except ValidationError as error:
                _service_error(request, error)
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Service '{service.ServiceName}' updated.")
                return redirect("core:service_list")
    else:
        form = ServiceForm(
            initial={
                "service_name": service.ServiceName,
                "base_cost": service.BaseCost,
                "est_duration": service.Est_Duration,
            },
            service=service,
        )

    return render(
        request,
        "core/service_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "edit",
            "service": service,
            # Shown so the administrator is warned before a price change that the
            # service is already in use; appointments keep the price they were
            # booked at, so edits are never retroactive.
            "appointment_count": service.appointments.count(),
            "technician_count": service.qualified_technicians.count(),
        },
    )



# ---------------------------------------------------------------------------
# Public online booking (Timebox 2 - no login required)
# ---------------------------------------------------------------------------
def booking_view(request):
    """Book a repair appointment online: captures the service request only.

    Deliberately public - a customer must not need an account to book. The form
    never collects vehicle data; the vehicle is recorded and linked at check-in
    once the customer actually arrives with it.
    """
    if request.session.get(services.SESSION_KEY_STAFF_ID):
        # Staff who are logged in manage bookings through the reception screen.
        return redirect("core:appointment_list")

    form = BookingForm()
    # A "book this service" link on the landing page names a service in the query
    # string, so the customer lands on the booking form with that service already
    # chosen. Only a service that actually exists is applied, and only on a fresh
    # GET: a POST carries its own selection and must not be overwritten.
    if request.method == "GET":
        preset_service = request.GET.get("service", "").strip()
        if preset_service:
            form = BookingForm(
                initial={"service": Service.objects.filter(
                    Service_code=preset_service
                ).first()}
            )
    if request.method == "POST":
        form = BookingForm(request.POST)
        if form.is_valid():
            try:
                appointment = services.book_appointment(
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    service_code=form.cleaned_data["service"].Service_code,
                    appointment_date=form.cleaned_data["appointment_date"],
                    appointment_time=form.cleaned_data["appointment_time"],
                    phone=form.cleaned_data.get("phone", ""),
                    email=form.cleaned_data.get("email", ""),
                    notes=form.cleaned_data.get("notes", ""),
                    car_model=form.cleaned_data["car_model"],
                    license_plate=form.cleaned_data["license_plate"],
                )
            except ServiceError as error:
                # Business rules are re-checked here, so a race for the last
                # free slot surfaces as an inline message rather than a 500.
                _service_error(request, error)
            else:
                # The hand-off to the confirmation page carries the reference
                # code, not the sequential AppointmentID, so the page finds the
                # booking by the same key a customer will later quote back.
                request.session["booking_reference"] = appointment.ReferenceCode
                return redirect("core:booking_success")
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/booking.html",
        {"form": form, "services": Service.objects.order_by("ServiceName")},
    )


def booking_success_view(request):
    """Confirmation page shown after a booking is stored."""
    reference_code = request.session.pop("booking_reference", None)
    appointment = None
    if reference_code:
        try:
            appointment = services.get_appointment_by_reference(reference_code)
        except NotFoundError:
            appointment = None
    if appointment is None:
        # Someone landed here without booking; send them back to the form.
        return redirect("core:booking")

    return render(request, "core/booking_success.html", {"appointment": appointment})


# ---------------------------------------------------------------------------
# Appointment management (Receptionist, Administrator)
# ---------------------------------------------------------------------------
@require_auth
@booking_staff
def appointment_list_view(request):
    """Searchable diary of appointments, soonest first, with a status filter."""
    search_form = AppointmentSearchForm(request.GET or None)
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()

    appointments = (
        services.search_appointments(query) if query else services.list_appointments()
    )
    if status:
        appointments = [a for a in appointments if a.Status == status]

    return render(
        request,
        "core/appointment_list.html",
        {
            "identity": _identity(request),
            "appointments": appointments,
            "search_form": search_form,
            "query": query,
            "status_filter": status,
            "status_choices": APPOINTMENT_STATUSES,
            # The review queue: every request nobody has accepted yet, whether a
            # customer made it online or reception took it at the counter.
            "pending_appointments": services.list_pending_appointments(),
        },
    )


@require_auth
@booking_staff
def appointment_create_view(request):
    """Receptionist creates an appointment on a customer's behalf."""
    form = AppointmentForm()
    if request.method == "POST":
        form = AppointmentForm(request.POST)
        if form.is_valid():
            try:
                appointment = services.book_appointment(
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    service_code=form.cleaned_data["service"].Service_code,
                    appointment_date=form.cleaned_data["appointment_date"],
                    appointment_time=form.cleaned_data["appointment_time"],
                    phone=form.cleaned_data.get("phone", ""),
                    email=form.cleaned_data.get("email", ""),
                    notes=form.cleaned_data.get("notes", ""),
                    car_model=form.cleaned_data["car_model"],
                    license_plate=form.cleaned_data["license_plate"],
                )
                # A vehicle may be linked straight away when it is known.
                vehicle = form.cleaned_data.get("vehicle")
                if vehicle is not None:
                    appointment = services.match_vehicle_to_appointment(
                        appointment.pk, vehicle.VehicleID
                    )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    f"Appointment {appointment.pk} booked for "
                    f"{appointment.customer_name}. Reference code "
                    f"{appointment.ReferenceCode} - give this to the customer. "
                    f"It is pending - accept it to confirm the slot.",
                )
                return redirect("core:appointment_detail", appointment_id=appointment.pk)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/appointment_form.html",
        {"identity": _identity(request), "form": form, "mode": "create"},
    )



@require_auth
@booking_staff
def appointment_detail_view(request, appointment_id: int):
    """One appointment with its lifecycle actions available inline.

    The status dropdown is limited to transitions that are legal from the
    current status, and cancel / match-vehicle actions are offered only when
    the appointment's state allows them, so the screen never invites an
    invalid move.
    """
    try:
        appointment = services.get_appointment(appointment_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:appointment_list")

    allowed = APPOINTMENT_TRANSITIONS.get(appointment.Status, [])
    status_form = AppointmentStatusForm(appointment=appointment)
    cancel_form = CancelAppointmentForm()
    match_form = MatchVehicleForm(appointment=appointment)

    # Cancellation is blocked once checked in / completed / terminal.
    can_cancel = appointment.Status not in services.TERMINAL_APPOINTMENT_STATUSES and (
        appointment.Status
        not in (services.APPOINTMENT_CHECKED_IN, services.APPOINTMENT_COMPLETED)
    )
    # Matching is blocked on terminal (cancelled / no-show) appointments.
    can_match = appointment.Status not in services.TERMINAL_APPOINTMENT_STATUSES

    # Timebox 3: the repair job raised for this appointment, if any, plus whether
    # one can be started. A job needs Checked In status *and* a linked vehicle,
    # which is exactly the gate create_repair enforces, so the link is only
    # offered when it would succeed.
    identity = _identity(request)
    repair = services.get_repair_for_appointment(appointment.pk)
    can_create_repair = (
        identity is not None
        and identity["role_name"] == Role.ADMINISTRATOR
        and appointment.Status == APPOINTMENT_CHECKED_IN
        and appointment.VehicleID_id is not None
        and repair is None
    )

    return render(
        request,
        "core/appointment_detail.html",
        {
            "identity": identity,
            "appointment": appointment,
            "status_form": status_form,
            "cancel_form": cancel_form,
            "match_form": match_form,
            "allowed_transitions": allowed,
            # Accepting is the decision pending requests wait on; it gets its own
            # button instead of sitting inside the status dropdown.
            "can_accept": appointment.is_pending,
            "can_cancel": can_cancel,
            "can_match": can_match,
            # Template hides the Edit button on finished (terminal) records.
            "terminal_statuses": services.TERMINAL_APPOINTMENT_STATUSES,
            "repair": repair,
            "can_create_repair": can_create_repair,
        },
    )


@require_auth
@booking_staff
def appointment_edit_view(request, appointment_id: int):
    """Edit an appointment's booking details (reschedule, change contact)."""
    try:
        appointment = services.get_appointment(appointment_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:appointment_list")

    if appointment.Status in services.TERMINAL_APPOINTMENT_STATUSES:
        messages.warning(
            request,
            f"A {appointment.Status.lower()} appointment can no longer be edited.",
        )
        return redirect("core:appointment_detail", appointment_id=appointment_id)

    initial = {
        "first_name": appointment.CustomerFirstName,
        "last_name": appointment.CustomerLastName,
        "phone": appointment.CustomerPhone,
        "email": appointment.CustomerEmail,
        "service": appointment.ServiceCode,
        "vehicle": appointment.VehicleID,
        # Prefill the vehicle details from whatever is currently linked, so a
        # correction starts from the stored values rather than a blank field.
        "car_model": appointment.VehicleID.Model if appointment.VehicleID else "",
        "license_plate": (
            appointment.VehicleID.LicensePlate if appointment.VehicleID else ""
        ),
        "notes": appointment.Notes,
        "appointment_date": appointment.AppointmentDate,
        "appointment_time": appointment.AppointmentTime.strftime("%H:%M"),
    }
    form = AppointmentForm(appointment=appointment, initial=initial)
    if request.method == "POST":
        form = AppointmentForm(request.POST, appointment=appointment)
        if form.is_valid():
            try:
                appointment = services.update_appointment(
                    appointment_id,
                    first_name=form.cleaned_data["first_name"],
                    last_name=form.cleaned_data["last_name"],
                    service_code=form.cleaned_data["service"].Service_code,
                    appointment_date=form.cleaned_data["appointment_date"],
                    appointment_time=form.cleaned_data["appointment_time"],
                    phone=form.cleaned_data.get("phone", ""),
                    email=form.cleaned_data.get("email", ""),
                    notes=form.cleaned_data.get("notes", ""),
                    car_model=form.cleaned_data["car_model"],
                    license_plate=form.cleaned_data["license_plate"],
                )
                vehicle = form.cleaned_data.get("vehicle")
                if vehicle is not None and appointment.VehicleID_id != vehicle.VehicleID:
                    appointment = services.match_vehicle_to_appointment(
                        appointment.pk, vehicle.VehicleID, confirm_replace=True
                    )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Appointment {appointment.pk} updated.")
                return redirect(
                    "core:appointment_detail", appointment_id=appointment.pk
                )
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/appointment_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "edit",
            "appointment": appointment,
        },
    )



@require_auth
@booking_staff
@require_POST
def appointment_accept_view(request, appointment_id: int):
    """Accept a pending request, confirming its slot.

    Deliberately a separate action from the status dropdown: accepting is the
    one decision staff make about every incoming request, so it gets its own
    button rather than living inside a six-option field. The service layer
    re-checks the transition, so accepting an already-confirmed or cancelled
    appointment is refused rather than silently ignored.
    """
    try:
        appointment = services.get_appointment(appointment_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:appointment_list")

    try:
        appointment = services.accept_appointment(appointment_id)
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(
            request,
            f"Appointment {appointment.pk} accepted for "
            f"{appointment.customer_name} on {appointment.slot}.",
        )

    # Return to where the request came from: the review queue on the diary
    # screen when accepted from the list, the detail page otherwise.
    next_url = request.POST.get("next")
    if next_url == reverse("core:appointment_list"):
        return redirect(next_url)
    return redirect("core:appointment_detail", appointment_id=appointment_id)


@require_auth
@booking_staff
@require_POST
def appointment_status_view(request, appointment_id: int):
    """Advance an appointment through a legal lifecycle transition.

    The dropdown only offers transitions allowed from the current status, and
    the service layer re-checks the move, so an appointment can never jump
    straight to completed or reopen a cancelled booking.
    """
    try:
        appointment = services.get_appointment(appointment_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:appointment_list")

    form = AppointmentStatusForm(request.POST, appointment=appointment)
    if form.is_valid():
        try:
            appointment = services.set_appointment_status(
                appointment_id, form.cleaned_data["status"]
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            messages.success(
                request,
                f"Appointment {appointment.pk} is now {appointment.Status.lower()}.",
            )
    else:
        messages.error(request, "Choose a valid status transition.")
    return redirect("core:appointment_detail", appointment_id=appointment_id)


@require_auth
@booking_staff
@require_POST
def appointment_cancel_view(request, appointment_id: int):
    """Cancel an appointment, recording an optional reason and freeing the slot."""
    try:
        services.get_appointment(appointment_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:appointment_list")

    form = CancelAppointmentForm(request.POST)
    if form.is_valid():
        try:
            appointment = services.cancel_appointment(
                appointment_id, reason=form.cleaned_data.get("reason", "")
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            messages.success(
                request,
                f"Appointment {appointment.pk} was cancelled; the slot is now free.",
            )
    else:
        messages.error(request, "The cancellation could not be completed.")
    return redirect("core:appointment_detail", appointment_id=appointment_id)


@require_auth
@booking_staff
@require_POST
def appointment_match_vehicle_view(request, appointment_id: int):
    """Link a recorded vehicle to an appointment ( 'match a vehicle')."""
    try:
        appointment = services.get_appointment(appointment_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:appointment_list")

    form = MatchVehicleForm(request.POST, appointment=appointment)
    if form.is_valid():
        vehicle = form.cleaned_data["vehicle"]
        try:
            appointment = services.match_vehicle_to_appointment(
                appointment_id,
                vehicle.VehicleID,
                confirm_replace=form.cleaned_data.get("confirm_replace", False),
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            messages.success(
                request,
                f"Vehicle {vehicle.display_name} linked to appointment "
                f"{appointment.pk}.",
            )
    else:
        messages.error(request, "Choose a vehicle to match.")
    return redirect("core:appointment_detail", appointment_id=appointment_id)



# ---------------------------------------------------------------------------
# Vehicle records (Receptionist, Administrator; detail also open to Technicians)
# ---------------------------------------------------------------------------
@require_auth
@booking_staff
def vehicle_list_view(request):
    """Searchable table of recorded vehicles."""
    search_form = VehicleSearchForm(request.GET or None)
    query = request.GET.get("q", "").strip()
    vehicles = services.search_vehicles(query) if query else services.list_vehicles()

    return render(
        request,
        "core/vehicle_list.html",
        {
            "identity": _identity(request),
            "vehicles": vehicles,
            "search_form": search_form,
            "query": query,
        },
    )


@require_auth
@booking_staff
def vehicle_create_view(request):
    """Record a vehicle directly (outside the check-in flow)."""
    form = VehicleForm()
    if request.method == "POST":
        form = VehicleForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                vehicle = services.create_vehicle(
                    make=form.cleaned_data.get("make", ""),
                    model=form.cleaned_data["model"],
                    vin=form.cleaned_data.get("vin"),
                    license_plate=form.cleaned_data.get("license_plate"),
                    year=form.cleaned_data.get("year"),
                    colour=form.cleaned_data.get("colour", ""),
                    image=form.cleaned_data.get("image"),
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Vehicle {vehicle.display_name} recorded.")
                return redirect("core:vehicle_detail", vehicle_id=vehicle.VehicleID)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/vehicle_form.html",
        {"identity": _identity(request), "form": form, "mode": "create"},
    )


@require_auth
@booking_staff
def vehicle_check_in_view(request):
    """Check a vehicle in: reuse an existing record or create a new one.

    Find-or-create matches on VIN *or* registration, so a returning customer is
    never recorded twice; the reused record is returned with ``created`` False.
    """
    form = VehicleCheckInForm()
    if request.method == "POST":
        form = VehicleCheckInForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                vehicle, created = services.check_in_vehicle(
                    make=form.cleaned_data.get("make", ""),
                    model=form.cleaned_data["model"],
                    vin=form.cleaned_data.get("vin"),
                    license_plate=form.cleaned_data.get("license_plate"),
                    year=form.cleaned_data.get("year"),
                    colour=form.cleaned_data.get("colour", ""),
                    image=form.cleaned_data.get("image"),
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    (
                        f"Vehicle {vehicle.display_name} checked in."
                        if created
                        else f"Existing vehicle {vehicle.display_name} found and "
                        "checked in."
                    ),
                )
                return redirect("core:vehicle_detail", vehicle_id=vehicle.VehicleID)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/vehicle_check_in.html",
        {"identity": _identity(request), "form": form},
    )



@require_auth
@vehicle_viewer
def vehicle_detail_view(request, vehicle_id: int):
    """A vehicle's record with its appointment history (readable by technicians too)."""
    try:
        vehicle = services.get_vehicle(vehicle_id)
        appointments = services.list_appointments_for_vehicle(vehicle_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:vehicle_list")

    identity = _identity(request)
    can_edit = identity["role_name"] in (Role.RECEPTIONIST, Role.ADMINISTRATOR)

    return render(
        request,
        "core/vehicle_detail.html",
        {
            "identity": identity,
            "vehicle": vehicle,
            "appointments": appointments,
            "can_edit": can_edit,
        },
    )


@require_auth
@booking_staff
def vehicle_edit_view(request, vehicle_id: int):
    """Update a vehicle's details; the photo is replaced only when a new one is uploaded."""
    try:
        vehicle = services.get_vehicle(vehicle_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:vehicle_list")

    initial = {
        "vin": vehicle.VIN or "",
        "license_plate": vehicle.LicensePlate or "",
        "make": vehicle.Make,
        "model": vehicle.Model,
        "year": vehicle.Year,
        "colour": vehicle.Colour,
    }
    form = VehicleForm(vehicle=vehicle, initial=initial)
    if request.method == "POST":
        form = VehicleForm(request.POST, request.FILES, vehicle=vehicle)
        if form.is_valid():
            image = form.cleaned_data.get("image")
            try:
                vehicle = services.update_vehicle(
                    vehicle_id,
                    make=form.cleaned_data.get("make", ""),
                    model=form.cleaned_data["model"],
                    vin=form.cleaned_data.get("vin"),
                    license_plate=form.cleaned_data.get("license_plate"),
                    year=form.cleaned_data.get("year"),
                    colour=form.cleaned_data.get("colour", ""),
                    image=image,
                    replace_image=bool(image),
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(request, f"Vehicle {vehicle.display_name} updated.")
                return redirect("core:vehicle_detail", vehicle_id=vehicle.VehicleID)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/vehicle_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "edit",
            "vehicle": vehicle,
        },
    )


@require_auth
@booking_staff
@require_POST
def vehicle_delete_view(request, vehicle_id: int):
    """Delete a vehicle, but only when it has no appointment history."""
    try:
        services.delete_vehicle(vehicle_id)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(request, "Vehicle record deleted.")
    return redirect("core:vehicle_list")


# ---------------------------------------------------------------------------
# Timebox 3 helpers
# ---------------------------------------------------------------------------
def _current_staff(request) -> Staff | None:
    """The signed-in ``Staff`` row, or ``None`` if the session has expired.

    Resolved from the session rather than from anything in the request body, so
    a technician cannot claim to be somebody else by posting a different
    identifier.
    """
    identity = _identity(request)
    if identity is None:
        return None
    return Staff.objects.select_related("RoleID").filter(
        pk=identity["staff_id"]
    ).first()


def _repair_denied(request, reason: str):
    """Refuse a repair action, the same way ``require_role`` refuses a role.

    Decorators can only gate on the *role*, but a technician's access also
    depends on whether this particular job is assigned to them. That per-job
    decision is made in the view, and lands here so the outcome - an error
    message plus the 403 access-denied page - matches the rest of the app.
    """
    messages.error(request, reason)
    return redirect("core:access_denied")


def _repair_context(request, repair: Repair) -> dict:
    """Shared context for every repair screen.

    Works out three things the templates need so no screen has to re-derive
    them: who is signed in, whether they may change this job, and whether they
    are its assigned technician. ``can_manage`` is what gates the status and
    inspection actions; ``is_admin`` additionally gates re-assignment, which is
    an Administrator function.
    """
    staff = _current_staff(request)
    identity = _identity(request)
    role_name = identity["role_name"] if identity else ""
    return {
        "identity": identity,
        "repair": repair,
        "staff": staff,
        "role_name": role_name,
        "is_admin": role_name == Role.ADMINISTRATOR,
        "can_manage": services.can_manage_repair(repair, staff),
        "allowed_transitions": REPAIR_TRANSITIONS.get(repair.Status, []),
        "progress_steps": _progress_steps(repair),
        # Kept in context rather than hard-coded in the templates so the
        # three-image rule has one source of truth.
        "max_inspection_images": MAX_INSPECTION_IMAGES,
    }


def _progress_steps(repair: Repair) -> list[dict]:
    """Describe where a job has reached, for the progress track.

    Returns one entry per status in lifecycle order, each marked ``done``,
    ``current`` or ``todo``. Deriving it from the ordered status list means the
    track cannot disagree with ``REPAIR_TRANSITIONS`` if the lifecycle is ever
    extended.
    """
    order = [value for value, _label in REPAIR_STATUSES]
    reached = order.index(repair.Status)
    return [
        {
            "label": label,
            "state": (
                "done" if index < reached
                else "current" if index == reached
                else "todo"
            ),
        }
        for index, (_value, label) in enumerate(REPAIR_STATUSES)
    ]


# ---------------------------------------------------------------------------
# Repair jobs (Administrator manages; Receptionist reads; Technician owns theirs)
# ---------------------------------------------------------------------------
@require_auth
@repair_viewer
def repair_list_view(request):
    """The repair job board, filterable by status, technician and priority (FR6).

    One listing serves all three roles that may open it, but what each may do
    from here differs: an Administrator sees the assignment and status controls,
    while a Receptionist - who has read-only visibility so they can answer a
    customer at the counter - sees the board without any action buttons.

    Filters are validated through ``RepairFilterForm`` so a hand-edited query
    string cannot silently produce an empty board.

    A technician is redirected to their own board: the scoping rule is that they
    may only see jobs assigned to them, so the full list is never rendered for
    that role - not even filtered down afterwards. The header navigation points
    each role at the right screen, so this redirect only catches a hand-typed URL.
    """
    identity = _identity(request)
    role_name = identity["role_name"] if identity else ""
    if role_name == Role.TECHNICIAN:
        return redirect("core:my_repairs")

    filter_form = RepairFilterForm(request.GET or None)
    status = ""
    priority = ""
    technician_id = None
    if filter_form.is_valid():
        status = filter_form.cleaned_data.get("status") or ""
        priority = filter_form.cleaned_data.get("priority") or ""
        chosen = filter_form.cleaned_data.get("technician")
        technician_id = chosen.pk if chosen is not None else None

    try:
        repairs = services.list_repairs(
            status=status, technician_id=technician_id, priority=priority
        )
    except ValidationError as error:
        messages.warning(request, str(error))
        repairs = services.list_repairs()

    return render(
        request,
        "core/repair_list.html",
        {
            "identity": identity,
            "repairs": repairs,
            "filter_form": filter_form,
            "status_filter": status,
            "priority_filter": priority,
            "technician_filter": technician_id,
            "is_admin": role_name == Role.ADMINISTRATOR,
            # Only checked-in appointments without a job can start one, so the
            # count tells an Administrator whether the button will have anything
            # to offer before they click it.
            "eligible_count": len(services.list_appointments_eligible_for_repair()),
        },
    )


@require_auth
@require_role(Role.TECHNICIAN)
def my_repairs_view(request):
    """The technician's own job board (FR8).

    Everything on this page is scoped by the assignment itself: the query asks
    for jobs whose ``TechnicianID`` is this technician's record, so another
    technician's work cannot appear here no matter what the URL says.

    Each job carries its next legal transition, so the board doubles as the place
    a technician starts and finishes work from rather than a page they read
    before opening each job in turn.
    """
    staff = _current_staff(request)
    technician = services.technician_for_staff(staff)

    if technician is None:
        # A Technician-role account with no Technician record cannot hold jobs.
        # Rather than show an empty board that implies everything is finished,
        # say what is actually wrong and send them to their profile.
        messages.error(
            request,
            "Your account has no technician record yet, so no repair jobs can be "
            "assigned to you. Ask an administrator to add one.",
        )
        return redirect("core:profile")

    repairs = services.list_repairs_for_technician(technician.pk)

    return render(
        request,
        "core/my_repairs.html",
        {
            "identity": _identity(request),
            "technician": technician,
            "repairs": repairs,
            # Counts for the board's header, so a technician sees at a glance how
            # much is waiting and how much is under way.
            "not_started": sum(
                1 for repair in repairs if repair.Status == REPAIR_NOT_STARTED
            ),
            "in_progress": sum(
                1 for repair in repairs if repair.Status == REPAIR_IN_PROGRESS
            ),
            "completed": sum(
                1 for repair in repairs if repair.Status == REPAIR_COMPLETED
            ),
        },
    )


@require_auth
@repair_manager
def repair_create_view(request):
    """Create a repair job for a checked-in appointment (FR6).

    Administrator-only: the booking desk's part of the story ended at check-in.
    The appointment dropdown already lists only bookings that are Checked In,
    have a vehicle linked and do not yet have a job, so the UI cannot propose a
    job the service layer would refuse; the service re-checks regardless.

    Technician and bay may both be left blank here. A job is raised the moment
    the vehicle arrives, and a qualified technician or a free bay is often only
    confirmed moments later - both are filled in from the detail page afterwards.

    ``?appointment=<id>`` pre-selects the appointment, which is how the
    appointment detail page hands off to this form.
    """
    preset = None
    if request.method == "GET":
        preset_id = request.GET.get("appointment")
        if preset_id:
            try:
                preset = services.get_appointment(int(preset_id))
            except (NotFoundError, ValueError, TypeError):
                messages.warning(
                    request, "That appointment could not be pre-selected."
                )

    form = RepairForm(preset_appointment=preset)
    if request.method == "POST":
        form = RepairForm(request.POST)
        if form.is_valid():
            appointment = form.cleaned_data["appointment"]
            technician = form.cleaned_data.get("technician")
            bay = form.cleaned_data.get("bay")
            try:
                repair = services.create_repair(
                    appointment_id=appointment.pk,
                    priority=form.cleaned_data["priority"],
                    technician_id=technician.pk if technician else None,
                    bay_id=bay.pk if bay else None,
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                # Spell out what is still outstanding, so a job created without
                # a technician is not mistaken for a fully-staffed one.
                outstanding = []
                if repair.TechnicianID_id is None:
                    outstanding.append("a technician")
                if repair.BayID_id is None:
                    outstanding.append("a service bay")
                suffix = (
                    f" Still needs {' and '.join(outstanding)}."
                    if outstanding
                    else ""
                )
                messages.success(
                    request,
                    f"Repair job #{repair.pk} created for "
                    f"{repair.customer_name} "
                    f"({repair.RepairPriority.lower()} priority).{suffix}",
                )
                return redirect("core:repair_detail", repair_id=repair.pk)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/repair_form.html",
        {
            "identity": _identity(request),
            "form": form,
            "mode": "create",
        },
    )


@require_auth
@repair_viewer
def repair_detail_view(request, repair_id: int):
    """One repair job with its inspection, assignments and progress actions (FR6-8).

    Every role that may open the page sees the same facts - job, vehicle,
    technician, bay, inspection - but the actions differ:

    * the assigned technician (or an Administrator) gets the progress control
      and the inspection form;
    * a technician who is *not* assigned sees the job read-only, matching the
      rule that they may not act on someone else's work;
    * an Administrator additionally gets the re-assignment form;
    * a Receptionist sees read-only visibility for counter questions.

    ``can_manage`` from the service layer decides the first distinction, so the
    templates and the POST views share one definition of ownership.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    context = _repair_context(request, repair)

    # A technician may not open another technician's job: their visibility is
    # scoped to their own assignments, on the detail page as much as the board.
    if context["role_name"] == Role.TECHNICIAN and not context["can_manage"]:
        return _repair_denied(
            request, "Access denied: that repair job is not assigned to you."
        )

    context["status_form"] = RepairStatusForm(repair=repair)
    context["inspection"] = repair.InspectionID
    # Timebox 4: the parts panel. Whether stock may be drawn is decided once here
    # and shared by the template and the POST view, so the "record parts" box can
    # never be shown for a job the service layer would then refuse - and the list
    # of lines the panel shows is the same list an invoice would be built from.
    context["parts_usage"] = services.list_parts_usage(repair.pk)
    context["parts_cost"] = services.parts_cost_for_repair(repair.pk)
    context["invoice"] = services.invoice_for_repair(repair.pk)
    context["can_record_parts"] = context["can_manage"] and _parts_open(repair, context)
    if context["can_record_parts"]:
        context["parts_form"] = PartsUsageForm(repair=repair)
    if context["is_admin"]:
        context["assignment_form"] = RepairAssignmentForm(repair=repair)
        # Which technicians are qualified for this job's service, so the detail
        # screen can say why a re-assignment failed without leaving the page.
        context["qualified_technicians"] = services.list_technicians_for_service(
            repair.AppointmentID.ServiceCode_id
        )
    return render(request, "core/repair_detail.html", context)


def _parts_open(repair: Repair, context: dict) -> bool:
    """True while stock may still be drawn against ``repair``.

    Mirrors the gate the service layer enforces, so the panel is only offered when
    recording would succeed: the job must be In Progress, it must not already be
    invoiced, and there must be at least one active part to choose from. A technician
    who is not assigned to the job is excluded by ``can_manage`` before this is even
    consulted.
    """
    if repair.Status != REPAIR_IN_PROGRESS:
        return False
    if context.get("invoice") is not None:
        return False
    return Parts.objects.filter(IsActive=True).exists()


@require_auth
@repair_viewer
@require_POST
def repair_status_view(request, repair_id: int):
    """Move a repair job to its next legal status (FR8).

    POST-only, like the appointment status action: this is a state change on
    somebody's work, so it must never happen through a link a crawler or a
    prefetch could follow.

    Ownership is checked here as well as hidden in the template. A technician
    who is not assigned to the job is refused outright, and the service layer
    re-checks the transition itself, so posting a status straight to the URL
    cannot skip a step either.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    staff = _current_staff(request)
    if not services.can_manage_repair(repair, staff):
        return _repair_denied(
            request,
            "Access denied: you can only update the status of repair jobs "
            "assigned to you.",
        )

    form = RepairStatusForm(request.POST, repair=repair)
    if form.is_valid():
        try:
            repair = services.set_repair_status(
                repair_id, form.cleaned_data["status"]
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            message = f"Repair job #{repair.pk} is now {repair.Status.lower()}."
            if repair.is_completed:
                # Say so explicitly: releasing the bay is the part an
                # administrator watching the floor needs to know happened.
                if repair.BayID is not None:
                    message += f" {repair.BayID.BayName} is available again."
                else:
                    message += " Its service bay is available again."
            messages.success(request, message)
    else:
        messages.error(request, "Choose a valid status transition.")

    return _repair_back(request, repair)


def _repair_back(request, repair: Repair):
    """Send a technician back to their own board, everyone else to the job.

    After an action from the "My Repairs" board, a technician should land back
    on that board rather than on the detail page, so the flow reads as "tick it
    off and carry on". Anyone acting from the detail page stays there.
    """
    identity = _identity(request)
    role_name = identity["role_name"] if identity else ""
    if role_name == Role.TECHNICIAN and request.POST.get("next") == reverse(
        "core:my_repairs"
    ):
        return redirect("core:my_repairs")
    return redirect("core:repair_detail", repair_id=repair.pk)


@require_auth
@repair_manager
@require_POST
def repair_assign_view(request, repair_id: int):
    """Update a repair job's technician and/or bay (FR6).

    Administrator-only, and both halves may be changed at once - the common case
    being a job created before a qualified technician or a free bay was
    confirmed, and both then filled in together.

    Either half may also be left alone: the form offers "leave unchanged" for
    both, so re-assigning a technician does not require re-picking the bay.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    form = RepairAssignmentForm(request.POST, repair=repair)
    if form.is_valid():
        technician = form.cleaned_data.get("technician")
        bay = form.cleaned_data.get("bay")
        if technician is None and bay is None:
            messages.info(
                request,
                "Choose a technician or a service bay to change the assignment.",
            )
            return redirect("core:repair_detail", repair_id=repair_id)
        try:
            repair = services.update_repair_assignment(
                repair_id,
                technician_id=technician.pk if technician else None,
                bay_id=bay.pk if bay else None,
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            changed = []
            if technician is not None:
                changed.append(f"technician {technician.full_name}")
            if bay is not None:
                changed.append(f"bay {bay.BayName}")
            messages.success(
                request,
                f"Repair job #{repair.pk} assigned to {' and '.join(changed)}.",
            )
    else:
        messages.error(request, "Please correct the errors below.")

    return redirect("core:repair_detail", repair_id=repair_id)


# ---------------------------------------------------------------------------
# Vehicle inspection (FR7)
# ---------------------------------------------------------------------------
@require_auth
@repair_viewer
def inspection_create_view(request, repair_id: int):
    """Record the vehicle's condition for a repair job (FR7).

    The assigned technician or an Administrator may open this. A technician who
    is not assigned is refused, matching the rule that they cannot inspect a
    vehicle attached to someone else's job. A job that already has an inspection
    is sent to the edit screen instead of creating a second record.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    staff = _current_staff(request)
    if not services.can_manage_repair(repair, staff):
        return _repair_denied(
            request,
            "Access denied: you can only inspect repair jobs assigned to you.",
        )

    if repair.InspectionID_id is not None:
        messages.info(
            request,
            f"Repair job #{repair.pk} already has an inspection; editing it instead.",
        )
        return redirect("core:inspection_edit", repair_id=repair_id)

    # An inspection documents a specific car, so there is nothing to record until
    # the appointment has a vehicle linked.
    if repair.AppointmentID.VehicleID_id is None:
        messages.error(
            request,
            "The appointment has no vehicle linked, so there is nothing to inspect.",
        )
        return redirect("core:repair_detail", repair_id=repair_id)

    form = InspectionForm(repair=repair)
    if request.method == "POST":
        form = InspectionForm(request.POST, request.FILES, repair=repair)
        if form.is_valid():
            try:
                repair, inspection = services.create_inspection(
                    repair_id,
                    staff=staff,
                    description=form.cleaned_data["description"],
                    images=form.uploaded_images(),
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    f"Inspection recorded for repair job #{repair.pk} "
                    f"({inspection.image_count} image"
                    f"{'' if inspection.image_count == 1 else 's'}).",
                )
                return redirect("core:repair_detail", repair_id=repair_id)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/inspection_form.html",
        {
            **_repair_context(request, repair),
            "form": form,
            "mode": "create",
        },
    )


@require_auth
@repair_viewer
def inspection_edit_view(request, repair_id: int):
    """Amend an existing inspection (FR7).

    Same ownership rule as creation. Photographs are added into the slots still
    free rather than replacing the stored ones, so amending the description does
    not silently discard the technician's existing photos - and the three-image
    ceiling is counted across both the stored and the newly-uploaded images.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    staff = _current_staff(request)
    if not services.can_manage_repair(repair, staff):
        return _repair_denied(
            request,
            "Access denied: you can only inspect repair jobs assigned to you.",
        )

    if repair.InspectionID_id is None:
        messages.info(
            request,
            f"Repair job #{repair.pk} has no inspection yet; recording one instead.",
        )
        return redirect("core:inspection_create", repair_id=repair_id)

    form = InspectionForm(repair=repair)
    if request.method == "POST":
        form = InspectionForm(request.POST, request.FILES, repair=repair)
        if form.is_valid():
            try:
                repair, inspection = services.update_inspection(
                    repair_id,
                    staff=staff,
                    description=form.cleaned_data["description"],
                    images=form.uploaded_images(),
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    f"Inspection for repair job #{repair.pk} updated.",
                )
                return redirect("core:repair_detail", repair_id=repair_id)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/inspection_form.html",
        {
            **_repair_context(request, repair),
            "form": form,
            "mode": "edit",
            "inspection": repair.InspectionID,
        },
    )


# ---------------------------------------------------------------------------
# Service bay management (FR9, Administrator)
# ---------------------------------------------------------------------------
@require_auth
@bay_manager
def bay_list_view(request):
    """Manage the workshop's service bays (FR9).

    Shows each bay's stored availability alongside the job currently holding it,
    so an administrator can see at a glance which bays are free before assigning
    one. A bay whose job has been completed reads Available again, which is the
    automatic release this screen is meant to demonstrate.
    """
    bays = services.list_service_bays()

    # The active job in each bay, if any, so the table can name what is holding
    # it. Fetched in one query and attached to each bay, rather than one lookup
    # per row. A bay can hold at most one active job - that is exactly what the
    # Occupied rule guarantees - so the mapping is unambiguous.
    active_repairs = {
        repair.BayID_id: repair
        for repair in Repair.objects.filter(
            Status__in=ACTIVE_REPAIR_STATUSES, BayID__isnull=False
        ).select_related(
            "AppointmentID", "AppointmentID__ServiceCode", "TechnicianID__StaffID"
        )
    }
    for bay in bays:
        bay.active_repair = active_repairs.get(bay.pk)

    return render(
        request,
        "core/bay_list.html",
        {
            "identity": _identity(request),
            "bays": bays,
            "bay_form": ServiceBayForm(),
            "available_count": sum(1 for bay in bays if bay.is_available),
            "occupied_count": sum(1 for bay in bays if not bay.is_available),
        },
    )


@require_auth
@bay_manager
@require_POST
def bay_create_view(request):
    """Add a service bay to the workshop (FR9).

    The new bay starts Available and stays that way until a repair job is
    assigned to it - nothing here can set the status directly.
    """
    form = ServiceBayForm(request.POST)
    if form.is_valid():
        try:
            bay = services.create_service_bay(form.cleaned_data["bay_name"])
        except ServiceError as error:
            _service_error(request, error)
        else:
            messages.success(request, f"{bay.BayName} added and available.")
    else:
        messages.error(request, "Please correct the errors below.")
    return redirect("core:bay_list")


@require_auth
@bay_manager
@require_POST
def bay_edit_view(request, bay_id: int):
    """Rename a service bay (FR9).

    Only the name is editable. Availability is derived from the job the bay
    holds, so it cannot be typed over - which is what keeps two repair jobs from
    being parked in one physical space.
    """
    form = ServiceBayForm(request.POST)
    if form.is_valid():
        try:
            bay = services.update_service_bay(bay_id, form.cleaned_data["bay_name"])
        except NotFoundError as error:
            messages.error(request, str(error))
        except ServiceError as error:
            _service_error(request, error)
        else:
            messages.success(request, f"{bay.BayName} renamed.")
    else:
        messages.error(request, "Please correct the errors below.")
    return redirect("core:bay_list")


@require_auth
@bay_manager
@require_POST
def bay_delete_view(request, bay_id: int):
    """Retire a service bay (FR9).

    Refused while the bay holds an active job, because deleting it would leave a
    repair claiming to be in a bay that no longer exists. Once the work is
    complete the bay can be retired, and old jobs keep their history since
    ``Repair.BayID`` is nullable.
    """
    try:
        services.delete_service_bay(bay_id)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(request, "Service bay deleted.")
    return redirect("core:bay_list")


# ---------------------------------------------------------------------------
# Spare-part inventory (FR10, Timebox 4)
# ---------------------------------------------------------------------------
def _parts_context(request) -> dict:
    """Context shared by the parts screens.

    Carries the low-stock count alongside whatever the page lists, so the badge in
    the header and the figures in the table are read from the same source and
    cannot disagree with each other.
    """
    identity = _identity(request)
    role_name = identity["role_name"] if identity else ""
    return {
        "identity": identity,
        "role_name": role_name,
        "is_admin": role_name == Role.ADMINISTRATOR,
        "low_stock_count": services.count_low_stock_parts(),
    }


@require_auth
@parts_viewer
def part_list_view(request):
    """The parts catalogue with current stock and the low-stock flag (T4-INV-02).

    Open to all three workshop roles because each needs a different thing from it:
    the Administrator manages the catalogue, the Technician checks what is on the
    shelf before starting a job, and the Receptionist answers a customer asking
    whether a part is available. Only the Administrator sees the edit, deactivate
    and delete actions - that split is made in the template from ``is_admin``, and
    enforced again by the decorators on the actions themselves.
    """
    query = request.GET.get("q", "").strip()
    show = request.GET.get("show", "active")

    # Deactivated parts are hidden by default but never unreachable: the
    # Administrator has to be able to find a retired part to reactivate it, and to
    # see that its usage history is still intact.
    include_inactive = show == "all"
    parts = (
        services.search_parts(query, include_inactive=include_inactive)
        if query
        else services.list_parts(include_inactive=include_inactive)
    )

    context = {
        **_parts_context(request),
        "parts": parts,
        "query": query,
        "show": show,
        "part_form": PartForm(),
        "active_total": Parts.objects.filter(IsActive=True).count(),
        "inactive_total": Parts.objects.filter(IsActive=False).count(),
    }
    return render(request, "core/part_list.html", context)


@require_auth
@inventory_manager
@require_POST
def part_create_view(request):
    """Add a part to the catalogue (T4-INV-01).

    POST-only, like the bay creation it sits alongside: this writes catalogue data
    rather than displaying a form of its own, and the add form lives inline on the
    parts list so an administrator restocking several items does not navigate away
    between them.
    """
    form = PartForm(request.POST)
    if form.is_valid():
        try:
            part = services.create_part(
                part_name=form.cleaned_data["part_name"],
                unit_price=form.cleaned_data["unit_price"],
                stock_qty=form.cleaned_data["stock_qty"],
                reorder_threshold=form.cleaned_data["reorder_threshold"],
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            message = f"'{part.PartName}' added to the catalogue."
            if part.is_low_stock:
                # Said at creation rather than discovered later on the low-stock
                # page: entering stock at or below the threshold is a real
                # condition, and the administrator should hear it now.
                message += (
                    f" It is already at or below its reorder threshold "
                    f"({part.StockQty} of {part.ReorderThreshold}), so it is "
                    "flagged low stock."
                )
            messages.success(request, message)
    else:
        messages.error(request, "Please correct the errors below.")
    return redirect("core:part_list")


def _part_back(request):
    """Return to the low-stock page when the edit was started from there.

    Restocking from the low-stock review should land back on that review, so the
    administrator can work down the list of shortages rather than being dropped into
    the full catalogue after each one. Anywhere else returns to the catalogue.
    """
    if request.POST.get("next") == reverse("core:part_low_stock"):
        return redirect("core:part_low_stock")
    return redirect("core:part_list")


@require_auth
@inventory_manager
def part_edit_view(request, part_id: int):
    """Amend a part's name, price, stock or threshold (T4-INV-03).

    This is also the restock action: updating the stock quantity is what clears the
    low-stock flag (T4-INV-10), so the edit form is reached from the low-stock page
    as well as from the catalogue.

    A separate page rather than an inline row, because four fields do not fit
    comfortably in a table cell and the same form has to serve both entry points.
    """
    try:
        part = services.get_part(part_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:part_list")

    form = PartForm(request.POST or None, part=part)
    if request.method == "POST":
        if form.is_valid():
            try:
                part = services.update_part(
                    part_id,
                    part_name=form.cleaned_data["part_name"],
                    unit_price=form.cleaned_data["unit_price"],
                    stock_qty=form.cleaned_data["stock_qty"],
                    reorder_threshold=form.cleaned_data["reorder_threshold"],
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                message = f"'{part.PartName}' updated."
                if part.is_low_stock:
                    # Said explicitly, because restocking to a figure still at or
                    # below the threshold leaves the flag up and the administrator
                    # should not have to work out why.
                    message += " It is still at or below its reorder threshold."
                else:
                    message += " Low-stock flag cleared."
                messages.success(request, message)
                return _part_back(request)
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "core/part_form.html",
        {
            **_parts_context(request),
            "part": part,
            "form": form,
            # Drives the wording of the retirement note: a part with history can
            # only be deactivated, and the page says so rather than leaving the
            # administrator to discover it by trying to delete.
            "has_usage": services.part_has_usage(part.pk),
            "usage_count": PartsUsage.objects.filter(PartID=part).count(),
        },
    )


@require_auth
@inventory_manager
@require_POST
def part_deactivate_view(request, part_id: int):
    """Retire a part without deleting its history (T4-INV-04).

    This is the retirement path for anything already used: deactivating keeps the
    row - and every invoice quoting it - intact, while stopping the part being
    offered for new repairs. A part with no usage history can be deleted outright
    instead, and the catalogue offers that button in its place.
    """
    try:
        part = services.deactivate_part(part_id)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(
            request,
            f"'{part.PartName}' deactivated. It can no longer be used on new "
            "repairs, but its usage history and any invoices quoting it are kept.",
        )
    return redirect("core:part_list")


@require_auth
@inventory_manager
@require_POST
def part_reactivate_view(request, part_id: int):
    """Bring a retired part back into the catalogue.

    Offered because deactivation is retirement rather than deletion: a part the
    workshop stopped stocking may be sourced again, and reactivating it keeps its
    usage history where a replacement row would start from nothing.
    """
    try:
        part = services.reactivate_part(part_id)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(request, f"'{part.PartName}' is active again.")
    return redirect("core:part_list")


@require_auth
@inventory_manager
@require_POST
def part_delete_view(request, part_id: int):
    """Delete a part that has never been used (T4-INV-04).

    Refused by the service layer the moment the part has usage history, with a
    message naming deactivation as the alternative. The button is also hidden for
    such parts, so this refusal is the backstop against a hand-crafted POST rather
    than something an administrator normally meets.
    """
    try:
        services.delete_part(part_id)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(request, "Part deleted.")
    return redirect("core:part_list")


@require_auth
@inventory_manager
def part_low_stock_view(request):
    """Review the active parts at or below their reorder threshold (T4-INV-10).

    Administrator-only, because restocking is a catalogue change. Each shortage is
    listed with its own restock form, so working down the list does not mean
    navigating to the edit page and back for every part.

    The shortfall is computed here and attached to each part rather than worked out
    in the template, because it is arithmetic that belongs in one place - the same
    approach ``bay_list_view`` takes for the job holding a bay. The restock form
    itself is validated by ``PartRestockForm`` on POST; the row only needs the
    part's current figure to pre-fill with.
    """
    parts = services.list_low_stock_parts()
    for part in parts:
        # How far below the threshold the shelf has fallen. Zero when the stock
        # sits exactly on the threshold, which still counts as low stock.
        part.short_by = max(part.ReorderThreshold - part.StockQty, 0)

    return render(
        request,
        "core/part_low_stock.html",
        {
            **_parts_context(request),
            "parts": parts,
            "out_of_stock_count": sum(1 for part in parts if part.StockQty == 0),
        },
    )


@require_auth
@inventory_manager
@require_POST
def part_restock_view(request, part_id: int):
    """Restock a part from the low-stock review page (T4-INV-10).

    The quantity is *set* rather than incremented: the administrator counts the
    shelf and enters what is there. Because the low-stock flag is derived from stock
    versus threshold, a restock above the threshold clears it with no separate step.
    """
    try:
        part = services.get_part(part_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:part_low_stock")

    form = PartRestockForm(request.POST, part=part)
    if form.is_valid():
        try:
            part = services.update_part(
                part_id, stock_qty=form.cleaned_data["stock_qty"]
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            if part.is_low_stock:
                # A warning rather than a success: the figure was saved, but the
                # shortage this page exists to clear is still there.
                messages.warning(
                    request,
                    f"'{part.PartName}' restocked to {part.StockQty}, which is still "
                    f"at or below its threshold of {part.ReorderThreshold}.",
                )
            else:
                messages.success(
                    request,
                    f"'{part.PartName}' restocked to {part.StockQty}. "
                    "Low-stock flag cleared.",
                )
    else:
        messages.error(request, "Enter a valid stock quantity to restock with.")
    return redirect("core:part_low_stock")


@require_auth
@inventory_manager
def part_usage_history_view(request, part_id: int):
    """A part's usage history: repair, quantity, date and who recorded it (T4-INV-11).

    The audit trail behind the stock figure. Each line links to the job it was drawn
    against, so an administrator reconciling a shortage can see exactly which repairs
    consumed the part and when.
    """
    try:
        part = services.get_part(part_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:part_list")

    # Materialised once, so the total and the rows come from the same query rather
    # than a separate aggregate that could disagree with what is on screen.
    rows = list(
        PartsUsage.objects.filter(PartID=part)
        .select_related(
            "RepairID",
            "RepairID__AppointmentID",
            "RepairID__AppointmentID__ServiceCode",
            "RecordedBy",
        )
        .order_by("-UsedAt", "-UsageID")
    )

    return render(
        request,
        "core/part_usage_history.html",
        {
            **_parts_context(request),
            "part": part,
            "usage": rows,
            "total_used": sum(item.Qty_Used for item in rows),
        },
    )


@require_auth
@repair_viewer
@require_POST
def repair_parts_record_view(request, repair_id: int):
    """Record a part used on a repair job and draw it from stock (T4-INV-05).

    Open to the assigned technician or an Administrator. The per-job ownership check
    that gates the status and inspection actions gates this one too, so a technician
    posting against someone else's job is refused here rather than only hidden in the
    template (TC4-06).

    Stock is reduced inside the same transaction as the usage row - that is done in
    the service layer - so the message shown afterwards can quote the resulting
    figure and, if the draw took the part to or below its reorder threshold, say so.
    Flagging it at the point of use is what makes the low-stock page get filled in
    the first place.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    staff = _current_staff(request)
    if not services.can_manage_repair(repair, staff):
        return _repair_denied(
            request,
            "Access denied: you can only record parts against repair jobs "
            "assigned to you.",
        )

    form = PartsUsageForm(request.POST, repair=repair)
    if form.is_valid():
        part = form.cleaned_data["part"]
        quantity = form.cleaned_data["qty_used"]
        try:
            usage, updated = services.record_parts_usage(
                repair_id, part.pk, quantity, staff
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            message = (
                f"{usage.Qty_Used} x {updated.PartName} recorded against job "
                f"#{repair.pk}. {updated.StockQty} left in stock."
            )
            if updated.is_low_stock:
                message += (
                    f" That is at or below its reorder threshold of "
                    f"{updated.ReorderThreshold}, so the part is now flagged "
                    "low stock."
                )
            messages.success(request, message)
    else:
        # The field-level reason (over-draw, non-numeric quantity) is more useful
        # than a generic line, so the first error is surfaced verbatim.
        first_error = next(iter(form.errors.values()), ["Please check the form."])
        messages.error(request, first_error[0])

    return _repair_back(request, repair)


@require_auth
@inventory_manager
@require_POST
def repair_parts_remove_view(request, repair_id: int, usage_id: int):
    """Remove a usage entry recorded in error, restoring the stock (T4-INV-08).

    Administrator-only, and refused once the job has been invoiced: the lines a bill
    was built from become the record at that point, and putting stock back behind
    them would leave an invoice charging for parts the system now says were never
    fitted.

    The repair id is part of the URL even though the usage row alone identifies the
    line, so the route reads as an action *on a job* and the redirect back to that
    job does not depend on anything in the request body.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:repair_list")

    staff = _current_staff(request)
    try:
        services.remove_parts_usage(usage_id, staff)
    except NotFoundError as error:
        messages.error(request, str(error))
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(
            request,
            f"Parts usage entry removed from job #{repair.pk} and the stock "
            "returned to the catalogue.",
        )
    return redirect("core:repair_detail", repair_id=repair_id)


# ---------------------------------------------------------------------------
# Invoicing (FR11, Receptionist and Administrator)
# ---------------------------------------------------------------------------
@require_auth
@invoice_staff
def invoice_list_view(request):
    """List invoices, filterable by payment status, plus the jobs awaiting one (T4-INV-28).

    Two lists on one page, because they are the two halves of the receptionist's
    billing task: what has been raised, and what is still waiting to be raised. The
    second list is the entry point for generation, so the dropdown of billable jobs
    lives here rather than on a page of its own.

    The filter is validated through ``InvoiceFilterForm`` so a hand-edited query
    string cannot silently produce an empty list that reads as "no invoices".
    """
    filter_form = InvoiceFilterForm(request.GET or None)
    payment_status = ""
    if filter_form.is_valid():
        payment_status = filter_form.cleaned_data.get("payment_status") or ""

    try:
        invoices = services.list_invoices(payment_status=payment_status)
    except ValidationError as error:
        messages.warning(request, str(error))
        invoices = services.list_invoices()

    return render(
        request,
        "core/invoice_list.html",
        {
            "identity": _identity(request),
            "invoices": invoices,
            "filter_form": filter_form,
            "payment_status": payment_status,
            # The completed-but-unbilled jobs, as direct links into each job's
            # generation page. Listed rather than offered through a form so the
            # picker works without JavaScript and lands on the preview before
            # anything is created.
            "eligible_repairs": services.list_repairs_eligible_for_invoice(),
            "pending_count": Invoice.objects.filter(
                PaymentStatus=PAYMENT_PENDING
            ).count(),
            "paid_count": Invoice.objects.filter(PaymentStatus=PAYMENT_PAID).count(),
            "total_outstanding": services.total_outstanding_invoices(),
        },
    )


@require_auth
@invoice_staff
def invoice_generate_view(request, repair_id: int):
    """Enter the tax rate and raise the bill for a completed job (T4-INV-20..25).

    The amounts are previewed from the same calculation the write path uses, so what
    the receptionist confirms is what gets stored. The zero-parts case shows a
    warning and requires the confirmation checkbox (T4-INV-25); a job with parts
    recorded does not show that checkbox at all.

    GET renders the preview, POST generates. A job that is not Completed, or is
    already invoiced, is redirected away with an explanation rather than shown a form
    that could only fail.
    """
    try:
        repair = services.get_repair(repair_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:invoice_list")

    existing = services.invoice_for_repair(repair_id)
    if existing is not None:
        # Sent to the bill that already exists rather than refused cold: the
        # receptionist almost certainly meant to open it, not raise a second.
        messages.info(
            request,
            f"Repair job #{repair.pk} has already been invoiced. Opening the "
            "existing invoice instead.",
        )
        return redirect("core:invoice_detail", invoice_id=existing.pk)

    if not repair.is_completed:
        messages.error(
            request,
            f"Repair job #{repair.pk} is {repair.Status.lower()}; an invoice can "
            "only be generated once the job is Completed.",
        )
        return redirect("core:repair_detail", repair_id=repair_id)

    staff = _current_staff(request)
    form = InvoiceGenerateForm(repair=repair)
    if request.method == "POST":
        form = InvoiceGenerateForm(request.POST, repair=repair)
        if form.is_valid():
            try:
                invoice = services.generate_invoice(
                    repair_id,
                    form.cleaned_data["tax_rate"],
                    staff,
                    confirm_no_parts=bool(form.cleaned_data.get("confirm_no_parts")),
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    f"Invoice #{invoice.pk} generated for {repair.customer_name}: "
                    f"{CURRENCY_CODE} {invoice.TotalAmount} ({invoice.PaymentStatus.lower()}).",
                )
                return redirect("core:invoice_detail", invoice_id=invoice.pk)
        else:
            messages.error(request, "Please correct the errors below.")

    try:
        preview = services.invoice_preview(
            repair_id, form["tax_rate"].value() or 0
        )
    except ValidationError:
        # An unusable rate in a re-rendered form: show the parts at zero tax so the
        # page still renders, and let the inline field error carry the explanation.
        preview = services.invoice_preview(repair_id, 0)

    return render(
        request,
        "core/invoice_form.html",
        {
            "identity": _identity(request),
            "repair": repair,
            "form": form,
            "preview": preview,
            "parts_usage": services.list_parts_usage(repair_id),
            # Drives the warning block: the form only offers the checkbox when this
            # is true, so the page and the validation agree on when it is needed.
            "needs_confirmation": form.needs_confirmation,
        },
    )


@require_auth
@invoice_staff
def invoice_detail_view(request, invoice_id: int):
    """One invoice with its itemised parts lines and the mark-paid action (T4-INV-28).

    Shows what the bill was built from: the labour line, each parts line at the price
    captured when it was used, and the tax. Those amounts are the stored copies, so
    the page renders an invoice issued weeks ago exactly as it was issued, even if
    the catalogues have moved since.

    The mark-paid button is offered only while the invoice is Pending. A Paid invoice
    is final (T4-INV-27), so the page says so rather than showing a control that
    would be refused.
    """
    try:
        invoice = services.get_invoice(invoice_id)
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:invoice_list")

    repair = invoice.RepairID
    return render(
        request,
        "core/invoice_detail.html",
        {
            "identity": _identity(request),
            "invoice": invoice,
            "repair": repair,
            # The lines the PartsCost figure was summed from, at their captured
            # prices - read rather than recalculated, so what is shown is what was
            # billed.
            "parts_usage": services.list_parts_usage(repair.pk),
            "can_mark_paid": not invoice.is_paid,
        },
    )


@require_auth
@invoice_staff
@require_POST
def invoice_pay_view(request, invoice_id: int):
    """Mark an invoice Paid when the customer settles the bill (T4-INV-26).

    POST-only: taking payment changes a financial record, so it must never happen
    through a link a prefetch or a crawler could follow. The refusal of an
    already-Paid invoice is enforced in the service layer, which is what makes a
    Paid invoice final (T4-INV-27) - there is no path back to Pending.
    """
    try:
        invoice = services.mark_invoice_paid(invoice_id, _current_staff(request))
    except NotFoundError as error:
        messages.error(request, str(error))
        return redirect("core:invoice_list")
    except ServiceError as error:
        _service_error(request, error)
    else:
        messages.success(
            request,
            f"Invoice #{invoice.pk} marked Paid for "
            f"{invoice.RepairID.customer_name}.",
        )
    return redirect("core:invoice_detail", invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# Technician payroll (FR12, Administrator)
# ---------------------------------------------------------------------------
@require_auth
@payroll_manager
def payroll_view(request):
    """Calculate and show technician pay for a chosen period (T4-PAY-01..06).

    A GET with query parameters runs the calculation, so the report is linkable and
    re-runnable without a stored run - payroll here is a review of what the data
    already implies, not a payslip to be saved. Opening the page with no parameters
    still runs it, for the current month, because an administrator coming here at
    month end wants figures rather than an empty form.

    Three outcomes are all reported on the page rather than being treated as
    failures:

    * a technician with no completed jobs appears at zero, with the information
      message from the service layer (T4-PAY-05);
    * a technician-role account with no hourly rate is listed separately as
      excluded, naming them (T4-PAY-06);
    * an empty period totals zero and says so.

    An invalid range is a form error, not a zero total, because "nobody did any
    work" and "the dates were typed backwards" need different responses.
    """
    form = PayrollForm(request.GET or None)
    report = None
    default_start, default_end = services.default_payroll_range()

    if form.is_valid():
        chosen = form.cleaned_data.get("technician")
        try:
            report = services.calculate_payroll(
                form.cleaned_data["start_date"],
                form.cleaned_data["end_date"],
                technician_id=chosen.pk if chosen is not None else None,
            )
        except ServiceError as error:
            _service_error(request, error)
        else:
            # The exclusions are surfaced as messages as well as on the page, so an
            # administrator who prints or exports the report has still been told
            # that somebody was left out and why.
            for excluded in report["excluded"]:
                messages.warning(request, excluded["reason"])
            if report["job_count"] == 0:
                messages.info(
                    request,
                    "No repair jobs were completed in this period, so every total "
                    "is zero.",
                )

    return render(
        request,
        "core/payroll.html",
        {
            "identity": _identity(request),
            "form": form,
            "report": report,
            "technicians": Technician.objects.select_related("StaffID"),
            "default_start": default_start,
            "default_end": default_end,
        },
    )


# ---------------------------------------------------------------------------
# Customer repair status view (FR14, public)
# ---------------------------------------------------------------------------
def repair_status_lookup_view(request):
    """Public repair-progress lookup by booking reference (T4-STS-01..10).

    Named distinctly from Timebox 3's ``repair_status_view``, which is the
    staff-facing action that *changes* a job's status: this one only reads, is
    public, and shares no code with it. Keeping the names apart stops the two being
    confused in the URLconf.

    Deliberately has no ``@require_auth``: the customer has no account, and the
    reference code they were given at booking is the only credential this page asks
    for. That is also why the page never exposes staff names, internal notes or
    itemised costs - anyone holding a reference can read it, so it is built from the
    customer-facing subset only.

    Both GET (no lookup yet) and POST (lookup) render the same template. An unknown
    or invalid reference produces an error message and no status (T4-STS-10), using
    the same wording either way so the page cannot be used to discover which codes
    exist.
    """
    form = RepairStatusLookupForm()
    status = None

    # After booking, the customer is sent here with their reference pre-filled
    # rather than being made to retype it.
    preset = request.GET.get("ref", "").strip()
    if preset and request.method == "GET":
        form = RepairStatusLookupForm(initial={"reference_code": preset})
        status = services.get_customer_repair_status(preset)

    if request.method == "POST":
        form = RepairStatusLookupForm(request.POST)
        if form.is_valid():
            status = services.get_customer_repair_status(
                form.cleaned_data["reference_code"]
            )
        # An invalid form falls through with status None: the inline field error
        # (empty reference) is the explanation, and no status block is rendered.

    return render(
        request,
        "core/repair_status_public.html",
        {"form": form, "status": status},
    )


def customer_invoice_view(request, reference_code: str):
    """The customer's itemised invoice, opened from the repair-status result.

    Public for the same reason as the status lookup: the booking reference is the
    only credential the customer has. An invoice is shown only once the job is
    Completed and billed; every other case - including an unknown reference -
    gets the same not-found message and a 404, so the page reveals nothing about
    references that are not the visitor's own.
    """
    try:
        invoice = services.get_customer_invoice(reference_code)
    except NotFoundError as error:
        return render(
            request,
            "core/customer_invoice.html",
            {"invoice": None, "error": str(error)},
            status=404,
        )
    return render(request, "core/customer_invoice.html", {"invoice": invoice})


def customer_payment_view(request, reference_code: str):
    """Choose how to pay an outstanding invoice; only Cash is offered for now.

    Choosing Cash records the customer's choice and leaves the invoice Pending:
    the money changes hands at the counter, and only staff can mark a bill Paid.
    A Paid invoice has nothing left to pay, so it sends the customer back to the
    invoice instead of showing the form.
    """
    try:
        invoice = services.get_customer_invoice(reference_code)
    except NotFoundError as error:
        return render(
            request,
            "core/customer_invoice.html",
            {"invoice": None, "error": str(error)},
            status=404,
        )

    if invoice["is_paid"]:
        messages.info(request, "This invoice has already been paid - thank you.")
        return redirect("core:customer_invoice", reference_code=invoice["reference_code"])

    form = CustomerPaymentForm()
    if request.method == "POST":
        form = CustomerPaymentForm(request.POST)
        if form.is_valid():
            try:
                services.choose_customer_payment_method(
                    reference_code, form.cleaned_data["payment_method"]
                )
            except ServiceError as error:
                _service_error(request, error)
            else:
                messages.success(
                    request,
                    "Cash payment selected. Please pay "
                    f"{CURRENCY_CODE} {invoice['total_amount']} at the counter when "
                    "you collect your vehicle.",
                )
            return redirect(
                "core:customer_invoice", reference_code=invoice["reference_code"]
            )

    return render(
        request,
        "core/customer_payment.html",
        {"invoice": invoice, "form": form},
    )



