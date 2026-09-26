"""View decorators that gate routes on authentication and role.

Authorisation is decided from the role held in the server-side session. The
session lives on the server and the browser only carries an opaque cookie
pointing at it, so a user cannot widen their own permissions by editing the
request — which is why the role is never read from a form field or query
string.

The full role/permission matrix is documented in ARCHITECTURE.md.
"""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .models import Role
from .services import (
    SESSION_KEY_ROLE_NAME,
    SESSION_KEY_STAFF_ID,
    AuthorizationError,
)

#: Roles allowed to manage roles, staff, technicians and service assignments.
ADMIN_ONLY_ROLES = frozenset({Role.ADMINISTRATOR})


def _session_identity(request):
    """Return ``(staff_id, role_name)`` from the session, or ``(None, None)``."""
    staff_id = request.session.get(SESSION_KEY_STAFF_ID)
    role_name = request.session.get(SESSION_KEY_ROLE_NAME)
    if not staff_id:
        return None, None
    return staff_id, role_name


def require_auth(view_func):
    """Redirect to the login page when no valid session exists.

    Applied to every route that reads or writes staff data, so an expired or
    absent session is caught before the view body runs rather than part way
    through it. The original path is carried in ``?next=`` so the user lands
    back where they were headed after logging in.
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        staff_id, _role = _session_identity(request)
        if staff_id is None:
            next_url = request.get_full_path()
            login_url = reverse("core:login")
            if next_url and next_url != login_url:
                login_url = f"{login_url}?next={next_url}"
            return redirect(login_url)
        return view_func(request, *args, **kwargs)

    return _wrapped


def require_role(*role_names):
    """Restrict a view to the given role name(s).

    Must be stacked on top of :func:`require_auth`. The two decorators produce
    deliberately different outcomes: a visitor with no session is sent to the
    login page, while a signed-in user with the wrong role is sent to the
    access-denied page. Sending the latter to login would just loop, since
    logging in again would not change their role.
    """
    allowed = frozenset(role_names)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            _staff_id, role_name = _session_identity(request)
            if role_name not in allowed:
                messages.error(
                    request,
                    "Access denied: your role does not have permission to use "
                    "this function.",
                )
                return redirect("core:access_denied")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


#: Convenience decorator for the Administrator-only management screens.
admin_only = require_role(Role.ADMINISTRATOR)

#: Roles allowed to manage appointments and vehicle records (Timebox 2).
#: Receptionists run the booking desk; administrators keep full access so they
#: can supervise and correct records. Technicians get read-only vehicle history
#: through a separate allowance rather than this decorator.
BOOKING_STAFF_ROLES = frozenset({Role.RECEPTIONIST, Role.ADMINISTRATOR})

#: Convenience decorator for the appointment and vehicle screens.
booking_staff = require_role(Role.RECEPTIONIST, Role.ADMINISTRATOR)

#: Vehicle detail (with appointment/repair history) is also open to
#: technicians, who need to see the history of a vehicle they are assigned to
#: (FR5). Editing, check-in and matching stay with the booking staff.
vehicle_viewer = require_role(
    Role.RECEPTIONIST, Role.ADMINISTRATOR, Role.TECHNICIAN
)


def enforce_admin(request) -> None:
    """Raise instead of redirecting, for code paths outside the view layer.

    Decorators can only be used on views, so any other entry point that needs
    the same Administrator check calls this. Raising rather than returning a
    response keeps the guard usable from service code.
    """
    _staff_id, role_name = _session_identity(request)
    if role_name not in ADMIN_ONLY_ROLES:
        raise AuthorizationError(
            "Administrator role is required for this operation."
        )


# ---------------------------------------------------------------------------
# Timebox 3 - repair job roles
# ---------------------------------------------------------------------------
#: Repair jobs are created, assigned and re-assigned by the Administrator alone.
#: The booking desk's part of the story ended at check-in (Timebox 2), so a
#: Receptionist cannot open or alter a job - only read one.
repair_manager = require_role(Role.ADMINISTRATOR)

#: Who may open a repair job page at all. Administrators manage every job;
#: technicians see the ones assigned to them; receptionists get read-only
#: visibility so they can answer a customer asking "how is my car coming along?"
#: at the counter. Which of these may *write* is decided per job in the view,
#: through ``services.can_manage_repair``, because it depends on the assignment
#: rather than on the role alone.
REPAIR_VIEWER_ROLES = frozenset(
    {Role.ADMINISTRATOR, Role.RECEPTIONIST, Role.TECHNICIAN}
)

#: Convenience decorator for the repair job and inspection screens.
repair_viewer = require_role(
    Role.ADMINISTRATOR, Role.RECEPTIONIST, Role.TECHNICIAN
)

#: Service bays are workshop infrastructure, so only the Administrator manages
#: the records themselves (FR9).
bay_manager = require_role(Role.ADMINISTRATOR)


# ---------------------------------------------------------------------------
# Timebox 4 - inventory, invoicing, payroll and the customer status view
# ---------------------------------------------------------------------------
#: Who may read the parts catalogue and its stock levels (T4-INV-02). All three
#: workshop roles need it, but for different reasons: the Administrator manages the
#: catalogue, the Technician checks what is on the shelf before starting a job, and
#: the Receptionist answers a customer asking whether a part is available. Reading
#: is therefore the widest allowance in this timebox - writing is much narrower.
PARTS_VIEWER_ROLES = frozenset(
    {Role.ADMINISTRATOR, Role.RECEPTIONIST, Role.TECHNICIAN}
)

#: Convenience decorator for the parts list.
parts_viewer = require_role(Role.ADMINISTRATOR, Role.RECEPTIONIST, Role.TECHNICIAN)

#: The catalogue itself - adding, editing, deactivating parts, the low-stock
#: review page and usage history - is Administrator-only (T4-INV-01/03/04/10/11).
#: A technician may draw stock against a job but may not change what the stock is.
inventory_manager = require_role(Role.ADMINISTRATOR)

#: Raising a bill and taking payment belongs to the counter: the Receptionist, with
#: the Administrator retaining access to supervise and correct (T4-INV-20/26/28).
#: A Technician is deliberately excluded - the workflow rules state plainly that a
#: technician cannot view invoices, since a bill shows what the workshop charges
#: rather than what the work involved.
INVOICE_ROLES = frozenset({Role.RECEPTIONIST, Role.ADMINISTRATOR})

#: Convenience decorator for the invoice screens.
invoice_staff = require_role(Role.RECEPTIONIST, Role.ADMINISTRATOR)

#: Payroll is an Administrator function (T4-PAY-01): it reports what the workshop
#: owes its own staff, which is neither a counter task nor something a technician
#: should see for their colleagues.
payroll_manager = require_role(Role.ADMINISTRATOR)
