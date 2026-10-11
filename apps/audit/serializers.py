from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "action", "actor", "target_type", "target_id", "target_repr", "changes", "created_at"]

    def get_actor(self, obj):
        if obj.actor is None:
            return None
        return {"id": obj.actor.pk, "username": obj.actor.username, "full_name": obj.actor.full_name}
