from fver.agent.parse import BugReport, ParseError, parse_submission
from fver.backends.base import SubmissionSpec

SPEC = SubmissionSpec(
    files={"function.c": "annotated C", "extra.v": "helper lemmas"},
    required=["function.c"],
    language_hints={"function.c": "c", "extra.v": "coq"},
)
ONE = SubmissionSpec(
    files={"function.c": "c"}, required=["function.c"], language_hints={"function.c": "c"}
)


def test_file_attr_format():
    text = "intro\n```c file=function.c\nint f(void) { return 0; }\n```\n```coq file=extra.v\nLemma x : True.\n```\n"
    s = parse_submission(text, SPEC)
    assert not isinstance(s, (ParseError, BugReport))
    assert s.files["function.c"].startswith("int f")
    assert s.files["extra.v"].strip() == "Lemma x : True."
    assert "intro" in s.note


def test_label_line_formats():
    for label in ("file: function.c", "### function.c", "**function.c**", "`function.c`:"):
        text = f"{label}\n```c\nint g(void);\n```\n"
        s = parse_submission(text, SPEC)
        assert not isinstance(s, (ParseError, BugReport)), label
        assert "int g" in s.files["function.c"]


def test_single_unlabeled_fence_with_one_required_file():
    s = parse_submission("```c\nint h(void);\n```", ONE)
    assert not isinstance(s, (ParseError, BugReport))
    assert "int h" in s.files["function.c"]


def test_language_hint_disambiguates():
    text = "```coq\nLemma y : True.\n```\n```c\nint z(void);\n```\n"
    s = parse_submission(text, SPEC)
    assert not isinstance(s, (ParseError, BugReport))
    assert set(s.files) == {"function.c", "extra.v"}


def test_missing_required_file():
    r = parse_submission("```coq file=extra.v\nLemma y : True.\n```", SPEC)
    assert isinstance(r, ParseError)
    assert "function.c" in r.message


def test_no_fences():
    r = parse_submission("I think it is fine.", SPEC)
    assert isinstance(r, ParseError)


def test_bug_line_wins():
    text = "BUG: n may be negative so buf[n-1] reads before the buffer.\nDetails follow.\n"
    r = parse_submission(text, SPEC)
    assert isinstance(r, BugReport)
    assert "negative" in r.explanation


def test_bug_inside_fence_is_not_a_report():
    text = "```c file=function.c\n// BUG: not really\nint f(void);\n```"
    s = parse_submission(text, SPEC)
    assert not isinstance(s, (ParseError, BugReport))
