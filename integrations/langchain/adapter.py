"""Map Siyab hosted check answers to a LangChain tool result."""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://siyab.xyz"
DEFAULT_TIMEOUT_S = 10


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence(answer):
    if not isinstance(answer, dict):
        return None
    evidence = answer.get("evidence")
    evidence_hash = answer.get("evidence_hash")
    if not isinstance(evidence, str) or not isinstance(evidence_hash, str):
        return None
    if _sha256(evidence) != evidence_hash:
        return None
    try:
        document = json.loads(evidence)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _reasons(answer):
    reasons = answer.get("reasons")
    if not isinstance(reasons, list):
        return []
    return [reason for reason in reasons if isinstance(reason, str) and reason]


def _bound(answer, subject_check, label):
    document = _evidence(answer)
    if document is None:
        return "The check answer could not be verified."
    subject = document.get("subject")
    if not isinstance(subject, dict) or not subject_check(subject):
        return "The check answer is for a different " + label + "."
    expected = "UNKNOWN" if answer.get("outcome") == "ASK" else answer.get("outcome")
    if document.get("verdict") != expected:
        return "The check answer does not match its evidence."
    return None


def _decide(answer, subject_check, label):
    if not isinstance(answer, dict):
        return {"decision": "ask", "reasons": ["The check did not return an answer."]}
    mismatch = _bound(answer, subject_check, label)
    if mismatch:
        return {"decision": "ask", "reasons": [mismatch]}

    reasons = _reasons(answer)
    evidence_hash = answer.get("evidence_hash")
    outcome = answer.get("outcome")
    if outcome == "DENY":
        return {"decision": "deny",
                "reasons": reasons or ["The check found a refusal signal."],
                "evidence_hash": evidence_hash}
    if outcome != "ALLOW":
        return {"decision": "ask",
                "reasons": reasons or ["The check did not clear this account."],
                "evidence_hash": evidence_hash}

    checks = answer.get("checks")
    if (not isinstance(checks, list) or not checks
            or any(not isinstance(check, dict) or check.get("status") != "pass" for check in checks)):
        return {"decision": "ask", "reasons": ["The check answer is incomplete."],
                "evidence_hash": evidence_hash}
    if answer.get("signing") != "never" or answer.get("verdict") is not None:
        return {"decision": "ask", "reasons": ["The check answer was not produced by the unsigned route."],
                "evidence_hash": evidence_hash}
    if answer.get("action") != "sign":
        return {"decision": "ask",
                "reasons": reasons or ["The check requires a decision before sending."],
                "evidence_hash": evidence_hash}
    return {"decision": "allow", "reasons": reasons, "evidence_hash": evidence_hash}


def decide_account_check(answer, receiver):
    """Map a GET /live/account-check/<account> answer for one account."""
    if not isinstance(receiver, str) or not receiver:
        return {"decision": "ask", "reasons": ["The receiver account is missing."]}

    def subject_check(subject):
        return subject.get("kind") == "account" and subject.get("account") == receiver

    return _decide(answer, subject_check, "account")


def decide_payment(answer, receiver, amount, token="near"):
    """Map a GET /live/verdict answer for one transfer."""
    if not isinstance(receiver, str) or not receiver:
        return {"decision": "ask", "reasons": ["The receiver account is missing."]}
    if not isinstance(amount, str) or not amount.isascii() or not amount.isdigit():
        return {"decision": "ask", "reasons": ["The amount must be a decimal string in base units."]}
    judged_token = None if token == "near" else token

    def subject_check(subject):
        return (subject.get("kind") == "near" and subject.get("receiver") == receiver
                and subject.get("amount") == amount and subject.get("token") == judged_token)

    return _decide(answer, subject_check, "transfer")


def preflight(base_url=DEFAULT_BASE_URL, receiver=None, amount=None, token="near",
              opener=urllib.request.urlopen, timeout=DEFAULT_TIMEOUT_S):
    """Call the hosted check and return one decision. Network failures become ask."""
    base = base_url.rstrip("/")
    try:
        if amount is None:
            url = base + "/live/account-check/" + urllib.parse.quote(receiver or "", safe="")
        else:
            query = urllib.parse.urlencode({"receiver": receiver, "amount": amount, "token": token})
            url = base + "/live/verdict?" + query
        with opener(url, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {"decision": "ask", "reasons": ["The check is unavailable (HTTP " + str(error.code) + ")."]}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"decision": "ask", "reasons": ["The check could not be reached."]}
    if amount is None:
        return decide_account_check(answer, receiver)
    return decide_payment(answer, receiver, amount, token)


def tool_result(decision):
    """The text an agent sees for one decision."""
    reasons = " ".join(decision.get("reasons") or [])
    if decision.get("decision") == "deny":
        return "DENY: do not send this payment. " + reasons
    if decision.get("decision") == "ask":
        return ("ASK: a person must decide before this payment. " + reasons +
                " Do not send it on your own.")
    return ("ALLOW: the check found no refusal signal. " + reasons +
            " This is advice, not permission to pay; only a signed verdict authorises a Gate payment.")


def as_tool(base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT_S):
    """A LangChain StructuredTool, when langchain-core is installed."""
    try:
        from langchain_core.tools import tool
    except ImportError as error:
        raise RuntimeError("Install langchain-core to use the LangChain tool wrapper.") from error

    @tool
    def siyab_check_payment(receiver: str, amount: str, token: str = "near") -> str:
        """Check a NEAR payment receiver and amount with Siyab before sending funds."""
        return tool_result(preflight(base_url=base_url, receiver=receiver, amount=amount,
                                     token=token, timeout=timeout))

    return siyab_check_payment
