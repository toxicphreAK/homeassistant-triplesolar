#!/usr/bin/env python3
"""Maintainability scanner: find and rank structural decay. Never fix anything.

This is the *deterministic half* of the quality-triage system. It runs a set of
optional third-party analysers over a repo, normalises everything into one
``Finding`` shape, and ranks the result per file. The *analysed* refactoring
issues are authored by an LLM from ``quality_triage.py``'s hand-off JSON — this
script deliberately has no opinion about what to do with a finding.

Design contracts, each of which exists because its absence caused a real bug:

**Skip-if-missing.** Every third-party tool is optional. A missing binary or
module yields ``ToolResult(status="skipped")``, never a crash and never a zero.
The builtin analysers (import graph, git churn) are the only always-on ones, so
the scanner is useful before anything is installed.

**A skipped tool banks no count.** ``summarize()`` records a count only for tools
that actually ran. Otherwise installing a tool later reads as a ``0 -> N``
regression against the baseline, and the honest fix (refresh the baseline) is
indistinguishable from the dishonest one. The mirror holds too: a tool the
baseline had and this run does not is *lost coverage*, and ``diff_baseline``
says so — counts only ever regress upward, so nothing else would notice.

**Roots scope what is reported, not only what is run.** Several analysers are
whole-module by construction (``staticcheck``, ``clippy``, ``go list``); their
output is filtered afterwards, or the declared roots would mean nothing for them.
A root that is *declared and absent* is not the same as one that was never
declared, and is said out loud in both places it matters: the skip note, and a
refusal to bank a baseline over a language nothing scanned.

**Identity never includes line numbers.** Cycles are compared as canonical
signatures; hotspots and clusters are keyed on files. An edit above a complex
function must not mint a "new" problem.

**Structural findings outrank everything.** Cycles and layering breaches are
architectural — they are the one class cheap enough to put on a merge path
(``--gate structural`` is pure stdlib) and the one class that never gets
cheaper to fix by waiting.

Zero dependencies: stdlib only. Configuration lives in ``.quality.json`` at the
repo root (see ``DEFAULT_CONFIG`` for the schema and ``quality-setup`` for the
generator).
"""
# Vendored from llmdev tools/quality @ 02c1cd5. Re-sync with /quality-setup +update.
# Local edits will be overwritten; upstream fixes belong in llmdev.

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 1
CONFIG_NAME = ".quality.json"

# --------------------------------------------------------------------------
# Weights and thresholds
# --------------------------------------------------------------------------

# Per-finding contribution to a file's hotspot score and a cluster's rank.
# Structural problems outrank everything because they are architectural rather
# than local: you cannot fix a cycle by editing one function.
KIND_WEIGHTS = {
    "circular_import": 4.0,
    "layering_violation": 4.0,
    "duplicate": 3.0,
    "maintainability": 2.5,
    "complexity": 2.0,
    "dead_code": 1.5,
}

STRUCTURAL_KINDS = ("circular_import", "layering_violation")


def complexity_weight(cc: Any) -> float:
    """Scale a complexity finding by radon's bands: D (21-30) and below keep the
    base weight, E (31-40) and F (41+) weigh more. At a flat 2.0, a lone function
    of complexity 100 in the hottest file topped out at 4.0 and could never clear
    the triage floor."""
    try:
        value = int(cc)
    except (TypeError, ValueError):
        return KIND_WEIGHTS["complexity"]
    return 4.0 if value > 40 else 3.0 if value > 30 else KIND_WEIGHTS["complexity"]

# The note of an adapter whose language this repo does not use. Shared with
# quality_triage, which must tell "not applicable" from "tool missing": only the
# second makes dismissals of its kind look stale.
NO_ROOTS_NOTE = "no {lang} roots configured"
# Configuration choices, not missing tools: a Go repo with no declared layering,
# and a tool switched off in `tools.disabled`.
NO_LAYERS_NOTE = "needs layers.order"
DISABLED_NOTE = f"disabled in {CONFIG_NAME}"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    # Per-language source roots, repo-relative. An empty list disables the
    # language's adapters entirely (they are not "skipped", they do not exist).
    "roots": {"python": [], "typescript": [], "go": [], "rust": [], "kotlin": []},
    # One constant, consumed in three syntaxes (see exclusion helpers below).
    "exclude_dirs": [
        ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
        "dist", "build", "target", "vendor", ".next", ".tox", ".mypy_cache",
        ".pytest_cache", "migrations", "site-packages", ".terraform",
    ],
    # Abstract -> concrete. A lower (more abstract) layer importing a more
    # concrete one is a violation. Empty order disables layering checks.
    "layers": {"order": []},
    "report_dir": "reports/code-quality",
    "thresholds": {
        "complexity_rank": "D",       # radon CC rank at or below which to report
        "mi_floor": 20.0,             # radon maintainability index floor
        "vulture_min_confidence": 80,
        "gocyclo_over": 15,
        "jscpd_min_lines": 12,
    },
    # Churn turns a static finding list into a *risk* list: a complex file
    # nobody touches costs nothing; the same file edited weekly costs every week.
    "churn": {"enabled": True, "days": 180, "weight": 1.0},
    "tools": {"disabled": []},
}


def load_config(repo_root: Path) -> dict[str, Any]:
    """Read ``.quality.json``, deep-merged over ``DEFAULT_CONFIG``.

    A missing file is not an error: the defaults describe a repo with no
    declared roots, which runs the language-agnostic tools and nothing else.
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = repo_root / CONFIG_NAME
    if path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"{CONFIG_NAME}: {exc}")
        if not isinstance(user, dict):
            raise SystemExit(f"{CONFIG_NAME}: top level must be an object")
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            elif key == "exclude_dirs" and isinstance(value, list):
                # **Extends**, never replaces. The setup command tells its author
                # to "add anything repo-specific here", and a plain assignment
                # silently dropped `dist`, `build`, `vendor` and `site-packages`
                # from every config that took that literally — which does not
                # fail, it just quietly changes what got scanned.
                cfg[key] = sorted(set(cfg[key]) | set(value))
            else:
                cfg[key] = value
    return cfg


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------


@dataclass
class ToolResult:
    """One analyser's outcome.

    ``status`` is ``ok`` (ran, findings may be empty), ``skipped`` (not
    installed / not applicable — banks no count) or ``error`` (ran and failed —
    also banks no count, but is reported loudly).

    ``incomplete`` is the middle case those three cannot express: the tool ran
    and its findings are real, but it covered *less than it was asked to*. An
    analyser invoked once per declared root (``knip``, ``madge``) hits this the
    moment one root produces no usable output. The findings stay — they are
    true — but the count must not be banked, for the same reason a skipped
    tool's is not. Distinct from the run-level ``partial`` flag, which is about
    ``--only`` deselecting whole analysers.
    """

    tool: str
    status: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    incomplete: bool = False

    @property
    def ran(self) -> bool:
        return self.status == "ok"

    def to_dict(self, run: dict[str, Any] | None = None) -> dict[str, Any]:
        out = {
            "tool": self.tool,
            "status": self.status,
            "note": self.note,
            "incomplete": self.incomplete,
            "metrics": self.metrics,
            "findings": self.findings,
        }
        if run:
            # Provenance travels with the data. Without it, a consumer cannot
            # tell a report refreshed thirty seconds ago from one left over from
            # a scan three weeks and one refactor back.
            out["run"] = run
        return out


def finding(file: str, kind: str, detail: str, *, tool: str,
            line: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalise one analyser observation into the single finding shape."""
    out: dict[str, Any] = {
        "file": file,
        "line": line,
        "kind": kind,
        "detail": detail,
        "tool": tool,
        "weight": KIND_WEIGHTS.get(kind, 1.0),
    }
    if extra:
        out.update(extra)
    return out


class Ctx:
    """Everything an adapter needs: paths, config, and derived exclusions."""

    def __init__(self, repo_root: Path, config: dict[str, Any]) -> None:
        self.repo = repo_root.resolve()
        self.config = config
        self.exclude_dirs: list[str] = list(config["exclude_dirs"])
        self.thresholds: dict[str, Any] = config["thresholds"]
        self.disabled: set[str] = set(config.get("tools", {}).get("disabled", []))

    def roots(self, lang: str) -> list[Path]:
        """Resolved, existing source roots for a language."""
        out = []
        for rel in self.config.get("roots", {}).get(lang, []) or []:
            path = (self.repo / rel).resolve()
            if path.exists():
                out.append(path)
        return out

    def missing_roots(self, lang: str) -> list[str]:
        """Roots this language declares that are not on disk.

        A typo'd root is not the same thing as an undeclared one, and the
        difference is invisible from ``roots()`` alone: both come back empty.
        ``roots.python = ["scr"]`` reported "no python roots configured" and
        scanned nothing, which is a clean report for a repo nobody looked at —
        the single most dangerous output this tool has.
        """
        return [str(rel) for rel in self.config.get("roots", {}).get(lang, []) or []
                if not (self.repo / rel).exists()]

    def all_missing_roots(self) -> dict[str, list[str]]:
        """``lang -> declared roots that do not exist``, for the ones that have any."""
        out = {}
        for lang in self.config.get("roots", {}) or {}:
            missing = self.missing_roots(lang)
            if missing:
                out[lang] = missing
        return out

    def no_roots_note(self, lang: str) -> str:
        """Why an adapter is skipping: nothing declared, or nothing that exists."""
        missing = self.missing_roots(lang)
        if missing:
            return (f"{lang} root(s) {', '.join(missing)} declared in {CONFIG_NAME} "
                    f"do not exist — nothing was scanned")
        return NO_ROOTS_NOTE.format(lang=lang)

    def relpath(self, path: str | Path, base: Path | None = None) -> str:
        """Repo-relative POSIX path.

        ``base`` re-roots a path a tool reported relative to its own cwd. Tools
        that run inside a subdirectory (knip, madge, cargo) otherwise produce a
        plausible-looking path that does not exist, which is worse than no path
        at all: triage follows it and reads the wrong file.
        """
        p = Path(path)
        if not p.is_absolute():
            p = (base or self.repo) / p
        try:
            return p.resolve().relative_to(self.repo).as_posix()
        except ValueError:
            return p.as_posix()


# --------------------------------------------------------------------------
# Directory exclusion: one constant, several syntaxes
# --------------------------------------------------------------------------
#
# Every tool consumes the exclusion list differently, and getting one wrong does
# not fail loudly — it silently changes the finding counts, which then drift
# between checkouts (a developer with a gitignored `venv/` inside the source
# root gets a different baseline from CI, naming no culprit file). The
# derivations live beside each other so they cannot drift apart.


def prune_dirs(dirnames: list[str], exclude: Sequence[str]) -> None:
    """In-place ``os.walk`` prune by directory *basename*.

    Hidden directories are pruned wholesale rather than enumerated: every
    ``.tox``/``.venv``/``.cache`` variant a tool invents would otherwise have to
    be added to the exclusion list before anyone noticed the counts had moved.
    """
    dirnames[:] = [d for d in dirnames if d not in exclude and not d.startswith(".")]


def radon_ignore_arg(exclude: Sequence[str]) -> str:
    """radon matches ``-i`` entries against directory basenames while walking."""
    return ",".join(exclude)


def vulture_exclude_arg(root: Path, exclude: Sequence[str]) -> str:
    """vulture fnmatches the **whole path**, and silently rewrites a
    wildcard-free pattern to ``*name*`` — a bare ``env`` would also drop
    ``env_helpers.py``. So the globs are explicit *and* anchored under ``root``,
    otherwise a checkout living under e.g. ``/ci/build/`` excludes itself.
    """
    parts = []
    for name in exclude:
        parts.append(f"{root}/{name}/*")
        parts.append(f"{root}/*/{name}/*")
    return ",".join(parts)


# What actually feeds an analyser. A README or a fixture JSON under a source root
# does not change any finding, so it must not invalidate a scan.
SOURCE_SUFFIXES = {
    "python": (".py",),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "rust": (".rs",),
    "kotlin": (".kt", ".kts"),
}

_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")


def is_test_path(path: str | Path) -> bool:
    """Shared by the layering rule and the vulture filter: tests are held to
    different rules than the code they exercise, in both directions."""
    posix = Path(path).as_posix()
    if _TEST_FILE_RE.search(posix):
        return True
    return any(part in ("test", "tests") for part in Path(posix).parts)


def source_scan_targets(ctx: "Ctx") -> list[tuple[Path, tuple[str, ...]]]:
    """``(root, suffixes)`` pairs describing what an analyser actually reads.

    A repo with **no** declared roots is not a repo with no code: the
    language-agnostic adapters fall back to the whole checkout. That fallback has
    to live in one place, because two consumers derive from it and they disagreed
    silently — jscpd scanned the checkout while the freshness check watched the
    (empty) configured roots, so findings existed that nothing was watching, and
    triage happily queued a cluster for two files that had since been deleted.
    """
    out: list[tuple[Path, tuple[str, ...]]] = []
    for lang, suffixes in SOURCE_SUFFIXES.items():
        for root in ctx.roots(lang):
            out.append((root, suffixes))
    if not out:
        every = tuple(sorted({s for sfx in SOURCE_SUFFIXES.values() for s in sfx}))
        out.append((ctx.repo, every))
    return out


def under_roots(ctx: "Ctx", rel: str, lang: str) -> bool:
    """Is ``rel`` inside one of ``lang``'s declared roots?

    Several adapters cannot be *pointed* at a subdirectory — ``staticcheck`` and
    ``cargo clippy`` analyse the whole module/workspace by construction — so the
    roots were acting as an on/off switch for them and as a scope for everything
    else. A repo that declares ``roots.go = ["internal"]`` then got findings from
    its vendored examples, which is precisely the noise the roots exist to
    exclude. With no roots declared for the language, nothing is excluded.
    """
    roots = ctx.roots(lang)
    if not roots or not rel:
        return True
    path = (ctx.repo / rel).resolve()
    for root in roots:
        if path == root or root in path.parents:
            return True
    return False


def is_watched_source(ctx: "Ctx", rel: str) -> bool:
    """Would the freshness check notice an edit to this file?

    ``source_scan_targets`` answers "what is read" as ``(root, suffixes)`` pairs,
    but the language-agnostic adapters are handed the *roots* only — so jscpd
    could report a clone in a `.md` file under a source root, and triage would
    queue a cluster nothing was watching for staleness. This is the same
    predicate the freshness check applies, so the two cannot drift.
    """
    if not rel:
        return False
    path = (ctx.repo / rel).resolve()
    for root, suffixes in source_scan_targets(ctx):
        if path.suffix not in suffixes:
            continue
        if path == root or root in path.parents:
            return True
    return False


def iter_files(roots: Sequence[Path], suffixes: Sequence[str],
               exclude: Sequence[str]) -> Iterable[Path]:
    for root in roots:
        if root.is_file():
            if root.suffix in suffixes:
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            prune_dirs(dirnames, exclude)
            for name in filenames:
                if Path(name).suffix in suffixes:
                    yield Path(dirpath) / name


# --------------------------------------------------------------------------
# Builtin: Python import graph (cycles + layering)
# --------------------------------------------------------------------------
#
# This is the only *always-on* structural analyser and the one slice cheap
# enough for a merge gate: pure stdlib, no venv, no npm.


def _root_prefix(root: Path, repo: Path | None) -> list[str]:
    """The dotted prefix that makes a root's modules unique.

    ``root.name`` alone is not an identity: a monorepo with ``api/src`` and
    ``web/src`` produced the *same* canonical name for both copies of
    ``src/core/a.py``, so one root silently overwrote the other in the module
    table — and a real cycle inside the losing root was reported as zero
    findings. The whole repo-relative path disambiguates them.
    """
    if repo is not None:
        try:
            # Empty when the root *is* the repo (`roots: ["."]`). An empty prefix
            # would collide, and the directory name is the checkout's, not the
            # repo's: a CI clone or a worktree under another name re-minted every
            # cycle signature and failed the gate on cycles the baseline held.
            return list(root.resolve().relative_to(repo).parts) or ["_"]
        except ValueError:
            pass
    return [root.name]


def _module_names(path: Path, root: Path,
                  repo: Path | None = None) -> tuple[str, list[str]]:
    """``(canonical_name, [aliases])`` for one file.

    A source root is imported one of two ways and the layout does not say which:
    ``src/myapp/x.py`` is reached as ``myapp.x`` when ``src`` is on the path, and
    as ``src.myapp.x`` when it is a package in its own right. Indexing only one
    form means *no import resolves at all* — and the analyser then reports zero
    cycles, which reads exactly like a clean codebase. So both forms are indexed;
    the root-prefixed one is canonical, the bare ones are aliases used only for
    lookup. A spurious edge is a visible wrong answer; a missing one is invisible.
    """
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    prefix = _root_prefix(root, repo)
    canonical = ".".join(prefix + parts) if parts else ".".join(prefix)
    aliases = []
    if parts:
        aliases.append(".".join(parts))                 # `myapp.x`
        if len(prefix) > 1:
            aliases.append(".".join([prefix[-1]] + parts))  # `src.myapp.x`
    return canonical, aliases


def build_python_graph(roots: Sequence[Path], exclude: Sequence[str],
                       repo: Path | None = None) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Return ``(edges, module -> file)`` for the Python corpus.

    Only edges *within* the corpus are recorded — a cycle through ``requests``
    is not a cycle in this repo, and a layering rule cannot constrain a package
    you do not own.
    """
    files: dict[str, Path] = {}
    module_root: dict[str, int] = {}
    # One alias table per root, plus a global one. The aliases are *bare* names
    # (`core.b`), so with two roots that share a subpath they collide — and a
    # single global table resolved every root but the first into root one's
    # modules. A real cycle inside a later root was then reported as zero
    # findings, which is the exact silent-clean the root prefix exists to stop:
    # it fixed the canonical names and left the lookup they are found by alone.
    # An import resolves against its own root first; the global table stays as
    # the fallback so a genuine cross-root import still produces an edge.
    per_root: list[dict[str, str]] = []
    index: dict[str, str] = {}  # importable name -> canonical module
    for ordinal, root in enumerate(roots):
        local: dict[str, str] = {}
        per_root.append(local)
        for path in sorted(iter_files([root], [".py"], exclude)):
            try:
                canonical, aliases = _module_names(path, root, repo)
            except ValueError:
                continue
            files[canonical] = path
            module_root[canonical] = ordinal
            for name in [canonical, *aliases]:
                local.setdefault(name, canonical)
                index.setdefault(name, canonical)  # first root wins, deterministically
    edges: dict[str, set[str]] = {m: set() for m in files}
    for module, path in files.items():
        own = per_root[module_root[module]]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        # An `__init__.py`'s module *is* its package; taking the parent resolved
        # `from .a import f` in `pkg/__init__.py` to `a`, dropping the edge — and
        # with it the most common real cycle (package <-> its own submodule).
        if path.name == "__init__.py" or "." not in module:
            pkg = module
        else:
            pkg = module.rsplit(".", 1)[0]
        for node in _import_time_nodes(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import -> resolve against the package
                    base = pkg.split(".")
                    up = node.level - 1
                    base = base[: len(base) - up] if up else base
                    prefix = ".".join(base)
                    head = f"{prefix}.{node.module}" if node.module else prefix
                else:
                    head = node.module or ""
                # One edge per imported name, not one per name *plus* one for the
                # package. `from pkg.services import svc` really does import both,
                # but counting it twice doubles the file's layering score for a
                # single line of code and skews every ranking downstream.
                targets = [f"{head}.{alias.name}" if head else alias.name
                           for alias in node.names] or ([head] if head else [])
            for target in targets:
                resolved = _resolve(target, own, index)
                if resolved and resolved != module:
                    edges[module].add(resolved)
    return edges, {m: str(p) for m, p in files.items()}


def _import_time_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node that runs when the module is imported — not function bodies.

    An import inside a function is the standard way to *break* a cycle; counting
    it reported the fix as the problem (a lazy `from ._vendor import X` closed a
    false cycle through the package's `__init__`). ``if TYPE_CHECKING:`` is the
    other standard fix, so only its ``else`` branch runs at import time.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        if (isinstance(node, ast.If)
                and getattr(node.test, "id", getattr(node.test, "attr", None)) == "TYPE_CHECKING"):
            stack.extend(node.orelse)
            continue
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                               ast.Lambda)))


def _resolve(target: str, *indexes: dict[str, str]) -> str | None:
    """Longest-prefix match of a dotted import onto a canonical corpus module.

    ``from pkg.mod import thing`` names a *symbol*, not a module, so the lookup
    walks the dotted name down until it hits something the corpus contains.

    Several tables are tried in order and each is walked to exhaustion before
    the next: callers pass the importing module's own root first, so a bare
    alias that two roots both define resolves inside the root that wrote it.
    """
    for index in indexes:
        parts = target.split(".")
        while parts:
            canonical = index.get(".".join(parts))
            if canonical is not None:
                return canonical
            parts.pop()
    return None


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCCs; every component of size > 1 is a cycle.

    Reporting components rather than every elementary circuit is deliberate: a
    tangle of six mutually importing modules is *one* problem to untangle, not
    the hundreds of distinct circuits it technically contains.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = [0]
    out: list[list[str]] = []

    def strong_connect(node: str) -> None:
        # Iterative: a deep package tree overflows the recursion limit, and a
        # crashed structural gate is indistinguishable from a passing one to
        # anyone reading only the exit code.
        work: list[tuple[str, Iterable[str]]] = [(node, iter(sorted(edges.get(node, ()))))]
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        while work:
            current, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[current] = min(low[current], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[current])
            if low[current] == index[current]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == current:
                        break
                if len(component) > 1:
                    out.append(sorted(component))

    for node in sorted(edges):
        if node not in index:
            strong_connect(node)
    return sorted(out, key=lambda c: (-len(c), c))


def cycle_signature(members: Sequence[str]) -> str:
    """Canonical, rotation-independent identity of a cycle.

    Compared on *both* sides of a baseline diff, so a reordered or hand-edited
    entry is not mistaken for a new cycle.
    """
    return " -> ".join(sorted(members))


_SEGMENT_RE = re.compile(r"[./]")


def layer_of(module: str, order: Sequence[str]) -> int | None:
    """Which layer a module sits in, or ``None`` if the rule does not reach it.

    Split on both separators: Python modules are dotted, Go import paths are
    slashed, and a `.`-only split silently classified every Go package as
    unlayered — the check ran, reported zero, and looked like a clean result.
    """
    parts = _SEGMENT_RE.split(module)
    for i, layer in enumerate(order):
        if layer in parts:
            return i
    return None


def find_layering_violations(edges: dict[str, set[str]], order: Sequence[str],
                             exempt: frozenset[str] = frozenset(),
                             names: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """A more abstract layer importing a more concrete one.

    ``order`` is abstract -> concrete, so index 0 may be imported by anyone and
    may import nobody in the list.

    ``exempt`` holds modules the rule does not bind. Test modules belong there:
    a suite that mirrors the source layout (``tests/unit/utils/test_x.py``) picks
    up a layer name from its *path*, and a utils test legitimately importing a
    service then reports as a violation. On the first real repo this was run
    against, every single layering finding was that false positive — enough to
    make the whole signal unbelievable.

    ``names`` maps a module to the name its layer is read from — the module's
    own path, without the root or Go module path in front. A repo called
    ``handler``, or a root under ``core/``, otherwise puts every module in that
    one layer: false violations in Go, and zero real ones in Python.
    """
    names = names or {}
    out = []
    for source in sorted(edges):
        if source in exempt:
            continue
        src_layer = layer_of(names.get(source, source), order)
        if src_layer is None:
            continue
        for target in sorted(edges[source]):
            tgt_layer = layer_of(names.get(target, target), order)
            if tgt_layer is None:
                continue
            if tgt_layer > src_layer:
                out.append((source, target))
    return out


def run_import_analysis(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("python")
    if not roots:
        return ToolResult("import-analysis", "skipped", note=ctx.no_roots_note("python"))
    edges, module_files = build_python_graph(roots, ctx.exclude_dirs, ctx.repo)
    if not edges:
        return ToolResult("import-analysis", "skipped", note="no python modules found")
    findings: list[dict[str, Any]] = []
    for component in find_cycles(edges):
        signature = cycle_signature(component)
        for module in component:
            findings.append(finding(
                ctx.relpath(module_files.get(module, module)),
                "circular_import",
                f"import cycle: {signature}",
                tool="import-analysis",
                extra={"signature": signature},
            ))
    order = ctx.config.get("layers", {}).get("order") or []
    # Repo-relative, always: `is_test_path` on an absolute path made every
    # module exempt for anyone whose checkout happened to live under a directory
    # called `test`/`tests` — the layering rule then reported zero, which reads
    # exactly like a clean codebase.
    exempt = frozenset(m for m, f in module_files.items()
                       if is_test_path(ctx.relpath(f)))
    names = {}
    for module, path in module_files.items():
        root = next((r for r in roots if r in Path(path).parents), None)
        if root is not None:
            names[module] = ".".join(Path(path).relative_to(root).with_suffix("").parts)
    for source, target in find_layering_violations(edges, order, exempt, names):
        signature = f"{source} -> {target}"
        findings.append(finding(
            ctx.relpath(module_files.get(source, source)),
            "layering_violation",
            f"layering violation: {signature}",
            tool="import-analysis",
            extra={"signature": signature},
        ))
    return ToolResult(
        "import-analysis", "ok", findings,
        metrics={"modules": len(edges), "edges": sum(len(v) for v in edges.values()),
                 "layers": list(order)},
    )


# --------------------------------------------------------------------------
# Builtin: git churn (a risk multiplier, not a finding source)
# --------------------------------------------------------------------------


def git_churn(ctx: Ctx) -> dict[str, int]:
    """Commits per file over the configured window.

    Churn is what separates *debt* from *dead weight*. A 400-line god function
    nobody has touched in three years costs nothing to leave alone; the same
    function edited every sprint charges interest on every one of those edits.
    Static findings alone cannot tell the two apart, so ranking multiplies by
    change frequency rather than pretending all complexity is equally urgent.
    """
    cfg = ctx.config.get("churn", {})
    if not cfg.get("enabled", True):
        return {}
    days = int(cfg.get("days", 180))
    # `git log --name-only` prints paths relative to the **git top-level**, but
    # every finding is relative to the scanned root. Scanning a subproject of a
    # larger checkout therefore matched nothing at all: `sub/src/a.py` from git
    # against `src/a.py` from the scanner, every lookup missing, every file
    # silently scored as never touched. Churn is the multiplier that separates
    # debt from dead weight, so losing it does not fail — it just quietly ranks
    # a monorepo the way it would rank a repo with no history.
    prefix = _git_prefix(ctx.repo)
    if prefix is None:
        return {}
    # `core.quotepath=false`: git otherwise C-quotes any path with a non-ASCII
    # byte ("src/\303\244.py"), which then matches no finding and silently drops
    # those files to zero churn — i.e. exactly the files get *under*-ranked.
    proc = _run(["git", "-c", "core.quotepath=false", "log",
                 f"--since={days} days ago", "--name-only",
                 "--pretty=format:", "--no-merges"], cwd=ctx.repo)
    if proc is None or proc.returncode != 0:
        return {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        if prefix:
            if not name.startswith(prefix):
                continue  # outside the scanned root; no finding can name it
            name = name[len(prefix):]
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _git_prefix(repo: Path) -> str | None:
    """``repo``'s path relative to the git top-level, ``""`` at the top, or
    ``None`` when it is not a working tree at all."""
    proc = _run(["git", "rev-parse", "--show-prefix"], cwd=repo)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# Process + availability helpers
# --------------------------------------------------------------------------


def _run(cmd: Sequence[str], cwd: Path | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess[str] | None:
    """Run a command, returning ``None`` if it cannot be run at all.

    ``None`` means *skipped* (binary vanished); a non-zero return code means the
    tool ran and had something to say, which for most analysers is how they
    report findings. A timeout is neither: the tool is installed and did not
    finish, so it comes back as a failed run with no output — which every
    adapter reports as `error`. As `skipped` it let `--update-baseline` bank a
    baseline that silently lacked the tool, and so never gated it again.
    """
    try:
        return subprocess.run(list(cmd), cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(cmd), -9, stdout="",
                                           stderr=f"timed out after {timeout}s")
    except (FileNotFoundError, PermissionError):
        return None


def _have_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _have_module(name: str) -> bool:
    proc = _run([sys.executable, "-c", f"import {name}"])
    return proc is not None and proc.returncode == 0


def _node_bin(root: Path, tool: str) -> Path | None:
    """Locate a node tool in the project's own ``node_modules``.

    Probing the path directly rather than shelling out to ``npx`` avoids a
    registry round-trip that turns "not installed" into a 30-second hang, and
    stops ``npx`` from helpfully *downloading* a tool mid-scan.
    """
    candidate = root / "node_modules" / ".bin" / tool
    return candidate if candidate.exists() else None


def _json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _json_after_preamble(text: str) -> Any:
    """Parse knip's JSON report, skipping anything printed before it.

    knip loads the project's own config files (webpack, jest, vite) through its
    plugins, and whatever those print lands on stdout ahead of the report. A strict parse then
    turned one chatty config into a permanent ``error`` that blocks the baseline.
    """
    payload = _json_loads(text)
    if payload is not None:
        return payload
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("{", "[")):
            payload = _json_loads(text[offset:])
            if payload is not None:
                return payload
        offset += len(line)
    return None


# --------------------------------------------------------------------------
# Python adapters
# --------------------------------------------------------------------------

_RANK_ORDER = "ABCDEF"


def _has_python_sources(ctx: Ctx, roots: Sequence[Path]) -> bool:
    """Is there anything under ``roots`` a Python analyser should have seen?

    The same exclusion list the analyser is given, so "it found nothing" and
    "there was nothing to find" cannot disagree: a root holding only excluded
    subdirectories legitimately yields an empty report, and must not be reported
    as a broken tool.
    """
    return next(iter_files(roots, [".py"], ctx.exclude_dirs), None) is not None


def run_radon(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("python")
    if not roots:
        return ToolResult("radon", "skipped", note=ctx.no_roots_note("python"))
    if not _have_module("radon"):
        return ToolResult("radon", "skipped", note="pip install radon")
    min_rank = str(ctx.thresholds.get("complexity_rank", "D")).upper()
    mi_floor = float(ctx.thresholds.get("mi_floor", 20.0))
    ignore = radon_ignore_arg(ctx.exclude_dirs)
    findings: list[dict[str, Any]] = []
    below_floor: set[str] = set()

    cc = _run([sys.executable, "-m", "radon", "cc", "-j", "-i", ignore, *map(str, roots)], cwd=ctx.repo)
    if cc is None:
        return ToolResult("radon", "skipped", note="radon could not be executed")
    cc_payload = _json_loads(cc.stdout)
    # `radon cc` leaves out files with no function or class, so `{}` is also its
    # honest answer for a root of plain scripts; `radon mi` lists every file it
    # read, so it is the one that says whether anything was read at all.
    mi = _run([sys.executable, "-m", "radon", "mi", "-j", "-i", ignore, *map(str, roots)], cwd=ctx.repo)
    mi_payload = _json_loads(mi.stdout) if mi is not None else None
    # radon exits 0 for almost everything — a path that does not exist, a root it
    # cannot read, an empty tree — and says so only by returning `{}`. So the
    # return code is not the discriminator here; *covering nothing while there is
    # something to cover* is. Left as a bare `or {}`, a radon that read no file at
    # all banked `radon: 0`, and a gate whose baseline was taken while it worked
    # then passed green with complexity no longer measured at all.
    if not isinstance(cc_payload, dict) or (not cc_payload and not mi_payload
                                            and _has_python_sources(ctx, roots)):
        detail = (cc.stderr or cc.stdout or "").strip()[-300:]
        return ToolResult("radon", "error", note=(
            f"radon cc produced no usable output for {', '.join(ctx.relpath(r) for r in roots)}"
            + (f": {detail}" if detail else "")))
    payload = cc_payload
    unparseable: list[str] = []
    for path, blocks in payload.items():
        if not isinstance(blocks, list):
            # radon reports a per-file parse error as {"error": "..."}. One
            # unreadable fixture must not withhold the whole count (that would
            # make the baseline unbankable for any repo that ships broken-syntax
            # test data), but it is coverage this run did not get, so it is said
            # out loud rather than dropped.
            unparseable.append(ctx.relpath(path))
            continue
        for block in blocks:
            # A class block's complexity is derived from its methods, which radon
            # also lists on their own — counting both doubled every complex method.
            if block.get("type") == "class":
                continue
            rank = str(block.get("rank", "A")).upper()
            if _RANK_ORDER.find(rank) < _RANK_ORDER.find(min_rank):
                continue
            findings.append(finding(
                ctx.relpath(path), "complexity",
                f"{block.get('name')} has cyclomatic complexity "
                f"{block.get('complexity')} (rank {rank})",
                tool="radon", line=block.get("lineno"),
                extra={"weight": complexity_weight(block.get("complexity"))},
            ))

    if isinstance(mi_payload, dict):
        for path, entry in mi_payload.items():
            if not isinstance(entry, dict) or "mi" not in entry:
                continue
            score = float(entry["mi"])
            if score < mi_floor:
                rel = ctx.relpath(path)
                below_floor.add(rel)
                findings.append(finding(
                    rel, "maintainability",
                    f"maintainability index {score:.1f} (floor {mi_floor:.0f})",
                    tool="radon",
                ))
    metrics: dict[str, Any] = {"mi_floor": mi_floor, "min_rank": min_rank,
                               "unparseable_files": len(unparseable)}
    notes = []
    if isinstance(mi_payload, dict):
        metrics["files_below_mi_floor"] = len(below_floor)
    else:
        # The same rule as a skipped tool, one level down: `radon cc` succeeded
        # and `radon mi` did not, so nothing here knows what the maintainability
        # index would have said. Recording 0 banked a false zero — which the
        # gate can never read as a regression, so the MI half went permanently
        # green while it was not being measured at all.
        notes.append("radon mi produced no usable output — complexity ranks only")
    if unparseable:
        shown = ", ".join(unparseable[:3]) + (f" (+{len(unparseable) - 3} more)"
                                              if len(unparseable) > 3 else "")
        notes.append(f"{len(unparseable)} file(s) radon could not parse: {shown}")
    return ToolResult("radon", "ok", findings, metrics=metrics, note="; ".join(notes))


_VULTURE_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<detail>.+?) \((?P<conf>\d+)% confidence\)$")
_VULTURE_PARSE_ERR_RE = re.compile(r"^(?P<file>.+?\.py):\d+: ")
# Deliberately not `unused attribute`: an attribute is never injected by name the
# way a fixture parameter is, so suppressing one would hide real dead code that
# merely shares a fixture's name.
_UNUSED_VAR_RE = re.compile(r"^unused variable '(?P<name>[^']+)'$")
def _fixture_names(source: str) -> set[str]:
    """Names pytest can inject from this source, honouring ``@fixture(name=...)``.

    An explicit ``name=`` *replaces* the function name — the function name stops
    being injectable — so the alias is not merely added.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        alias = None
        is_fixture = False
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            target = call.func if call else dec
            attr = getattr(target, "attr", None) or getattr(target, "id", None)
            if attr != "fixture":
                continue
            is_fixture = True
            if call:
                for kw in call.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        alias = str(kw.value.value)
        if is_fixture:
            names.add(alias or node.name)
    return names


def _fixture_names_in_file(path: Path) -> set[str]:
    try:
        return _fixture_names(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return set()


def conftest_fixture_names(directory: Path, stop: Path) -> set[str]:
    """Fixtures reaching ``directory`` from ``conftest.py`` files above it."""
    names: set[str] = set()
    current = directory
    while True:
        conftest = current / "conftest.py"
        if conftest.is_file():
            names |= _fixture_names_in_file(conftest)
        if current == stop or current.parent == current:
            break
        current = current.parent
    return names


def visible_fixture_names(path: Path, stop: Path) -> set[str]:
    """Fixtures pytest would inject into ``path``: its own, plus every
    ``conftest.py`` in an *ancestor* directory. A conftest below the file is not
    visible, and neither is a plugin's fixture.
    """
    return _fixture_names_in_file(path) | conftest_fixture_names(path.parent, stop)


def filter_fixture_params(findings: Sequence[dict[str, Any]], repo: Path) -> tuple[list[dict[str, Any]], int]:
    """Drop ``unused variable`` findings that are really pytest fixture params.

    pytest injects fixtures **by parameter name at runtime**, so a
    side-effect-only fixture — one whose entire job is the ``monkeypatch.setenv``
    it performs — has a parameter that is never read and never will be. vulture
    reports it at 100% confidence, and acting on the report deletes the line that
    establishes the condition under test: the test keeps passing while no longer
    testing anything.

    The filter matches the *shape*, not the directory, on all three of: an
    unused-variable finding, in a test file, whose name is a fixture visible to
    that file. Excluding test roots outright would also hide genuinely dead test
    code; an allowlist entry would be name-global and would silence a production
    variable of the same name.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    # Two caches, because the two halves have different scopes: the conftest
    # chain is shared by every file in a directory, a file's own fixtures are
    # not. Caching both by directory would let one test file's fixtures suppress
    # a same-named finding in its neighbour.
    conftest_cache: dict[Path, set[str]] = {}
    own_cache: dict[Path, set[str]] = {}
    for item in findings:
        match = _UNUSED_VAR_RE.match(item.get("detail", ""))
        if not match or not is_test_path(item["file"]):
            kept.append(item)
            continue
        path = repo / item["file"]
        if path.parent not in conftest_cache:
            conftest_cache[path.parent] = conftest_fixture_names(path.parent, repo)
        if path not in own_cache:
            own_cache[path] = _fixture_names_in_file(path)
        if match.group("name") in (own_cache[path] | conftest_cache[path.parent]):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def run_vulture(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("python")
    if not roots:
        return ToolResult("vulture", "skipped", note=ctx.no_roots_note("python"))
    if not _have_module("vulture"):
        return ToolResult("vulture", "skipped", note="pip install vulture")
    min_conf = int(ctx.thresholds.get("vulture_min_confidence", 80))
    cmd = [sys.executable, "-m", "vulture", "--min-confidence", str(min_conf),
           "--exclude", vulture_exclude_arg(ctx.repo, ctx.exclude_dirs), *map(str, roots)]
    allowlist = ctx.repo / ".vulture_allowlist.py"
    if allowlist.is_file():
        cmd.append(str(allowlist))
    proc = _run(cmd, cwd=ctx.repo)
    if proc is None:
        return ToolResult("vulture", "skipped", note="vulture could not be executed")
    # vulture's exit codes: 0 = nothing found, 3 = dead code found, 1 = invalid
    # input, 2 = bad arguments. A file it cannot parse is reported on stderr as
    # `path:line: message` and *skipped* — the run continues, and the exit code
    # is 3 if anything else was found, 1 if not. Treated like radon's per-file
    # parse errors: named in the note, not allowed to withhold the whole count
    # (whether the run "failed" would otherwise hinge on whether dead code
    # happened to exist elsewhere).
    unparseable = sorted({ctx.relpath(m.group("file")) for m in map(
        _VULTURE_PARSE_ERR_RE.match, proc.stderr.splitlines()) if m})
    if proc.returncode not in (0, 3) and not (proc.returncode == 1 and unparseable):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return ToolResult("vulture", "error", note=(
            f"vulture exited {proc.returncode}: {detail[0][:300] if detail else 'no detail'}"))
    raw: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        match = _VULTURE_RE.match(line.strip())
        if not match:
            continue
        raw.append(finding(ctx.relpath(match.group("file")), "dead_code",
                           match.group("detail"), tool="vulture",
                           line=int(match.group("line")),
                           extra={"confidence": int(match.group("conf"))}))
    kept, dropped = filter_fixture_params(raw, ctx.repo)
    note = ""
    if unparseable:
        shown = ", ".join(unparseable[:3]) + (f" (+{len(unparseable) - 3} more)"
                                              if len(unparseable) > 3 else "")
        note = f"{len(unparseable)} file(s) vulture could not parse: {shown}"
    return ToolResult("vulture", "ok", kept, note=note,
                      metrics={"min_confidence": min_conf,
                               "suppressed_fixture_params": dropped,
                               "unparseable_files": len(unparseable)})


# --------------------------------------------------------------------------
# Language-agnostic: jscpd (duplication)
# --------------------------------------------------------------------------


def _find_node_bin(ctx: Ctx, tool: str) -> tuple[Path | None, Path]:
    """``(binary, cwd)`` for a node tool, searching the TS roots then the repo."""
    for root in ctx.roots("typescript") + [ctx.repo]:
        found = _node_bin(root, tool)
        if found:
            return found, root
    if _have_binary(tool):
        return Path(tool), ctx.repo
    return None, ctx.repo


_CLONE_PARTNER_RE = re.compile(r"shared with (.+)$")


def _reroot(ctx: Ctx, raw: str, bases: Sequence[Path]) -> str:
    """Resolve a tool-reported path that is relative to *one of* several bases.

    jscpd reports each finding relative to the scan target it came from, not to
    the process cwd — so once more than one root is passed, the same string can
    mean different files. Resolving against the wrong base yields a path that
    looks entirely plausible and does not exist, which is worse than no path:
    triage follows it and reads whatever happens to be there.

    So the base is chosen by *checking the filesystem*, not by assuming.
    """
    if not raw:
        return raw
    candidate = Path(raw)
    if candidate.is_absolute():
        return ctx.relpath(candidate)
    matches: list[Path] = []
    for base in dict.fromkeys([*bases, ctx.repo]):
        resolved = base / candidate
        if resolved.exists():
            resolved = resolved.resolve()
            if resolved not in matches:
                matches.append(resolved)
    if len(matches) == 1:
        return ctx.relpath(matches[0])
    # Zero matches, or several. **Existence is not uniqueness**: with roots
    # `api/` and `web/` both containing `src/a.js`, taking the first match
    # attributed a clone that lives in `web/` to an innocent, non-duplicated
    # `api/src/a.js`. Keep the raw string rather than inventing a repo-relative
    # path that resolves to the wrong file — no path beats a plausible wrong one.
    # `mark_unresolved` flags whatever comes back here that is not a real file.
    return raw


def run_jscpd(ctx: Ctx) -> ToolResult:
    """Copy-paste detection. Language-agnostic, so it is the one duplication
    signal every configured stack gets."""
    binary, cwd = _find_node_bin(ctx, "jscpd")
    if binary is None:
        return ToolResult("jscpd", "skipped", note="npm i -D jscpd")
    min_lines = int(ctx.thresholds.get("jscpd_min_lines", 12))
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [str(binary), "--reporters", "json", "--output", tmp,
               "--min-lines", str(min_lines), "--silent",
               # Absolute paths remove the re-rooting guess entirely: jscpd
               # otherwise reports each finding relative to the scan target it
               # came from, which is ambiguous the moment two roots share a
               # subpath. `_reroot` stays as the fallback for versions that
               # ignore this flag.
               "--absolute"]
        config = ctx.repo / ".jscpd.json"
        if config.is_file():
            cmd += ["--config", str(config)]
        else:
            # Without a config, exclusion has to be passed explicitly — jscpd's
            # own default ignores are not the same list as ours. One comma-joined
            # flag: jscpd 4.x keeps only the *last* of repeated `--ignore`s.
            if ctx.exclude_dirs:
                cmd += ["--ignore", ",".join(f"**/{name}/**" for name in ctx.exclude_dirs)]
        # Scan the declared source roots, not the whole checkout. A repo that
        # says its code lives in `src/` does not want clone reports from `docs/`,
        # `examples/`, test fixtures, or the vendored copy of this very script.
        # Shared with the freshness check, so what gets scanned and what gets
        # watched for staleness cannot drift apart.
        bases = sorted({root for root, _ in source_scan_targets(ctx)})
        cmd.extend(str(path) for path in bases)
        proc = _run(cmd, cwd=cwd)
        if proc is None:
            return ToolResult("jscpd", "skipped", note="jscpd could not be executed")
        report = Path(tmp) / "jscpd-report.json"
        if not report.is_file():
            return ToolResult("jscpd", "error", note=(proc.stderr or proc.stdout)[-400:])
        payload = _json_loads(report.read_text(encoding="utf-8")) or {}

    findings: list[dict[str, Any]] = []
    unwatched = 0
    for clone in payload.get("duplicates", []) or []:
        first = clone.get("firstFile") or {}
        second = clone.get("secondFile") or {}
        first_path = _reroot(ctx, first.get("name", ""), bases)
        second_path = _reroot(ctx, second.get("name", ""), bases)
        # jscpd is handed root *directories* and picks its own formats, so it
        # reports clones in READMEs and fixture JSON under a source root. The
        # freshness check watches source suffixes only, so those findings were
        # the one class that could go stale unnoticed. Dropped as a *pair*:
        # keeping one side leaves a cluster edge pointing at an unwatched file.
        if not (is_watched_source(ctx, first_path) and is_watched_source(ctx, second_path)):
            unwatched += 1
            continue
        lines = clone.get("lines") or 0
        # Recorded from both sides: a clone is a property of a *pair*, and a
        # cluster is only correct if both files carry the edge.
        # `partner_line` lets triage pair the two *fragments*, not just the files.
        for own, other, entry, other_entry in ((first_path, second_path, first, second),
                                               (second_path, first_path, second, first)):
            findings.append(finding(
                own, "duplicate", f"{lines}-line clone shared with {other}",
                tool="jscpd", line=(entry.get("start") or None),
                extra={"partner": other, "clone_lines": lines,
                       "partner_line": other_entry.get("start") or None},
            ))
    return ToolResult("jscpd", "ok", findings,
                      metrics={"min_lines": min_lines,
                               "clone_pairs": len(payload.get("duplicates", []) or []),
                               "skipped_unwatched_pairs": unwatched})


# --------------------------------------------------------------------------
# TypeScript adapters
# --------------------------------------------------------------------------


def _nearest(ctx: Ctx, root: Path, name: str) -> Path | None:
    """The nearest ``name`` at or above ``root``, without leaving the repo."""
    for d in (root, *root.parents):
        if (d / name).is_file():
            return d / name
        if d == ctx.repo:
            break
    return None


def _package_dir(ctx: Ctx, root: Path) -> Path:
    """The directory holding the package.json for ``root``, or ``root`` itself."""
    found = _nearest(ctx, root, "package.json")
    return found.parent if found else root


def run_knip(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("typescript")
    if not roots:
        return ToolResult("knip", "skipped", note=ctx.no_roots_note("typescript"))
    binary, _ = _find_node_bin(ctx, "knip")
    if binary is None:
        return ToolResult("knip", "skipped", note="npm i -D knip")
    # Every declared root. Running only in `roots[0]` gave a second TS root no
    # dead-code coverage at all, and said nothing about it anywhere in the
    # report — the silent half-scan a reader has no way to notice.
    findings: list[dict[str, Any]] = []
    ran: list[str] = []
    failed: list[str] = []
    # knip refuses to run without a package.json in its cwd, and the usual layout
    # keeps it above the source root (`package.json` + `src/`). It then analyses
    # the whole package, so `under_roots` scopes the findings back down.
    for base in dict.fromkeys(_package_dir(ctx, root) for root in roots):
        proc = _run([str(binary), "--reporter", "json", "--no-progress"], cwd=base)
        payload = _json_after_preamble(proc.stdout) if proc is not None else None
        if not isinstance(payload, dict):
            failed.append(ctx.relpath(base))
            continue
        ran.append(ctx.relpath(base))
        # Unused files: a top-level `files` list up to knip 5, a per-issue
        # `files: [{name}]` from knip 6 on. Reading only the old shape dropped
        # every orphan file on knip 6 without a word.
        orphans = set(payload.get("files", []) or [])
        orphans.update(issue.get("file", "") for issue in payload.get("issues", []) or []
                       if issue.get("files"))
        for entry in sorted(orphans):
            # knip reports paths relative to its own cwd; resolving them against
            # the repo root produces a plausible path that does not exist.
            rel = ctx.relpath(entry, base)
            if under_roots(ctx, rel, "typescript"):
                findings.append(finding(rel, "dead_code",
                                        "file is not imported anywhere", tool="knip"))
        for issue in payload.get("issues", []) or []:
            rel = ctx.relpath(issue.get("file", ""), base)
            if not under_roots(ctx, rel, "typescript"):
                continue
            # Only "unused" categories. `unlisted` is a dependency that *is* used
            # (just missing from package.json), and `duplicates` lists aliases.
            for category in ("exports", "types", "enumMembers"):
                items = issue.get(category) or []
                if isinstance(items, dict):
                    items = [v for group in items.values() for v in group]
                for item in items:
                    name = item.get("name") if isinstance(item, dict) else str(item)
                    line = item.get("line") if isinstance(item, dict) else None
                    findings.append(finding(rel, "dead_code", f"unused {category[:-1]} '{name}'",
                                            tool="knip", line=line))
    if not ran:
        return ToolResult("knip", "error",
                          note=f"knip produced no usable output in {', '.join(failed)}")
    return ToolResult("knip", "ok", findings,
                      # A count that covers three roots and a count that covers
                      # one are not comparable, so the report records which roots
                      # the number came from — and `incomplete` keeps the
                      # non-comparable one out of the baseline entirely.
                      metrics={"bases": ran, "bases_failed": failed},
                      note=(f"no usable output in {', '.join(failed)}" if failed else ""),
                      incomplete=bool(failed))


def run_madge(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("typescript")
    if not roots:
        return ToolResult("madge", "skipped", note=ctx.no_roots_note("typescript"))
    binary, _ = _find_node_bin(ctx, "madge")
    if binary is None:
        return ToolResult("madge", "skipped", note="npm i -D madge")
    findings: list[dict[str, Any]] = []
    ran: list[str] = []
    failed: list[str] = []
    cycles = 0
    # Every declared root, for the same reason as knip — and it matters more
    # here: a structural analyser that quietly covered one of three roots
    # reports zero cycles for the other two, which reads as clean.
    for base in roots:
        # madge's default extension list is JS-only: on a TypeScript project it
        # traverses nothing and cheerfully reports zero cycles. A silent zero
        # from a structural analyser is the worst possible output, so the
        # extensions are always passed explicitly.
        cmd = [str(binary), "--circular", "--json",
               "--extensions", "ts,tsx,mts,cts,js,jsx,mjs,cjs"]
        # A built `dist/` under the root otherwise doubles every cycle, and makes
        # the signatures differ between a built checkout and a fresh one.
        if ctx.exclude_dirs:
            cmd += ["--exclude", "(^|/)(" + "|".join(map(re.escape, ctx.exclude_dirs)) + ")/"]
        # Usually at the repo root, not in `src/`. Without it, every `@/…` path
        # alias resolves to nothing and the graph silently loses those edges.
        tsconfig = _nearest(ctx, base, "tsconfig.json")
        if tsconfig is not None:
            cmd += ["--ts-config", str(tsconfig)]
        proc = _run(cmd + ["."], cwd=base)
        payload = _json_loads(proc.stdout) if proc is not None else None
        if not isinstance(payload, list):
            failed.append(ctx.relpath(base))
            continue
        ran.append(ctx.relpath(base))
        cycles += len(payload)
        for cycle in payload:
            members = [ctx.relpath(member, base) for member in cycle]
            signature = cycle_signature(members)
            for member in members:
                findings.append(finding(member, "circular_import",
                                        f"import cycle: {signature}", tool="madge",
                                        extra={"signature": signature}))
    if not ran:
        return ToolResult("madge", "error",
                          note=f"madge produced no usable output in {', '.join(failed)}")
    # `incomplete` matters most here: madge is structural, so a root it could
    # not read contributes no signatures either — and `diff_baseline` only ever
    # reports *new* ones, so cycles disappearing is invisible on its own.
    return ToolResult("madge", "ok", findings,
                      metrics={"cycles": cycles, "bases": ran, "bases_failed": failed},
                      note=(f"no usable output in {', '.join(failed)}" if failed else ""),
                      incomplete=bool(failed))


# --------------------------------------------------------------------------
# Go adapters  (unvalidated against a real Go repo — see docs)
# --------------------------------------------------------------------------

_GOCYCLO_RE = re.compile(r"^(?P<cc>\d+)\s+(?P<pkg>\S+)\s+(?P<func>\S+)\s+(?P<file>.+):(?P<line>\d+):\d+$")


def run_gocyclo(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("go")
    if not roots:
        return ToolResult("gocyclo", "skipped", note=ctx.no_roots_note("go"))
    if not _have_binary("gocyclo"):
        return ToolResult("gocyclo", "skipped", note="go install github.com/fzipp/gocyclo/cmd/gocyclo@latest")
    over = int(ctx.thresholds.get("gocyclo_over", 15))
    findings: list[dict[str, Any]] = []
    # Once per declared root, so the same rule knip and madge follow applies:
    # a root that could not be scanned makes the count cover less than it claims.
    # `continue` alone banked a full count for a partial scan — and counts only
    # ever regress upward, so breaking gocyclo in one root of a monorepo read as
    # an improvement.
    ran: list[str] = []
    failed: list[str] = []
    for root in roots:
        proc = _run(["gocyclo", "-over", str(over), str(root)], cwd=ctx.repo)
        if proc is None:
            failed.append(ctx.relpath(root))
            continue
        matches = [m for m in (_GOCYCLO_RE.match(line.strip())
                               for line in proc.stdout.splitlines()) if m]
        # gocyclo exits 1 for **both** "something is over the threshold" and
        # "I could not read that path", so the exit code cannot tell them apart
        # — but the output can: the first prints the functions it found, the
        # second prints nothing at all. Reading the exit code alone, an
        # installed gocyclo pointed at an unreadable root came back `ok` with
        # zero findings, which is bankable as a baseline and can only ever be
        # read as an improvement afterwards.
        if proc.returncode != 0 and not matches:
            failed.append(ctx.relpath(root))
            continue
        ran.append(ctx.relpath(root))
        for match in matches:
            findings.append(finding(
                ctx.relpath(match.group("file")), "complexity",
                f"{match.group('func')} has cyclomatic complexity {match.group('cc')} (over {over})",
                tool="gocyclo", line=int(match.group("line")),
                extra={"weight": complexity_weight(match.group("cc"))},
            ))
    if not ran:
        return ToolResult("gocyclo", "error",
                          note=f"gocyclo could not be run in {', '.join(failed)}")
    return ToolResult("gocyclo", "ok", findings,
                      metrics={"over": over, "bases": ran, "bases_failed": failed},
                      note=(f"could not be run in {', '.join(failed)}" if failed else ""),
                      incomplete=bool(failed))


def run_staticcheck(ctx: Ctx) -> ToolResult:
    """Only the unused-code checks (``U1000``). The correctness checks belong on
    a lint gate, not in a maintainability queue."""
    roots = ctx.roots("go")
    if not roots:
        return ToolResult("staticcheck", "skipped", note=ctx.no_roots_note("go"))
    if not _have_binary("staticcheck"):
        return ToolResult("staticcheck", "skipped", note="go install honnef.co/go/tools/cmd/staticcheck@latest")
    findings: list[dict[str, Any]] = []
    # Run from each root rather than the repo: `./...` needs a go.mod at or
    # above its cwd, and a monorepo keeps it in `backend/`, not at the top.
    for root in roots:
        proc = _run(["staticcheck", "-f", "json", "-checks", "U1000", "./..."], cwd=root)
        if proc is None:
            return ToolResult("staticcheck", "skipped", note="staticcheck could not be executed")
        error = _staticcheck_findings(ctx, proc, findings)
        if error:
            return ToolResult("staticcheck", "error", note=error)
    return ToolResult("staticcheck", "ok", findings)


def _staticcheck_findings(ctx: Ctx, proc: subprocess.CompletedProcess[str],
                          findings: list[dict[str, Any]]) -> str:
    """Append one run's U1000 findings; return why the run failed, or ``""``."""
    entries = 0
    # Anything that is not the check we asked for. With `-checks U1000` the only
    # realistic source is staticcheck reporting that it could not analyse the
    # package at all — `{"code": "compile", ...}` for a module that does not
    # build. Both halves of that were wrong before: the compile error was turned
    # into a `dead_code` *finding* (which clusters, ranks, and reaches the triage
    # queue as "unused code: syntax error: unexpected }"), and the run that
    # analysed nothing banked `staticcheck: 0`.
    blockers: list[str] = []
    for line in proc.stdout.splitlines():
        # staticcheck -f json emits one JSON object per line, not an array.
        entry = _json_loads(line)
        if not isinstance(entry, dict):
            continue
        entries += 1
        if str(entry.get("code") or "") != "U1000":
            blockers.append(f"{entry.get('code', '?')}: {entry.get('message', '')}".strip())
            continue
        location = entry.get("location") or {}
        rel = ctx.relpath(location.get("file", ""))
        # `./...` is module-wide by construction, so the roots have to be applied
        # afterwards or they mean nothing here — a repo declaring
        # `roots.go = ["internal"]` was getting findings from its examples tree.
        if not under_roots(ctx, rel, "go") or any(
                f["file"] == rel and f["line"] == location.get("line") for f in findings):
            continue
        findings.append(finding(rel, "dead_code",
                                entry.get("message", "unused"), tool="staticcheck",
                                line=location.get("line")))
    if blockers:
        return (f"staticcheck reported {len(blockers)} non-U1000 diagnostic(s) — the "
                f"package did not analyse cleanly: {blockers[0][:200]}")
    if not entries and proc.returncode != 0:
        # No JSON at all and a failure exit: the module did not build, so nothing
        # here knows what staticcheck would have said. Reporting that as clean
        # banks a false zero, exactly as it would for clippy.
        return ((proc.stderr or proc.stdout or "").strip()[-400:] or
                f"staticcheck exited {proc.returncode} with no output")
    return ""


def run_go_layering(ctx: Ctx) -> ToolResult:
    """Layering over the Go package graph.

    Go's compiler already forbids import cycles, so there is nothing to detect
    there — layering is the whole value of building this graph.
    """
    roots = ctx.roots("go")
    order = ctx.config.get("layers", {}).get("order") or []
    if not roots:
        # No Go in this repo: not applicable, in the note every other adapter
        # uses for that, so triage does not read it as a missing tool.
        return ToolResult("go-layering", "skipped", note=ctx.no_roots_note("go"))
    if not order:
        # Name the half that is missing. "needs go roots and layers.order" was
        # the whole remedy for both cases, and the structural gate quotes this
        # note when it refuses — sending a Go repo that simply has no declared
        # layering off to add roots it already has.
        return ToolResult("go-layering", "skipped", note=NO_LAYERS_NOTE)
    if not _have_binary("go"):
        return ToolResult("go-layering", "skipped", note="go toolchain not installed")
    packages: list[dict[str, Any]] = []
    for root in roots:
        # Per root for the same go.mod reason as staticcheck. `-e` keeps a package
        # with a broken dependency in the graph instead of failing the whole list.
        proc = _run(["go", "list", "-e", "-json", "./..."], cwd=root)
        if proc is None or proc.returncode != 0:
            # The toolchain is here and the graph was not built: `skipped` let
            # the structural gate pass on a comparison it never made.
            note = (proc.stderr.strip()[-400:] if proc else "go list could not be executed")
            return ToolResult("go-layering", "error", note=note or "go list failed")
        packages.extend(_decode_json_stream(proc.stdout))
    by_path = {p.get("ImportPath", ""): p for p in packages}
    own = set(by_path)
    edges = {p.get("ImportPath", ""): {i for i in (p.get("Imports") or []) if i in own}
             for p in packages}
    # Layers are read from the path inside the module: a module named
    # `github.com/acme/handler` otherwise puts every package in `handler`.
    # The module-root package itself has no path inside the module, so no layer.
    names = {}
    for path, p in by_path.items():
        mod = (p.get("Module") or {}).get("Path") or ""
        names[path] = "" if mod and path == mod else path.removeprefix(mod + "/")
    findings = []
    for source, target in find_layering_violations(edges, order, names=names):
        signature = f"{source} -> {target}"
        site, line = _go_import_site(by_path.get(source) or {}, target)
        rel = ctx.relpath(site)
        # `go list ./...` is module-wide; the roots scope it afterwards.
        if not under_roots(ctx, rel, "go"):
            continue
        findings.append(finding(rel, "layering_violation",
                                f"layering violation: {signature}",
                                tool="go-layering", line=line,
                                extra={"signature": signature}))
    return ToolResult("go-layering", "ok", findings, metrics={"packages": len(packages)})


def _decode_json_stream(text: str) -> list[dict[str, Any]]:
    """``go list -json`` concatenates objects without separators."""
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        try:
            obj, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        out.append(obj)
    return out


def _go_import_site(package: dict[str, Any], target: str) -> tuple[str, int | None]:
    """The ``(file, line)`` in ``package`` that imports ``target``.

    Reporting the package *directory* was the obvious thing and it was wrong in
    two ways: churn is keyed on files, so every Go violation scored zero commits
    and was systematically under-ranked; and a triage step told to "read the file
    at that line" had neither. Falls back to the directory only when the import
    cannot be located.
    """
    directory = package.get("Dir", "")
    needle = f'"{target}"'
    for name in package.get("GoFiles") or []:
        path = Path(directory) / name
        try:
            for number, text in enumerate(path.read_text(encoding="utf-8", errors="replace")
                                          .splitlines(), start=1):
                if needle in text:
                    return str(path), number
        except OSError:
            continue
    return directory, None


# --------------------------------------------------------------------------
# Rust adapters  (unvalidated against a real Rust repo — see docs)
# --------------------------------------------------------------------------

_CLIPPY_KIND = {
    "dead_code": "dead_code",
    "unused_imports": "dead_code",
    "unused_variables": "dead_code",
    "clippy::cognitive_complexity": "complexity",
    "clippy::too_many_lines": "complexity",
    "clippy::too_many_arguments": "complexity",
    "clippy::type_complexity": "complexity",
}


def run_clippy(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("rust")
    if not roots:
        return ToolResult("clippy", "skipped", note=ctx.no_roots_note("rust"))
    if not _have_binary("cargo"):
        return ToolResult("clippy", "skipped", note="cargo not installed")
    # From each root's own crate, not the repo: a monorepo keeps Cargo.toml in
    # `rust/`, and cargo run from the top finds nothing to build.
    findings: list[dict[str, Any]] = []
    for crate in dict.fromkeys(_nearest(ctx, root, "Cargo.toml") or root / "Cargo.toml"
                               for root in roots):
        result = _clippy_crate(ctx, crate.parent)
        if result.status != "ok":
            return result
        # `--all-targets` lints the lib and its test harness separately, and both
        # emit the same warning: one unused import was counted twice.
        for item in result.findings:
            if not any(f["file"] == item["file"] and f["line"] == item["line"]
                       and f["detail"] == item["detail"] for f in findings):
                findings.append(item)
    return ToolResult("clippy", "ok", findings)


def _clippy_crate(ctx: Ctx, cwd: Path) -> ToolResult:
    proc = _run(["cargo", "clippy", "--message-format=json", "--all-targets", "--",
                 "-W", "clippy::cognitive_complexity", "-W", "clippy::too_many_lines",
                 "-W", "dead_code"], cwd=cwd, timeout=900)
    if proc is None:
        return ToolResult("clippy", "skipped", note="cargo clippy could not be executed")
    if "no such command" in (proc.stderr or "").lower():
        # The component is not installed, which is *skipped*, not a failure —
        # otherwise a repo that simply never added clippy fails its gate.
        return ToolResult("clippy", "skipped", note="rustup component add clippy")
    findings: list[dict[str, Any]] = []
    # Cargo says out loud whether the build it just ran succeeded. Everything
    # below used to hang off `"error" in stderr`, which is an English substring
    # from a tool that is free to reword it — and off `not findings`, which is
    # the half of the bug that mattered: a workspace where one crate lints
    # cleanly and the next fails to compile produced *some* findings, so the
    # failure was never noticed and a partial count was banked as a full one.
    build_ok: bool | None = None
    blockers: list[str] = []
    for line in proc.stdout.splitlines():
        entry = _json_loads(line)
        if not isinstance(entry, dict):
            continue
        if entry.get("reason") == "build-finished":
            build_ok = bool(entry.get("success"))
            continue
        if entry.get("reason") != "compiler-message":
            continue
        message = entry.get("message") or {}
        code = ((message.get("code") or {}).get("code")) or ""
        kind = _CLIPPY_KIND.get(code)
        # An error-level message that is *not* one of the lints we asked for is
        # a compile error — the same discriminator staticcheck uses on its
        # non-U1000 diagnostics. Filtering on the code rather than the level
        # alone keeps a crate that raises one of our own kinds to `deny` from
        # being reported as a build failure.
        if kind is None:
            if str(message.get("level") or "") == "error":
                blockers.append(str(message.get("message") or "").strip())
            continue
        for span in message.get("spans") or []:
            if not span.get("is_primary"):
                continue
            # Relative to the workspace root, which may sit above `cwd`.
            rel = _reroot(ctx, span.get("file_name", ""),
                          [d for d in (cwd, *cwd.parents) if ctx.repo in (d, *d.parents)])
            # cargo analyses the whole workspace; the roots are applied here for
            # the same reason as staticcheck's.
            if not under_roots(ctx, rel, "rust"):
                continue
            findings.append(finding(rel, kind,
                                    f"{code}: {message.get('message', '')}",
                                    tool="clippy", line=span.get("line_start")))
    if blockers or build_ok is False:
        # Some crate did not compile, so nothing here knows what clippy would
        # have said about it. Whatever findings the crates that *did* build
        # produced are a partial answer, and a partial count is the one thing
        # a baseline can never notice — counts only regress upward.
        detail = blockers[0][:200] if blockers else (proc.stderr or "").strip()[-200:]
        return ToolResult("clippy", "error", note=(
            f"the workspace did not build cleanly, so clippy covered less than it "
            f"was asked to: {detail or 'no detail'}"))
    if build_ok is None and proc.returncode != 0:
        # No `build-finished` at all and a failure exit: cargo never got as far
        # as running the check. Reporting that as clean banks a false zero.
        return ToolResult("clippy", "error",
                          note=(proc.stderr or proc.stdout or "").strip()[-400:] or
                               f"cargo clippy exited {proc.returncode} with no output")
    return ToolResult("clippy", "ok", findings)


# --------------------------------------------------------------------------
# Kotlin: detekt (complexity, size, dead code)
# --------------------------------------------------------------------------

# detekt's default ruleset is far wider than maintainability, and running it
# whole is how this adapter would have produced an unread report: on a 30k-line
# DSP codebase the default set returned 4049 findings of which 3585 were
# `MagicNumber`. A queue that is 88% "consider naming this constant" buries the
# one 3300-line class that actually needs splitting.
#
# So only rules that describe *structural decay* are mapped, and everything
# else detekt says is dropped — the same decision the staticcheck adapter makes
# when it keeps U1000 and discards the correctness checks. Style (MaxLineLength,
# WildcardImport), correctness (TooGenericExceptionCaught, SwallowedException)
# and opinion (ReturnCount, whose default threshold of 2 flags ordinary Kotlin
# guard clauses) belong to a linter on the merge path, not to a refactoring
# backlog ranked by churn.
DETEKT_RULES = {
    # One function doing too much. These are the cluster that maps directly onto
    # "extract a function" work.
    "CyclomaticComplexMethod": "complexity",
    "CognitiveComplexMethod": "complexity",
    "LongMethod": "complexity",
    "LongParameterList": "complexity",
    "NestedBlockDepth": "complexity",
    "ComplexCondition": "complexity",
    # One class holding too much. Weighted above complexity because splitting a
    # type is the more structural of the two changes.
    "LargeClass": "maintainability",
    "TooManyFunctions": "maintainability",
    "ComplexInterface": "maintainability",
    "MethodOverloading": "maintainability",
    # Genuinely unreachable. detekt resolves `override` itself, so unlike
    # vulture's Python equivalent this does not flag interface signatures.
    "UnusedPrivateMember": "dead_code",
    "UnusedPrivateProperty": "dead_code",
    "UnusedPrivateClass": "dead_code",
    "UnusedParameter": "dead_code",
}

# Where a project's own detekt tuning conventionally lives. Reusing it matters:
# a team that already raised `LongMethod` to 80 lines has made a decision, and
# an adapter that silently re-applies the default 60 files issues against a
# threshold nobody agreed to.
DETEKT_CONFIG_LOCATIONS = (
    "detekt.yml",
    "detekt-config.yml",
    "config/detekt/detekt.yml",
    "config/detekt.yml",
)


def detekt_exclude_arg(exclude: Sequence[str]) -> str:
    """detekt takes Ant-style globs matched against the whole path."""
    return ",".join(f"**/{name}/**" for name in exclude)


def _detekt_config(ctx: "Ctx") -> Path | None:
    """The project's detekt config, if it has one.

    An explicit ``thresholds.detekt_config`` wins; otherwise the conventional
    locations are probed. A configured path that does not exist is *not*
    silently ignored — see the caller, which refuses rather than scanning with
    defaults the project did not choose.
    """
    declared = ctx.thresholds.get("detekt_config")
    if declared:
        return (ctx.repo / str(declared))
    for name in DETEKT_CONFIG_LOCATIONS:
        candidate = ctx.repo / name
        if candidate.is_file():
            return candidate
    return None


def run_detekt(ctx: Ctx) -> ToolResult:
    roots = ctx.roots("kotlin")
    if not roots:
        return ToolResult("detekt", "skipped", note=ctx.no_roots_note("kotlin"))
    if not _have_binary("detekt"):
        return ToolResult("detekt", "skipped",
                          note="install detekt-cli: https://detekt.dev/docs/gettingstarted/cli")

    config = _detekt_config(ctx)
    if config is not None and not config.is_file():
        # Only reachable for an explicitly declared path: falling back to the
        # default thresholds here would file issues against limits the project
        # never agreed to, and say nothing about why.
        return ToolResult("detekt", "error",
                          note=f"detekt_config {ctx.relpath(config)} does not exist")

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "detekt.xml"
        cmd = ["detekt",
               "--input", ",".join(str(r) for r in roots),
               # Repo-relative paths straight out of the report; without this
               # every `file name` is absolute and has to be re-rooted by guess.
               "--base-path", str(ctx.repo),
               "--excludes", detekt_exclude_arg(ctx.exclude_dirs),
               "--parallel",
               "--report", f"xml:{report}"]
        if config is not None:
            cmd += ["--config", str(config), "--build-upon-default-config"]
        proc = _run(cmd, cwd=ctx.repo, timeout=900)
        if proc is None:
            return ToolResult("detekt", "skipped", note="detekt could not be executed")
        # detekt exits 2 for "I found issues", which is the *expected* outcome
        # here and not a failure. 1 and 3 are a crash and a bad config. Reading
        # `returncode != 0` as an error would make every non-clean repo error;
        # reading it as success would make a broken config bank a zero.
        if proc.returncode not in (0, 2):
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            return ToolResult("detekt", "error",
                              note=detail or f"detekt exited {proc.returncode}")
        if not report.is_file():
            # Ran, said nothing, wrote nothing: cannot be distinguished from a
            # clean repo except by the missing artefact, so say so rather than
            # banking the zero.
            return ToolResult("detekt", "error", note="detekt wrote no XML report")
        try:
            tree = ET.parse(report)
        except ET.ParseError as exc:
            return ToolResult("detekt", "error", note=f"unreadable detekt report: {exc}")

    findings: list[dict[str, Any]] = []
    dropped = 0
    for file_node in tree.getroot():
        rel = ctx.relpath(file_node.get("name") or "")
        if not under_roots(ctx, rel, "kotlin"):
            continue
        for err in file_node:
            rule = (err.get("source") or "").removeprefix("detekt.")
            kind = DETEKT_RULES.get(rule)
            if kind is None:
                dropped += 1
                continue
            line = err.get("line")
            findings.append(finding(
                rel, kind, f"{rule}: {err.get('message') or ''}".strip(),
                tool="detekt", line=int(line) if line and line.isdigit() else None,
            ))
    return ToolResult("detekt", "ok", findings,
                      metrics={"rules": sorted(DETEKT_RULES),
                               "config": ctx.relpath(config) if config else None,
                               "dropped_out_of_scope": dropped})


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

# Ordered so the cheap, always-available analysers run first: an interrupted
# scan still leaves the structural picture behind.
RUNNERS: dict[str, Any] = {
    "import-analysis": run_import_analysis,
    "go-layering": run_go_layering,
    "radon": run_radon,
    "vulture": run_vulture,
    "jscpd": run_jscpd,
    "knip": run_knip,
    "madge": run_madge,
    "gocyclo": run_gocyclo,
    "staticcheck": run_staticcheck,
    "clippy": run_clippy,
    "detekt": run_detekt,
}

# The merge-path slice: builtin, stdlib-only, no third-party install. These are
# the only checks whose failure message names a specific culprit, which is the
# property that makes a gate trustworthy rather than something people bypass.
STRUCTURAL_RUNNERS = ("import-analysis", "go-layering")


def mark_unresolved(ctx: Ctx, results: Sequence[ToolResult]) -> int:
    """Flag findings that did not name a real file **at scan time**.

    Whether a path resolves is knowable here and only guessable later. Deciding
    it at read time instead conflated two different things: a file that was
    deleted since the scan (re-scan — that is the point of the check) and an
    adapter that never produced a usable path in the first place (re-scanning
    reproduces it exactly). Triage refused on both, told the user to re-run the
    scanner, and the second case had no way out — one bad path from any adapter
    took the whole triage step down, other tools' good findings with it.

    Doing it once, here, also makes the guarantee uniform: every adapter is
    covered, not just the one that happened to have a re-rooting helper.
    """
    flagged = 0
    for result in results:
        for item in result.findings:
            rel = item.get("file")
            if not rel or not (ctx.repo / rel).exists():
                item["unresolved"] = True
                flagged += 1
    return flagged


def run_all(ctx: Ctx, only: Sequence[str] | None = None) -> list[ToolResult]:
    results = []
    for name, runner in RUNNERS.items():
        if only is not None and name not in only:
            continue
        if name in ctx.disabled:
            results.append(ToolResult(name, "skipped", note=DISABLED_NOTE))
            continue
        try:
            results.append(runner(ctx))
        except Exception as exc:  # one broken adapter must not lose the rest
            results.append(ToolResult(name, "error", note=f"{type(exc).__name__}: {exc}"))
    mark_unresolved(ctx, results)
    return results


def rank_hotspots(results: Sequence[ToolResult], churn: dict[str, int] | None = None,
                  churn_weight: float = 1.0) -> list[dict[str, Any]]:
    """Merge every finding per file and rank by weighted score x churn factor.

    The churn factor is ``1 + weight * log1p(commits)/log1p(max_commits)``: a
    bounded multiplier in ``[1, 1+weight]`` rather than a raw commit count, so a
    single hyperactive file cannot dominate the whole ranking and a repo with no
    git history ranks exactly as it would without churn.
    """
    churn = churn or {}
    max_commits = max(churn.values()) if churn else 0
    per_file: dict[str, dict[str, Any]] = {}
    for result in results:
        for item in result.findings:
            entry = per_file.setdefault(item["file"], {
                "file": item["file"], "base_score": 0.0, "kinds": {},
                "tools": set(), "findings": 0,
            })
            entry["base_score"] += float(item.get("weight", 1.0))
            entry["kinds"][item["kind"]] = entry["kinds"].get(item["kind"], 0) + 1
            entry["tools"].add(item["tool"])
            entry["findings"] += 1
    out = []
    for entry in per_file.values():
        commits = churn.get(entry["file"], 0)
        factor = 1.0
        if max_commits > 0 and commits > 0:
            factor += churn_weight * (math.log1p(commits) / math.log1p(max_commits))
        out.append({
            "file": entry["file"],
            "score": round(entry["base_score"] * factor, 2),
            "base_score": round(entry["base_score"], 2),
            "churn_commits": commits,
            "churn_factor": round(factor, 3),
            "findings": entry["findings"],
            "kinds": dict(sorted(entry["kinds"].items())),
            "tools": sorted(entry["tools"]),
        })
    # Deterministic: score desc, then filename. No wall-clock, no set ordering.
    return sorted(out, key=lambda h: (-h["score"], h["file"]))


def summarize(results: Sequence[ToolResult]) -> dict[str, Any]:
    """Reduce a run to the few numbers a baseline can be compared on.

    Only tools that actually **ran**, and ran *whole*, contribute a count. A
    skipped tool banking a phantom zero would make installing it later read as a
    ``0 -> N`` regression; an ``incomplete`` tool banking a count is the same
    defect with the coverage loss hidden one level down, inside a number that
    still looks like a full answer. Dropping the tool here routes it into
    ``diff_baseline``'s lost-coverage rule, which is the only thing that says
    out loud that the gate is now comparing less than the baseline did.

    An ``incomplete`` result's *findings* are still reported and still queued —
    they are true. It is only the count, and the structural signatures derived
    from it, that cannot be compared.
    """
    counts: dict[str, int] = {}
    structural: list[str] = []
    files_below_floor: int | None = None
    for result in results:
        if not result.ran or result.incomplete:
            continue
        counts[result.tool] = len(result.findings)
        if "files_below_mi_floor" in result.metrics:
            files_below_floor = int(result.metrics["files_below_mi_floor"])
        for item in result.findings:
            if item["kind"] in STRUCTURAL_KINDS and item.get("signature"):
                structural.append(f"{item['kind']}:{item['signature']}")
    return {
        "schema": SCHEMA_VERSION,
        "tool_finding_counts": counts,
        "structural_signatures": sorted(set(structural)),
        "files_below_mi_floor": files_below_floor,
    }


def _cycle_members(signature: str) -> set[str]:
    return set(signature.split(":", 1)[1].split(" -> "))


def diff_baseline(current: dict[str, Any], baseline: dict[str, Any],
                  gate: str = "all") -> list[str]:
    """Regressions of ``current`` against ``baseline``. Empty list == pass.

    Four asymmetries, each of which was a bug before it was a rule:

    1. A tool **absent from the baseline** is skipped entirely. Newly available
       tooling is new coverage, not a regression.
    2. But a tool **present in the baseline and absent now** *is* reported. The
       mirror of rule 1 was missing, so a CI runner that lost its venv passed a
       green ``--gate all`` having measured less than the baseline did — and
       uninstalling radon was the cheapest way to silence a complexity finding.
       Counts can only ever regress upward, so nothing else would have caught it.
    3. Structural signatures are compared canonically on **both** sides, so a
       rotated or hand-edited cycle is not a new one.
    4. The maintainability signal is a **file count**, not the worst file's
       score. A clamped worst-score can never regress once it is floored. An
       absent count is *not* a zero (rule 1), but losing a count the baseline
       had is a coverage loss (rule 2).

    Rule 2 is suspended for a **partial** run, which knows nothing about the
    tools it did not run and would otherwise report all of them as lost.
    """
    problems: list[str] = []
    baseline_sigs = set(baseline.get("structural_signatures", []))
    new_structural = set(current.get("structural_signatures", [])) - baseline_sigs
    # A cycle's signature is its whole member list, so breaking one edge of a
    # three-module cycle minted a "new" two-module one and failed the gate on an
    # improvement. A cycle inside a baseline cycle's members is not new.
    base_cycles = [_cycle_members(s) for s in baseline_sigs if s.startswith("circular_import:")]
    new_structural = {s for s in new_structural
                      if not (s.startswith("circular_import:")
                              and any(_cycle_members(s) <= b for b in base_cycles))}
    for signature in sorted(new_structural):
        problems.append(f"new structural issue: {signature}")
    if gate == "structural":
        return problems

    base_counts = baseline.get("tool_finding_counts", {}) or {}
    current_counts = current.get("tool_finding_counts", {}) or {}
    for tool, count in sorted(current_counts.items()):
        if tool not in base_counts:
            continue
        if count > base_counts[tool]:
            problems.append(f"{tool}: findings {base_counts[tool]} -> {count}")

    partial = bool((current.get("run") or {}).get("partial"))
    if not partial:
        for tool in sorted(set(base_counts) - set(current_counts)):
            problems.append(
                f"{tool}: ran for the baseline ({base_counts[tool]} findings) and "
                f"reported no comparable count now — coverage lost, not a clean "
                f"result")

    current_floor = current.get("files_below_mi_floor")
    base_floor = baseline.get("files_below_mi_floor")
    if current_floor is not None and base_floor is not None and current_floor > base_floor:
        problems.append(f"files below MI floor: {base_floor} -> {current_floor}")
    elif current_floor is None and base_floor is not None and not partial:
        problems.append(f"maintainability index not measured, but the baseline has "
                        f"{base_floor} file(s) below the floor — coverage lost")
    return problems


# Where a partial run's reports go. The canonical directory must always describe
# **one whole scan**, because that is the invariant every consumer relies on and
# none of them can check from a single file.
PARTIAL_SUBDIR = "partial"

# Everything in the report directory that is not one analyser's output.
NON_TOOL_REPORTS = ("hotspots.json", "summary.json", "baseline.json")


def prune_stale_reports(report_dir: Path, written: Iterable[str]) -> list[str]:
    """Delete tool reports this run did not produce. Returns what was removed.

    Nothing else ever removed them, and ``load_findings`` refuses a report set
    whose files carry two different ``run_id``s — so a single report left behind
    by a renamed or removed analyser wedged the triage step **permanently**, with
    an error telling the reader to re-run the scanner, which is precisely the one
    action that could not clear it. Re-syncing a vendored engine (``+update``) is
    the normal way a runner disappears, so this is not a hypothetical.

    Only files that actually look like an analyser's output are removed. The
    report directory belongs to this script, but deleting something unrecognised
    inside it would be a worse failure than the one being prevented.
    """
    keep = set(written)
    removed: list[str] = []
    for path in sorted(report_dir.glob("*.json")):
        if path.name in keep or path.name in NON_TOOL_REPORTS:
            continue
        try:
            payload = _json_loads(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if isinstance(payload, dict) and "tool" in payload and "findings" in payload:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def new_run_id() -> str:
    """Identity of one scan. Random, not time-derived: two scans inside the same
    second must not collide into looking like one run."""
    return uuid.uuid4().hex[:12]


def write_reports(results: Sequence[ToolResult], hotspots: Sequence[dict[str, Any]],
                  report_dir: Path, *, partial: bool = False,
                  run_id: str | None = None) -> dict[str, Any]:
    """Persist a run. Returns the summary whether or not it was written.

    A **partial** run (``--only`` / ``--gate structural``) writes its per-tool
    reports into ``partial/`` and touches nothing else. Letting it write into the
    canonical directory produced a genuinely dangerous state: the tools it did
    not run kept their reports from an earlier scan, while ``summary.json`` and
    ``hotspots.json`` were rewritten to describe only the tools it did. The
    triage step then clustered fresh structural findings together with stale
    complexity findings and filed refactoring issues against code that had
    already been fixed — with no signal anywhere that anything was out of date.
    """
    now = datetime.now(timezone.utc)
    run = {"run_id": run_id or new_run_id(),
           "generated_at": now.isoformat(timespec="seconds"),
           # The ISO string is truncated to seconds for readability, which loses
           # up to a second — enough for a scan to look older than a file it
           # already read. Consumers compare against this epoch value instead.
           "generated_at_ts": now.timestamp(),
           "partial": partial,
           "tools": sorted(r.tool for r in results)}
    target = report_dir / PARTIAL_SUBDIR if partial else report_dir
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for result in results:
        name = f"{result.tool}.json"
        written.append(name)
        (target / name).write_text(
            json.dumps(result.to_dict(run), indent=2) + "\n", encoding="utf-8")
    if not partial:
        # A full run is the whole truth about the canonical directory, so it is
        # also the only run entitled to say what does not belong there. A partial
        # one writes into `partial/` and touches nothing.
        prune_stale_reports(target, written)
    summary = summarize(results)
    summary["run"] = run
    if partial:
        return summary
    (report_dir / "hotspots.json").write_text(
        json.dumps(list(hotspots), indent=2) + "\n", encoding="utf-8")
    (report_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _cell(text: str, limit: int = 160) -> str:
    """A tool note is arbitrary stderr; an unescaped ``|`` or newline in it
    silently reshapes the table around it."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_markdown(results: Sequence[ToolResult], hotspots: Sequence[dict[str, Any]],
                    limit: int = 15) -> str:
    lines = ["# Code quality", "", "| Tool | Status | Findings | Note |", "|---|---|---:|---|"]
    for result in results:
        lines.append(f"| {result.tool} | {result.status} | "
                     f"{len(result.findings) if result.ran else '—'} | {_cell(result.note)} |")
    lines += ["", f"## Top {min(limit, len(hotspots))} hotspots", "",
              "| Score | Churn | File | Findings |", "|---:|---:|---|---|"]
    for hotspot in hotspots[:limit]:
        kinds = ", ".join(f"{k}×{v}" for k, v in hotspot["kinds"].items())
        lines.append(f"| {hotspot['score']} | {hotspot['churn_commits']} | "
                     f"`{hotspot['file']}` | {kinds} |")
    if not hotspots:
        lines.append("| — | — | _no findings_ | — |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--gate", choices=["none", "structural", "all"], default="none",
                        help="compare against baseline.json and exit non-zero on regression")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write the current summary as the new baseline")
    parser.add_argument("--only", help="comma-separated subset of tools to run")
    parser.add_argument("--report-dir", help="override report_dir (e.g. to scan a repo "
                                             "without writing into it)")
    parser.add_argument("--json", dest="json_out", help="also write the full findings here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    ctx = Ctx(repo, load_config(repo))
    report_dir = Path(args.report_dir).resolve() if args.report_dir else repo / ctx.config["report_dir"]

    only: Sequence[str] | None
    if args.only:
        only = [name.strip() for name in args.only.split(",") if name.strip()]
        unknown = [name for name in only if name not in RUNNERS]
        # A typo used to select *no* runner at all: the scan analysed nothing,
        # wrote nothing and exited 0 — including under `--gate all`, which is a
        # permanently green gate one character away from a working one.
        if unknown:
            parser.error(f"--only: unknown tool(s) {', '.join(sorted(unknown))} — "
                         f"choose from {', '.join(RUNNERS)}")
        if not only:
            parser.error("--only: no tools named")
    elif args.gate == "structural":
        only = STRUCTURAL_RUNNERS
    else:
        only = None
    partial = only is not None

    if partial and args.update_baseline:
        # The guard is on *coverage*, not on which flag produced it. A partial
        # run knows nothing about the tools it skipped, so writing its summary as
        # the baseline deletes their counts — and a tool absent from the baseline
        # is skipped in the diff entirely, which leaves `--gate all` permanently
        # toothless for exactly the tools that were quietly dropped.
        parser.error("--update-baseline needs a full scan; it is refused under "
                     "--only and --gate structural")

    if args.update_baseline and args.gate != "none":
        # Banking and gating are opposite instructions: one accepts the current
        # state, the other refuses it. `--update-baseline` returned 0 before the
        # comparison ever ran, so `--gate all --update-baseline` on a merge path
        # was a gate that could not fail and rewrote the thing it was meant to
        # compare against. Refuse rather than pick a winner silently.
        parser.error("--update-baseline and --gate are contradictory: one accepts "
                     "the current state, the other refuses it. Run them separately.")

    results = run_all(ctx, only)
    churn = git_churn(ctx) if args.gate != "structural" else {}
    hotspots = rank_hotspots(results, churn, float(ctx.config.get("churn", {}).get("weight", 1.0)))
    summary = write_reports(results, hotspots, report_dir, partial=partial)
    if not partial:
        (report_dir / "summary.md").write_text(render_markdown(results, hotspots), encoding="utf-8")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(
            {"summary": summary, "hotspots": hotspots,
             # Same provenance the on-disk reports carry: this file is the one
             # output a consumer might read on its own, so it must be able to
             # say which scan it came from.
             "results": [r.to_dict(summary["run"]) for r in results]},
            indent=2) + "\n", encoding="utf-8")

    # `skipped` is honest — the tool is not installed and banks no count.
    # `error` means it *is* installed and did not work, so nothing here knows
    # what it would have said.
    errored = [r for r in results if r.status == "error"]
    incomplete = [r for r in results if r.incomplete]
    missing_roots = ctx.all_missing_roots()
    if not args.quiet:
        print(render_markdown(results, hotspots))
        if partial:
            print(f"partial run ({', '.join(summary['run']['tools'])}) — reports written to "
                  f"{ctx.relpath(report_dir / PARTIAL_SUBDIR)}; the canonical report set "
                  f"was left untouched.")
    # Unconditional: `--quiet` suppresses the report, not the news that an
    # analyser broke. Under a gate this is the reason for the exit code.
    for result in errored:
        print(f"error: {result.tool}: {result.note}", file=sys.stderr)
    # Same rule: the count is silently withheld from the summary, so without
    # this line a `--quiet` run gives no hint that a root went unscanned.
    for result in incomplete:
        print(f"warning: {result.tool}: {result.note or 'partial coverage'} — its count "
              f"is withheld from the summary rather than banked as a full one",
              file=sys.stderr)
    # A root that does not exist is indistinguishable from a language nobody
    # declared, from inside an adapter: both arrive as an empty list. The whole
    # language then skips, the report is clean, and a typo in `.quality.json`
    # reads as a codebase with nothing wrong in it.
    for lang, missing in sorted(missing_roots.items()):
        print(f"warning: {lang}: declared root(s) {', '.join(missing)} do not exist "
              f"under {ctx.repo} — nothing under them was scanned; fix "
              f"{CONFIG_NAME} or drop them", file=sys.stderr)

    structural_results = [r for r in results if r.tool in STRUCTURAL_RUNNERS]
    if (args.gate == "structural"
            and all(r.status == "skipped" for r in structural_results)):
        # Every one of them *skipped*, not crashed — a crash has its own, more
        # specific message below and must not be reported as a config problem.
        # Both structural analysers are language-specific, so on a TypeScript- or
        # Rust-only repo this gate compared nothing and exited 0 — for as long as
        # anyone left it on a merge path. Exactly the "green for months without
        # ever having compared anything" state the missing-baseline check exists
        # to prevent, one level further out.
        if structural_results:
            detail = "; ".join(f"{r.tool}: {r.note or r.status}" for r in structural_results)
            remedy = (f"Declare python or go roots in {CONFIG_NAME}, or drop the "
                      f"structural gate for this repo.")
        else:
            # `--only` deselected them outright. The empty join printed
            # "no structural analyser ran ()" and then advised declaring roots,
            # which is not the problem and would not fix it.
            detail = (f"--only selected {', '.join(only or ())}; no structural "
                      f"analyser among them")
            remedy = (f"Drop --only, or name one of {', '.join(STRUCTURAL_RUNNERS)}.")
        print(f"gate: no structural analyser ran ({detail}) — this gate can only "
              f"pass vacuously. {remedy}", file=sys.stderr)
        return 1

    baseline_path = report_dir / "baseline.json"
    if args.update_baseline:
        if errored or incomplete or missing_roots:
            # A baseline banked from a run with a crashed adapter has no entry
            # for it, and `diff_baseline` skips tools absent from the baseline —
            # so the gate goes permanently blind to exactly the tool that was
            # broken on the day the baseline was taken. An `incomplete` adapter
            # opens the same hole: its count is withheld from the summary, so
            # banking one writes a baseline that never mentions it again.
            for result in errored:
                print(f"refusing to bank a baseline: {result.tool} failed and the "
                      f"summary cannot describe it", file=sys.stderr)
            for result in incomplete:
                print(f"refusing to bank a baseline: {result.tool} did not cover every "
                      f"declared root ({result.note or 'partial coverage'}) — fix that "
                      f"root, or disable the tool in {CONFIG_NAME}", file=sys.stderr)
            # Same hole, one level further out: every adapter for that language
            # skipped, so the baseline has no entry for any of them — and a tool
            # absent from the baseline is skipped in the diff entirely. Banking
            # here makes the gate permanently blind to a whole language because
            # of a typo.
            for lang, missing in sorted(missing_roots.items()):
                print(f"refusing to bank a baseline: {lang} root(s) "
                      f"{', '.join(missing)} do not exist, so nothing under them "
                      f"was measured — fix {CONFIG_NAME} or drop them",
                      file=sys.stderr)
            return 1
        baseline_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"baseline written: {ctx.relpath(baseline_path)}")
        return 0

    if args.gate == "none":
        return 0
    # A crashed analyser fails the gate. `--gate structural` runs exactly two of
    # them; if one raises, a gate that returns 0 is reporting "no new cycles"
    # about a graph it never built, and nobody reading an exit code can tell.
    problems = [f"{r.tool}: analyser failed — "
                f"{(r.note.splitlines() or ['no detail'])[0][:160]}" for r in errored]
    if not baseline_path.is_file():
        # No baseline yet is not a pass. Silently succeeding here is how a gate
        # ends up green for months without ever having compared anything.
        print(f"gate: no baseline at {ctx.relpath(baseline_path)} — "
              f"run with --update-baseline once to establish one", file=sys.stderr)
        return 1
    baseline = _json_loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict):
        print(f"gate: {ctx.relpath(baseline_path)} is not readable JSON", file=sys.stderr)
        return 1
    if args.gate == "all" and partial:
        # `--gate all` under `--only` compares the tools it ran and stays silent
        # about the ones it did not: the lost-coverage rule is suspended for a
        # partial run (correctly — it knows nothing about them), and under
        # `--quiet` even the "partial run" line is suppressed. So
        # `--gate all --only import-analysis --quiet` exited 0 having printed
        # nothing, having compared one tool of ten. Same vacuous-gate shape the
        # structural gate's own guard exists to prevent, so it gets the same
        # treatment: say what was actually compared, and refuse when that is
        # nothing at all.
        base_counts = baseline.get("tool_finding_counts", {}) or {}
        compared = sorted(set(summary.get("tool_finding_counts", {})) & set(base_counts))
        if not compared:
            print(f"gate: --gate all ran only {', '.join(summary['run']['tools'])}, and the "
                  f"baseline has no count for any of them — this gate compared nothing and "
                  f"can only pass vacuously. Drop --only, or use --gate structural.",
                  file=sys.stderr)
            return 1
        print(f"warning: --gate all is comparing {len(compared)} of {len(base_counts)} "
              f"baselined tool(s) ({', '.join(compared)}) — the rest were deselected by "
              f"--only and are not being gated by this run", file=sys.stderr)
    problems.extend(diff_baseline(summary, baseline, gate=args.gate))
    for problem in problems:
        print(f"gate: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
