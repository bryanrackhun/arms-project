"""Django admin registration for the ARMS tables."""

from django.contrib import admin

from .models import (
    Appointment,
    Inspection,
    Repair,
    Role,
    Service,
    ServiceBay,
    Staff,
    Technician,
    Vehicle,
    generate_reference_code,
)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("RoleID", "RoleName")
    search_fields = ("RoleName",)


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ("StaffID", "FirstName", "LastName", "Email", "RoleID", "IsActive")
    list_filter = ("IsActive", "RoleID")
    search_fields = ("FirstName", "LastName", "Email")
    # Password holds a hash; it is never displayed or edited in the admin.
    exclude = ("Password",)


@admin.register(Technician)
class TechnicianAdmin(admin.ModelAdmin):
    list_display = ("TechnicianID", "StaffID", "Hourly_rate", "assigned_services")
    search_fields = ("StaffID__FirstName", "StaffID__LastName")

    @admin.display(description="Assigned services")
    def assigned_services(self, obj):
        """Technician_Service rows (composite PK, so not admin-registerable)."""
        return ", ".join(
            obj.service_assignments.values_list("ServiceCode__ServiceName", flat=True)
        ) or "—"


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("Service_code", "ServiceName", "BaseCost", "Est_Duration")
    search_fields = ("ServiceName",)


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("VehicleID", "LicensePlate", "VIN", "Make", "Model", "Year")
    search_fields = ("LicensePlate", "VIN", "Make", "Model")
    list_filter = ("Make",)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = (
        "AppointmentID",
        "ReferenceCode",
        "customer_name",
        "ServiceCode",
        "AppointmentDate",
        "AppointmentTime",
        "Status",
        "VehicleID",
    )
    list_filter = ("Status", "AppointmentDate")
    search_fields = (
        "ReferenceCode",
        "CustomerFirstName",
        "CustomerLastName",
        "CustomerEmail",
        "CustomerPhone",
    )
    date_hierarchy = "AppointmentDate"
    raw_id_fields = ("VehicleID",)
    # customer_name is a read-only model property, not a database field. The
    # reference code is shown but never editable: the customer holds it.
    readonly_fields = ("customer_name", "ReferenceCode")

    def save_model(self, request, obj, form, change):
        # An appointment added here bypasses ``book_appointment``, and the
        # code column is unique, so give it a code rather than a blank.
        if not change and not obj.ReferenceCode:
            obj.ReferenceCode = generate_reference_code()
        super().save_model(request, obj, form, change)


# TechnicianService is not registered: Django admin rejects composite primary
# keys. Assignments are shown via TechnicianAdmin.assigned_services above.


@admin.register(ServiceBay)
class ServiceBayAdmin(admin.ModelAdmin):
    list_display = ("BayID", "BayName", "Status")
    list_filter = ("Status",)
    search_fields = ("BayName",)
    # Status is derived from the repair job the bay holds, so editing it here
    # would let it disagree with that job.
    readonly_fields = ("Status",)


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("InspectionID", "VehicleID", "image_count", "CreatedAt")
    search_fields = ("Description", "VehicleID__LicensePlate", "VehicleID__VIN")
    raw_id_fields = ("VehicleID",)
    readonly_fields = ("CreatedAt", "UpdatedAt")


@admin.register(Repair)
class RepairAdmin(admin.ModelAdmin):
    list_display = (
        "RepairID",
        "AppointmentID",
        "customer_name",
        "RepairPriority",
        "TechnicianID",
        "BayID",
        "Status",
    )
    list_filter = ("Status", "RepairPriority")
    search_fields = (
        "AppointmentID__CustomerFirstName",
        "AppointmentID__CustomerLastName",
    )
    raw_id_fields = ("AppointmentID", "TechnicianID", "InspectionID", "BayID")
    # customer_name is read through the appointment, not stored on the job.
    readonly_fields = ("customer_name", "CreatedAt", "UpdatedAt")

