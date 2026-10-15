from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from . import registry, services
from .models import DocumentKind, DocumentTemplate


class DocumentTemplateSerializer(serializers.ModelSerializer):
    file = serializers.FileField(write_only=True)
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = DocumentTemplate
        fields = [
            "id",
            "kind",
            "name",
            "file",
            "original_filename",
            "size",
            "is_default",
            "uploaded_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "original_filename", "size", "uploaded_by_name", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None:  # kind and file are fixed once uploaded
            fields["kind"].read_only = True
            fields.pop("file")
        return fields

    def get_uploaded_by_name(self, obj):
        return obj.uploaded_by.full_name if obj.uploaded_by else ""

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError(_("Enter a name."))
        return value

    def validate(self, attrs):
        upload = attrs.get("file")
        if upload is not None:
            attrs["data"] = services.read_upload(upload, attrs["kind"])
            attrs["original_filename"] = services.clean_filename(upload.name, limit=255)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        org = self.context["organization"]
        data = validated_data.pop("data")
        validated_data.pop("file")
        kind = validated_data["kind"]
        first_of_kind = not DocumentTemplate.objects.filter(organization=org, kind=kind).exists()
        if validated_data.get("is_default") or first_of_kind:
            DocumentTemplate.objects.filter(organization=org, kind=kind).update(is_default=False)
            validated_data["is_default"] = True
        template = DocumentTemplate(
            organization=org,
            uploaded_by=self.context["request"].user,
            size=len(data),
            sha256=services.sha256(data),
            **validated_data,
        )
        template.file.save("template.docx", ContentFile(data), save=False)
        template.save()
        return template

    @transaction.atomic
    def update(self, instance, validated_data):
        if validated_data.get("is_default"):
            DocumentTemplate.objects.filter(organization=instance.organization, kind=instance.kind).exclude(
                pk=instance.pk
            ).update(is_default=False)
        return super().update(instance, validated_data)


class ExportParamsSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=DocumentKind.choices)
    template = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    lang = serializers.ChoiceField(choices=registry.LANGS, required=False)
    date = serializers.DateField(required=False, allow_null=True)


class StarterParamsSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=DocumentKind.choices)
    lang = serializers.ChoiceField(choices=registry.LANGS, default="en")
