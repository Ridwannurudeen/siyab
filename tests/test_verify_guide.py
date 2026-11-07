import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "VERIFY.md"
FENCES = re.compile(r"```([^\r\n]*)\r?\n(.*?)\r?\n```", re.DOTALL)
PYTHON_SUBSTITUTION = re.compile(r'\$\(\s*python -c "([^"]*)"\s*\)')
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
PLACEHOLDER = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
SOCKET_BLOCK_WRAPPER = """
import runpy
import socket
import sys

def blocked(*args, **kwargs):
    raise OSError("network disabled in verify guide test")

socket.socket.connect = blocked
socket.create_connection = blocked

mode = sys.argv[1]
if mode == "-m":
    module = sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
elif mode == "-c":
    source = sys.argv[2]
    sys.argv = ["-c", *sys.argv[3:]]
    exec(compile(source, "<guide command>", "exec"), {"__name__": "__main__"})
else:
    raise SystemExit(f"unsupported python invocation: {mode}")
"""


def shorten_hashes(text):
    return HEX64.sub(lambda match: f"{match.group(0)[:8]}...{match.group(0)[-8:]}", text)


def replace_named_placeholders(expected, actual):
    pieces = []
    placeholders = []
    cursor = 0
    for index, match in enumerate(PLACEHOLDER.finditer(expected)):
        pieces.append(re.escape(expected[cursor:match.start()]))
        group = f"placeholder_{index}"
        pieces.append(f"(?P<{group}>[^\r\n]+?)")
        placeholders.append((group, match.group(0)))
        cursor = match.end()
    pieces.append(re.escape(expected[cursor:]))
    pattern = re.compile(r"\A" + "".join(pieces) + r"\Z")
    matched = pattern.fullmatch(actual)
    if matched is None:
        return actual

    for group, marker in reversed(placeholders):
        start, end = matched.span(group)
        actual = actual[:start] + marker + actual[end:]
    return actual


def output_matches(expected, actual):
    return replace_named_placeholders(expected, actual) == expected


class VerifyGuideTest(unittest.TestCase):
    def run_python(self, arguments):
        self.assertTrue(arguments and arguments[0] == "python", arguments)
        result = subprocess.run(
            [sys.executable, "-c", SOCKET_BLOCK_WRAPPER, *arguments[1:]],
            cwd=ROOT,
            capture_output=True,
            check=False,
            encoding="utf-8",
            text=True,
        )
        return result.returncode, result.stdout + result.stderr

    def run_command(self, command):
        while match := PYTHON_SUBSTITUTION.search(command):
            status, expanded = self.run_python(["python", "-c", match.group(1)])
            self.assertEqual(status, 0, f"{match.group(0)}\n{expanded}")
            command = command[:match.start()] + expanded.strip() + command[match.end():]

        self.assertNotIn("$(", command, f"unsupported substitution in {command}")
        arguments = shlex.split(command, posix=True)
        self.assertTrue(arguments and arguments[0] == "python", command)
        return self.run_python(arguments)

    def test_offline_commands_match_the_published_output(self):
        blocks = [(info.strip(), body) for info, body in FENCES.findall(GUIDE.read_text(encoding="utf-8"))]
        offline = [index for index, (info, _) in enumerate(blocks) if info == "sh offline"]
        self.assertTrue(offline, "the guide has no fenced offline commands")

        for index in offline:
            with self.subTest(command_block=index):
                self.assertLess(index + 1, len(blocks))
                expected_info, expected = blocks[index + 1]
                self.assertEqual(expected_info, "text expected")
                actual_parts = []
                for command in blocks[index][1].splitlines():
                    if not command.strip():
                        continue
                    status, output = self.run_command(command.strip())
                    self.assertEqual(status, 0, f"{command}\n{output}")
                    actual_parts.append(output.removesuffix("\n"))

                actual = shorten_hashes("\n".join(actual_parts))
                self.assertTrue(
                    output_matches(expected, actual),
                    f"guide output block {index + 1} differs from the command output\n"
                    f"expected:\n{expected}\nactual:\n{actual}",
                )

    def test_output_comparison_rejects_extra_or_changed_lines(self):
        expected = "first line\nsecond line"
        self.assertTrue(output_matches(expected, expected))
        self.assertFalse(output_matches(expected, expected + "\nextra line"))
        self.assertFalse(output_matches(expected, "changed line\nsecond line"))

    def test_named_placeholders_replace_only_the_marked_value(self):
        expected = "file: {{TEMP_PATH}}/evidence.json\nstatus: ok"
        actual = "file: C:/temp/run-1/evidence.json\nstatus: ok"
        self.assertTrue(output_matches(expected, actual))
        self.assertFalse(output_matches(expected, actual.replace("/evidence.json", "/other.json")))
        self.assertFalse(output_matches(expected, actual + "\nextra line"))

    def test_socket_calls_fail_under_the_network_block(self):
        attempts = (
            "import socket; socket.socket().connect(('127.0.0.1', 9))",
            "import socket; socket.create_connection(('127.0.0.1', 9))",
        )
        for source in attempts:
            with self.subTest(source=source):
                status, output = self.run_python(["python", "-c", source])
                self.assertNotEqual(status, 0, output)
                self.assertIn("network disabled in verify guide test", output)


if __name__ == "__main__":
    unittest.main()
