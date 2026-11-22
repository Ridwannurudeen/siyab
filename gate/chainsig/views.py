"""Read-only signer views, pinned to one final block. No sign method is called."""

from gate import canon
from gate.chainsig import derive_public_key, ed25519_bytes, secp256k1_bytes, tweak

_PROTOCOL_CURVES = {"CaitSith": "secp256k1", "Frost": "ed25519"}


def fetch_root(client, contract, domain_id):
    """Read the Running domain registry and root key through rpc.Client views.

    Missing or unknown protocol/purpose/state, inconsistent block heights and
    malformed keys raise ValueError. Provider errors propagate. A view result
    is provider-reported data, not a proof of the signer's deployed code.
    """
    canon.account_id(contract, "signer contract")
    if type(domain_id) is not int or not 0 <= domain_id < 2**64:
        raise ValueError("domain_id must be a u64 integer")
    state = client.call_function(contract, "state", {})
    height = client.last_block_height
    sources = [{"method": "state", "args": {}, "source": client.last_source}]
    if type(height) is not int or height < 0:
        raise ValueError("signer state has no valid block height")
    if not isinstance(state, dict) or set(state) != {"Running"} or not isinstance(state["Running"], dict):
        raise ValueError("signer state is not Running")
    registry = state["Running"].get("domains")
    if not isinstance(registry, dict) or not isinstance(registry.get("domains"), list):
        raise ValueError("signer domain registry is missing")
    selected, seen = None, set()
    for domain in registry["domains"]:
        if (not isinstance(domain, dict) or type(domain.get("id")) is not int
                or not 0 <= domain["id"] < 2**64 or domain["id"] in seen):
            raise ValueError("signer domain registry has an invalid or duplicate id")
        seen.add(domain["id"])
        if domain["id"] == domain_id:
            selected = domain
    if selected is None:
        raise ValueError("requested domain is not in the signer registry")
    protocol = selected.get("protocol")
    if (selected.get("purpose") != "Sign" or not isinstance(protocol, str)
            or protocol not in _PROTOCOL_CURVES):
        raise ValueError("requested domain is not a supported Sign domain")
    root = client.call_function(contract, "public_key", {"domain_id": domain_id}, block_id=height)
    if client.last_block_height != height or type(client.last_block_height) is not int:
        raise ValueError("signer root was not read at the state block")
    sources.append({"method": "public_key", "args": {"domain_id": domain_id}, "source": client.last_source})
    curve = _PROTOCOL_CURVES[protocol]
    if curve == "secp256k1":
        secp256k1_bytes(root)
    else:
        ed25519_bytes(root)
    return {"contract": contract, "domain_id": domain_id, "protocol": protocol, "curve": curve,
            "public_key": root, "block_height": height, "sources": sources}


def read_derivation(client, contract, domain_id, predecessor, path):
    """Fetch a root, derive locally and compare with the signer's derived key view."""
    tweak(predecessor, path)  # Validate the predecessor and UTF-8 path before any network access.
    root = fetch_root(client, contract, domain_id)
    child = derive_public_key(root["public_key"], predecessor, path)
    args = {"path": path, "predecessor": predecessor, "domain_id": domain_id}
    reported = client.call_function(contract, "derived_public_key", args, block_id=root["block_height"])
    if (client.last_block_height != root["block_height"] or type(client.last_block_height) is not int
            or reported != child):
        raise ValueError("local derivation does not match the pinned signer view")
    root["sources"].append({"method": "derived_public_key", "args": args, "source": client.last_source})
    return {"root": root, "predecessor": predecessor, "path": path, "public_key": child,
            "contract_cross_check": True}
