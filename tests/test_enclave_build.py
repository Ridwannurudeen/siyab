import ast
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.enclave_compose import app_compose


ROOT = Path(__file__).resolve().parent.parent
IMAGE = "registry.example/siyab@sha256:" + "ab" * 32


class EnclaveBuildTests(unittest.TestCase):
    def test_compose_is_deterministic_and_has_no_environment_inputs(self):
        first = app_compose(IMAGE)
        second = app_compose(IMAGE)
        self.assertEqual(first, second)
        self.assertEqual(first["manifest_version"], 2)
        self.assertEqual(first["runner"], "docker-compose")
        self.assertEqual(first["allowed_envs"], [])
        self.assertFalse(first["kms_enabled"])
        self.assertFalse(first["local_key_provider_enabled"])
        self.assertFalse(first["gateway_enabled"])
        self.assertFalse(first["public_logs"])
        self.assertFalse(first["public_sysinfo"])
        compose = first["docker_compose_file"]
        self.assertEqual(compose.count("    image:"), 1)
        self.assertEqual(compose.count("      - /var/run/dstack.sock:"), 1)
        for token in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "core: 0", "/tmp:rw,noexec"):
            self.assertIn(token, compose)
        for token in ("environment:", "env_file:", "command:", "entrypoint:", "build:", "privileged:", "ssh", "${"):
            self.assertNotIn(token, compose)

    def test_mutable_and_injectable_image_references_refused(self):
        for image in ("siyab:latest", "registry.example/siyab", IMAGE.upper(), IMAGE + "\nservices: {}",
                      IMAGE.replace("siyab@", "siyab:latest@"), "$(id)/siyab@sha256:" + "ab" * 32):
            with self.subTest(image=image), self.assertRaises(ValueError):
                app_compose(image)

    def test_cli_outputs_identical_utf8_bytes(self):
        command = [sys.executable, str(ROOT / "scripts/enclave_compose.py"), "--image", IMAGE]
        first = subprocess.run(command, capture_output=True, check=True).stdout
        second = subprocess.run(command, capture_output=True, check=True).stdout
        self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        self.assertEqual(json.loads(first), app_compose(IMAGE))
        self.assertTrue(first.endswith(b"\n"))
        self.assertNotIn(b"\r\n", first)

    def test_dockerfile_copies_only_explicit_runtime_inputs(self):
        dockerfile = (ROOT / "enclave/Dockerfile").read_text(encoding="utf-8")
        copies = [line.split()[1:-1] for line in dockerfile.splitlines() if line.startswith("COPY ")]
        for sources in copies:
            for source in sources:
                self.assertTrue((ROOT / source).is_file(), source)
                self.assertNotIn("*", source)
                self.assertTrue(source.endswith((".py", ".json", ".jsonl", ".lock")))
        copied = {source for sources in copies for source in sources}
        self.assertNotIn("enclave/relay.py", copied)
        self.assertNotIn("gate/__main__.py", copied)
        self.assertIn("fixtures/reduced-recordings.json", copied)
        self.assertIn("intel/labels.jsonl", copied)
        for source in copied:
            if not source.endswith(".py"):
                continue
            tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in ("gate", "intel", "enclave"):
                    for name in node.names:
                        self.assertIn(f"{node.module}/{name.name}.py", copied)

    def test_build_settings_and_entrypoint_are_fixed_in_image(self):
        dockerfile = (ROOT / "enclave/Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"FROM --platform=linux/amd64 python:3\.12\.10-slim-bookworm@sha256:[0-9a-f]{64}")
        self.assertIn("ARG SIYAB_REGISTRY\n", dockerfile)
        self.assertIn("ARG SIYAB_NETWORK=mainnet", dockerfile)
        self.assertIn("BuildConfig(", dockerfile)
        self.assertIn("dataclasses.asdict(config)", dockerfile)
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn('ENTRYPOINT ["python", "-E", "-s", "-B", "-m", "enclave.service"]', dockerfile)
        self.assertNotIn("ENV SIYAB_", dockerfile)
        record = json.loads((ROOT / "enclave/build-record.json").read_text(encoding="utf-8"))
        self.assertTrue(record["equal"])
        self.assertTrue(record["no_cache"])
        self.assertTrue(record["independent_builders"])
        self.assertTrue(record["all_blob_hashes_verified"])
        self.assertEqual(len(record["builds"]), 2)
        self.assertEqual(record["builds"][0], record["builds"][1])
        for name, digest in record["input_sha256"].items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest, name)

    def test_lock_has_exact_wheel_urls_and_hashes(self):
        lines = (ROOT / "enclave/requirements.lock").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z]+ @ https://files\.pythonhosted\.org/packages/.+\.whl --hash=sha256:[0-9a-f]{64}$")
        self.assertIn("pynacl-1.6.2-cp38-abi3-manylinux2014_x86_64", lines[0])
        self.assertIn("cffi-2.0.0-cp312-cp312-manylinux2014_x86_64", lines[1])
        self.assertIn("pycparser-3.0-py3-none-any", lines[2])
