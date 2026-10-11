from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

hex_color_validator = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message=_("Enter a color in #RRGGBB format."),
)
