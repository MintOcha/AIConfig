"""Error classes and the exit codes the CLI reports.

Exit codes (also printed by `romcp --help`):
  0  finished and succeeded
  1  finished but failed (an operation error, failed tests, lint errors)
  2  bad arguments or tool setup problem
  3  Studio unavailable or busy (not connected, Play ended, locked by someone else): retry later
  4  timed out, or still running in the background (the operation ID is printed)
"""

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_TIMEOUT = 0, 1, 2, 3, 4

UNAVAILABLE_HINTS = ('No live', 'receiver', 'not connected', 'foreground', 'proxy transport', 'No current Studio')


class StudioBusy(RuntimeError):
    """Another agent holds the Studio lock or owns the running Play session."""
    code = 'unavailable'


def classify(error):
    """Map any exception to a short code the CLI turns into an exit code."""
    code = getattr(error, 'code', None)
    if code:
        return code
    if isinstance(error, TimeoutError):
        return 'timeout'
    text = str(error)
    if any(hint in text for hint in UNAVAILABLE_HINTS):
        return 'unavailable'
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return 'usage'
    return 'failed'


def exit_code(code):
    return {'unavailable': EXIT_UNAVAILABLE, 'timeout': EXIT_TIMEOUT, 'usage': EXIT_USAGE}.get(code, EXIT_FAILED)
