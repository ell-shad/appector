"""Translation support (U11).

Appector ships English strings; this module wires up gettext so
translators can provide .mo catalogs without further code changes.

Locale directory layout (standard GNOME):

    /usr/share/locale/<lang>/LC_MESSAGES/appector.mo

Usage in GTK code::

    from .i18n import _

    label = Gtk.Label(label=_("Remove marked apps…"))

Strings stay English in the source; `_("...")` marks them for extraction
(pot creation via `msgfmt`/`xgettext --language=Python`).

The bindtextdomain call is idempotent and safe when the locale directory
does not exist (source checkouts): translation then degrades to the
source strings.
"""

import gettext
import os
from pathlib import Path

DOMAIN = "appector"

# The packaged layout; overridden by APECTOR_LOCALEDIR for development.
DEFAULT_LOCALEDIR = Path("/usr/share/locale")


def _localedir() -> Path:
    override = os.environ.get("APECTOR_LOCALEDIR")
    if override:
        return Path(override)
    return DEFAULT_LOCALEDIR


def init_translations():
    """Bind the text domain and install the translator.

    Call once at startup (appector.app). Returns the translator; the `_`
    marker falls back to the source string when no catalog exists.
    """
    localedir = _localedir()
    try:
        translation = gettext.translation(
            DOMAIN,
            localedir=str(localedir),
            fallback=True,
        )
    except OSError:
        translation = gettext.NullTranslations()

    if not isinstance(translation, gettext.NullTranslations):
        try:
            gettext.bindtextdomain(DOMAIN, str(localedir))
            gettext.textdomain(DOMAIN)
        except OSError:
            pass

    translation.install()
    return translation


def _(message: str) -> str:
    """Mark a string for translation and translate it at runtime."""
    return gettext.dgettext(DOMAIN, message) or message
