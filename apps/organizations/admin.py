from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model, password_validation
from rest_framework import serializers

from apps.accounts.serializers import validate_email_value, validate_username_value
from apps.audit.services import log_action

from . import services
from .models import Invitation, Organization, OrganizationSettings, OrgMembership

User = get_user_model()


class SettingsInline(admin.StackedInline):
    model = OrganizationSettings


class OrganizationAddForm(forms.ModelForm):
    """
    Adding an organization here sets it up exactly like the public sign-up does
    (settings, default roles and event types, owner account), which is how
    superusers onboard organizations when public sign-up is turned off.
    """

    owner_first_name = forms.CharField(label="First name", max_length=150)
    owner_last_name = forms.CharField(label="Last name", max_length=150, required=False)
    owner_username = forms.CharField(label="Username", max_length=150)
    owner_email = forms.EmailField(label="Email", required=False)
    owner_password = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput(render_value=True))

    class Meta:
        model = Organization
        fields = ["name", "country", "currency", "timezone", "default_language"]

    def _drf_clean(self, validator, field):
        try:
            return validator(self.cleaned_data.get(field))
        except serializers.ValidationError as exc:
            raise forms.ValidationError([str(e) for e in exc.detail]) from exc

    def clean_owner_username(self):
        return self._drf_clean(validate_username_value, "owner_username")

    def clean_owner_email(self):
        return self._drf_clean(validate_email_value, "owner_email")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("owner_username") and cleaned.get("owner_password"):
            user = User(
                username=cleaned["owner_username"],
                email=cleaned.get("owner_email"),
                first_name=cleaned.get("owner_first_name", ""),
            )
            try:
                password_validation.validate_password(cleaned["owner_password"], user)
            except forms.ValidationError as exc:
                self.add_error("owner_password", exc)
        return cleaned


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "country", "default_language", "is_active", "created_at")
    search_fields = ("name", "slug")
    inlines = [SettingsInline]
    add_fieldsets = (
        (None, {"fields": ("name", "country", "currency", "timezone", "default_language")}),
        (
            "Owner (first admin)",
            {"fields": ("owner_first_name", "owner_last_name", "owner_username", "owner_email", "owner_password")},
        ),
    )

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = OrganizationAddForm
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else super().get_fieldsets(request, obj)

    def get_inlines(self, request, obj):
        # Settings are created by the sign-up service on add; edit them afterwards.
        return self.inlines if obj is not None else []

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        data = form.cleaned_data
        membership = services.signup_organization(
            organization={f: data[f] for f in OrganizationAddForm.Meta.fields},
            admin={
                "first_name": data["owner_first_name"],
                "last_name": data.get("owner_last_name", ""),
                "username": data["owner_username"],
                "email": data.get("owner_email"),
                "password": data["owner_password"],
            },
            units=[],
            branding={},
        )
        org = membership.organization
        # Hand the created row back to the admin so its redirect/message point at it.
        obj.pk, obj.slug, obj.created_at, obj.updated_at = org.pk, org.slug, org.created_at, org.updated_at
        obj._state.adding = False
        obj._state.db = org._state.db
        log_action(request, "organization.created", organization=org, target=org, changes={"via": "admin"})


@admin.register(OrgMembership)
class OrgMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "status", "is_owner")
    list_filter = ("status", "is_owner")
    search_fields = ("user__username", "user__email", "organization__name")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("code", "membership", "expires_at", "accepted_at", "revoked_at")
