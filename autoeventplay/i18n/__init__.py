"""Internationalisation. English is the source and default language.

* ``Msg("Channel {n} does not exist", n=5)`` is a ``str`` (the English text)
  that remembers its template and parameters, so it can be translated later
  – e.g. when an error message is returned to a German web UI.
* ``t(text)`` translates into the language of the current request (a
  context variable set by the web middleware) or an explicit language.
  Unknown texts are returned unchanged.
* Catalogs map the English template to the translation (see ``de.py``).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .de import DE

LANGUAGES = {"en": "English", "de": "Deutsch"}
DEFAULT_LANGUAGE = "en"
CATALOGS: dict[str, dict[str, str]] = {"de": DE}

_current: ContextVar[str] = ContextVar("aep_lang", default=DEFAULT_LANGUAGE)


class Msg(str):
    """English text with template + parameters for later translation."""

    template: str
    params: dict[str, Any]

    def __new__(cls, template: str, **params: Any) -> "Msg":
        obj = super().__new__(cls, template.format(**params) if params else template)
        obj.template = template
        obj.params = params
        return obj


def normalize_language(lang: str | None) -> str:
    lang = (lang or "").lower()[:2]
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def set_language(lang: str | None):  # noqa: ANN201 - returns a ContextVar token
    return _current.set(normalize_language(lang))


def current_language() -> str:
    return _current.get()


def t(text: Any, lang: str | None = None) -> str:
    if text is None:
        return ""
    lang = lang or _current.get()
    if lang == DEFAULT_LANGUAGE:
        return str(text)
    catalog = CATALOGS.get(lang, {})
    template = getattr(text, "template", None)
    if template is not None:
        params = {k: t(v, lang) if isinstance(v, str) else v for k, v in text.params.items()}
        translated = catalog.get(template, template)
        try:
            return translated.format(**params)
        except (KeyError, IndexError, ValueError):
            return str(text)
    return catalog.get(str(text), str(text))


def catalog_for(lang: str) -> dict[str, str]:
    return CATALOGS.get(normalize_language(lang), {})


def text_of(exc: BaseException) -> str:
    """The (possibly translatable) message of an exception."""
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)
