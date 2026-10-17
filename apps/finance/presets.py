"""Names and symbols, in English and Arabic, for currencies scout groups in the region use."""

# code: (name_en, name_ar, symbol_en, symbol_ar, decimals)
PRESETS: dict[str, tuple[str, str, str, str, int]] = {
    "USD": ("US dollar", "دولار أميركي", "$", "$", 2),
    "LBP": ("Lebanese pound", "ليرة لبنانية", "LBP", "ل.ل.", 0),
    "EUR": ("Euro", "يورو", "€", "€", 2),
    "GBP": ("British pound", "جنيه إسترليني", "£", "£", 2),
    "SAR": ("Saudi riyal", "ريال سعودي", "SAR", "ر.س", 2),
    "AED": ("UAE dirham", "درهم إماراتي", "AED", "د.إ", 2),
    "QAR": ("Qatari riyal", "ريال قطري", "QAR", "ر.ق", 2),
    "KWD": ("Kuwaiti dinar", "دينار كويتي", "KWD", "د.ك", 3),
    "BHD": ("Bahraini dinar", "دينار بحريني", "BHD", "د.ب", 3),
    "OMR": ("Omani rial", "ريال عماني", "OMR", "ر.ع", 3),
    "JOD": ("Jordanian dinar", "دينار أردني", "JOD", "د.أ", 3),
    "EGP": ("Egyptian pound", "جنيه مصري", "EGP", "ج.م", 2),
    "SYP": ("Syrian pound", "ليرة سورية", "SYP", "ل.س", 0),
    "IQD": ("Iraqi dinar", "دينار عراقي", "IQD", "د.ع", 0),
    "MAD": ("Moroccan dirham", "درهم مغربي", "MAD", "د.م", 2),
    "TND": ("Tunisian dinar", "دينار تونسي", "TND", "د.ت", 3),
    "DZD": ("Algerian dinar", "دينار جزائري", "DZD", "د.ج", 2),
    "TRY": ("Turkish lira", "ليرة تركية", "₺", "₺", 2),
    "CAD": ("Canadian dollar", "دولار كندي", "CA$", "CA$", 2),
    "AUD": ("Australian dollar", "دولار أسترالي", "A$", "A$", 2),
    "CHF": ("Swiss franc", "فرنك سويسري", "CHF", "CHF", 2),
}


def preset(code: str) -> dict:
    code = (code or "USD").upper()
    name_en, name_ar, symbol_en, symbol_ar, decimals = PRESETS.get(code, (code, "", code, code, 2))
    return {
        "code": code,
        "name_en": name_en,
        "name_ar": name_ar,
        "symbol_en": symbol_en,
        "symbol_ar": symbol_ar,
        "decimals": decimals,
    }
