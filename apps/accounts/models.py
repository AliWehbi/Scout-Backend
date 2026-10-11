from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _


class Language(models.TextChoices):
    ENGLISH = "en", _("English")
    ARABIC = "ar", _("Arabic")


class UserManager(DjangoUserManager):
    def get_by_natural_key(self, username):
        return self.get(username__iexact=username)

    def create_user(self, username, email=None, password=None, **extra_fields):
        return super().create_user(username, email or None, password, **extra_fields)


class User(AbstractUser):
    """
    A person who can sign in.

    - `username` is required and unique (case-insensitive).
    - `email` is optional, but unique when present. Users without an email are
      managed by their organization admins (password resets, invitations by code).
    """

    email = models.EmailField(_("email address"), blank=True, null=True)
    email_verified = models.BooleanField(default=False)
    preferred_language = models.CharField(max_length=5, choices=Language.choices, default=Language.ENGLISH)

    objects = UserManager()

    REQUIRED_FIELDS: list[str] = []

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("username"), name="user_username_ci_unique"),
            models.UniqueConstraint(Lower("email"), name="user_email_ci_unique"),
        ]

    def save(self, *args, **kwargs):
        self.username = self.username.strip().lower()
        self.email = self.email.strip().lower() if self.email and self.email.strip() else None
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return self.get_full_name() or self.username

    @property
    def has_email(self) -> bool:
        return bool(self.email)

    def __str__(self):
        return self.username
