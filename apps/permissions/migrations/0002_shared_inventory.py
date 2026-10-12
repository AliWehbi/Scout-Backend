"""
Phase 5: the equipment store is shared by the whole organization.

- adds `inventory.check_out` and makes the inventory permissions organization-wide,
- gives the new permission to the default Org Admin, Unit Leader and Quartermaster roles,
- turns existing unit-limited inventory grants into organization-wide ones.
"""

from django.db import migrations

INVENTORY = ("inventory.view", "inventory.check_out", "inventory.manage")
ROLES_WITH_CHECK_OUT = ("org_admin", "unit_leader", "quartermaster")


def forwards(apps, schema_editor):
    Permission = apps.get_model("permissions", "Permission")
    Role = apps.get_model("permissions", "Role")
    PermissionGrant = apps.get_model("permissions", "PermissionGrant")
    for codename in INVENTORY:
        Permission.objects.update_or_create(codename=codename, defaults={"group": "inventory", "unit_scoped": False})
    check_out = Permission.objects.get(codename="inventory.check_out")
    for role in Role.objects.filter(key__in=ROLES_WITH_CHECK_OUT):
        role.permissions.add(check_out)
    for grant in PermissionGrant.objects.filter(permission__codename__in=INVENTORY, unit__isnull=False):
        org_wide = PermissionGrant.objects.filter(
            membership=grant.membership, permission=grant.permission, unit__isnull=True
        ).exists()
        if org_wide:
            grant.delete()
        else:
            grant.unit = None
            grant.save(update_fields=["unit"])


class Migration(migrations.Migration):
    dependencies = [("permissions", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
