"""The assistant's name: what Callsheet calls itself in Chat, the brief and Today.

The owner sets it in Setup › Your day › You or by asking in Chat (prefs kind `assistant_name`, with Undo). New installs
start as DEFAULT, and so does an install whose settings were saved before the name was a setting. Everything that shows or matches the name goes through here: `clean` checks a typed name, `of` reads it from the
settings, `pattern` is the regular expression that matches it in the owner's words (each word escaped, any spaces
between them), and `prompt` is the name as it may appear in a model prompt.
"""
import functools
import re

DEFAULT = "Bruce"
MAX_LEN = 40
_BAD = re.compile(r"[\x00-\x1f\x7f<>{}\[\]\\`\"]|https?:", re.I)


class Invalid(ValueError):
    """A name that can't be used; the message is a plain sentence."""


def clean(name):
    """The name with its spaces tidied, or Invalid: 1-40 characters, letters or digits in it, no control
    characters, brackets, quotes, backslashes or links."""
    if not isinstance(name, str):
        raise Invalid("The assistant's name must be text.")
    if _BAD.search(name):   # before the spaces are tidied: a newline or tab is refused, not folded into a space
        raise Invalid("Use letters, digits, spaces and simple punctuation in the assistant's name.")
    v = " ".join(name.split())
    if not v:
        raise Invalid("Give the assistant a name.")
    if len(v) > MAX_LEN:
        raise Invalid(f"The assistant's name is at most {MAX_LEN} characters.")
    if _BAD.search(v) or not re.search(r"[^\W_]", v):
        raise Invalid("Use letters, digits, spaces and simple punctuation in the assistant's name.")
    return v


def of(settings):
    """The configured name from a settings dict (DEFAULT when it's missing or unusable)."""
    try:
        return clean((settings or {}).get("assistant_name"))
    except (Invalid, AttributeError):
        return DEFAULT


@functools.lru_cache(maxsize=32)
def pattern(name):
    """A regex source (no flags, no groups that capture) matching the name: each word escaped, \\s+ between words,
    and a word edge only where the name starts or ends with a letter or digit."""
    try:
        words = clean(name).split(" ")
    except Invalid:
        words = [DEFAULT]
    core = r"\s+".join(re.escape(w) for w in words)
    lead = r"\b" if re.match(r"[^\W_]", words[0]) else ""
    tail = r"\b" if re.search(r"[^\W_]$", words[-1]) else ""
    return f"(?:{lead}{core}{tail})"


def prompt(name):
    """The name for a model prompt: cleaned (no braces, quotes or newlines can reach the prompt), else DEFAULT."""
    try:
        return clean(name)
    except Invalid:
        return DEFAULT
