"""Folding text to plain letters, for sheet column names and for reading replies."""

import unicodedata

# NFKD splits "ş" into "s" plus a combining cedilla, which fold() drops; "İ" splits the same way
# into "I" plus a dot. Turkish dotless "ı" has nothing to split off, so it is mapped by hand.
LETTERS = str.maketrans({"ı": "i"})


def fold(text: str) -> str:
    """Strip accents and Turkish letter marks, keeping case: "Kanal Adı" -> "Kanal Adi",
    "ÇIKARIN" -> "CIKARIN", "Prénom" -> "Prenom". Plain ASCII comes back unchanged."""
    decomposed = unicodedata.normalize("NFKD", text.translate(LETTERS))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))
