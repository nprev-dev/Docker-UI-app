"""Small helpers shared by the collectors."""

from __future__ import annotations


def brief(exc: BaseException | None) -> str:
    """The root cause in a few words; libraries wrap it in several layers of their own errors."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        inner = exc.__cause__ or exc.__context__
        if inner is None:
            break
        exc = inner
    if isinstance(exc, OSError) and exc.strerror:
        text = exc.strerror
    else:
        text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= 140 else text[:137] + "..."
