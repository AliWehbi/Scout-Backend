from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import update_last_login
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import (
    read_email_verification_token,
    send_password_reset_email,
    send_verification_email,
)
from .serializers import (
    ChangePasswordSerializer,
    EmailVerifySerializer,
    LoginSerializer,
    MeUpdateSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    UserSerializer,
    tokens_for,
)

User = get_user_model()


class AuthThrottle(AnonRateThrottle):
    rate = "20/min"


class LoginView(APIView):
    """Sign in with a username *or* an email address."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        update_last_login(None, user)
        return Response({**tokens_for(user), "user": UserSerializer(user).data})


class LogoutView(APIView):
    def post(self, request):
        try:
            RefreshToken(request.data.get("refresh", "")).blacklist()
        except TokenError:
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        user = request.user
        old_email = user.email
        serializer = MeUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        if user.email != old_email:
            user.email_verified = False
            user.save(update_fields=["email_verified"])
            send_verification_email(user)
        return Response(UserSerializer(user).data)


class ChangePasswordView(APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(APIView):
    """
    Always answers 200 so accounts can't be discovered. Users without an email address
    can't reset by themselves; their organization admin sets a new password for them.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.get_user()
        if user is not None:
            send_password_reset_email(user)
        return Response({"detail": _("If an account with an email address matches, we sent a reset link.")})


class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            user = User.objects.get(pk=force_str(urlsafe_base64_decode(data["uid"])))
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            user = None
        if user is None or not default_token_generator.check_token(user, data["token"]):
            raise ValidationError({"token": [_("This reset link is invalid or has expired.")]}, code="invalid_token")
        try:
            password_validation.validate_password(data["new_password"], user)
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": exc.messages}) from exc
        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class VerifyEmailView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = EmailVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = read_email_verification_token(serializer.validated_data["token"])
        except signing.SignatureExpired:
            raise ValidationError({"token": [_("This link has expired.")]}, code="expired") from None
        except signing.BadSignature:
            raise ValidationError({"token": [_("This link is invalid.")]}, code="invalid_token") from None
        user = User.objects.filter(pk=payload["uid"], email=payload["email"]).first()
        if user is None:
            raise ValidationError({"token": [_("This link is invalid.")]}, code="invalid_token") from None
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        return Response({"detail": _("Email confirmed.")})


class ResendVerificationView(APIView):
    def post(self, request):
        if request.user.email and not request.user.email_verified:
            send_verification_email(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
