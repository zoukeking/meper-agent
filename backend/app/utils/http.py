"""HTTP helpers.

``Content-Disposition`` (RFC 6266) construction that survives non-ASCII
filenames. HTTP header values are restricted to latin-1 by the spec and
Starlette enforces this in ``Response.init_headers`` (``v.encode("latin-1")``).
A filename containing CJK / emoji / other non-ASCII characters therefore raises
``UnicodeEncodeError`` when placed in a header verbatim, which the global
exception middleware turns into a 500.

``build_content_disposition`` emits both an ASCII ``filename`` fallback (old
clients) and a percent-encoded ``filename*`` (RFC 5987, modern clients), so
Chinese filenames download correctly while pure-ASCII names keep the exact
legacy form ``attachment; filename="report.pdf"``.
"""
from __future__ import annotations

from urllib.parse import quote


def build_content_disposition(
    filename: str, *, disposition: str = "attachment"
) -> str:
    """Build a ``Content-Disposition`` header value safe for non-ASCII names.

    For pure-ASCII filenames this returns the legacy form, e.g.
    ``attachment; filename="report.pdf"`` (unchanged behaviour, keeps existing
    tests passing).

    For filenames containing characters outside latin-1 it additionally emits
    the RFC 5987 ``filename*`` parameter, e.g.
    ``attachment; filename="report.txt"; filename*=UTF-8''%E5%87%BA%E5%B7%AE.txt``.
    Modern browsers prefer ``filename*`` and restore the original name; old
    clients fall back to the ASCII-only ``filename``.
    """
    # ASCII fallback: drop any character outside latin-1 so the header stays
    # encodable. ``errors="ignore"`` is intentional — this is a best-effort
    # fallback for legacy clients, the real name travels in filename*.
    ascii_fallback = filename.encode("latin-1", errors="ignore").decode("latin-1")
    if ascii_fallback == filename:
        # Pure ASCII — keep the simple legacy form.
        return f'{disposition}; filename="{filename}"'

    # Mixed / non-ASCII: emit both. quote() percent-encodes with UTF-8 by
    # default and leaves safe punctuation unencoded.
    encoded = quote(filename, encoding="utf-8")
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded}"
    )
