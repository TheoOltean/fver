"""RefinedC backend: verify C functions with RefinedC on Rocq.

Settings ([backend.refinedc] in config.toml), all optional:
  refinedc_bin       = "refinedc"   path/name of the refinedc CLI
  coqc_bin           = "coqc"       Coq/Rocq compiler used for the audit
  dune_bin           = "dune"
  cerberus_bin       = "cerberus"
  coq_root           = "refinedc.project.fver"  logical root of generated files
  extra_check_args   = []           extra argv appended to `refinedc check`
  pass_include_flags = false        forward -I/-D from the build to refinedc check
  keep_artifacts     = true         keep generated .v files after a check
  allowed_axioms     = []           extra axiom names the audit tolerates

Nothing here ever writes outside the backend workspace directory, and the
user's source files are only ever read.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fver.backends.base import (
    AuditResult,
    CheckOutcome,
    CheckResult,
    FunctionTask,
    PromptContext,
    Submission,
    SubmissionSpec,
    TranslateResult,
)
from fver.backends.refinedc import annotations as ann
from fver.backends.refinedc import facts
from fver.backends.refinedc.parse_output import classify
from fver.core.models import FunctionInfo, Target, ToolStatus, TranslationUnit, sha256_text
from fver.util.platform import install_hint
from fver.util.proc import run, version_of, which

_PROMPTS_DIR = Path(__file__).parent / "prompts"

INSTALL_HINT = (
    "Install via opam: `opam repo add coq-released https://coq.inria.fr/opam/released && "
    "opam repo add iris-dev https://gitlab.mpi-sws.org/iris/opam.git && "
    "opam pin add refinedc https://gitlab.mpi-sws.org/iris/refinedc.git`"
)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "x"


class RefinedCBackend:
    name = "refinedc"

    def __init__(self, workspace_dir: Path, settings: dict[str, Any], target: Target):
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.settings = dict(settings or {})
        self.target = target
        self.refinedc_bin: str = self.settings.get("refinedc_bin", facts.REFINEDC_BIN)
        self.coqc_bin: str = self.settings.get("coqc_bin", facts.COQC_BIN)
        self.dune_bin: str = self.settings.get("dune_bin", facts.DUNE_BIN)
        self.cerberus_bin: str = self.settings.get("cerberus_bin", facts.CERBERUS_BIN)
        self.coq_root: str = self.settings.get("coq_root", facts.DEFAULT_COQ_ROOT)
        self.extra_check_args: list[str] = list(self.settings.get("extra_check_args", []))
        self.pass_include_flags: bool = bool(self.settings.get("pass_include_flags", False))
        self.keep_artifacts: bool = bool(self.settings.get("keep_artifacts", True))
        self.allowed_axioms = set(facts.ALLOWED_AXIOMS) | set(
            self.settings.get("allowed_axioms", [])
        )
        self._versions: dict[str, str] | None = None

    # ------------------------------------------------------------------ tools

    def _have_refinedc(self) -> bool:
        return which(self.refinedc_bin) is not None

    def doctor(self) -> list[ToolStatus]:
        def status(
            binname: str, required: bool, hint: str, version_argv: list[str] | None = None
        ) -> ToolStatus:
            path = which(binname)
            ver = version_of([binname] + (version_argv or ["--version"])) if path else None
            return ToolStatus(
                name=binname,
                found=path is not None,
                path=path,
                version=ver,
                required=required,
                hint=hint,
            )

        coq_hint = "Install Rocq/Coq via opam (`opam install coq` or `opam install rocq-prover`)."
        out = [
            status(self.refinedc_bin, True, INSTALL_HINT),
            status(self.coqc_bin, True, coq_hint),
            status(self.dune_bin, True, "`opam install dune`"),
            status(
                facts.OPAM_BIN,
                False,
                install_hint("opam", url="https://opam.ocaml.org/doc/Install.html"),
            ),
            status(
                self.cerberus_bin,
                False,
                "Installed as a RefinedC dependency; `opam install cerberus` otherwise.",
            ),
        ]
        if not out[1].found and which(facts.ROCQ_BIN):
            out[1] = status(facts.ROCQ_BIN, True, coq_hint)
        return out

    def tool_versions(self) -> dict[str, str]:
        if self._versions is None:
            v: dict[str, str] = {}
            for binname in (self.refinedc_bin, self.coqc_bin, self.cerberus_bin):
                ver = version_of([binname, "--version"])
                if ver:
                    v[Path(binname).name] = ver
            self._versions = v
        return dict(self._versions)

    # ---------------------------------------------------------------- project

    @property
    def project_file(self) -> Path:
        return self.workspace_dir / facts.PROJECT_FILE

    def prepare(self, tus: list[TranslationUnit], repo_root: Path) -> None:
        if self.project_file.exists():
            return
        if self._have_refinedc():
            r = run([self.refinedc_bin, *facts.INIT_ARGV], cwd=self.workspace_dir, timeout=120)
            if r.ok and self.project_file.exists():
                return
        # Fall back to writing the project file ourselves (facts.PROJECT_FILE_TEMPLATE).
        self.project_file.write_text(
            facts.PROJECT_FILE_TEMPLATE.format(coq_root=self.coq_root), encoding="utf-8"
        )

    def _module_path(self, c_file: Path) -> str:
        """Coq module path of the generated files for a .c file inside the project."""
        rel = c_file.resolve().relative_to(self.workspace_dir.resolve()).with_suffix("")
        return ".".join(_safe(p) for p in rel.parts)

    def _proofs_dir(self, c_file: Path) -> Path:
        rel = c_file.resolve().relative_to(self.workspace_dir.resolve()).with_suffix("")
        return self.workspace_dir / facts.PROOFS_DIR_NAME / rel

    def _check_argv(self, c_file: Path, tu: TranslationUnit | None) -> list[str]:
        argv = [self.refinedc_bin, *facts.CHECK_ARGV, str(c_file)]
        if self.pass_include_flags and tu is not None:
            for i, a in enumerate(tu.arguments):
                if a.startswith("-I") and len(a) > 2:
                    argv.append(facts.INCLUDE_FLAG_FMT.format(dir=a[2:]))
                elif a == "-I" and i + 1 < len(tu.arguments):
                    argv.append(facts.INCLUDE_FLAG_FMT.format(dir=tu.arguments[i + 1]))
                elif a.startswith("-D") and len(a) > 2:
                    argv.append(facts.DEFINE_FLAG_FMT.format(macro=a[2:]))
        argv += self.extra_check_args
        return argv

    # -------------------------------------------------------------- translate

    def translate(
        self, tu: TranslationUnit, functions: list[FunctionInfo], repo_root: Path
    ) -> TranslateResult:
        names = [f.name for f in functions]
        src = Path(repo_root) / tu.source_path
        dest_dir = self.workspace_dir / "src" / tu.id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(tu.source_path).name
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return TranslateResult(
                supported={n: False for n in names},
                reasons={n: f"cannot read source: {e}" for n in names},
                tu_error=str(e),
            )
        if not ann.has_refinedc_include(text):
            text = facts.HEADER_INCLUDE + "\n" + text
        dest.write_text(text, encoding="utf-8")  # a copy; the user's file is untouched
        if not self._have_refinedc():
            reason = "refinedc not installed; front-end check skipped"
            return TranslateResult(
                supported={n: True for n in names},
                reasons={n: reason for n in names},
                artifacts={"copy": str(dest)},
            )
        self.prepare([tu], repo_root)
        r = run(self._check_argv(dest, tu), cwd=self.workspace_dir, timeout=600)
        combined = r.stdout + "\n" + r.stderr
        if r.ok:
            return TranslateResult(
                supported={n: True for n in names}, artifacts={"copy": str(dest)}
            )
        # Map errors to functions by line (the copy has one extra header line).
        offset = (
            0 if ann.has_refinedc_include(src.read_text(encoding="utf-8", errors="replace")) else 1
        )
        supported = {n: True for n in names}
        reasons: dict[str, str] = {}
        tu_error: str | None = None
        for ln in combined.splitlines():
            m = re.search(facts.LOCATION_REGEX, ln)
            if not m:
                continue
            line = int(m.group("line")) - offset
            hit = next((f for f in functions if f.start_line <= line <= f.end_line), None)
            if hit is not None:
                supported[hit.name] = False
                reasons[hit.name] = ln.strip()
            else:
                tu_error = (tu_error + "\n" if tu_error else "") + ln.strip()
        if tu_error is None and all(supported.values()):
            tu_error = "refinedc check failed: " + "\n".join(combined.strip().splitlines()[-10:])
        if tu_error and all(supported.values()):
            # A whole-file failure we could not attribute to a function.
            supported = {n: False for n in names}
            reasons = {n: "translation unit rejected by the front-end" for n in names}
        return TranslateResult(
            supported=supported, reasons=reasons, tu_error=tu_error, artifacts={"copy": str(dest)}
        )

    # ----------------------------------------------------------------- prompt

    def submission_spec(self) -> SubmissionSpec:
        return SubmissionSpec(
            files={
                "function.c": "the target function with RefinedC annotations; code otherwise unchanged",
                facts.LEMMAS_FILE: "optional Rocq helper lemmas (only when a pure side condition needs one)",
            },
            required=["function.c"],
            language_hints={"function.c": "c", facts.LEMMAS_FILE: "coq"},
        )

    def prompt_context(self) -> PromptContext:
        def read(name: str) -> str:
            p = _PROMPTS_DIR / name
            return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

        forbidden = [re.escape(a) for a in facts.LLM_FORBIDDEN_ATTRIBUTES] + list(
            facts.FORBIDDEN_COQ_PATTERNS
        )
        return PromptContext(
            reference=read("reference.md"),
            examples=read("examples.md"),
            instructions=read("instructions.md"),
            forbidden_patterns=forbidden,
        )

    # -------------------------------------------------------------- guardrail

    def guardrail(self, submission: Submission) -> list[str]:
        problems: list[str] = []
        fn = submission.files.get("function.c")
        if fn is None:
            problems.append("submission is missing function.c")
        else:
            for a in ann.find_attributes(fn):
                if a.name in facts.LLM_FORBIDDEN_ATTRIBUTES:
                    problems.append(f"forbidden attribute {a.name}")
            for m in re.finditer(r"^\s*#\s*include\b.*$", fn, re.MULTILINE):
                if "refinedc.h" not in m.group(0):
                    problems.append(f"submission adds an include: {m.group(0).strip()}")
            problems += ann.vacuity_check(fn)
        lem = submission.files.get(facts.LEMMAS_FILE)
        if lem:
            for pat in facts.FORBIDDEN_COQ_PATTERNS:
                if re.search(pat, lem):
                    problems.append(f"lemmas.v uses forbidden Coq construct matching /{pat}/")
        for name in submission.files:
            if name not in self.submission_spec().files:
                problems.append(f"unexpected file in submission: {name}")
        return problems

    # ------------------------------------------------------------------ check

    def _check_dir(self, task: FunctionTask) -> Path:
        wd = Path(task.workdir)
        try:
            wd.resolve().relative_to(self.workspace_dir.resolve())
        except ValueError:
            wd = self.workspace_dir / "checks" / _safe(task.function.id)
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def _build_source(self, task: FunctionTask, annotated_fn: str, lemmas_path: str | None) -> str:
        fn_text = annotated_fn
        if lemmas_path:
            # The backend, not the LLM, wires the helper module in.
            imp = facts.IMPORT_ATTR_FMT.format(module=facts.LEMMAS_MODULE, path=lemmas_path)
            fn_text = imp + "\n" + fn_text.lstrip()
        # Verified callees and externals become spec-carrying prototypes; if a
        # callee is defined in this file its definition is replaced (so it is
        # not re-verified), otherwise the prototype is prepended.
        callee_repl: dict[str, tuple[FunctionInfo, str]] = {}
        prototypes: list[str] = []
        for cname, contract in {**task.external_specs, **task.callee_specs}.items():
            proto = ann.contract_prototype(contract)
            rng = ann.find_definition_range(task.source_text, cname)
            if rng is not None and cname != task.function.name:
                info = FunctionInfo(
                    id=f"{task.tu.id}:{cname}",
                    name=cname,
                    tu_id=task.tu.id,
                    source_path=task.function.source_path,
                    start_line=rng[0],
                    end_line=rng[1],
                    signature="",
                    body_hash="",
                )
                callee_repl[cname] = (info, proto)
            else:
                prototypes.append(proto)
        spliced = ann.splice(task.source_text, task.function, fn_text, callee_repl)
        return ann.prepend_prototypes(spliced, prototypes)

    def check(
        self, task: FunctionTask, submission: Submission, timeout_seconds: int
    ) -> CheckResult:
        problems = self.guardrail(submission)
        annotated = submission.files.get("function.c", "")
        if annotated:
            same, why = ann.code_unchanged(task.function_text, annotated)
            if not same:
                problems.append(why)
        if problems:
            return CheckResult(
                outcome=CheckOutcome.GUARDRAIL,
                feedback="Submission rejected before checking:\n- " + "\n- ".join(problems),
            )
        wd = self._check_dir(task)
        stem = _safe(Path(task.function.source_path).stem) or "unit"
        c_file = wd / f"{stem}.c"
        proofs_dir = self._proofs_dir(c_file)
        lemmas_text = submission.files.get(facts.LEMMAS_FILE)
        lemmas_path: str | None = None
        if lemmas_text:
            proofs_dir.mkdir(parents=True, exist_ok=True)
            (proofs_dir / facts.LEMMAS_FILE).write_text(lemmas_text, encoding="utf-8")
            lemmas_path = f"{self.coq_root}.{self._module_path(c_file)}"
        source = self._build_source(task, annotated, lemmas_path)
        c_file.write_text(source, encoding="utf-8")
        artifacts = {"source": str(c_file)}
        if lemmas_text:
            artifacts["lemmas"] = str(proofs_dir / facts.LEMMAS_FILE)
        if not self._have_refinedc():
            return CheckResult(
                outcome=CheckOutcome.TOOL_ERROR,
                feedback=f"`{self.refinedc_bin}` not found on PATH. {INSTALL_HINT}",
                artifacts=artifacts,
                tool_versions=self.tool_versions(),
            )
        self.prepare([task.tu], task.repo_root)
        r = run(self._check_argv(c_file, task.tu), cwd=self.workspace_dir, timeout=timeout_seconds)
        outcome, feedback, goals, witness = classify(
            r.stdout, r.stderr, r.returncode, r.timed_out, task.function.name
        )
        generated = sorted(proofs_dir.glob("*.v")) if proofs_dir.exists() else []
        for g in generated:
            artifacts[g.name] = str(g)
        proof_hash = None
        if outcome is CheckOutcome.OK:
            h = "\n".join(
                f"{g.name}\n{g.read_text(encoding='utf-8', errors='replace')}" for g in generated
            )
            proof_hash = sha256_text(h + "\n" + submission.content_hash())
        elif not self.keep_artifacts and proofs_dir.exists():
            shutil.rmtree(proofs_dir, ignore_errors=True)
        assumptions = [f"external:{n}" for n in task.external_specs] + [
            f"callee:{n}" for n in task.callee_specs
        ]
        return CheckResult(
            outcome=outcome,
            feedback=feedback,
            goals=goals,
            stdout=r.stdout,
            stderr=r.stderr,
            artifacts=artifacts,
            proof_hash=proof_hash,
            assumptions=assumptions,
            tool_versions=self.tool_versions(),
            duration_seconds=r.duration,
            witness=witness,
        )

    # ------------------------------------------------------------------ audit

    def audit(self, task: FunctionTask, result: CheckResult) -> AuditResult:
        violations: list[str] = []
        fn = task.function.name
        proof_name = facts.GENERATED_PROOF_FMT.format(fn=fn)
        proof_file = result.artifacts.get(proof_name)
        if proof_file is None or not Path(proof_file).exists():
            return AuditResult(
                passed=False, violations=[f"generated proof file {proof_name} not found"]
            )
        for name, path in result.artifacts.items():
            p = Path(path)
            if p.suffix == ".v" and p.exists():
                text = p.read_text(encoding="utf-8", errors="replace")
                for pat in facts.FORBIDDEN_COQ_PATTERNS:
                    if re.search(pat, text):
                        violations.append(f"{name}: forbidden construct /{pat}/")
        if not which(self.coqc_bin):
            violations.append(
                "audit tool unavailable: coqc not found; cannot run Print Assumptions"
            )
            return AuditResult(passed=False, violations=violations)
        c_file = Path(result.artifacts.get("source", ""))
        modpath = self._module_path(c_file) if c_file.exists() else _safe(fn)
        proofs_dir = Path(proof_file).parent
        audit_file = proofs_dir / facts.AUDIT_FILE_FMT.format(fn=fn)
        audit_file.write_text(
            facts.AUDIT_SCRIPT_FMT.format(
                coq_root=self.coq_root,
                modpath=modpath,
                proof_module=proof_name[:-2],
                lemma=facts.PROOF_LEMMA_FMT.format(fn=fn),
            ),
            encoding="utf-8",
        )
        build_dir = self.workspace_dir / facts.DUNE_BUILD_DIR / facts.PROOFS_DIR_NAME
        r = run(
            [self.coqc_bin, "-Q", str(build_dir), self.coq_root, str(audit_file)],
            cwd=proofs_dir,
            timeout=600,
        )
        out = r.stdout + "\n" + r.stderr
        assumptions: list[str] = []
        if facts.AUDIT_CLOSED_MARKER in out:
            pass
        elif facts.AUDIT_AXIOMS_HEADER in out:
            block = out.split(facts.AUDIT_AXIOMS_HEADER, 1)[1]
            for ln in block.splitlines():
                m = re.match(r"\s*([A-Za-z_][\w.']*)\s*:", ln)
                if m:
                    assumptions.append(m.group(1))
            for a in assumptions:
                if a not in self.allowed_axioms and a.split(".")[-1] not in self.allowed_axioms:
                    violations.append(f"unexpected axiom: {a}")
        else:
            violations.append("audit failed to run: " + "\n".join(out.strip().splitlines()[-5:]))
        assumptions += [a for a in result.assumptions if a not in assumptions]
        v = self.tool_versions().get(Path(self.refinedc_bin).name)
        if v:
            assumptions.append(f"refinedc:{v}")
        return AuditResult(passed=not violations, assumptions=assumptions, violations=violations)

    # ------------------------------------------------------------ contracts

    def extract_spec(self, submission: Submission, function: FunctionInfo) -> str:
        return ann.extract_contract(submission.files.get("function.c", ""))
