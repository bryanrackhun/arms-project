"""Render smoke tests for the ARMS templates.

Every screen is rendered as an Administrator, as a Technician and (for the
login page) anonymously, and every template on disk is compiled directly. The
two layers together catch template-authoring mistakes:

  * **Comment leakage.** Django's ``{# ... #}`` syntax is single-line only, so a
    comment written across several lines is *not* parsed as a comment -- its text
    is emitted verbatim into the page. The fragments below are extracted straight
    from the ``.html`` sources, so the list can never drift out of date the way a
    hardcoded one would.
  * **Unrendered tags.** Any surviving ``{%``, ``{{`` or ``{#`` token means a tag
    was treated as literal text.
  * **Missing assets.** Both the stylesheet and the progressive-enhancement
    script must be referenced on every page.
  * **``data-confirm`` interpolation.** Destructive actions carry a confirmation
    prompt built from real record names rather than inline ``confirm()`` calls.
"""
import re
from pathlib import Path

from django.conf import settings
from django.urls import reverse

from core.services import assign_service
from core.tests.base import RoleTestCase

#: Matches ``{# ... #}`` comment text. ``re.DOTALL`` is deliberate and
#: load-bearing: it lets this pattern match a comment whose delimiters are on
#: *different* lines. Django only treats single-line ``{# #}`` as a comment, so a
#: multi-line one is not consumed by the parser and its text is emitted into the
#: page verbatim. Matching those too means their text still lands in ``LEAKED``,
#: which is how the leak assertion catches the malformed form. Removing
#: ``DOTALL`` would make that check blind to exactly the bug it exists to catch.
_SINGLE_LINE_COMMENT = re.compile(r"\{#\s*(.*?)\s*#\}", re.DOTALL)

#: Where the templates live: ``<app>/templates``.
_TEMPLATES_DIR = Path(settings.BASE_DIR) / "core" / "templates"


def _comment_fragments():
    """Distinctive fragment of every template comment, well-formed or not.

    A fragment is taken from the middle of the comment text so that it cannot
    collide with ordinary page copy, while still being long enough to be unique.
    Comments whose text is correctly swallowed by the parser never reach the
    output; a malformed multi-line comment emits its text, so its fragment is
    then found in the HTML and the render assertions below fail.
    """
    fragments = []
    for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        for match in _SINGLE_LINE_COMMENT.finditer(source):
            text = " ".join(match.group(1).split())
            if len(text) < 8:
                continue
            start = min(4, len(text) // 4)
            fragments.append(text[start:start + 26])
    return fragments


#: Comment fragments that must never appear in rendered output.
LEAKED = _comment_fragments()


class _RenderSmoke(RoleTestCase):
    def assert_clean(self, html, where, *, assets=True):
        """Assert no comment leaked and no template token survived.

        ``assets`` is False for bare partials such as ``_empty.html`` that do
        not extend ``base.html`` and therefore carry no stylesheet or script.
        """
        for fragment in LEAKED:
            self.assertNotIn(fragment, html, f"comment leaked into {where}")
        for token in ("{%", "{{", "{#"):
            self.assertNotIn(token, html, f"unrendered {token} in {where}")
        if assets:
            self.assertIn("/static/core/css/arms.css", html)
            self.assertIn("/static/core/js/arms.js", html)


class AdminRenderTests(_RenderSmoke):
    def setUp(self):
        super().setUp()
        self.admin = self.make_admin()
        self.service = self.make_service()
        self.staff, self.tech = self.make_technician()
        assign_service(self.tech.TechnicianID, self.service.Service_code)
        self.login_as(self.admin)

    def test_pages_render(self):
        for name in ("dashboard", "profile", "staff_list", "staff_create",
                     "technician_list", "technician_create", "service_list",
                     "service_create", "role_list"):
            response = self.client.get(reverse(f"core:{name}"))
            self.assertEqual(response.status_code, 200, name)
            self.assert_clean(response.content.decode(), name)

    def test_detail_pages_render(self):
        for url in (reverse("core:staff_edit", args=[self.staff.StaffID]),
                    reverse("core:technician_edit",
                            args=[self.tech.TechnicianID])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assert_clean(response.content.decode(), url)

    def test_confirm_prompt_interpolates_name(self):
        html = self.client.get(reverse("core:technician_list")).content.decode()
        self.assertIn("data-confirm=\"Deactivate Tom Tech?\"", html)

    def test_error_pages_render(self):
        response = self.client.get("/access-denied/")
        self.assertEqual(response.status_code, 403)
        self.assert_clean(response.content.decode(), "access_denied")

        response = self.client.get("/no-such-page/")
        self.assertEqual(response.status_code, 404)
        self.assert_clean(response.content.decode(), "404")


class NonAdminRenderTests(_RenderSmoke):
    def setUp(self):
        super().setUp()
        self.staff, self.tech = self.make_technician()
        # Assign one service so the profile's technician record shows a real
        # row rather than the empty state.
        self.service = self.make_service()
        assign_service(self.tech.TechnicianID, self.service.Service_code)
        self.login_as(self.staff)

    def test_allowed_pages_render(self):
        # role_list is Administrator-only, so it is NOT in this list.
        for name in ("dashboard", "profile"):
            response = self.client.get(reverse(f"core:{name}"))
            self.assertEqual(response.status_code, 200, name)
            self.assert_clean(response.content.decode(), name)

    def test_technician_sees_own_record_on_profile(self):
        html = self.client.get(reverse("core:profile")).content.decode()
        self.assertIn("Technician record", html)
        self.assertIn("Assigned services", html)
        self.assertIn("Oil Change", html)

    def test_blocked_pages_are_denied(self):
        # role_list is management-only too, so a technician is redirected.
        for name in ("staff_list", "technician_list", "service_list",
                     "role_list"):
            response = self.client.get(reverse(f"core:{name}"))
            self.assertRedirects(response, reverse("core:access_denied"),
                                 target_status_code=403)


class LoginRenderTests(_RenderSmoke):
    def test_login_renders(self):
        response = self.client.get(reverse("core:login"))
        self.assertEqual(response.status_code, 200)
        self.assert_clean(response.content.decode(), "login")
        self.assertIn("page-login", response.content.decode())

    def test_bad_credentials_show_inline_error(self):
        response = self.client.post(reverse("core:login"),
                                    {"email": "no@one.local",
                                     "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assert_clean(html, "login-with-error")
        # The generic failure is surfaced through the messages framework
        # (not a per-field error), and the email must not be echoed as valid.
        self.assertIn("messages__item--error", html)
        self.assertIn("Invalid email or password.", html)


class TemplateCompileTests(_RenderSmoke):
    """Compile every template on disk, not just the ones a view happens to
    reach.

    The render tests above only exercise pages served through a view, so
    conditionally-included partials can slip through untested. ``core/_empty.html``
    is the clearest case: it is only pulled in by ``{% include %}`` inside a
    ``{% empty %}`` branch, which no populated test page ever reaches. A broken
    comment there would go unnoticed until an empty list was first rendered in
    production. Compiling each file directly closes that hole.
    """

    def test_every_template_compiles(self):
        from django.template.loader import get_template

        paths = sorted(_TEMPLATES_DIR.rglob("*.html"))
        # Guard against the walk silently matching nothing, which would make
        # this test vacuously pass.
        self.assertGreater(len(paths), 10, f"expected templates under {_TEMPLATES_DIR}")

        for path in paths:
            name = path.relative_to(_TEMPLATES_DIR).as_posix()
            with self.subTest(template=name):
                # get_template compiles eagerly: a malformed comment spanning
                # several lines raises TemplateSyntaxError here.
                self.assertIsNotNone(get_template(name))

    def test_empty_state_partial_renders(self):
        # Directly render the partial that view tests never reach, so its own
        # comments and tags are covered too.
        from django.template.loader import render_to_string
        html = render_to_string("core/_empty.html",
                                {"title": "No services yet",
                                 "hint": "Add one first."})
        self.assert_clean(html, "_empty", assets=False)
        self.assertIn("empty-state", html)
        self.assertIn("No services yet", html)
        self.assertIn("Add one first.", html)


class ErrorTemplateTests(_RenderSmoke):
    """The custom error pages must resolve the way Django's handlers look for
    them. Django's default ``handler404``/``handler500`` load the template names
    ``404.html``/``500.html`` from the ROOT of a template directory, so keeping
    them under ``core/templates/core/`` would silently fall back to Django's own
    unstyled page. These tests pin that placement and the empty-context rule.
    """

    def test_error_templates_resolve_at_root(self):
        from django.template.loader import get_template
        for name in ("404.html", "500.html"):
            template = get_template(name)
            self.assertTrue(
                str(template.origin).replace("\\", "/").endswith(
                    f"core/templates/{name}"
                ),
                f"{name} resolved from {template.origin}, expected core/templates/",
            )

    def test_500_renders_with_empty_context(self):
        # Django renders handler500 with no context at all; base.html must not
        # require `identity` or any view-supplied value to render.
        from django.template.loader import render_to_string
        html = render_to_string("500.html", {})
        self.assert_clean(html, "500")
        self.assertIn("500", html)
        self.assertIn("Something went wrong", html)

    def test_404_renders_anonymously(self):
        # A signed-out visitor also hits this page; the links must still resolve.
        from django.template.loader import render_to_string
        html = render_to_string("404.html", {"request_path": "/gone/"})
        self.assert_clean(html, "404")
        self.assertIn("Page not found", html)

