"""Browser graph data from recorded investigations, retaining every evidence record."""

from copy import deepcopy

from intel import trace


def _payload(kind, root, depths, records, edges, account_paths):
    nodes = [{"id": account, "label": account,
              "url": f"../{account_paths[account]}" if account in account_paths else None, "depth": depth}
             for account, depth in depths.items()]
    return {"kind": kind, "root": root, "nodes": nodes, "edges": edges, "records": records,
            "total_nodes": len(nodes), "total_edges": len(edges), "total_records": len(records)}


def flow_payload(document, account_paths):
    """Group directed transfers by asset; evidence and direct shares keep their original scope."""
    depths = {document["subject"]: 0}
    records, grouped = [], {}
    for index, (hop, depth) in enumerate(zip(document["hops"], trace.hop_depths(document))):
        record = deepcopy(hop)
        timestamp = int(hop["block_timestamp"])
        milliseconds = timestamp // 1000000
        record.update(id=f"record-{index}", block_timestamp=str(timestamp),
                      timestamp_ms=milliseconds if abs(milliseconds) <= 2 ** 53 - 1 else None,
                      depth=depth, provenance="onchain", confidence=None)
        for field in ("usd_amount", "human_amount"):
            record[field] = None if hop[field] is None else str(hop[field])
        records.append(record)
        depths.setdefault(hop["to"], depth)
        key = (hop["from"], hop["to"], hop["asset_id"])
        if key not in grouped:
            grouped[key] = {"id": f"edge-{len(grouped)}", "from": hop["from"], "to": hop["to"],
                            "asset_id": hop["asset_id"], "record_ids": [], "provenance": "onchain",
                            "confidence": None}
        grouped[key]["record_ids"].append(record["id"])
    result = _payload("flow", document["subject"], depths, records, list(grouped.values()), account_paths)
    result.update(
        bounds={"window": deepcopy(document["window"]), "requested_depth": document["depth"],
                "reached_depth": document["depth_reached"]},
        coverage={"truncated": document["truncated"], "completeness": deepcopy(document["completeness"]),
                  "filters": deepcopy(document["filters"]), "terminal_accounts": list(document["terminal_accounts"])},
        source={"provider": "FastNEAR Transfers API", "fetched_at": document["fetched_at"],
                "recorded": True, "network": "mainnet"},
        assertion_ids=list(document["assertion_ids"]), shares=deepcopy(document["shares"]),
        state_at_block=deepcopy(document["state_at_block"]))
    return result


def cluster_payload(document, account_paths):
    """Keep relationship rules distinct and retain the facts behind each inference."""
    depths = dict.fromkeys(document["seeds"])
    records, edges = [], []
    for index, relationship in enumerate(document["edges"]):
        record = deepcopy(relationship)
        record.update(id=f"record-{index}", asset_id=None, block_timestamp=None, timestamp_ms=None)
        records.append(record)
        for account in (relationship["from"], relationship["to"]):
            depths.setdefault(account, None)
        edges.append({"id": f"edge-{index}", "from": relationship["from"], "to": relationship["to"],
                      "asset_id": None, "record_ids": [record["id"]], "rule": relationship["rule"],
                      "provenance": relationship["provenance"], "confidence": relationship["confidence"]})
    result = _payload("cluster", document["seeds"][0], depths, records, edges, account_paths)
    result.update(
        bounds={"seeds": list(document["seeds"])},
        coverage={"precision": deepcopy(document["precision"]), "depositors": deepcopy(document["depositors"]),
                  "standard": document["standard"]},
        source={"provider": "Recorded chain reads and attribution inputs", "fetched_at": document["fetched_at"],
                "recorded": True, "network": "mainnet"},
        facts=deepcopy(document["facts"]))
    return result
