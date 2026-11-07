"""Check backticked repository citations in the README and docs."""

import os
import re
import sys
from pathlib import Path

CODE_SPAN = re.compile(r"`([^`]+)`")
FILE_CITATION = re.compile(
    r"(?<![A-Za-z0-9_./:-])"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*(?=[A-Za-z0-9_.-]*[A-Za-z_])[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9_-]*)"
    r"(?P<suffix>::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*"
    r"|:[0-9]+(?:-[0-9]+)?|:[A-Za-z_][A-Za-z0-9_]*)?"
)
CONTINUATION = re.compile(r":[0-9]+(?:-[0-9]+)?")
ADDITIONAL_LINE = re.compile(r",\s*(?P<first>[0-9]+)(?:-(?P<last>[0-9]+))?")
DEFAULT_PATH = re.compile(r"line numbers without a file are `([^`]+)`", re.IGNORECASE)


def _documents(root):
    readme = root / "README.md"
    if readme.is_file():
        yield readme
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            if path.relative_to(root).as_posix() not in {"docs/LOG.md", "docs/DECISIONS.md"}:
                yield path


def _repository_files(root):
    files = set()
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if name not in {".git", "__pycache__", "target", ".venv"}]
        relative_dir = Path(directory).relative_to(root).as_posix()
        for filename in filenames:
            relative = (Path(relative_dir) / filename).as_posix() if relative_dir != "." else filename
            files.add(relative)
    return files


def _resolve_citation_path(path, current_path, repository_files):
    if path in repository_files:
        return path
    if current_path and (current_path == path or current_path.endswith("/" + path)):
        return current_path
    if current_path and "/" not in path:
        sibling = current_path.rpartition("/")[0] + "/" + path
        if sibling in repository_files:
            return sibling
    if "/" in path:
        matches = [candidate for candidate in repository_files if candidate.endswith("/" + path)]
    else:
        matches = [candidate for candidate in repository_files if Path(candidate).name == path]
    if len(matches) == 1:
        return matches[0]
    if current_path in matches:
        return current_path
    return path

def _source(root, relative, cache):
    if relative not in cache:
        path = (root / Path(*relative.split("/"))).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            cache[relative] = None
        elif path.name == ".env" or path.name.startswith(".env."):
            cache[relative] = None
        else:
            cache[relative] = path.read_text(encoding="utf-8")
    return cache[relative]


def _problem(root, doc, line_number, citation, relative, suffix, cache):
    source = _source(root, relative, cache)
    if source is None:
        return (doc, line_number, citation, "file does not exist or is not scanned")
    if suffix.startswith(":") and re.fullmatch(r":[0-9]+(?:-[0-9]+)?", suffix):
        first, separator, last = suffix[1:].partition("-")
        end = int(last) if separator else int(first)
        if int(first) < 1:
            return (doc, line_number, citation, "line numbers must be positive")
        line_count = len(source.splitlines())
        if end > line_count:
            return (doc, line_number, citation, f"line {end} is past end of file ({line_count} lines)")
    else:
        symbol = suffix.lstrip(":").split("::")[-1]
        if symbol not in source:
            return (doc, line_number, citation, f"symbol {symbol!r} does not occur in file")
    return None


def _scan_cell(root, doc, line_number, cell, current_path, repository_files, cache, default_path):
    found = []
    previous_end = 0
    for span in CODE_SPAN.finditer(cell):
        if default_path and "(" in cell[previous_end:span.start()]:
            current_path = default_path
        previous_end = span.end()
        value = span.group(1)
        consumed = []
        for match in FILE_CITATION.finditer(value):
            relative = _resolve_citation_path(match.group("path"), current_path, repository_files)
            suffix = match.group("suffix") or ""
            if suffix or (current_path is None and relative in repository_files):
                current_path = relative
            consumed.append(match.span())
            if suffix:
                citation = f"{match.group('path')}{suffix}"
                problem = _problem(root, doc, line_number, citation, relative, suffix, cache)
                if problem:
                    found.append(problem)
            if suffix.startswith(":") and suffix[1:2].isdigit():
                for extra in ADDITIONAL_LINE.finditer(value[match.end():]):
                    first = extra.group("first")
                    last = extra.group("last")
                    extra_suffix = f":{first}-{last}" if last else f":{first}"
                    citation = f"{match.group('path')}{extra_suffix}"
                    problem = _problem(root, doc, line_number, citation, relative, extra_suffix, cache)
                    if problem:
                        found.append(problem)
        continuation = CONTINUATION.fullmatch(value.strip())
        if continuation and not consumed and current_path is not None:
            suffix = continuation.group(0)
            problem = _problem(root, doc, line_number, suffix, current_path, suffix, cache)
            if problem:
                found.append(problem)
    return found, current_path

def scan(root):
    """Return (document, line, citation, reason) for each invalid backticked citation."""
    root = Path(root).resolve()
    cache = {}
    repository_files = _repository_files(root)
    problems = []
    for document in _documents(root):
        relative_doc = document.relative_to(root).as_posix()
        content = document.read_text(encoding="utf-8")
        default_match = DEFAULT_PATH.search(content)
        default_path = default_match.group(1) if default_match else None
        if default_path:
            default_path = _resolve_citation_path(default_path, None, repository_files)
        current_path = None
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                current_path = None
            if default_path and line.startswith("|"):
                for cell in line.split("|"):
                    found, _ = _scan_cell(root, relative_doc, line_number, cell, default_path,
                                          repository_files, cache, default_path)
                    problems.extend(found)
                current_path = None
            else:
                found, current_path = _scan_cell(root, relative_doc, line_number, line, current_path,
                                                 repository_files, cache, None)
                problems.extend(found)
    return problems

def main():
    root = Path(__file__).resolve().parent.parent
    problems = scan(root)
    for document, line_number, citation, reason in problems:
        print(f"{document}:{line_number}: {citation}: {reason}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
