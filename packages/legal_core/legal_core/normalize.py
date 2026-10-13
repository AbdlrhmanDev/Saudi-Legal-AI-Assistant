"""Arabic-aware normalization for search (FR-03).

The original text is always stored untouched; these functions only produce
search-side representations.
"""

import re
import unicodedata

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_CHAR_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "ک": "ك",
        "ی": "ي",
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Extended Arabic-Indic digits
    }
)
_NON_WORD = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES = re.compile(r"\s+")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

# Longest first. Applied only when at least 3 letters remain.
_PREFIXES = ("وبال", "وكال", "فبال", "وال", "بال", "كال", "فال", "ولل", "لل", "ال")
_SINGLE_PREFIXES = ("و", "ف")
_PRONOUN_SUFFIXES = ("هما", "ها")

# Function words plus corpus-generic legal terms that carry no retrieval signal.
STOPWORDS = frozenset(
    """
    في من الي علي عن مع هذا هذه ذلك تلك التي الذي الذين ما ماذا هل كم كيف متي اين لماذا
    او ان اذا لا لم لن قد كل اي بين عند حتي ثم بعد قبل هو هي هم كان كانت يكون تكون به بها له لها
    فيه فيها منه منها عليه عليها غير حسب وفق وفقا بشان حول ينص تنص نص نظام ماده يجوز يجب شيء
    the a an of in on to for and or is are what which how does do under about by with
    """.split()
)


def normalize_ar(text: str) -> str:
    """Canonical search form: no diacritics/tatweel, unified letter variants, western digits."""
    text = unicodedata.normalize("NFKC", text)
    text = _DIACRITICS.sub("", text).replace(_TATWEEL, "")
    text = text.translate(_CHAR_MAP).lower()
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def _strip_prefix(token: str) -> str:
    for prefix in _PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 3:
            return token[len(prefix) :]
    for prefix in _SINGLE_PREFIXES:
        if token.startswith(prefix) and len(token) - 1 >= 4:
            rest = token[1:]
            return rest[2:] if rest.startswith("ال") and len(rest) - 2 >= 3 else rest
    return token


def light_stem(token: str) -> str:
    """Strip common proclitics and the attached pronoun 'ها', so 'والعامل' / 'للعامل' / 'العامل'
    and 'مدتها' / 'المدة' share one lexical key. Deliberately conservative: no root extraction."""
    token = _strip_prefix(token)
    for suffix in _PRONOUN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            # ta marbuta is written as 'ت' before a suffix: مدتها -> مدت -> مده
            return token[:-1] + "ه" if token.endswith("ت") else token
    return token


def lexical_tokens(text: str, *, drop_stopwords: bool = True) -> list[str]:
    tokens = []
    for raw in _TOKEN.findall(normalize_ar(text)):
        if drop_stopwords and raw in STOPWORDS:
            continue
        stem = light_stem(raw)
        if drop_stopwords and (stem in STOPWORDS or len(stem) < 2):
            continue
        tokens.append(stem)
    return tokens


def lexical_text(text: str) -> str:
    """Space-joined stems, indexed with to_tsvector('simple', ...)."""
    return " ".join(lexical_tokens(text, drop_stopwords=False))


def detect_language(text: str) -> str:
    arabic = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if arabic == 0 and latin == 0:
        return "unknown"
    return "ar" if arabic >= latin else "en"
