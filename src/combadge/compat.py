"""Narrow process-local workarounds for the target QNX Python build."""

import sys


def configure_asyncio() -> bool:
    """Handle QNX sysconf(SC_IOV_MAX)=-1 in Python 3.14's buffered sendmsg path.

    An indeterminate sysconf result is not a valid itertools.islice limit.
    Send one buffer per sendmsg call when the runtime supplies no usable limit.
    This leaves system Python files and valid platform limits untouched.
    """
    if not sys.platform.startswith("qnx"):
        return False
    from asyncio import selector_events

    if not hasattr(selector_events, "SC_IOV_MAX"):
        return False
    limit = selector_events.SC_IOV_MAX
    if type(limit) is int and 0 < limit <= sys.maxsize:
        return False
    selector_events.SC_IOV_MAX = 1
    return True
