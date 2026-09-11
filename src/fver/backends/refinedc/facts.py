"""Every assumption about RefinedC that could not be verified against a real
install, in one place.

RefinedC is not installed on the machine this backend was written on. Each
constant below encodes something we believe about its CLI, file layout,
annotation language or error output. When a real install disagrees, fix the
constant here; nothing else in the backend should hardcode these.

Sources of the beliefs: the RefinedC paper (Sammler et al., PLDI 2021), the
public repository layout (gitlab.mpi-sws.org/iris/refinedc) as remembered,
and its examples directory. Confidence is noted per item.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Binaries (confidence: high for names, medium for `rocq` replacing `coqc`)
# ---------------------------------------------------------------------------
REFINEDC_BIN = "refinedc"
COQC_BIN = "coqc"  # Rocq >= 9 also ships `rocq compile`; we try both.
ROCQ_BIN = "rocq"
DUNE_BIN = "dune"
OPAM_BIN = "opam"
CERBERUS_BIN = "cerberus"

# ---------------------------------------------------------------------------
# CLI (confidence: high)
# ---------------------------------------------------------------------------
INIT_ARGV = ["init"]  # `refinedc init` run inside the project directory
CHECK_ARGV = ["check"]  # `refinedc check <file.c>`; searches upward for PROJECT_FILE
CLEAN_ARGV = ["clean"]
VERSION_ARGV = ["--version"]

# ASSUMPTION (confidence: low): `refinedc check` does not take -I/-D flags; the
# file is preprocessed by Cerberus with its own libc headers. If a real install
# accepts include flags, set `pass_include_flags = true` and adjust this format.
INCLUDE_FLAG_FMT = "-I{dir}"
DEFINE_FLAG_FMT = "-D{macro}"

# ---------------------------------------------------------------------------
# Project layout (confidence: medium)
# ---------------------------------------------------------------------------
PROJECT_FILE = "rc-project.toml"
# `refinedc init` writes rc-project.toml with a `coq_root` key naming the
# dune/Coq logical root of the generated files. We write our own if the tool
# is missing so the rest of the pipeline can be exercised.
PROJECT_FILE_TEMPLATE = 'coq_root = "{coq_root}"\n'
DEFAULT_COQ_ROOT = "refinedc.project.fver"

# Generated files for <project>/<rel>/<stem>.c land in <project>/proofs/<rel>/<stem>/
# (confidence: medium; some versions use <dir of file>/proofs/<stem>/).
PROOFS_DIR_NAME = "proofs"
GENERATED_CODE = "generated_code.v"
GENERATED_SPEC = "generated_spec.v"
GENERATED_PROOF_FMT = "generated_proof_{fn}.v"
# Coq module path of generated files: <coq_root>.<rel path with '/' -> '.'>.<stem>
# dune compiles them into _build/default/proofs/... (confidence: medium).
DUNE_BUILD_DIR = "_build/default"

# The typing lemma RefinedC generates for a function (confidence: high).
PROOF_LEMMA_FMT = "type_{fn}"
SPEC_DEF_FMT = "type_of_{fn}"

# ---------------------------------------------------------------------------
# Header and annotations (confidence: high for names)
# ---------------------------------------------------------------------------
HEADER_INCLUDE = "#include <refinedc.h>"
HEADER_NAMES = ("refinedc.h",)
ATTR_PREFIX = "rc::"

# Function-level attributes (before the definition).
ATTR_PARAMETERS = "rc::parameters"
ATTR_ARGS = "rc::args"
ATTR_RETURNS = "rc::returns"
ATTR_REQUIRES = "rc::requires"
ATTR_ENSURES = "rc::ensures"
ATTR_EXISTS = "rc::exists"
ATTR_TACTICS = "rc::tactics"
ATTR_LEMMAS = "rc::lemmas"
ATTR_IMPORT = "rc::import"
# Loop-level attributes (before for/while/do).
ATTR_INV_VARS = "rc::inv_vars"
ATTR_CONSTRAINTS = "rc::constraints"
# Struct-level.
ATTR_REFINED_BY = "rc::refined_by"
ATTR_FIELD = "rc::field"
ATTR_GLOBAL = "rc::global"

# Escape hatches an LLM must never use (confidence: high for names).
FORBIDDEN_ATTRIBUTES = ("rc::trust_me", "rc::skip", "rc::manual_proof")
# rc::import is added by the backend itself when a lemmas.v is supplied; the
# LLM may not import arbitrary modules.
LLM_FORBIDDEN_ATTRIBUTES = FORBIDDEN_ATTRIBUTES + ("rc::import",)

# Ghost statements from refinedc.h that are semantically no-ops and may be
# added by the annotator (confidence: medium for the exact list).
GHOST_STATEMENT_PREFIX = "rc_"
GHOST_STATEMENTS = (
    "rc_unfold",
    "rc_unfold_int",
    "rc_annot",
    "rc_unlock",
    "rc_lock",
    "rc_copy_alloc_id",
    "rc_reduce",
    "rc_learn",
)

# ---------------------------------------------------------------------------
# Helper lemmas mechanism (confidence: low-medium)
# ---------------------------------------------------------------------------
# A file lemmas.v placed in the generated proofs directory is compiled by dune
# alongside the generated files. It is made visible to the proof of function
# <fn> by adding [[rc::import("lemmas", "<coq_root>.<module path>")]] to the
# function's attribute block, and its lemmas are handed to the solver via
# [[rc::lemmas("name", ...)]]. The LLM writes rc::lemmas itself.
LEMMAS_FILE = "lemmas.v"
LEMMAS_MODULE = "lemmas"
IMPORT_ATTR_FMT = '[[rc::import("{module}", "{path}")]]'

# ---------------------------------------------------------------------------
# Forbidden Coq vocabulary in LLM-written lemmas.v (confidence: high)
# ---------------------------------------------------------------------------
FORBIDDEN_COQ_PATTERNS = (
    r"\bAdmitted\b",
    r"\badmit\b",
    r"\bgive_up\b",
    r"\bAxiom\b",
    r"\bAxioms\b",
    r"\bParameter\b",
    r"\bParameters\b",
    r"\bHypothesis\b",
    r"\bHypotheses\b",
    r"\bConjecture\b",
    r"\bDeclare\s+ML\s+Module\b",
    r"\bUnset\s+Guard\s+Checking\b",
    r"\bUnset\s+Positivity\s+Checking\b",
    r"\bUnset\s+Universe\s+Checking\b",
    r"\bUnset\s+Strict\s+Universe\s+Declaration\b",
    r"\bLtac2\s+@\s*external\b",
)

# ---------------------------------------------------------------------------
# Output classification (confidence: low; refine against real output)
# ---------------------------------------------------------------------------
# Substrings marking a front-end (Cerberus / annotation parser) failure.
FRONTEND_ERROR_MARKERS = (
    "cerberus",
    "Cerberus",
    "parse error",
    "Parse error",
    "Parsing error",
    "unknown attribute",
    "Unknown attribute",
    "invalid annotation",
    "Invalid annotation",
    "annotation error",
    "Annotation error",
    "unsupported",
    "Unsupported",
    "not supported",
    "Not supported",
    "syntax error",
    "Syntax error",
    "undeclared",
    "Undeclared",
)
# Front-end output that indicates the *code* has UB (Cerberus can detect some
# statically). Treated as a bug witness.
UB_MARKERS = ("undefined behaviour", "undefined behavior", "UB:")
# Coq's goal separator.
COQ_GOAL_SEPARATOR = "============================"
COQ_ERROR_MARKER = "Error:"
# Text in a failing goal that means Lithium got stuck typing (annotations
# wrong or missing) rather than leaving a pure side condition.
STUCK_MARKERS = (
    "◁",
    "⊢",
    "typed_",
    "find_in_context",
    "subsume",
    "Cannot find",
    "cannot find",
    "no ownership",
    "not found in context",
    "li_",
    "FindLoc",
    "FindVal",
    "typing",
    "type_",
    "solve_goal failed",
    "Cannot solve side condition",
    "Cannot solve",
)
# Words that make a goal look like a pure fact (arithmetic / lists / sets).
PURE_GOAL_HINTS = ("≤", "<", "≥", ">", "=", "length", "replicate", "take", "drop", "∈", "∉", "Z")
# Source-location regexes (file:line[:col]).
LOCATION_REGEX = r"(?P<file>[^\s:]+\.c):(?P<line>\d+)(?::(?P<col>\d+))?"
# RefinedC sometimes names the function being checked.
FUNCTION_REGEX = r"(?:function|Function|in)\s+`?(?P<fn>[A-Za-z_]\w*)`?"

# ---------------------------------------------------------------------------
# Audit (confidence: low-medium)
# ---------------------------------------------------------------------------
# Run `coqc -Q <build proofs dir> <coq_root> audit_<fn>.v` where the audit file
# contains `Require Import <coq_root>.<modpath>.generated_proof_<fn>.` and
# `Print Assumptions type_<fn>.`
AUDIT_FILE_FMT = "audit_{fn}.v"
AUDIT_SCRIPT_FMT = (
    "From {coq_root}.{modpath} Require Import {proof_module}.\nPrint Assumptions {lemma}.\n"
)
AUDIT_CLOSED_MARKER = "Closed under the global context"
AUDIT_AXIOMS_HEADER = "Axioms:"
# Axioms Iris/stdpp/RefinedC themselves rely on; anything else fails the audit.
ALLOWED_AXIOMS = frozenset(
    {
        "functional_extensionality_dep",
        "propositional_extensionality",
        "proof_irrelevance",
        "classic",
        "Classical_Prop.classic",
        "constructive_indefinite_description",
        "Eqdep.Eq_rect_eq.eq_rect_eq",
        "JMeq_eq",
        "epsilon_statement",
    }
)

# ---------------------------------------------------------------------------
# Behavioural assumptions (confidence: medium)
# ---------------------------------------------------------------------------
# 1. Functions with no rc:: attributes are translated but not verified, so a
#    file with one annotated function checks only that function.
# 2. A prototype (declaration without body) carrying rc:: attributes is
#    accepted as an assumed spec, which is how verified callees and external
#    functions are introduced without re-verifying their bodies.
# 3. `refinedc check` exits 0 only if every generated proof compiles.
