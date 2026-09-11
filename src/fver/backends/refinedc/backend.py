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
  posix_shims         = true         add Cerberus's posix/ headers and fver's shim
                                     headers (-I, after the project's own) so files
                                     that include <unistd.h>, <sys/types.h>, ... parse
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
from fver.extract.functions import extract_from_source
from fver.util.platform import install_hint
from fver.util.proc import run, version_of, which

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SHIMS_DIR = Path(__file__).parent / facts.SHIMS_DIR_NAME

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


def cpp_line_map(cpp_output: str, for_file: str) -> dict[int, int]:
    """Physical line of a `cc -E -C` output -> source line, for lines that
    belong to `for_file` (compared by basename). Line markers `# N "file"`
    reset the counter and are themselves counted, as Cerberus does."""
    out: dict[int, int] = {}
    cur_file: str | None = None
    cur_line = 0
    want = Path(for_file).name
    lines = cpp_output.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for phys, ln in enumerate(lines, start=1):
        m = re.match(facts.CPP_LINE_MARKER_REGEX, ln)
        if m:
            cur_file = m.group("file")
            cur_line = int(m.group("line"))
            continue
        if cur_file is not None and Path(cur_file).name == want:
            out[phys] = cur_line
        cur_line += 1
    return out


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
        self.posix_shims: bool = bool(self.settings.get("posix_shims", True))
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
        if self.posix_shims:
            for shim in self._shim_dirs():
                flags.append(facts.INCLUDE_FLAG_FMT.format(dir=str(shim)))
        return flags

    def _shim_dirs(self) -> list[Path]:
        """Cerberus's own posix/ headers (present in the switch but not on
        RefinedC's include path) first, then fver's minimal shims for what
        is still missing. Both come after the project's directories."""
        out: list[Path] = []
        env = self._env()
        runtime = env.get("CERB_RUNTIME")
        if runtime:
            posix = Path(runtime) / "libc" / "include" / "posix"
            if posix.is_dir():
                out.append(posix)
        if _SHIMS_DIR.is_dir():
            out.append(_SHIMS_DIR)
        return out

    def _check_argv(
        self, c_file: Path, tu: TranslationUnit | None, repo_root: Path | None, no_build: bool
    ) -> list[str]:
        argv = [self.refinedc_bin, *facts.CHECK_ARGV, facts.NO_EXTRA_ANALYSIS_FLAG]
        if no_build:
            argv.append(facts.NO_BUILD_FLAG)
        if self.posix_shims and (_SHIMS_DIR / facts.PRELUDE_HEADER).is_file():
            argv.append(
                facts.INCLUDE_FILE_FLAG_FMT.format(file=str(_SHIMS_DIR / facts.PRELUDE_HEADER))
            )
        argv += self._cpp_flags(tu, repo_root)
        argv += self.extra_check_args
        argv.append(str(c_file))
        return argv

    def _preprocess(
        self, c_file: Path, tu: TranslationUnit | None, repo_root: Path | None
    ) -> tuple[dict[int, int], str]:
        """Run RefinedC's own preprocessor command on `c_file`. Returns the
        physical-line -> `c_file`-line map and the preprocessed text; both
        empty on failure."""
        env = self._env()
        runtime = env.get("CERB_RUNTIME")
        prefix = env.get("OPAM_SWITCH_PREFIX")
        argv = list(facts.CPP_ARGV_PREFIX)
        if runtime:
            argv.append(facts.INCLUDE_FLAG_FMT.format(dir=str(Path(runtime) / "libc" / "include")))
        if prefix and (Path(prefix) / "lib" / "refinedc" / "include").is_dir():
            argv.append(
                facts.INCLUDE_FLAG_FMT.format(
                    dir=str(Path(prefix) / "lib" / "refinedc" / "include")
                )
            )
        if self.posix_shims and (_SHIMS_DIR / facts.PRELUDE_HEADER).is_file():
            argv += ["-include", str(_SHIMS_DIR / facts.PRELUDE_HEADER)]
        argv += self._cpp_flags(tu, repo_root)
        argv += [facts.DEFINE_FLAG_FMT.format(macro=m) for m in facts.CPP_PREDEFINES]
        argv.append(str(c_file))
        r = run(argv, cwd=self.workspace_dir, timeout=120, env=env)
        if not r.ok:
            return {}, ""
        return cpp_line_map(r.stdout, c_file.name), r.stdout

    def _cpp_map_for(
        self, c_file: Path, tu: TranslationUnit | None, repo_root: Path | None
    ) -> dict[int, int]:
        return self._preprocess(c_file, tu, repo_root)[0]

    def _definition_ranges(
        self, c_file: Path, tu: TranslationUnit | None, repo_root: Path | None
    ) -> dict[str, tuple[int, int]]:
        """Function definitions in `c_file` as {name: (start, end)} in the
        file's own line numbers, found by parsing the *preprocessed* text
        (macros in declarators such as `ZEXPORT` or `YAML_DECLARE(int)` and
        #ifdef'd bodies confuse a parse of the raw source) and mapping the
        lines back through the preprocessor's line markers."""
        line_map, text = self._preprocess(c_file, tu, repo_root)
        if not text:
            return {}
        out: dict[str, tuple[int, int]] = {}
        for f in extract_from_source(text, c_file.name, "cpp"):
            a, b = line_map.get(f.start_line), line_map.get(f.end_line)
            if a is not None and b is not None and a <= b:
                out[f.name] = (a, b)
        return out

    def _bisect_crash(
        self,
        dest: Path,
        tu: TranslationUnit,
        repo_root: Path,
        text: str,
        ranges: dict[str, tuple[int, int]],
        stubbed: set[str],
    ) -> tuple[str, str] | None:
        """Find one function whose stubbing stops an internal error. Tries the
        largest remaining definitions first (crashes come from bodies). Returns
        (name, text with that function stubbed) or None."""
        cands = sorted(
            ((n, r) for n, r in ranges.items() if n not in stubbed),
            key=lambda kv: -(kv[1][1] - kv[1][0]),
        )
        for name, (a, b) in cands[: facts.CRASH_BISECT_LIMIT]:
            trial = ann.stub_definition(text, a, b)
            dest.write_text(trial, encoding="utf-8")
            r = self._run(
                self._check_argv(dest, tu, repo_root, no_build=True), self.workspace_dir, 600
            )
            combined = strip_ansi(r.stdout + "\n" + r.stderr)
            crashed = (
                r.returncode == facts.EXIT_INTERNAL_ERROR or facts.INTERNAL_ERROR_MARKER in combined
            )
            if not crashed:
                return name, trial
        dest.write_text(text, encoding="utf-8")
        return None

    # -------------------------------------------------------------- translate

    def translate(
        self, tu: TranslationUnit, functions: list[FunctionInfo], repo_root: Path
    ) -> TranslateResult:
        """Run the front-end over a copy of the TU. A construct the front-end
        rejects inside one function marks only that function unsupported: its
        definition is replaced by a prototype (line-preserving) and the
        front-end is rerun, until the file passes or an error falls outside
        every function (then the whole TU is unsupported)."""
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
        supported = {n: True for n in names}
        reasons: dict[str, str] = {}
        stubbed: set[str] = set()  # function names reduced to prototypes
        blanked: set[str] = set()  # prototypes removed too
        stubbed_ranges: list[tuple[int, int]] = []  # copy-line ranges already stubbed
        tu_error: str | None = None
        by_name = {f.name: f for f in functions}
        # Accurate definition ranges (copy coordinates) from the preprocessed
        # text; the extractor's ranges fill in for anything not found there.
        ranges: dict[str, tuple[int, int]] = {
            f.name: (f.start_line + offset, f.end_line + offset) for f in functions
        }
        ranges.update(self._definition_ranges(dest, tu, repo_root))

        def fn_at(copy_line: int) -> FunctionInfo | None:
            for name, (a, b) in ranges.items():
                if a <= copy_line <= b:
                    fi = by_name.get(name)
                    if fi is None:  # a definition the extractor missed
                        fi = FunctionInfo(
                            id=f"{tu.id}:{name}",
                            name=name,
                            tu_id=tu.id,
                            source_path=tu.source_path,
                            start_line=a - offset,
                            end_line=b - offset,
                            signature=name,
                            body_hash="",
                        )
                    return fi
            return None

        for _round in range(2 * len(functions) + 5):
            r = self._run(
                self._check_argv(dest, tu, repo_root, no_build=True), self.workspace_dir, 600
            )
            combined = strip_ansi(r.stdout + "\n" + r.stderr)
            if r.ok and facts.INTERNAL_ERROR_MARKER not in combined:
                break
            if r.returncode == facts.EXIT_INTERNAL_ERROR or facts.INTERNAL_ERROR_MARKER in combined:
                lines_ = combined.splitlines()
                first = next(
                    (ln.strip() for ln in lines_ if "Failure" in ln or "Assertion" in ln),
                    next(
                        (ln.strip() for ln in lines_ if "exception" in ln),
                        "refinedc internal error",
                    ),
                )
                # A crash carries no location: bisect by stubbing one
                # remaining function at a time until the crash goes away.
                culprit = self._bisect_crash(dest, tu, repo_root, text, ranges, stubbed)
                if culprit is None:
                    tu_error = f"refinedc front-end crashed on this file: {first}"
                    break
                name, text = culprit
                if name in by_name:
                    supported[name] = False
                reasons[name] = f"front-end crash (internal error) inside this function: {first}"
                stubbed.add(name)
                stubbed_ranges.append(ranges[name])
                dest.write_text(text, encoding="utf-8")
                continue
            errors = extract_frontend_errors(combined)
            if not errors:
                tu_error = "refinedc check failed: " + " | ".join(
                    combined.strip().splitlines()[-3:]
                )
                break
            progressed = False
            n_copy_lines = text.count("\n") + 1
            cpp_map: dict[int, int] | None = None
            for err in errors:
                same_file = Path(err.file).name in (dest.name, src.name)
                if not same_file or err.line is None:
                    tu_error = f"{Path(err.file).name}:{err.line}: {err.message}"
                    break
                # The line may be a real source line or a physical line of the
                # preprocessed output (RefinedC mixes both); try both readings.
                candidates: list[int] = []
                if err.line <= n_copy_lines:
                    candidates.append(err.line)
                if cpp_map is None:
                    cpp_map = self._cpp_map_for(dest, tu, repo_root)
                mapped = cpp_map.get(err.line)
                if mapped is not None and mapped not in candidates:
                    candidates.append(mapped)
                handled = False
                # 1. A known function not yet stubbed.
                for c in candidates:
                    hit = fn_at(c)
                    if hit is not None and hit.name not in stubbed:
                        a, b = ranges[hit.name]
                        if hit.name in by_name:
                            supported[hit.name] = False
                        reasons[hit.name] = f"line {c - offset}: {err.message}"
                        stubbed.add(hit.name)
                        text = ann.stub_definition(text, a, b)
                        stubbed_ranges.append((a, b))
                        handled = True
                        break
                if handled:
                    progressed = True
                    continue
                # 2. A body the extractor did not recognise (macro in the declarator).
                for c in candidates:
                    if any(a <= c <= b for a, b in stubbed_ranges):
                        continue
                    enc = ann.enclosing_definition(text, c)
                    if enc is not None:
                        a, b, name = enc
                        text = ann.stub_definition(text, a, b)
                        stubbed_ranges.append((a, b))
                        if name in by_name:
                            supported[name] = False
                            reasons[name] = f"line {c - offset}: {err.message}"
                            stubbed.add(name)
                        handled = True
                        break
                if handled:
                    progressed = True
                    continue
                # 3. The stubbed prototype itself is rejected: blank it.
                for c in candidates:
                    hit = fn_at(c)
                    if hit is not None and hit.name in stubbed and hit.name not in blanked:
                        a, b = ranges[hit.name]
                        blanked.add(hit.name)
                        reasons[hit.name] = reasons.get(hit.name, "") + (
                            f"; prototype also rejected: {err.message}"
                        )
                        text = ann.blank_lines(text, a, b)
                        handled = True
                        break
                if handled:
                    progressed = True
                    continue
                tu_error = f"{Path(err.file).name}:{err.line}: {err.message}"
                break
            if tu_error is not None or not progressed:
                break
            dest.write_text(text, encoding="utf-8")
        else:
            tu_error = "front-end still failing after isolating every function"
        if tu_error:
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
        # Every other function definition in the file becomes a plain
        # prototype (line-preserving): RefinedC does not need their bodies,
        # and this keeps the target checkable when an unrelated function uses
        # a construct the front-end rejects.
        base = task.source_text
        for other in extract_from_source(base, task.function.source_path, task.tu.id):
            if other.name == task.function.name or other.name in callee_repl:
                continue
            if (
                other.start_line <= task.function.end_line
                and other.end_line >= task.function.start_line
            ):
                continue  # overlaps the target (nested/odd parse); leave it alone
            base = ann.stub_definition(base, other.start_line, other.end_line)
        spliced = ann.splice(base, task.function, annotated_fn, callee_repl)
        spliced = self._stub_remaining_definitions(spliced, task, callee_repl)
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

    def _stub_remaining_definitions(
        self, spliced: str, task: FunctionTask, callee_repl: dict[str, tuple[FunctionInfo, str]]
    ) -> str:
        """Definitions the raw-source extractor missed (macro-decorated
        declarators) are found by preprocessing the spliced file and turned
        into prototypes too. Line-preserving; the target is left alone."""
        wd = self._check_dir(task)
        probe = wd / f"{self._stem_for(task.function.source_path)}_probe.c"
        probe.write_text(spliced, encoding="utf-8")
        try:
            ranges = self._definition_ranges(probe, task.tu, task.repo_root)
        finally:
            probe.unlink(missing_ok=True)
        target = ranges.get(task.function.name)
        for name, (a, b) in sorted(ranges.items(), key=lambda kv: -kv[1][0]):
            if name == task.function.name or name in callee_repl:
                continue
            if target is not None and a <= target[1] and b >= target[0]:
                continue
            body = spliced.split("\n")[a - 1 : b]
            if not any("{" in ln for ln in body):
                continue  # already a prototype
            spliced = ann.stub_definition(spliced, a, b)
        return spliced

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
