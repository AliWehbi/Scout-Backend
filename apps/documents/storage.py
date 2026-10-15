"""
Uploaded Word templates live outside MEDIA_ROOT: nginx serves /media publicly, while
templates may only be read through the API by members of their organization.
"""

import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.functional import LazyObject, empty


class PrivateStorage(LazyObject):
    def _setup(self):
        self._wrapped = FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


private_storage = PrivateStorage()


def get_private_storage():
    # A callable keeps the server path out of migrations.
    return private_storage


@receiver(setting_changed)
def _reset_private_storage(setting, **kwargs):
    if setting == "PRIVATE_MEDIA_ROOT":
        private_storage._wrapped = empty


def template_path(instance, _filename) -> str:
    # Never reuse the uploaded name: it is untrusted and could clash or traverse.
    return f"templates/{instance.organization_id}/{uuid.uuid4().hex}.docx"
