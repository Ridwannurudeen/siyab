"""The measured label floor with additions that cannot relax a verdict."""

import dataclasses
import hashlib
import http.client
import re
import urllib.error
import urllib.parse
import urllib.request

from gate import rpc, rules
from intel import labels

MAX_DELTA_BYTES = 1_048_576
DELTA_TIMEOUT_S = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        response.close()
        raise ValueError("label delta redirects are refused")


def fetch_delta(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("label delta needs a measured HTTPS URL without credentials, query or fragment")
    opener = urllib.request.build_opener(rpc._DeadlineHTTPSHandler, _NoRedirect)
    try:
        with rpc.request_deadline(DELTA_TIMEOUT_S):
            with opener.open(url, timeout=DELTA_TIMEOUT_S) as response:
                if response.status != 200:
                    raise ValueError("label delta needs a successful response")
                lengths = response.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
                    raise ValueError("label delta has an invalid Content-Length")
                if lengths and int(lengths[0]) > MAX_DELTA_BYTES:
                    raise ValueError("label delta is too large")
                data = response.read(MAX_DELTA_BYTES + 1)
                if len(data) > MAX_DELTA_BYTES:
                    raise ValueError("label delta is too large")
                if lengths and len(data) != int(lengths[0]):
                    raise ValueError("label delta is truncated")
                return data
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError("label delta request failed") from error
    except (OSError, http.client.HTTPException) as error:
        raise ValueError("label delta request failed") from error


def merge(store, delta):
    if not isinstance(delta, dict) or set(delta) != {"add"} or not isinstance(delta["add"], list):
        raise labels.LabelError("a delta contains exactly one add array")
    current = {assertion.id: assertion for assertion in store.assertions}
    added = []
    for row in delta["add"]:
        assertion = labels.validate(row)
        if assertion.category not in rules.DENY_CATEGORIES or assertion.superseded_by is not None:
            raise labels.LabelError("a delta may only add current denylist or incident assertions")
        if assertion.id in current:
            if assertion != current[assertion.id]:
                raise labels.LabelError("a delta may not modify an existing assertion")
            continue
        current[assertion.id] = assertion
        added.append(assertion)
    assertions = store.assertions + added
    cited = {(a.subject, a.chain) for a in assertions if a.category == "incident"
             and a.provenance in ("third_party", "onchain") and a.superseded_by is None}
    for assertion in added:
        if assertion.provenance == "inferred" and (assertion.subject, assertion.chain) not in cited:
            raise labels.LabelError("an inferred threat requires a current cited incident on the same subject")
    if not added:
        return store
    data = "".join(labels.row_line(dataclasses.asdict(a)) + "\n" for a in assertions).encode("utf-8")
    return labels.Store(assertions, hashlib.sha256(data).hexdigest())
