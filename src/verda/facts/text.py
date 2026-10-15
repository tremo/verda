"""Turkish-aware matching for quote verification. Pure functions, no I/O."""
import re
import unicodedata

ELLIPSIS = re.compile(r'…|\.\.\.')
_SPACE = re.compile(r'\s+')
_PUNCT = re.compile(r'[^\w\s/]')
_QUOTES = str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', '«': '"', '»': '"',
                         '–': '-', '—': '-', ' ': ' '})
_SENTENCES = re.compile(r'(?<=[.!?…])\s+|\n+')

MIN_QUOTE = 6      # folded characters; shorter quotes are ambiguous without context
MAX_QUOTE = 500    # raw characters

# Words that turn a statement into a guess. A sentence containing one is never
# accepted automatically; it goes to the user with its quote attached.
_HEDGES = (
    'sanırım', 'sanıyorum', 'galiba', 'muhtemelen', 'herhalde', 'bilmiyorum', 'bilmiyoruz',
    'bilgim yok', 'emin değil', 'kesin değil', 'kesin bilmiyorum', 'diye biliyorum', 'diye duydum',
    'duyduğum kadarıyla', 'araştırıp dön', 'araştıracağım', 'teyit edeceğim', 'teyit etmek lazım', 'sorabilirsiniz',     # not "teyit ettim": that is a confirmation 'sorarsanız', 'kontrol edin',
    'kontrol ettirin', 'bakmak lazım', 'olabilir', 'olabiliyor', 'diyemem', 'ihtimal', 'tahmin',
)


def fold(text) -> str:
    """Case, spacing and punctuation insensitive form of a Turkish text.

    Dotted and dotless i are one letter ("SİT", "sit"; "BULUNMAMAKTADIR", "bulunmamaktadır").
    Other diacritics are kept: a quote must be the speaker's actual spelling.
    """
    value = unicodedata.normalize('NFC', str(text)).translate(_QUOTES)
    value = value.replace('İ', 'i').replace('I', 'i').replace('ı', 'i').casefold().replace('i̇', 'i')
    return _SPACE.sub(' ', _PUNCT.sub(' ', value)).strip()


HEDGES = tuple(fold(h) for h in _HEDGES)


def has_ellipsis(text) -> bool:
    return bool(ELLIPSIS.search(str(text)))


def is_hedged(folded_sentence: str) -> bool:
    return any(h in folded_sentence for h in HEDGES)


def sentences(text) -> list[str]:
    return [s for s in _SENTENCES.split(str(text)) if s.strip()]


def locate(quote, text):
    """Find `quote` inside `text`. Returns (sentence_raw, sentence_folded) or None.

    The sentence is what hedging and truncation are judged on, so a model cannot
    lift "sit yoktur" out of "Sanırım sit yoktur".
    """
    needle = fold(quote)
    if not needle:
        return None
    for sentence in sentences(text):
        folded = fold(sentence)
        if needle in folded:
            return sentence, folded
    whole = fold(text)
    if needle in whole:      # spans sentences: judge the whole text, conservatively
        return str(text), whole
    return None
