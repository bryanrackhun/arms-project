"""URL configuration for the ``core`` app.

Route protection:
    * ``home`` / ``login`` / ``logout`` / ``access_denied`` are public. ``home``
      is the landing page: it sends customers to the booking form and staff to
      the login page, and forwards an already signed-in user to the dashboard.
    * ``booking`` / ``booking_success`` are public (online booking, Timebox 2).
    * ``dashboard`` and ``profile`` require a valid session (any role).
    * ``appointments/*`` and ``vehicles/*`` require Receptionist or
      Administrator (``booking_staff``); ``vehicles/<id>/`` is also open to
      Technicians for history viewing (``vehicle_viewer``).
    * ``repairs/*`` are open to Administrator, Receptionist and Technician
      (``repair_viewer``); creating and re-assigning a job is Administrator-only
      (``repair_manager``), and acting on a specific job additionally requires
      that job to be assigned to the signed-in technician (checked per job in
      the view, via ``services.can_manage_repair``). ``my-repairs/`` is the
      Technician's own board.
    * ``bays/*`` are Administrator-only (``bay_manager``).
    * Everything else is Administrator-only, enforced by the ``admin_only``
      decorator in :mod:`core.decorators`.
"""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    # public landing page: booking entry point for customers, link to login
    path("", views.home_view, name="home"),

    # authentication
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("access-denied/", views.access_denied_view, name="access_denied"),

    # dashboard + own profile (all roles)
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("profile/", views.profile_view, name="profile"),

    # roles (Administrator)
    path("roles/", views.role_list_view, name="role_list"),

    # staff accounts (Administrator)
    path("staff/", views.staff_list_view, name="staff_list"),
    path("staff/new/", views.staff_create_view, name="staff_create"),
    path("staff/<int:staff_id>/edit/", views.staff_edit_view, name="staff_edit"),
    path(
        "staff/<int:staff_id>/deactivate/",
        views.staff_deactivate_view,
        name="staff_deactivate",
    ),
    path(
        "staff/<int:staff_id>/reactivate/",
        views.staff_reactivate_view,
        name="staff_reactivate",
    ),

    # technicians (Administrator)
    path("technicians/", views.technician_list_view, name="technician_list"),
    path("technicians/new/", views.technician_create_view, name="technician_create"),
    path(
        "technicians/<int:technician_id>/edit/",
        views.technician_edit_view,
        name="technician_edit",
    ),
    path(
        "technicians/<int:technician_id>/deactivate/",
        views.technician_deactivate_view,
        name="technician_deactivate",
    ),

    # technician-service assignments (Administrator)
    path(
        "technicians/<int:technician_id>/services/assign/",
        views.technician_assign_service_view,
        name="technician_assign_service",
    ),
    path(
        "technicians/<int:technician_id>/services/<int:service_code>/remove/",
        views.technician_remove_service_view,
        name="technician_remove_service",
    ),

    # services (Administrator)
    path("services/", views.service_list_view, name="service_list"),
    path("services/new/", views.service_create_view, name="service_create"),

    # ------------------------------------------------------------------
    # Timebox 2 - appointments and vehicles
    # ------------------------------------------------------------------
    # public booking (no login required)
    path("book/", views.booking_view, name="booking"),
    path("book/done/", views.booking_success_view, name="booking_success"),

    # appointments (Receptionist, Administrator)
    path("appointments/", views.appointment_list_view, name="appointment_list"),
    path(
        "appointments/new/",
        views.appointment_create_view,
        name="appointment_create",
    ),
    path(
        "appointments/<int:appointment_id>/",
        views.appointment_detail_view,
        name="appointment_detail",
    ),
    path(
        "appointments/<int:appointment_id>/edit/",
        views.appointment_edit_view,
        name="appointment_edit",
    ),
    path(
        "appointments/<int:appointment_id>/accept/",
        views.appointment_accept_view,
        name="appointment_accept",
    ),
    path(
        "appointments/<int:appointment_id>/status/",
        views.appointment_status_view,
        name="appointment_status",
    ),
    path(
        "appointments/<int:appointment_id>/cancel/",
        views.appointment_cancel_view,
        name="appointment_cancel",
    ),
    path(
        "appointments/<int:appointment_id>/match-vehicle/",
        views.appointment_match_vehicle_view,
        name="appointment_match_vehicle",
    ),

    # vehicles (Receptionist, Administrator; detail also open to Technicians)
    path("vehicles/", views.vehicle_list_view, name="vehicle_list"),
    path("vehicles/new/", views.vehicle_create_view, name="vehicle_create"),
    path("vehicles/check-in/", views.vehicle_check_in_view, name="vehicle_check_in"),
    path(
        "vehicles/<int:vehicle_id>/",
        views.vehicle_detail_view,
        name="vehicle_detail",
    ),
    path(
        "vehicles/<int:vehicle_id>/edit/",
        views.vehicle_edit_view,
        name="vehicle_edit",
    ),
    path(
        "vehicles/<int:vehicle_id>/delete/",
        views.vehicle_delete_view,
        name="vehicle_delete",
    ),

    # ------------------------------------------------------------------
    # Timebox 3 - repair jobs, inspections and service bays
    # ------------------------------------------------------------------
    # repair jobs (Administrator manages; Receptionist reads; a Technician is
    # sent to their own board)
    path("repairs/", views.repair_list_view, name="repair_list"),
    path("repairs/new/", views.repair_create_view, name="repair_create"),
    path(
        "repairs/<int:repair_id>/",
        views.repair_detail_view,
        name="repair_detail",
    ),
    path(
        "repairs/<int:repair_id>/status/",
        views.repair_status_view,
        name="repair_status",
    ),
    path(
        "repairs/<int:repair_id>/assign/",
        views.repair_assign_view,
        name="repair_assign",
    ),

    # technician's own board (Technician)
    path("my-repairs/", views.my_repairs_view, name="my_repairs"),

    # vehicle inspection, keyed by repair job (assigned Technician, Administrator)
    path(
        "repairs/<int:repair_id>/inspection/",
        views.inspection_create_view,
        name="inspection_create",
    ),
    path(
        "repairs/<int:repair_id>/inspection/edit/",
        views.inspection_edit_view,
        name="inspection_edit",
    ),

    # service bays (Administrator)
    path("bays/", views.bay_list_view, name="bay_list"),
    path("bays/new/", views.bay_create_view, name="bay_create"),
    path("bays/<int:bay_id>/edit/", views.bay_edit_view, name="bay_edit"),
    path("bays/<int:bay_id>/delete/", views.bay_delete_view, name="bay_delete"),
]
