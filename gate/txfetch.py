"""One mainnet transaction's signer, receiver and actions, read from the FastNEAR transactions API."""

from gate import base58, rpc


def is_hash(value):
    """Whether value is the base58 text of 32 bytes."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    try:
        raw = base58.decode(value)
    except ValueError:
        return False
    return len(raw) == 32 and base58.encode(raw) == value


def fetch_transaction(tx_hash, *, client):
    """The signer_id, receiver_id and actions of the one transaction tx_hash names; client is an http
    rpc.Client of https://tx.main.fastnear.com."""
    if not is_hash(tx_hash):
        raise ValueError("a transaction hash must be base58 for 32 bytes")
    body = client.post("/v0/transactions", {"tx_hashes": [tx_hash]})
    transactions = body.get("transactions") if isinstance(body, dict) else None
    if (not isinstance(transactions, list) or len(transactions) != 1 or not isinstance(transactions[0], dict)
            or not isinstance(transactions[0].get("transaction"), dict)
            or transactions[0]["transaction"].get("hash") != tx_hash):
        raise rpc.MalformedResponse("the transactions API did not return exactly the pasted transaction")
    transaction = transactions[0]["transaction"]
    if not {"signer_id", "receiver_id", "actions"} <= set(transaction):
        raise rpc.MalformedResponse("the transaction lacks signer_id, receiver_id or actions")
    return {field: transaction[field] for field in ("signer_id", "receiver_id", "actions")}
