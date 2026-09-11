"""Turn the model's reply into a Submission (or a bug report, or an error).

Accepted layouts, in order of preference:

    ```c file=function.c
    ...
    ```

    file: function.c        (or "### function.c", or "**function.c**")
    ```c
    ...
    ```

A line beginning with "BUG:" outside any code fence means the model believes
the code has a real defect and is declining to force a proof.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fver.backends.base import Submission, SubmissionSpec

_FENCE_RE = re.compile(r"^```([^\n`]*)\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_FILE_ATTR_RE = re.compile(r"file\s*=\s*[\"']?([^\s\"'`]+)[\"']?")
_LABEL_RE = re.compile(
    r"^\s*(?:#+\s*|\*\*|file\s*:\s*|filename\s*:\s*|`)?\s*([\w./+-]+\.[A-Za-z0-9]+)\s*(?:\*\*|`)?\s*:?\s*$",
    re.IGNORECASE,
)
_BUG_RE = re.compile(r"^\s*BUG:\s*(.*)$", re.MULTILINE)


@dataclass
class ParseError:
    message: str  # feedback text suitable for sending back to the model


@dataclass
class BugReport:
    explanation: str


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text)


def parse_submission(text: str, spec: SubmissionSpec) -> Submission | ParseError | BugReport:
    outside = _strip_fences(text)
    m = _BUG_RE.search(outside)
    if m:
        # Take the BUG: line plus the rest of the paragraph after it.
        start = m.start()
        explanation = outside[start:].strip()
        return BugReport(explanation=explanation)

    files: dict[str, str] = {}
    lines = text.split("\n")
    # Map fence start offsets to the preceding non-blank line for labels.
    for fm in _FENCE_RE.finditer(text):
        info, body = fm.group(1), fm.group(2)
        name = None
        am = _FILE_ATTR_RE.search(info)
        if am:
            name = am.group(1)
        else:
            # Look at the nearest non-blank line above the fence.
            before = text[: fm.start()].rstrip("\n").split("\n")
            for prev in reversed(before[-3:]):
                if prev.strip():
                    lm = _LABEL_RE.match(prev)
                    if lm:
                        name = lm.group(1)
                    break
        if name is None:
            # A single unlabeled fence with a single required file is unambiguous.
            if len(spec.required) == 1 and len(spec.files) == 1:
                name = spec.required[0]
            else:
                lang = info.strip().split()[0] if info.strip() else ""
                cands = [
                    f for f, hint in spec.language_hints.items() if hint == lang and f not in files
                ]
                if len(cands) == 1:
                    name = cands[0]
        if name is None:
            continue
        name = name.split("/")[-1]
        if name in spec.files or not spec.files:
            files[name] = body if body.endswith("\n") else body + "\n"
    del lines

    if not files:
        expected = ", ".join(f"`{f}`" for f in spec.required) or "the required files"
        return ParseError(
            "No submission found. Reply with each file as a fenced code block whose opening "
            f"line is ```<lang> file=<name>, for {expected}."
        )
    missing = [f for f in spec.required if f not in files]
    if missing:
        return ParseError(
            "Missing required file(s): "
            + ", ".join(f"`{f}`" for f in missing)
            + ". Include every required file as a fenced block ```<lang> file=<name>."
        )
    note = outside.strip()
    return Submission(files=files, note=note[:4000])
