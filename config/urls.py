from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts import views as account_views
from apps.attendance.views import AttendanceReportView, SessionViewSet
from apps.audit.views import AuditLogViewSet
from apps.core.views import HealthView, SiteConfigView
from apps.documents.views import DocumentTemplateViewSet
from apps.events import views as event_views
from apps.finance import views as finance_views
from apps.inventory import views as inventory_views
from apps.members import views as member_views
from apps.organizations import views as org_views
from apps.permissions.views import PermissionCatalogView, RoleViewSet
from apps.units.views import PatrolViewSet, UnitViewSet

router = DefaultRouter()
router.register("users", org_views.MemberViewSet, basename="member")
router.register("units", UnitViewSet, basename="unit")
router.register("roles", RoleViewSet, basename="role")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")
router.register("members", member_views.MemberViewSet, basename="person")
router.register("custom-fields", member_views.CustomFieldViewSet, basename="custom-field")
router.register("change-requests", member_views.ChangeRequestViewSet, basename="change-request")
router.register("patrols", PatrolViewSet, basename="patrol")
router.register("attendance/sessions", SessionViewSet, basename="session")
router.register("event-types", event_views.EventTypeViewSet, basename="event-type")
router.register("events", event_views.EventViewSet, basename="event")
router.register("document-templates", DocumentTemplateViewSet, basename="document-template")
router.register("inventory/categories", inventory_views.CategoryViewSet, basename="inventory-category")
router.register("inventory/locations", inventory_views.LocationViewSet, basename="inventory-location")
router.register("inventory/external-borrowers", inventory_views.ExternalBorrowerViewSet, basename="inventory-external")
router.register("inventory/items", inventory_views.ItemViewSet, basename="inventory-item")
router.register("inventory/loans", inventory_views.LoanViewSet, basename="inventory-loan")
router.register("inventory/stocktakes", inventory_views.StockTakeViewSet, basename="inventory-stocktake")
router.register("finance/currencies", finance_views.CurrencyViewSet, basename="finance-currency")
router.register("finance/categories", finance_views.CategoryViewSet, basename="finance-category")
router.register("finance/entries", finance_views.EntryViewSet, basename="finance-entry")

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

family_patterns = [
    path("", member_views.MyFamilyView.as_view(), name="my-family"),
    path("children/", member_views.MyChildrenView.as_view(), name="my-children"),
    path("children/<int:pk>/", member_views.MyChildView.as_view(), name="my-child"),
    path("requests/", member_views.MyRequestsView.as_view(), name="my-requests"),
]

my_event_patterns = [
    path("", event_views.MyEventsView.as_view(), name="my-events"),
    path("invitations/<int:pk>/respond/", event_views.MyInvitationRespondView.as_view(), name="my-event-respond"),
    path("invitations/<int:pk>/sign/", event_views.MyInvitationSignView.as_view(), name="my-event-sign"),
]

api_v1 = [
    path("site/", SiteConfigView.as_view(), name="site-config"),
    path("guardians/options/", member_views.GuardianOptionsView.as_view(), name="guardian-options"),
    path("guardians/contacts/", member_views.GuardianContactsView.as_view(), name="guardian-contacts"),
    path("my/family/", include(family_patterns)),
    path("my/events/", include(my_event_patterns)),
    path("my/calendar/", event_views.MyCalendarView.as_view(), name="my-calendar"),
    path("calendar/<str:token>.ics", event_views.CalendarFeedView.as_view(), name="calendar-feed"),
    path("attendance/report/", AttendanceReportView.as_view(), name="attendance-report"),
    path("inventory/summary/", inventory_views.SummaryView.as_view(), name="inventory-summary"),
    path("finance/summary/", finance_views.SummaryView.as_view(), name="finance-summary"),
    path("finance/events/", finance_views.EventOptionsView.as_view(), name="finance-events"),
    path("inventory/lookup/", inventory_views.LookupView.as_view(), name="inventory-lookup"),
    path("inventory/borrowers/", inventory_views.BorrowerOptionsView.as_view(), name="inventory-borrowers"),
    path("events/<int:event_id>/gear/", inventory_views.EventGearView.as_view(), name="event-gear"),
    path(
        "events/<int:event_id>/gear/check-out/",
        inventory_views.EventGearCheckOutView.as_view(),
        name="event-gear-checkout",
    ),
    path("my/gear/", inventory_views.MyGearView.as_view(), name="my-gear"),
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
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
