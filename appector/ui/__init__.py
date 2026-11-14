"""UI building blocks split out of window.py (ADR-0002 next step).

Modules here own the reusable dialog and task plumbing; `window` composes
them. Everything GTK-bound in this package is imported lazily where tests
need the module without a display.
"""
