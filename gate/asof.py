"""Chain reads bounded by an incident's final block."""

from gate.rpc import Client, MalformedResponse, RpcError, response_gate


class AsOf:
    def __init__(self, client, height):
        if type(height) is not int or height < 0:
            raise ValueError("height must be a non negative integer")
        self.client = client
        self.height = height

    @property
    def last_source(self):
        return self.client.last_source

    @property
    def last_block_height(self):
        return self.client.last_block_height

    def call(self, method, params):
        if (method not in ("query", "block") or not isinstance(params, dict)
                or method == "query" and params.get("request_type") not in
                ("view_account", "view_access_key_list", "call_function")):
            raise ValueError("read cannot be pinned to the replay block")
        params = dict(params)
        if "finality" in params:
            if params["finality"] != "final" or "block_id" in params:
                raise ValueError("read cannot be pinned to the replay block")
            del params["finality"]
            params["block_id"] = self.height
        height = params.get("block_id")
        if type(height) is not int or not 0 <= height <= self.height:
            raise ValueError("read must name a height at or before the replay block")
        try:
            result = self.client.call(method, params)
        except RpcError as error:
            if method == "block":
                raise MalformedResponse("the pinned block could not be read") from error
            try:
                response_gate({"error": error.error}, method, params, block_hash=self._block_hash)
            except RpcError:
                raise error
        response_gate({"result": result}, method, params, block_hash=self._block_hash)
        body = result.get("header") if method == "block" and isinstance(result, dict) else result
        observed = body.get("height" if method == "block" else "block_height") if isinstance(body, dict) else None
        if type(observed) is not int or observed != height:
            raise MalformedResponse(f"read at block {height} came back at block {observed}")
        return result

    def _block_hash(self, height):
        source, observed = self.client.last_source, self.client.last_block_height
        try:
            return self.block_at(height)["header"]["hash"]
        finally:
            self.client.last_source, self.client.last_block_height = source, observed

    view_account = Client.view_account
    view_account_at = Client.view_account_at
    code_hash = Client.code_hash
    call_function = Client.call_function
    view_access_key_list = Client.view_access_key_list
    final_block = Client.final_block
    block_at = Client.block_at

    def __getattr__(self, name):
        raise ValueError(f"read path {name!r} cannot be pinned to the replay block")
