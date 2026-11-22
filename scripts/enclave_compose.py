"""Print a dstack app-compose manifest for a reviewed immutable enclave image."""

import argparse
import json
import re
import sys


IMAGE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{64}")


def app_compose(image):
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("image must be a repository pinned by lowercase sha256 digest without a tag")
    compose = (
        "services:\n"
        "  enclave:\n"
        f"    image: {image}\n"
        "    platform: linux/amd64\n"
        "    user: '65532:65532'\n"
        "    read_only: true\n"
        "    cap_drop: [ALL]\n"
        "    security_opt: [no-new-privileges:true]\n"
        "    ulimits:\n"
        "      core: 0\n"
        "    tmpfs:\n"
        "      - /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777\n"
        "    volumes:\n"
        "      - /var/run/dstack.sock:/var/run/dstack.sock:ro\n"
        "    ports:\n"
        "      - '8080:8080'\n"
        "    restart: unless-stopped\n"
    )
    return {"manifest_version": 2, "name": "siyab-enclave", "runner": "docker-compose",
            "docker_compose_file": compose, "allowed_envs": [], "kms_enabled": False,
            "gateway_enabled": False, "local_key_provider_enabled": False,
            "public_logs": False, "public_sysinfo": False, "public_tcbinfo": True,
            "no_instance_id": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="reviewed repository@sha256:digest")
    args = parser.parse_args(argv)
    try:
        document = app_compose(args.image)
    except ValueError as error:
        parser.error(str(error))
    sys.stdout.buffer.write((json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
