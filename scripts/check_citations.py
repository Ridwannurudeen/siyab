"""Check backticked repository citations in Markdown documentation."""

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
IDENTIFIER_PATH = (
    r"(?:[A-Za-z0-9_.-]+/)*(?=[A-Za-z0-9_.-]*[A-Za-z_])"
    r"[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9_-]*"
)
IDENTIFIER_LINE = r"[0-9]+(?:-[0-9]+)?"
IDENTIFIER_LINES = rf"{IDENTIFIER_LINE}(?:\s*,\s*{IDENTIFIER_LINE})*"
IDENTIFIER_CITATION_SPAN = (
    rf"`(?:{IDENTIFIER_PATH}:{IDENTIFIER_LINES}|:{IDENTIFIER_LINE})`"
)
IDENTIFIER_CITATION_SEQUENCE = (
    rf"{IDENTIFIER_CITATION_SPAN}(?:\s*[,;]\s*{IDENTIFIER_CITATION_SPAN})*"
)
NAMED_ADJACENT_CITATION = re.compile(
    rf"`(?P<name>[A-Za-z_][A-Za-z0-9_]*)`\s*\(\s*"
    rf"(?P<citations>{IDENTIFIER_CITATION_SEQUENCE})\s*\)"
)
NAMED_PARENTHETICAL_CITATION = re.compile(
    rf"\(\s*`(?P<name>[A-Za-z_][A-Za-z0-9_]*)`\s*,\s*"
    rf"(?P<citations>{IDENTIFIER_CITATION_SEQUENCE})\s*\)"
)
STRING_LITERAL = re.compile(r"""(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')""")

def _documents(root):
    for relative in ("README.md", "scripts/README.md", "mcp/README.md"):
        path = root / relative
        if path.is_file():
            yield path
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            if relative in {"docs/LOG.md", "docs/DECISIONS.md", "docs/SPIKES.md"}:
                continue
            if relative.startswith("docs/reviews/"):
                continue
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


def _named_citations(line, inherited_path, relative_doc, repository_files):
    patterns = (NAMED_ADJACENT_CITATION, NAMED_PARENTHETICAL_CITATION)
    for pattern in patterns:
        for match in pattern.finditer(line):
            current_path = inherited_path
            for span in CODE_SPAN.finditer(line, 0, match.start()):
                value = span.group(1)
                for citation in FILE_CITATION.finditer(value):
                    relative = _resolve_citation_path(citation.group("path"), relative_doc, repository_files)
                    suffix = citation.group("suffix") or ""
                    if suffix or (current_path is None and relative in repository_files):
                        current_path = relative
            citations = {}
            for span in CODE_SPAN.finditer(match.group("citations")):
                value = span.group(1)
                if value.startswith(":"):
                    if current_path is not None:
                        citations.setdefault(current_path, []).append(value[1:])
                    continue
                file_match = re.fullmatch(
                    rf"(?P<path>{IDENTIFIER_PATH}):(?P<lines>{IDENTIFIER_LINES})", value
                )
                if file_match:
                    current_path = _resolve_citation_path(
                        file_match.group("path"), relative_doc, repository_files
                    )
                    citations.setdefault(current_path, []).append(file_match.group("lines"))
            line_ranges = {}
            for citation_path, spans in citations.items():
                line_ranges[citation_path] = []
                for cited_lines in spans:
                    for value in re.findall(IDENTIFIER_LINE, cited_lines):
                        first, separator, last = value.partition("-")
                        line_ranges[citation_path].append(
                            (int(first), int(last) if separator else int(first))
                        )
            yield match.group("name"), line_ranges


def _code_text(source, identifier, relative):
    suffix = Path(relative).suffix.lower()
    if suffix == ".md":
        return source
    lines = []
    for line in source.splitlines():
        if suffix == ".sh":
            lines.append(line.split("#", 1)[0])
            continue

        def keep_field_name(match):
            value = match.group(0)[1:-1]
            before = line[:match.start()]
            after = line[match.end():]
            if value == identifier and (
                re.match(r"\s*:", after) or re.search(r"(?:\.get\s*\(|\[\s*)$", before)
            ):
                return f" {value} "
            return " "

        line = STRING_LITERAL.sub(keep_field_name, line)
        if suffix == ".py":
            line = line.split("#", 1)[0]
        elif suffix == ".rs":
            line = line.split("//", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def _identifier_is_code_name(sources, identifier):
    token = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])")
    has_value_only = False
    for source, relative, _ in sources:
        code = _code_text(source, identifier, relative)
        if token.search(code):
            return True
        if token.search(code.casefold()) or token.search(source):
            has_value_only = True
    return not has_value_only


def _identifier_is_cited(source, identifier, line_ranges, relative):
    source_lines = source.splitlines()
    token = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])")
    for first, last in line_ranges:
        for number in range(max(first, 1), min(last, len(source_lines)) + 1):
            line = _code_text(source_lines[number - 1], identifier, relative)
            if token.search(line) or token.search(line.casefold()):
                return True
    return False


def identifier_citation_count(root):
    root = Path(root).resolve()
    repository_files = _repository_files(root)
    cache = {}
    count = 0
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
            line_path = default_path if default_path and line.startswith("|") else current_path
            for name, citations in _named_citations(line, line_path, relative_doc, repository_files):
                sources = []
                for citation_path in citations:
                    source = _source(root, citation_path, cache)
                    if source is not None:
                        sources.append((source, citation_path, []))
                if _identifier_is_code_name(sources, name):
                    count += 1
            if default_path and line.startswith("|"):
                for cell in line.split("|"):
                    _scan_cell(root, relative_doc, line_number, cell, default_path,
                               repository_files, cache, default_path)
                current_path = None
            else:
                _, current_path = _scan_cell(root, relative_doc, line_number, line, current_path,
                                              repository_files, cache, None)
    return count

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
            line_path = default_path if default_path and line.startswith("|") else current_path
            for name, citations in _named_citations(line, line_path, relative_doc, repository_files):
                sources = []
                for citation_path, line_ranges in citations.items():
                    source = _source(root, citation_path, cache)
                    if source is not None:
                        sources.append((source, citation_path, line_ranges))
                if _identifier_is_code_name(sources, name):
                    if not any(
                        _identifier_is_cited(source, name, line_ranges, relative)
                        for source, relative, line_ranges in sources
                    ):
                        problems.append((relative_doc, line_number, "citation", f"{name} not in cited lines"))
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
