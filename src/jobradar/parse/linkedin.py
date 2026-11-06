"""LinkedIn job URL canonicalization.

`https://ae.linkedin.com/jobs/view/ai-engineer-remote-at-joblet-ai-4487329670?trk=x` and
`https://www.linkedin.com/jobs/view/4487329670/` are the same listing: job ID 4487329670.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_VIEW_RE = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})/?$")


def is_linkedin_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def linkedin_job_id(url: str) -> str | None:
    """Return the numeric job ID for a LinkedIn job URL, or None if it is not one."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not is_linkedin_host(parts.hostname or ""):
        return None
    m = _VIEW_RE.search(parts.path)
    if m:
        return m.group(1)
    # Search/collection pages carry the selected job in a query parameter.
    for key in ("currentJobId", "jobId"):
        vals = parse_qs(parts.query).get(key)
        if vals and vals[0].isdigit():
            return vals[0]
    return None


def canonical_linkedin_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"
