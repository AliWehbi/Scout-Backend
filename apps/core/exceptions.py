from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """DRF's handler, plus a stable machine-readable `code` on every error response."""
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(response.data, dict):
        if "detail" in response.data:
            code = getattr(response.data["detail"], "code", None) or getattr(exc, "default_code", "error")
            response.data["code"] = code
        else:
            response.data = {"detail": "Invalid input.", "code": "invalid", "errors": response.data}
    elif isinstance(response.data, list):
        response.data = {"detail": "Invalid input.", "code": "invalid", "errors": response.data}
    return response
