"""HyP3 (hyp3_sdk) log parser spec.

Markers: ``hyp3_sdk`` package path and exception names
(https://github.com/ASFHyP3/hyp3-sdk/blob/develop/src/hyp3_sdk/exceptions.py: HyP3Error,
ServerError, AuthenticationError, ServiceUnavailableError), ``Job.__str__`` format
``HyP3 {job_type} job {job_id}`` (jobs.py), the job-type names used by wintersar's HyP3
adapter (INSAR_ISCE_BURST / INSAR_ISCE_MULTI_BURST / INSAR_GAMMA) and the SDK error
format ``<Response [400]> ...`` (exceptions.py ``_raise_for_hyp3_status``).
"""

from __future__ import annotations

import re

from wintersar.diagnose.parsers.generic import ParserSpec

SPEC = ParserSpec(
    name="hyp3",
    filename_hints=("hyp3",),
    content_markers=(
        re.compile(r"\bhyp3(?:_sdk)?\b", re.IGNORECASE),
        re.compile(r"\bHyP3(?:Error|SDKError)\b|hyp3_sdk\.exceptions"),
        re.compile(r"HyP3 \w+ job [0-9a-f-]+"),
        re.compile(r"\bINSAR_(?:ISCE_(?:MULTI_)?BURST|GAMMA)\b|\bRTC_GAMMA\b|\bAUTORIFT\b"),
        re.compile(r"<Response \[\d{3}\]>"),
    ),
    event_patterns=(
        ("error", re.compile(r"<Response \[[45]\d{2}\]>")),
        # bounded gap instead of ``.*`` (quadratic on a carriage-return progress line)
        ("error", re.compile(r"\bstatus_code\W{1,3}FAILED\b|\bjob [^\n\r]{0,200} FAILED\b")),
    ),
)
