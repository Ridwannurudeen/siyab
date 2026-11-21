"""Map Siyab hosted check answers to a LangChain tool result."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://siyab.xyz"
DEFAULT_TIMEOUT_S = 10
ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")


class PaymentRefused(RuntimeError):
    """The check refused the payment. Send nothing."""

    decision = "deny"


class ConfirmationRequired(RuntimeError):
    """A person must decide, or the confirmation was refused."""

    decision = "ask"


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence(answer):
    """The evidence document, only when the answer's hash matches it.

    The hash is unsigned, so this shows internal consistency, not authenticity.
    """
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


def _strings(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _checks(document):
    checks = document.get("checks") if isinstance(document, dict) else None
    return checks if isinstance(checks, list) else []


def _answer(answer):
    """Validation and the part of an answer that is safe to read without evidence."""
    if not isinstance(answer, dict):
        return None, "The check did not return an answer."
    outcome = answer.get("outcome")
    if not isinstance(outcome, str):
        return None, "The check answer has no outcome."
    outcome = outcome.strip().upper()
    if outcome == "UNKNOWN":
        outcome = "ASK"
    if outcome not in ("ALLOW", "DENY", "ASK"):
        return None, "The check answer has an unknown outcome."
    return outcome, None


def _decide(answer, subject_check, label):
    outcome, problem = _answer(answer)
    if problem:
        return {"decision": "ask", "reasons": [problem]}

    reasons = _strings(answer.get("reasons"))
    if outcome == "DENY":
        # A denial blocks a payment, so it is honoured before any binding check.
        document = _evidence(answer)
        shown = _strings(document.get("reasons")) if document else []
        return {"decision": "deny", "reasons": shown or reasons or ["The check refused this transfer."]}

    document = _evidence(answer)
    if document is None:
        return {"decision": "ask", "reasons": ["The check answer could not be read."]}
    subject = document.get("subject")
    if not isinstance(subject, dict) or not subject_check(subject):
        return {"decision": "ask", "reasons": ["The check answer is for a different " + label + "."]}
    expected = "UNKNOWN" if outcome == "ASK" else outcome
    if document.get("verdict") != expected:
        return {"decision": "ask", "reasons": ["The check answer does not match its evidence."]}

    shown = _strings(document.get("reasons")) or reasons
    if outcome == "ASK":
        return {"decision": "ask", "reasons": shown or ["The check did not clear this transfer."]}

    checks = _checks(document)
    if not checks or any(not isinstance(check, dict) or check.get("status") != "pass" for check in checks):
        return {"decision": "ask", "reasons": ["The check answer is incomplete."]}
    if answer.get("signing") != "never" or answer.get("verdict") is not None:
        return {"decision": "ask", "reasons": ["The check answer did not come from the unsigned route."]}
    if answer.get("action") != "sign":
        return {"decision": "ask", "reasons": shown or ["The check needs a decision before sending."]}
    return {"decision": "allow", "reasons": shown}


def decide_account_check(answer, receiver):
    """Map a GET /live/account-check/<account> answer for one account."""
    if not isinstance(receiver, str) or not ACCOUNT_ID.match(receiver or ""):
        return {"decision": "ask", "reasons": ["The receiver account is not a valid account id."]}

    def subject_check(subject):
        return subject.get("kind") == "account" and subject.get("account") == receiver

    return _decide(answer, subject_check, "account")


def decide_payment(answer, receiver, amount, token="near"):
    """Map a GET /live/verdict answer for one transfer."""
    if not isinstance(receiver, str) or not ACCOUNT_ID.match(receiver or ""):
        return {"decision": "ask", "reasons": ["The receiver account is not a valid account id."]}
    if not isinstance(amount, str) or not amount.isascii() or not amount.isdigit() or int(amount) <= 0:
        return {"decision": "ask", "reasons": ["The amount must be a positive decimal string in base units."]}
    if token != "near" and (not isinstance(token, str) or not ACCOUNT_ID.match(token or "")):
        return {"decision": "ask", "reasons": ["The token must be near or a valid account id."]}
    kind = "near" if token == "near" else "ft"
    judged_token = None if token == "near" else token

    def subject_check(subject):
        return (subject.get("kind") == kind and subject.get("receiver") == receiver
                and subject.get("amount") == amount and subject.get("token") == judged_token)

    return _decide(answer, subject_check, "transfer")


def _reject(message):
    return {"decision": "ask", "reasons": [message]}


def preflight(*, receiver, amount=None, token="near", base_url=DEFAULT_BASE_URL,
              opener=urllib.request.urlopen, timeout=DEFAULT_TIMEOUT_S):
    """Call the hosted check and return one decision. Every failure becomes ask."""
    if not isinstance(receiver, str) or not ACCOUNT_ID.match(receiver or ""):
        return _reject("The receiver account is not a valid account id.")
    if amount is not None and (not isinstance(amount, str) or not amount.isascii()
                              or not amount.isdigit() or int(amount) <= 0):
        return _reject("The amount must be a positive decimal string in base units.")
    if token != "near" and (not isinstance(token, str) or not ACCOUNT_ID.match(token or "")):
        return _reject("The token must be near or a valid account id.")

    base = base_url.rstrip("/")
    if amount is None:
        url = base + "/live/account-check/" + urllib.parse.quote(receiver, safe="")
    else:
        url = base + "/live/verdict?" + urllib.parse.urlencode(
            {"receiver": receiver, "amount": amount, "token": token})
    try:
        with opener(url, timeout=timeout) as response:
            raw = response.read()
        answer = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        return _reject("The check is unavailable (HTTP " + str(error.code) + ").")
    except Exception:
        return _reject("The check could not be reached.")
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


def guard_payment(*, send, receiver, amount, token="near", confirm=None, **options):
    """Run the check, then call send() only when it is safe to do so.

    A deny raises PaymentRefused. An ask raises ConfirmationRequired unless
    confirm is a callable that returns true for this decision.
    """
    decision = preflight(receiver=receiver, amount=amount, token=token, **options)
    if decision["decision"] == "deny":
        raise PaymentRefused(tool_result(decision))
    if decision["decision"] == "ask":
        approved = False
        if callable(confirm):
            try:
                approved = confirm(decision) is True
            except Exception:
                approved = False
        if not approved:
            raise ConfirmationRequired(tool_result(decision))
    return send()


def as_tool(base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT_S):
    """A LangChain StructuredTool, when langchain-core is installed."""
    try:
        from langchain_core.tools import tool
    except ImportError as error:
        raise RuntimeError("Install langchain-core to use the LangChain tool wrapper.") from error

    @tool
    def siyab_check_payment(receiver: str, amount: str, token: str = "near") -> str:
        """Check a NEAR payment receiver and amount with Siyab before sending funds."""
        return tool_result(preflight(receiver=receiver, amount=amount, token=token,
                                     base_url=base_url, timeout=timeout))

    return siyab_check_payment
