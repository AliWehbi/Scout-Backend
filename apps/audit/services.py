from .models import AuditLog


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_action(request, action: str, *, organization=None, target=None, changes: dict | None = None, actor=None):
    """Record an auditable action. `target` is any model instance (or None)."""
    if actor is None and request is not None and getattr(request, "user", None) and request.user.is_authenticated:
        actor = request.user
    return AuditLog.objects.create(
        organization=organization,
        actor=actor,
        action=action,
        target_type=target._meta.model_name if target is not None else "",
        target_id=str(target.pk) if target is not None else "",
        target_repr=str(target)[:255] if target is not None else "",
        changes=changes or {},
        ip_address=_client_ip(request),
    )
