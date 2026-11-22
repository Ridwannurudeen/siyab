FROM --platform=linux/amd64 python:3.12.10-slim-bookworm@sha256:97983fa8cc88343512862c62307159a82261c3528dc025f79e5a3f7af43e50b4

ARG SOURCE_DATE_EPOCH=1795304440
ARG SIYAB_NETWORK=mainnet
ARG SIYAB_REGISTRY
ARG SIYAB_DELTA_URL=

WORKDIR /app
COPY enclave/requirements.lock /build/requirements.lock
RUN python -m pip install --no-cache-dir --no-compile --only-binary=:all: --require-hashes -r /build/requirements.lock

COPY enclave/dstack.py enclave/keys.py enclave/labels.py enclave/measure.py enclave/service.py /app/enclave/
COPY gate/__init__.py gate/agent.py gate/base58.py gate/canon.py gate/ed25519.py gate/evidence.py gate/freshness.py gate/rpc.py gate/rules.py gate/signer.py gate/verdict.py /app/gate/
COPY intel/__init__.py intel/alerts.py intel/budget.py intel/labels.py intel/reports.py intel/labels.jsonl intel/issuers.json /app/intel/
COPY fixtures/reduced-recordings.json /app/fixtures/reduced-recordings.json

RUN python -B -c 'import dataclasses,json,os,pathlib; from enclave.service import BuildConfig; config=BuildConfig(os.environ["SIYAB_NETWORK"],os.environ["SIYAB_REGISTRY"],os.environ["SIYAB_DELTA_URL"] or None); pathlib.Path("/app/enclave/build.json").write_text(json.dumps(dataclasses.asdict(config),sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8"); epoch=int(os.environ["SOURCE_DATE_EPOCH"]); paths=[pathlib.Path("/app"),pathlib.Path("/build"),pathlib.Path("/usr/local/lib/python3.12/site-packages")]; [os.utime(p,(epoch,epoch),follow_symlinks=False) for root in paths for p in [root,*root.rglob("*")]]; [pathlib.Path(p).unlink(missing_ok=True) for p in ("/usr/bin/sh","/usr/bin/bash","/usr/bin/dash")]; assert not any(pathlib.Path(p).exists() for p in ("/bin/sh","/bin/bash","/bin/dash","/usr/bin/ssh","/usr/sbin/sshd"))'

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "-E", "-s", "-B", "-m", "enclave.service"]
