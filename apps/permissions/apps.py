from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_catalog(sender, **kwargs):
    from .service import sync_permission_catalog

    sync_permission_catalog()


class PermissionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.permissions"
    label = "permissions"

    def ready(self):
        post_migrate.connect(_sync_catalog, sender=self)
