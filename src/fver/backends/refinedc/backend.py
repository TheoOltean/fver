"""RefinedC backend: verify C functions with RefinedC on Rocq.

Settings ([backend.refinedc] in config.toml), all optional:
  refinedc_bin        = "refinedc"   path/name of the refinedc CLI
  coqc_bin            = "coqc"       Rocq compiler used for the audit
  dune_bin            = "dune"
  coq_root            = "refinedc.project.fver"  logical root of generated files
  extra_check_args    = []           extra argv appended to `refinedc check`
  include_dirs        = []           extra -I directories (absolute or repo-relative)
  defines             = []           extra -D macros
  forward_build_flags = true         forward -I/-D from the captured build
  allowed_axioms      = []           extra axiom names the audit tolerates

The backend never writes outside its workspace directory
(<repo>/.fver/backend/refinedc/); user sources are only read. Every path
segment it creates under the workspace is a valid Coq identifier, because
RefinedC turns directory names and file stems into Coq module paths.

RefinedC (and dune underneath) must not run concurrently in one project:
dune holds a build lock. A process-wide lock serialises tool invocations.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
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
from fver.backends.refinedc.parse_output import (
    classify_full,
    extract_frontend_errors,
    strip_ansi,
)
from fver.core.models import FunctionInfo, Target, ToolStatus, TranslationUnit, sha256_text
from fver.util.platform import install_hint
from fver.util.proc import run, version_of, which

_PROMPTS_DIR = Path(__file__).parent / "prompts"

INSTALL_HINT = (
    "Install via opam (see RefinedC's README): `opam repo add coq-released "
    "https://coq.inria.fr/opam/released && opam repo add iris-dev "
    "https://gitlab.mpi-sws.org/iris/opam.git && opam update && opam pin add -n -y cerberus-lib "
    "'git+https://github.com/rems-project/cerberus.git#f11e6b335a687c1b77539f7e5695607d09dfc3ea' "
    "&& opam pin add refinedc git+https://gitlab.mpi-sws.org/iris/refinedc.git`"
)

_TOOL_LOCK = threading.Lock()


def _tool_env(*bins: str) -> dict[str, str]:
    """Environment for running RefinedC outside an activated opam switch.

    refinedc shells out to dune and rocq (they must be on PATH next to it),
    finds its own include directory through OPAM_SWITCH_PREFIX (frontend/
    main.ml) and the Cerberus libc through the switch's runtime directory.
    When the configured binary lives in <prefix>/bin/, point those variables
    at <prefix>: the binary's own switch is authoritative even if the shell
    has another switch activated.
    """
    env = dict(os.environ)
    resolved = [Path(which(b) or b).resolve() for b in bins if b]
    dirs = [str(p.parent) for p in resolved if p.is_absolute() and p.parent.is_dir()]
    if dirs:
        env["PATH"] = os.pathsep.join(dict.fromkeys(dirs + env.get("PATH", "").split(os.pathsep)))
    for p in resolved:
        prefix = p.parent.parent
        if p.parent.name == "bin" and (prefix / "lib" / "refinedc").is_dir():
            env["OPAM_SWITCH_PREFIX"] = str(prefix)
            runtime = prefix / "lib" / "cerberus-lib" / "runtime"
            if runtime.is_dir():
                env["CERB_RUNTIME"] = str(runtime)
            break
    return env


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    return text.strip().splitlines()[0] if text.strip() else None


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
        self.coq_root: str = self.settings.get("coq_root", facts.DEFAULT_COQ_ROOT)
        self.extra_check_args: list[str] = list(self.settings.get("extra_check_args", []))
        self.include_dirs: list[str] = list(self.settings.get("include_dirs", []))
        self.defines: list[str] = list(self.settings.get("defines", []))
        self.forward_build_flags: bool = bool(self.settings.get("forward_build_flags", True))
        self.allowed_axioms = set(facts.ALLOWED_AXIOMS) | set(
            self.settings.get("allowed_axioms", [])
        )
        self._versions: dict[str, str] | None = None

    # ------------------------------------------------------------------ tools

    def _have_refinedc(self) -> bool:
        return which(self.refinedc_bin) is not None

    def _env(self) -> dict[str, str]:
        return _tool_env(self.refinedc_bin, self.coqc_bin, self.dune_bin)

    def _run(self, argv: list[str], cwd: Path, timeout: float):
        with _TOOL_LOCK:
            return run(argv, cwd=cwd, timeout=timeout, env=self._env())

    def doctor(self) -> list[ToolStatus]:
        def status(label: str, binname: str, required: bool, hint: str) -> ToolStatus:
            path = which(binname)
            ver = _first_line(version_of([binname, "--version"])) if path else None
            return ToolStatus(
                name=label,
                found=path is not None,
                path=path,
                version=ver or (path if path else None),
                required=required,
                hint=hint,
            )

        coq_hint = "Install Rocq via opam: `opam install rocq-prover` (RefinedC pulls it in)."
        rows = [
            status("refinedc", self.refinedc_bin, True, INSTALL_HINT),
            status("coqc", self.coqc_bin, True, coq_hint),
            status("dune", self.dune_bin, True, "`opam install dune`"),
            status(
                "opam",
                facts.OPAM_BIN,
                False,
                install_hint("opam", url="https://opam.ocaml.org/doc/Install.html"),
            ),
        ]
        if not rows[1].found and which(facts.ROCQ_BIN):
            rows[1] = status("coqc", facts.ROCQ_BIN, True, coq_hint)
        return rows

    def tool_versions(self) -> dict[str, str]:
        if self._versions is None:
            v: dict[str, str] = {}
            for label, binname in (("refinedc", self.refinedc_bin), ("coqc", self.coqc_bin)):
                ver = _first_line(version_of([binname, "--version"]))
                if ver:
                    v[label] = ver
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
            argv = [
                self.refinedc_bin,
                *facts.INIT_ARGV,
                facts.INIT_COQ_PATH_FLAG.format(path=self.coq_root),
            ]
            r = self._run(argv, self.workspace_dir, 120)
            if r.ok and self.project_file.exists():
                return
        # Fall back to writing the project file ourselves so the rest of the
        # pipeline (and tests) can run without the tool.
        self.project_file.write_text(
            facts.PROJECT_FILE_TEMPLATE.format(coq_root=self.coq_root), encoding="utf-8"
        )

    # Paths -------------------------------------------------------------------

    def _tu_dir(self, tu: TranslationUnit) -> Path:
        return self.workspace_dir / "src" / ann.coq_ident("tu_" + tu.id, "tu")

    def _check_dir(self, task: FunctionTask) -> Path:
        d = self.workspace_dir / "checks" / ann.coq_ident("f_" + task.function.id, "f")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _stem_for(source_path: str) -> str:
        return ann.coq_ident(Path(source_path).stem, "f")

    def _module_path(self, c_file: Path) -> str:
        rel = c_file.resolve().relative_to(self.workspace_dir.resolve())
        parts = [ann.coq_ident(p) for p in rel.parent.parts] + [ann.coq_ident(rel.stem, "f")]
        return ".".join(parts)

    def _proofs_dir(self, c_file: Path) -> Path:
        return c_file.parent / facts.PROOFS_DIR_NAME / ann.coq_ident(c_file.stem, "f")

    def _build_dir_for(self, c_file: Path) -> Path:
        rel = self._proofs_dir(c_file).resolve().relative_to(self.workspace_dir.resolve())
        return self.workspace_dir / facts.DUNE_BUILD_DIR / rel

    def _cpp_flags(self, tu: TranslationUnit | None, repo_root: Path | None) -> list[str]:
        """-I / -D for `refinedc check`: the original source's directory (so
        sibling headers resolve although we check a copy), then the captured
        build's include dirs and defines, then configured extras."""
        flags: list[str] = []
        seen: set[str] = set()

        def add_inc(d: str) -> None:
            p = Path(d)
            if not p.is_absolute() and tu is not None:
                p = Path(tu.directory) / p
            key = str(p)
            if key not in seen:
                seen.add(key)
                flags.append(facts.INCLUDE_FLAG_FMT.format(dir=key))

        if tu is not None and repo_root is not None:
            add_inc(str((Path(repo_root) / tu.source_path).parent))
        if tu is not None and self.forward_build_flags:
            args = tu.arguments
            i = 1
            while i < len(args):
                a = args[i]
                if a == "-I" and i + 1 < len(args):
                    add_inc(args[i + 1])
                    i += 2
                    continue
                if a.startswith("-I") and len(a) > 2:
                    add_inc(a[2:])
                elif a == "-D" and i + 1 < len(args):
                    flags.append(facts.DEFINE_FLAG_FMT.format(macro=args[i + 1]))
                    i += 2
                    continue
                elif a.startswith("-D") and len(a) > 2:
                    flags.append(a)
                i += 1
        for d in self.include_dirs:
            p = Path(d)
            if not p.is_absolute() and repo_root is not None:
                p = Path(repo_root) / p
            add_inc(str(p))
        for m in self.defines:
            flags.append(facts.DEFINE_FLAG_FMT.format(macro=m))
        return flags

    def _check_argv(
        self, c_file: Path, tu: TranslationUnit | None, repo_root: Path | None, no_build: bool
    ) -> list[str]:
        argv = [self.refinedc_bin, *facts.CHECK_ARGV, facts.NO_EXTRA_ANALYSIS_FLAG]
        if no_build:
            argv.append(facts.NO_BUILD_FLAG)
        argv += self._cpp_flags(tu, repo_root)
        argv += self.extra_check_args
        argv.append(str(c_file))
        return argv

    # -------------------------------------------------------------- translate

    def translate(
        self, tu: TranslationUnit, functions: list[FunctionInfo], repo_root: Path
    ) -> TranslateResult:
        names = [f.name for f in functions]
        src = Path(repo_root) / tu.source_path
        dest_dir = self._tu_dir(tu)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{self._stem_for(tu.source_path)}.c"
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return TranslateResult(
                supported={n: False for n in names},
                reasons={n: f"cannot read source: {e}" for n in names},
                tu_error=str(e),
            )
        offset = 0
        if not ann.has_refinedc_include(text):
            text = facts.HEADER_INCLUDE + "\n" + text
            offset = 1
        dest.write_text(text, encoding="utf-8")  # a copy; the user's file is untouched
        if not self._have_refinedc():
            reason = "refinedc not installed; front-end check skipped"
            return TranslateResult(
                supported={n: True for n in names},
                reasons={n: reason for n in names},
                artifacts={"copy": str(dest)},
            )
        self.prepare([tu], repo_root)
        r = self._run(self._check_argv(dest, tu, repo_root, no_build=True), self.workspace_dir, 600)
        combined = strip_ansi(r.stdout + "\n" + r.stderr)
        if r.ok and facts.INTERNAL_ERROR_MARKER not in combined:
            return TranslateResult(
                supported={n: True for n in names}, artifacts={"copy": str(dest)}
            )
        supported = {n: True for n in names}
        reasons: dict[str, str] = {}
        tu_errors: list[str] = []
        if r.returncode == facts.EXIT_INTERNAL_ERROR or facts.INTERNAL_ERROR_MARKER in combined:
            first = next(
                (
                    ln.strip()
                    for ln in combined.splitlines()
                    if "Failure" in ln or "exception" in ln
                ),
                "refinedc internal error",
            )
            tu_errors.append(f"refinedc front-end crashed on this file: {first}")
        for e in extract_frontend_errors(combined):
            same_file = Path(e.file).name == dest.name or Path(e.file).name == src.name
            if same_file and e.line is not None:
                line = e.line - offset
                hit = next((f for f in functions if f.start_line <= line <= f.end_line), None)
                if hit is not None:
                    supported[hit.name] = False
                    reasons[hit.name] = f"line {line}: {e.message}"
                    continue
            tu_errors.append(f"{Path(e.file).name}:{e.line}: {e.message}")
        if not tu_errors and all(supported.values()):
            tu_errors.append(
                "refinedc check failed: " + " | ".join(combined.strip().splitlines()[-3:])
            )
        tu_error = "\n".join(dict.fromkeys(tu_errors)) if tu_errors else None
        if tu_error:
            # An error outside any function (a header, a type, a crash) means
            # the whole file cannot be translated.
            for n in names:
                if supported[n]:
                    supported[n] = False
                    reasons[n] = "file rejected by the front-end: " + tu_error.splitlines()[0]
        return TranslateResult(
            supported=supported, reasons=reasons, tu_error=tu_error, artifacts={"copy": str(dest)}
        )

    # ----------------------------------------------------------------- prompt

    def submission_spec(self) -> SubmissionSpec:
        return SubmissionSpec(
            files={
                "function.c": "the target function with RefinedC annotations; code otherwise unchanged",
                facts.LEMMAS_FILE: "optional Rocq helper lemmas, applied from rc::tactics",
            },
            required=["function.c"],
            language_hints={"function.c": "c", facts.LEMMAS_FILE: "coq"},
        )

    def prompt_context(self) -> PromptContext:
        def read(name: str) -> str:
            p = _PROMPTS_DIR / name
            return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

        forbidden = [re.escape(a) for a in facts.LLM_FORBIDDEN_ATTRIBUTES] + [
            r"//@rc::" + d + r"\b" for d in facts.LLM_FORBIDDEN_DIRECTIVES
        ]
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
            for name, _payload in ann.find_directives(fn):
                if name in facts.LLM_FORBIDDEN_DIRECTIVES:
                    problems.append(
                        f"forbidden directive //@rc::{name} (the backend wires lemmas.v itself)"
                    )
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

    def _build_source(
        self, task: FunctionTask, annotated_fn: str, lemmas_path: str | None
    ) -> tuple[str, int, int]:
        """Return (spliced source, marker line, lines inserted before the function).

        Verified callees and externals become spec-carrying prototypes; a callee
        defined in this file is replaced by its prototype (so it is not
        re-verified), others are prepended. The line marker function goes
        first after the includes so Coq-level locations can be mapped back.
        """
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
        spliced = ann.splice(task.source_text, task.function, annotated_fn, callee_repl)
        head = [facts.LINE_MARKER_TEXT]
        if lemmas_path:
            head.append(
                facts.IMPORT_DIRECTIVE_FMT.format(module=facts.LEMMAS_MODULE, path=lemmas_path)
            )
        block = head + prototypes
        before = spliced.count("\n") + 1
        out = ann.prepend_prototypes(spliced, block)
        inserted = out.count("\n") + 1 - before
        marker_line = next(
            (i + 1 for i, ln in enumerate(out.split("\n")) if facts.LINE_MARKER_FN in ln), 0
        )
        # The include added by splice() (if any) also shifts the function.
        added_include = 1 if not ann.has_refinedc_include(task.source_text) else 0
        return out, marker_line, inserted + added_include

    @staticmethod
    def _marker_offset(generated_code: str, marker_source_line: int) -> int | None:
        """Reported line of the marker's `return` minus its source line."""
        text = generated_code
        m = re.search(
            r"Definition\s+"
            + re.escape(facts.IMPL_DEF_FMT.format(fn=facts.LINE_MARKER_FN))
            + r"\b(?P<body>.*?)\|\}\.",
            text,
            re.DOTALL,
        )
        if not m or marker_source_line <= 0:
            return None
        locs = set(re.findall(r"\bloc_\d+\b", m.group("body")))
        lines: list[int] = []
        for lm in re.finditer(facts.LOCATION_INFO_REGEX, text):
            if lm.group("name") in locs:
                lines.append(int(lm.group("l1")))
        if not lines:
            return None
        return min(lines) - marker_source_line

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
        c_file = wd / f"{self._stem_for(task.function.source_path)}.c"
        proofs_dir = self._proofs_dir(c_file)
        if proofs_dir.exists():
            shutil.rmtree(proofs_dir, ignore_errors=True)
        lemmas_text = submission.files.get(facts.LEMMAS_FILE)
        lemmas_path: str | None = None
        if lemmas_text:
            proofs_dir.mkdir(parents=True, exist_ok=True)
            (proofs_dir / facts.LEMMAS_FILE).write_text(lemmas_text, encoding="utf-8")
            lemmas_path = f"{self.coq_root}.{self._module_path(c_file)}"
        source, marker_line, shift = self._build_source(task, annotated, lemmas_path)
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
        r = self._run(
            self._check_argv(c_file, task.tu, task.repo_root, no_build=False),
            self.workspace_dir,
            timeout_seconds,
        )
        # Map preprocessed line numbers back to the source we wrote.
        src_lines = source.split("\n")
        line_of = None
        code_v = proofs_dir / facts.GENERATED_CODE
        if code_v.exists():
            offset = self._marker_offset(
                code_v.read_text(encoding="utf-8", errors="replace"), marker_line
            )
            if offset is not None:
                fn_start = task.function.start_line + shift

                def line_of(
                    reported: int, _o=offset, _s=shift, _f=fn_start
                ) -> tuple[int, str] | None:
                    idx = reported - _o
                    if 1 <= idx <= len(src_lines):
                        # Report in the coordinates of the user's file when the
                        # line is at/after the insertion point.
                        user_line = idx - _s if idx >= _f - 0 else idx
                        return user_line, src_lines[idx - 1]
                    return None

        cl = classify_full(
            r.stdout, r.stderr, r.returncode, r.timed_out, task.function.name, line_of
        )
        generated = sorted(proofs_dir.glob("*.v")) if proofs_dir.exists() else []
        for g in generated:
            artifacts[g.name] = str(g)
        proof_hash = None
        if cl.outcome is CheckOutcome.OK:
            h = "\n".join(
                f"{g.name}\n{g.read_text(encoding='utf-8', errors='replace')}" for g in generated
            )
            proof_hash = sha256_text(h + "\n" + submission.content_hash())
        assumptions = [f"external:{n}" for n in task.external_specs] + [
            f"callee:{n}" for n in task.callee_specs
        ]
        return CheckResult(
            outcome=cl.outcome,
            feedback=cl.feedback,
            goals=cl.goals,
            stdout=r.stdout,
            stderr=r.stderr,
            artifacts=artifacts,
            proof_hash=proof_hash,
            assumptions=assumptions,
            tool_versions=self.tool_versions(),
            duration_seconds=r.duration,
            witness=cl.witness,
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
            if p.suffix == ".v" and p.exists() and not p.name.startswith("generated_"):
                text = p.read_text(encoding="utf-8", errors="replace")
                for pat in facts.FORBIDDEN_COQ_PATTERNS:
                    if re.search(pat, text):
                        violations.append(f"{name}: forbidden construct /{pat}/")
        proof_text = Path(proof_file).read_text(encoding="utf-8", errors="replace")
        if "Admitted" in proof_text or "trust_me" in proof_text:
            violations.append(f"{proof_name}: proof is not closed by Qed")
        if not which(self.coqc_bin):
            violations.append(
                "audit tool unavailable: coqc not found; cannot run Print Assumptions"
            )
            return AuditResult(passed=False, violations=violations)
        c_file = Path(result.artifacts.get("source", ""))
        if not c_file.exists():
            return AuditResult(passed=False, violations=violations + ["checked source not found"])
        modpath = self._module_path(c_file)
        build_dir = self._build_dir_for(c_file)
        audit_dir = self.workspace_dir / "audits" / ann.coq_ident("f_" + task.function.id, "f")
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = audit_dir / facts.AUDIT_FILE_FMT.format(fn=ann.coq_ident(fn, "f"))
        audit_file.write_text(
            facts.AUDIT_SCRIPT_FMT.format(
                coq_root=self.coq_root,
                modpath=modpath,
                proof_module=proof_name[:-2],
                lemma=facts.PROOF_LEMMA_FMT.format(fn=fn),
            ),
            encoding="utf-8",
        )
        r = self._run(
            [self.coqc_bin, "-Q", str(build_dir), f"{self.coq_root}.{modpath}", str(audit_file)],
            audit_dir,
            600,
        )
        out = strip_ansi(r.stdout + "\n" + r.stderr)
        assumptions: list[str] = []
        if facts.AUDIT_CLOSED_MARKER in out:
            pass
        elif facts.AUDIT_AXIOMS_HEADER in out:
            block = out.split(facts.AUDIT_AXIOMS_HEADER, 1)[1]
            for ln in block.splitlines():
                m = re.match(r"\s*([A-Za-z_][\w.']*)\s*:", ln)
                if m:
                    assumptions.append("axiom:" + m.group(1))
            for a in assumptions:
                bare = a[len("axiom:") :]
                if (
                    bare not in self.allowed_axioms
                    and bare.split(".")[-1] not in self.allowed_axioms
                ):
                    violations.append(f"unexpected axiom: {bare}")
        else:
            violations.append("audit failed to run: " + "\n".join(out.strip().splitlines()[-5:]))
        assumptions += [a for a in result.assumptions if a not in assumptions]
        v = self.tool_versions().get("refinedc")
        if v:
            assumptions.append(f"refinedc:{v}")
        return AuditResult(passed=not violations, assumptions=assumptions, violations=violations)

    # ------------------------------------------------------------ contracts

    def extract_spec(self, submission: Submission, function: FunctionInfo) -> str:
        return ann.extract_contract(submission.files.get("function.c", ""))
