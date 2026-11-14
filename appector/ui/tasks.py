"""Background task plumbing for the GTK UI (U3).

`run_async` is the single way window code runs blocking work:

- the task always runs inside try/except; an exception is reported to
  ``on_error`` and its traceback written to the activity log instead of
  killing the thread silently behind a modal window;
- results and errors are always marshalled back with ``GLib.idle_add``;
- ``finally`` always runs, so control re-enabling code cannot be skipped.

`CancelToken` wraps a ``threading.Event`` and is handed to the process
runner, whose commands run in their own session and can therefore be
terminated as a whole process group (S7).
"""

import threading
import traceback


def _log_traceback(name, error):
    from .._proc import _log_action

    _log_action(
        f"WORKER_ERROR task={name} error={type(error).__name__}: {error}"
    )


def run_async(task, on_done=None, on_error=None, *, name="task", cancellable=None):
    """Run ``task()`` in a daemon thread and marshal the result back.

    ``on_done(result)`` runs on the GTK main thread when the task returns;
    ``on_error(exc)`` runs instead when it raises. Both callbacks are
    invoked via ``GLib.idle_add`` and are safe to touch widgets. Exactly one
    of them runs — never both, never neither.
    """
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    def wrapper():
        try:
            result = task()
        except Exception as error:
            # The exception variable is unbound once the except block
            # exits, so it is captured explicitly for the callback.
            captured = error
            _log_traceback(name, captured)
            detail = traceback.format_exc()
            if on_error is not None:
                GLib.idle_add(
                    lambda exc=captured: on_error(exc) or False
                )
            _log_traceback(f"{name}-traceback", RuntimeError("\n" + detail))
            return
        if on_done is not None:
            GLib.idle_add(lambda value=result: on_done(value) or False)

    thread = threading.Thread(target=wrapper, name=f"appector-{name}", daemon=True)
    thread.start()
    return thread


class CancelToken:
    """A cancellable handle shared between the UI and a running command."""

    def __init__(self):
        self._event = threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()
