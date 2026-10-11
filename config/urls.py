from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts import views as account_views
from apps.audit.views import AuditLogViewSet
from apps.organizations import views as org_views
from apps.permissions.views import PermissionCatalogView, RoleViewSet
from apps.units.views import UnitViewSet

router = DefaultRouter()
router.register("users", org_views.MemberViewSet, basename="member")
router.register("units", UnitViewSet, basename="unit")
router.register("roles", RoleViewSet, basename="role")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

auth_patterns = [
    path("login/", account_views.LoginView.as_view(), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("logout/", account_views.LogoutView.as_view(), name="logout"),
    path("signup/", org_views.SignupView.as_view(), name="signup"),
    path("password-reset/", account_views.PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset/confirm/", account_views.PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("verify-email/", account_views.VerifyEmailView.as_view(), name="verify-email"),
    path("invitations/<str:key>/", org_views.InvitationPublicView.as_view(), name="invitation-detail"),
    path("invitations/<str:key>/accept/", org_views.AcceptInvitationView.as_view(), name="invitation-accept"),
]

me_patterns = [
    path("", account_views.MeView.as_view(), name="me"),
    path("password/", account_views.ChangePasswordView.as_view(), name="me-password"),
    path("resend-verification/", account_views.ResendVerificationView.as_view(), name="me-resend-verification"),
    path("organizations/", org_views.MyOrganizationsView.as_view(), name="me-organizations"),
    path("permissions/", org_views.MyPermissionsView.as_view(), name="me-permissions"),
]

api_v1 = [
    path("auth/", include(auth_patterns)),
    path("me/", include(me_patterns)),
    path("org/", org_views.CurrentOrganizationView.as_view(), name="org"),
    path("org/settings/", org_views.OrganizationSettingsView.as_view(), name="org-settings"),
    path("dashboard/summary/", org_views.DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("permissions/catalog/", PermissionCatalogView.as_view(), name="permission-catalog"),
    path("", include(router.urls)),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
