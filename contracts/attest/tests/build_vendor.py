"""Rebuild the two test-only, pinned-clock verifier WASMs.

Run under Linux with Rust 1.99.1, its wasm target and clang-18 installed:
  python3 contracts/attest/tests/build_vendor.py --target-dir /home/user/target/attest-t0

Use --archive for a previously downloaded, hash-checked source archive.
The resulting hashes depend on the compiler and Cargo registry paths.
Changed hashes require independent review before updating the build record.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
VENDOR = Path(__file__).resolve().parent / "vendor"
REVISION = "e691bdb98d68dcf74653da58d043453a6318765f"
CRATES = ("tee-verifier", "tee-verifier-interface", "tee-verifier-conversions")
PINS = (("mpc", 1786622400), ("nearai", 1795274641))
ARCHIVE_LIMIT = 64 * 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    target = args.target_dir.resolve()
    if target.is_relative_to(ROOT):
        parser.error("--target-dir must be outside the repository")
    metadata = json.loads((VENDOR / "builds.json").read_text())
    recipes = {}
    for name, key in (("Cargo.toml", "minimal_workspace_manifest_sha256"),
                      ("Cargo.lock", "minimal_lock_sha256")):
        raw = (VENDOR / f"TEST-ONLY-{name}").read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata[key]:
            parser.error(f"TEST-ONLY-{name} does not match the recorded SHA256")
        recipes[name] = raw
    if args.archive:
        if args.archive.stat().st_size > ARCHIVE_LIMIT:
            parser.error("the source archive exceeds 64 MiB")
        raw = args.archive.read_bytes()
    else:
        url = f"https://codeload.github.com/near/mpc/tar.gz/{REVISION}"
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read(ARCHIVE_LIMIT + 1)
    if (len(raw) > ARCHIVE_LIMIT
            or hashlib.sha256(raw).hexdigest() != metadata["upstream_archive"]["sha256"]):
        parser.error("the source archive does not match the recorded SHA256")
    scratch = ROOT / ".codex"
    scratch.mkdir(exist_ok=True)
    artifacts = []
    with tempfile.TemporaryDirectory(prefix="attest-t0-build-", dir=scratch) as temporary:
        work = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for crate in CRATES:
                for name in ("Cargo.toml", "src/lib.rs"):
                    relative = f"crates/{crate}/{name}"
                    member = archive.getmember(f"mpc-{REVISION}/{relative}")
                    if not member.isfile():
                        parser.error(f"the source archive member is not a file: {relative}")
                    data = archive.extractfile(member).read()
                    if name == "Cargo.toml":
                        text = data.decode("utf-8")
                        start = text.index("[dev-dependencies]")
                        end = text.index("\n[lints]", start)
                        data = (text[:start] + text[end + 1:]).encode("utf-8")
                    path = work / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
        for name, raw in recipes.items():
            (work / name).write_bytes(raw)
        env = dict(os.environ, CARGO_TARGET_DIR=str(target),
                   CC_wasm32_unknown_unknown="clang-18")
        for name, pin in PINS:
            env["TEE_VERIFIER_PINNED_NOW_SECONDS"] = str(pin)
            subprocess.run(["cargo", "+1.99.1", "build", "--locked", "-p", "tee-verifier",
                            "--no-default-features", "--target", "wasm32-unknown-unknown",
                            "--profile", "release-contract", "-j", "2"],
                           cwd=work, env=env, check=True)
            wasm = target / "wasm32-unknown-unknown/release-contract/tee_verifier.wasm"
            artifacts.append((f"tee-verifier-TEST-ONLY-{name}.wasm", wasm.read_bytes()))
    for name, raw in artifacts:
        digest = hashlib.sha256(raw).hexdigest()
        expected = next(item["sha256"] for item in metadata["artifacts"] if item["file"] == name)
        if digest != expected:
            parser.error(f"{name} has SHA256 {digest}, expected {expected}. "
                         "Compiler or Cargo registry path differences require owner "
                         "independent review before updating builds.json; neither WASM was written")
    for name, raw in artifacts:
        digest = hashlib.sha256(raw).hexdigest()
        (VENDOR / name).write_bytes(raw)
        print(f"{name}: {len(raw)} bytes, SHA256 {digest}, recorded match True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
