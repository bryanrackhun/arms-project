"""Test suite for the ARMS core app.

Django's test runner discovers every ``test_*.py`` module in this package
automatically, so no re-exports are needed here.

Modules:
    * base.py               - shared fixtures (roles, staff, technicians)
    * test_models.py        - database connection, tables, constraints
    * test_services.py      - staff/technician CRUD, assignments, validation
    * test_auth.py          - login, invalid login, logout
    * test_authorization.py - role permissions and unauthorized access
    * test_views.py         - end-to-end UI flows writing to the database
    * test_forms.py         - form-level validation
    * test_render.py        - template render smoke tests (leaks, assets, errors)
    * test_booking_vehicle.py - booking collects the car model + plate, links a Vehicle
    * test_pending_accept.py - Timebox 2: booking requests start Pending
    * test_smoke_tb2.py     - Timebox 2: every screen renders per role
    * test_timebox3.py      - Timebox 3: repairs, inspections, bays, progress
"""

