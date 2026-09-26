def split_words(text):
    """Split text into words on whitespace."""
    return text.split()


def word_count(text):
    return len(split_words(text))


def title_case(text):
    """Capitalize each word; hyphenated compounds are one word ('state-of-the-art' -> 'State-of-the-art')."""
    return " ".join(w[:1].upper() + w[1:] for w in split_words(text))
