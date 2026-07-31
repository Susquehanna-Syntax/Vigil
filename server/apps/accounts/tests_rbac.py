"""Per-site role resolution and the capability matrix.

Every branch of role_of() is a security boundary, so the resolution table in
docs/rbac.md §1.1 is tested exhaustively rather than by sampling.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import (
    CAPABILITIES, NONE, OWNER, Capability, can, role_of,
)
from apps_business.sites.models import Site, UserSiteRole


def mkuser(name, **kw):
    return get_user_model().objects.create_user(name, password="x", **kw)


class CapabilityVocabularyTests(TestCase):
    """A typo in a permission name must fail loudly, not deny silently."""

    def test_every_app_declares_a_frozen_verb_set(self):
        self.assertTrue(CAPABILITIES)
        for app, verbs in CAPABILITIES.items():
            self.assertIsInstance(verbs, frozenset, app)
            self.assertIn("view", verbs, f"{app} must have a view verb")

    def test_unknown_app_raises(self):
        with self.assertRaises(ValueError):
            can(mkuser("a"), None, "nonsense", "view")

    def test_unknown_verb_raises(self):
        with self.assertRaises(ValueError):
            can(mkuser("b"), None, "hosts", "teleport")

    def test_capability_helper_validates_too(self):
        with self.assertRaises(ValueError):
            Capability("hosts", "teleport")
        self.assertEqual(Capability("hosts", "view").as_tuple(), ("hosts", "view"))


class RoleResolutionTests(TestCase):
    """docs/rbac.md §1.1, one test per branch."""

    def setUp(self):
        self.glob = Site.objects.global_site()
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")

    # -- branch 1: owner ---------------------------------------------------

    def test_superuser_is_owner_everywhere(self):
        u = mkuser("root", is_superuser=True)
        self.assertEqual(role_of(u, self.west), OWNER)
        self.assertEqual(role_of(u, None), OWNER)

    def test_owner_beats_any_row(self):
        u = mkuser("root2", is_superuser=True)
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.VIEWER)
        self.assertEqual(role_of(u, self.west), OWNER)

    # -- branch 3: no rows, unchanged from today ---------------------------

    def test_staff_with_no_rows_is_admin_everywhere(self):
        u = mkuser("staff", is_staff=True)
        self.assertEqual(role_of(u, self.west), Role.ADMIN)
        self.assertEqual(role_of(u, None), Role.ADMIN)

    def test_no_profile_and_no_rows_is_viewer(self):
        self.assertEqual(role_of(mkuser("nobody"), self.west), Role.VIEWER)

    def test_profile_role_with_no_rows_applies_everywhere(self):
        u = mkuser("dana")
        UserProfile.objects.update_or_create(user=u, defaults={"role": Role.OPERATOR})
        self.assertEqual(role_of(u, self.west), Role.OPERATOR)
        self.assertEqual(role_of(u, self.lab), Role.OPERATOR)

    def test_anonymous_has_no_role(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(role_of(AnonymousUser(), self.west), "")

    # -- branch 4: a row for this site -------------------------------------

    def test_row_for_this_site_wins(self):
        u = mkuser("dana2")
        UserProfile.objects.update_or_create(user=u, defaults={"role": Role.VIEWER})
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.OPERATOR)
        self.assertEqual(role_of(u, self.west), Role.OPERATOR)

    # -- branch 5: the global floor ----------------------------------------

    def test_global_row_is_the_floor_for_other_sites(self):
        u = mkuser("dana3")
        UserSiteRole.objects.create(user=u, site=self.glob, role=Role.VIEWER)
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.OPERATOR)
        self.assertEqual(role_of(u, self.west), Role.OPERATOR)
        self.assertEqual(role_of(u, self.lab), Role.VIEWER)

    def test_global_floor_covers_a_site_created_later(self):
        u = mkuser("dana4")
        UserSiteRole.objects.create(user=u, site=self.glob, role=Role.OPERATOR)
        newer = Site.objects.create(name="HQ", slug="hq")
        self.assertEqual(role_of(u, newer), Role.OPERATOR)

    # -- branch 6: scoped, but not to here ---------------------------------

    def test_rows_elsewhere_and_none_here_is_no_access(self):
        """The sharp edge: granting a first site role narrows access."""
        u = mkuser("dana5")
        UserProfile.objects.update_or_create(user=u, defaults={"role": Role.ADMIN})
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.OPERATOR)
        self.assertEqual(role_of(u, self.lab), NONE)

    def test_scoped_user_asking_about_no_site_uses_their_best_role(self):
        """Unscoped areas (settings, accounts) must still resolve for someone
        who holds site rows — otherwise scoping a user locks them out of
        their own account page."""
        u = mkuser("dana6")
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.ADMIN)
        self.assertEqual(role_of(u, None), Role.ADMIN)


class CanTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")

    def test_owner_can_do_everything(self):
        u = mkuser("root3", is_superuser=True)
        for app, verbs in CAPABILITIES.items():
            for verb in verbs:
                self.assertTrue(can(u, self.west, app, verb), f"{app}:{verb}")

    def test_admin_can_do_everything_in_their_sites_only(self):
        u = mkuser("adm")
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.ADMIN)
        self.assertTrue(can(u, self.west, "hosts", "delete"))
        self.assertFalse(can(u, self.lab, "hosts", "view"))

    def test_viewer_gets_view_verbs_only(self):
        u = mkuser("vw")
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.VIEWER)
        self.assertTrue(can(u, self.west, "hosts", "view"))
        self.assertFalse(can(u, self.west, "hosts", "edit"))
        self.assertFalse(can(u, self.west, "alerts", "ack"))

    def test_operator_gets_exactly_what_the_matrix_grants(self):
        from apps_business.sites.models import SiteCapability
        u = mkuser("op")
        row = UserSiteRole.objects.create(user=u, site=self.west, role=Role.OPERATOR)
        SiteCapability.objects.create(user_site_role=row, app="baselines", verb="run")
        self.assertTrue(can(u, self.west, "baselines", "run"))
        self.assertFalse(can(u, self.west, "baselines", "edit"))
        self.assertFalse(can(u, self.west, "hosts", "approve"))

    def test_operator_with_an_empty_matrix_can_do_nothing(self):
        u = mkuser("op2")
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.OPERATOR)
        self.assertFalse(can(u, self.west, "hosts", "view"))

    def test_no_access_role_can_do_nothing(self):
        u = mkuser("op3")
        UserSiteRole.objects.create(user=u, site=self.west, role=Role.OPERATOR)
        self.assertFalse(can(u, self.lab, "hosts", "view"))

    def test_unscoped_check_uses_the_unscoped_role(self):
        u = mkuser("staff2", is_staff=True)
        self.assertTrue(can(u, None, "tasks", "view"))


class LegacyRoleOfTests(TestCase):
    """The zero-argument call must keep behaving exactly as it does today, so
    no existing caller changes in this step."""

    def test_staff_is_still_admin(self):
        self.assertEqual(role_of(mkuser("s1", is_staff=True)), Role.ADMIN)

    def test_superuser_is_still_admin_shaped_for_legacy_callers(self):
        u = mkuser("s2", is_superuser=True)
        self.assertIn(role_of(u), (Role.ADMIN, OWNER))

    def test_plain_user_is_still_viewer(self):
        self.assertEqual(role_of(mkuser("s3")), Role.VIEWER)
