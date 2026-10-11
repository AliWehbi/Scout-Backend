"""
Resolving the active organization for a request.

The frontend sends the selected organization in the `X-Organization-ID` header. It is
only honoured when the user has an *active* membership there, so one tenant can never
read or write another tenant's data. Users with exactly one membership may omit it.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import NotAuthenticated, PermissionDenied

ORG_HEADER = "HTTP_X_ORGANIZATION_ID"


def get_active_membership(request):
    if hasattr(request, "_active_membership"):
        return request._active_membership

    from apps.organizations.models import MembershipStatus, OrgMembership

    user = request.user
    if not user or not user.is_authenticated:
        raise NotAuthenticated()

    memberships = OrgMembership.objects.select_related("organization", "user").filter(
        user=user, status=MembershipStatus.ACTIVE, organization__is_active=True
    )
    org_id = request.META.get(ORG_HEADER, "").strip()
    try:
        if org_id:
            membership = memberships.filter(organization_id=org_id).first()
        else:
            candidates = list(memberships[:2])
            membership = candidates[0] if len(candidates) == 1 else None
            if membership is None and candidates:
                raise PermissionDenied(_("Select an organization (X-Organization-ID header)."), code="org_required")
    except DjangoValidationError:  # malformed UUID
        membership = None

    if membership is None:
        raise PermissionDenied(_("You are not a member of this organization."), code="not_member")

    request._active_membership = membership
    return membership


class TenantScopedMixin:
    """
    For viewsets over `TenantModel` subclasses: filters every queryset by the active
    organization and stamps it on create.
    """

    @property
    def membership(self):
        return get_active_membership(self.request)

    @property
    def organization(self):
        return self.membership.organization

    def get_queryset(self):
        return super().get_queryset().filter(organization=self.organization)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.request and self.request.user.is_authenticated:
            context["organization"] = self.organization
            context["membership"] = self.membership
        return context

    def perform_create(self, serializer):
        serializer.save(organization=self.organization)
