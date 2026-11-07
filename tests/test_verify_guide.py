import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from intel import __main__ as intel_main

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "VERIFY.md"
FENCES = re.compile(r"```([^\r\n]*)\r?\n(.*?)\r?\n```", re.DOTALL)
PYTHON_SUBSTITUTION = re.compile(r'\$\(\s*python -c "([^"]*)"\s*\)')
HEX64 = re.compile(r"[0-9a-f]{64}")


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def shorten_hashes(text):
    return HEX64.sub(lambda match: f"{match.group(0)[:8]}...{match.group(0)[-8:]}", text)


class VerifyGuideTest(unittest.TestCase):
    def run_command(self, command):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                while match := PYTHON_SUBSTITUTION.search(command):
                    expanded = io.StringIO()
                    with contextlib.redirect_stdout(expanded):
                        exec(compile(match.group(1), "<guide substitution>", "exec"), {"__name__": "__main__"})
                    command = command[:match.start()] + expanded.getvalue().strip() + command[match.end():]

                self.assertNotIn("$(", command, f"unsupported substitution in {command}")
                arguments = shlex.split(command, posix=True)
                self.assertTrue(arguments and arguments[0] == "python", command)
                if arguments[1:3] == ["-m", "gate"]:
                    status = gate_main.main(arguments[3:])
                elif arguments[1:3] == ["-m", "intel"]:
                    status = intel_main.main(arguments[3:])
                elif arguments[1:2] == ["-c"]:
                    exec(compile(arguments[2], "<guide command>", "exec"), {"__name__": "__main__"})
                    status = 0
                else:
                    self.fail(f"unsupported offline command: {command}")
        return status, (stdout.getvalue() + stderr.getvalue()).replace("\\", "/")

    def test_offline_commands_match_the_published_output(self):
        blocks = [(info.strip(), body) for info, body in FENCES.findall(GUIDE.read_text(encoding="utf-8"))]
        offline = [index for index, (info, _) in enumerate(blocks) if info == "sh offline"]
        self.assertTrue(offline, "the guide has no fenced offline commands")

        for index in offline:
            with self.subTest(command_block=index):
                self.assertLess(index + 1, len(blocks))
                expected_info, expected = blocks[index + 1]
                self.assertEqual(expected_info, "text expected")
                output = []
                for command in blocks[index][1].splitlines():
                    if not command.strip():
                        continue
                    status, actual = self.run_command(command.strip())
                    self.assertEqual(status, 0, f"{command}\n{actual}")
                    output.append(actual)

                normalized = shorten_hashes("\n".join(output))
                for line in expected.splitlines():
                    if line.strip():
                        self.assertIn(line, normalized, line)


if __name__ == "__main__":
    unittest.main()
