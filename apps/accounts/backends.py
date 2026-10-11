from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
    """Authenticate with either a username or an email address (case-insensitive)."""

    def authenticate(self, request, username=None, password=None, identifier=None, **kwargs):
        identifier = (identifier or username or "").strip()
        if not identifier or password is None:
            return None

        User = get_user_model()
        user = User._default_manager.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).first()
        if user is None:
            # Run the hasher anyway to reduce timing differences between existing and missing users.
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
