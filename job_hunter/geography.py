"""Conservative location screening, never a work-authorization assertion."""
import re

from .deduplicate import normalized_text

LOCAL = r"\b(costa rica|heredia|alajuela|cartago|gam|gran area metropolitana)\b"
FOREIGN = r"\b(united states|usa|u s|canada|mexico|colombia|brazil|brasil|argentina|chile|india|gurgaon|bengaluru|bangalore|hyderabad|mumbai|pune|chennai|noida|united kingdom|uk|south africa|poland|spain|germany|california|new york|texas)\b"


def eligibility(location: str, description: str = "") -> tuple[str, str]:
    loc, desc = normalized_text(location), normalized_text(description)
    # Explicit exclusions/restrictions take precedence over regional marketing.
    if re.search(r"(?:excluding|except|not available in|excluye) costa rica", desc):
        return "ineligible", "Posting explicitly excludes Costa Rica"
    for clause in re.split(r"[.\n;]", description.lower()):
        clause = normalized_text(clause)
        if re.search(r"must (?:currently )?(?:reside|live|be (?:located|based))|residency required|residencia obligatoria|debe residir|only candidates (?:based|located)", clause):
            if re.search(FOREIGN, clause) and not re.search(LOCAL, clause):
                return "ineligible", "Mandatory residence outside Costa Rica"
    if re.search(LOCAL, loc) or re.search(r"\bcr\b", loc):
        return "eligible", "Costa Rica location; work authorization still unconfirmed"
    if re.search(r"\b(remote|remoto|remota|hybrid|hibrido|hibrida)\b", desc) and re.search(LOCAL, desc):
        return "eligible", "Posting text explicitly places the remote/hybrid role in Costa Rica; work authorization still unconfirmed"
    if "san jose" in loc and not re.search(FOREIGN, loc):
        return "eligible", "San Jose location without conflicting country"
    if re.search(FOREIGN, loc):
        return "ineligible", "Position location restricted to another country"
    if re.search(r"\b(remote|remoto)\b", loc) and re.search(r"\b(latam|latin america|americas|worldwide|global)\b", loc):
        return "eligible", "Regional/global remote scope with no clear Costa Rica exclusion; confirm hiring eligibility"
    return "unknown", "No explicit Costa Rica or eligible regional remote scope"
