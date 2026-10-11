from django.contrib.auth import authenticate, get_user_model, password_validation
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

USERNAME_HELP = _("Letters, digits and . _ - only.")


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    has_email = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "has_email",
            "email_verified",
            "first_name",
            "last_name",
            "full_name",
            "preferred_language",
            "last_login",
            "date_joined",
        ]
        read_only_fields = ["id", "username", "email_verified", "last_login", "date_joined"]


def validate_username_value(value: str, instance=None) -> str:
    value = value.strip().lower()
    if not value:
        raise serializers.ValidationError(_("This field may not be blank."))
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if any(ch not in allowed for ch in value):
        raise serializers.ValidationError(USERNAME_HELP)
    if "@" in value:
        raise serializers.ValidationError(USERNAME_HELP)
    qs = User.objects.filter(username__iexact=value)
    if instance is not None:
        qs = qs.exclude(pk=instance.pk)
    if qs.exists():
        raise serializers.ValidationError(_("This username is already taken."))
    return value


def validate_email_value(value, instance=None):
    if not value:
        return None
    value = value.strip().lower()
    qs = User.objects.filter(email__iexact=value)
    if instance is not None:
        qs = qs.exclude(pk=instance.pk)
    if qs.exists():
        raise serializers.ValidationError(_("This email is already used by another account."))
    return value


def tokens_for(user) -> dict:
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(help_text=_("Username or email"))
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(self.context.get("request"), identifier=attrs["identifier"], password=attrs["password"])
        if user is None:
            raise AuthenticationFailed(_("Incorrect username/email or password."), code="invalid_credentials")
        attrs["user"] = user
        return attrs


class MeUpdateSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "preferred_language"]

    def validate_email(self, value):
        return validate_email_value(value, self.instance)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError(_("Your current password is incorrect."))
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value


class PasswordResetRequestSerializer(serializers.Serializer):
    identifier = serializers.CharField()

    def get_user(self):
        identifier = self.validated_data["identifier"].strip()
        return (
            User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier), is_active=True)
            .exclude(email__isnull=True)
            .first()
        )


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)


class EmailVerifySerializer(serializers.Serializer):
    token = serializers.CharField()
