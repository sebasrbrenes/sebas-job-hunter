"""Conservative numeric requirements in English and Spanish."""
import re
import unicodedata


def experience_minima(description: str) -> list[int]:
    text = unicodedata.normalize("NFKD", description.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("\\+", "+").replace("\\-", "-")
    for word, number in {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
                         "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10}.items():
        text = re.sub(r"\b" + word + r"(?=\s+anos?\b)", str(number), text)
    values = []
    for sentence in re.split(r"[\n.;]", text):
        if re.search(r"preferred|nice to have|not required|company|founded|preferible|deseable|no (?:se )?requiere|empresa|fundad", sentence):
            continue
        for match in re.finditer(r"\b(\d+)\s*(?:\+|(?:[-–]|to|a|y)\s*\d+)?\s*(?:years?|yrs?|anos?)\b", sentence):
            # Spanish standalone ranges are common under Requisitos; English
            # age-like numbers still need an experience/requirement signal.
            if re.search(r"anos?\b|experience|required|minimum|at least|must", sentence):
                values.append(int(match.group(1)))
    return values
