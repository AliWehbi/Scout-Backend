from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.translation import gettext as _
from django.utils.translation import override

EMAIL_VERIFY_SALT = "accounts.verify-email"
EMAIL_VERIFY_MAX_AGE = 60 * 60 * 24 * 3  # 3 days


def make_email_verification_token(user) -> str:
    return signing.dumps({"uid": user.pk, "email": user.email}, salt=EMAIL_VERIFY_SALT)


def read_email_verification_token(token: str) -> dict:
    return signing.loads(token, salt=EMAIL_VERIFY_SALT, max_age=EMAIL_VERIFY_MAX_AGE)


def send_verification_email(user) -> None:
    if not user.email:
        return
    link = f"{settings.FRONTEND_URL}/verify-email?token={make_email_verification_token(user)}"
    with override(user.preferred_language):
        send_mail(
            subject=_("Confirm your email address"),
            message=_("Hello %(name)s,\n\nConfirm your email address by opening this link:\n%(link)s\n")
            % {"name": user.full_name, "link": link},
            from_email=None,
            recipient_list=[user.email],
        )


def send_password_reset_email(user) -> None:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}"
    with override(user.preferred_language):
        send_mail(
            subject=_("Reset your password"),
            message=_(
                "Hello %(name)s,\n\nSomeone asked to reset the password for %(username)s.\n"
                "Open this link to choose a new password:\n%(link)s\n\n"
                "If this wasn't you, you can ignore this email."
            )
            % {"name": user.full_name, "username": user.username, "link": link},
            from_email=None,
            recipient_list=[user.email],
        )
