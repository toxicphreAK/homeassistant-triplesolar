#!/usr/bin/env python3
"""Turn a scanner firehose into a short, ranked, root-cause queue.

``quality_scan.py`` writes hundreds to thousands of findings. Nobody reads that.
This script is their consumer, and it exists because of one observation:

    **Raw findings are not units of work. Root causes are.**

One helper copy-pasted into six files is *one* refactor; a clone detector reports
it as fifteen pairwise clones. A function doing three jobs is one refactor; a
complexity tool reports it once for cyclomatic complexity and again for a low
maintainability index. So findings are clustered by shared cause, ranked, cut off
at a deliberately small top-N, and handed to an LLM to *analyse* — never filed as
a tool dump.

Division of labour
------------------
**Deterministic engine (this script):** cluster -> fingerprint -> dedup -> rank ->
cut off. Every step reproducible, no model in the loop.

**LLM author (the ``/quality-triage`` command):** read the actual code behind each
queued cluster, pin the root cause to ``file:line``, consolidate clusters that
share one cause into a single issue, discard false positives, and file scoped
refactoring issues carrying the ``(qfp:<hash>)`` marker of every cluster covered.

Those markers are the whole dedup contract: on the next run a cluster whose
marker appears in an open issue is already tracked and drops out of the queue.

A cluster the author read and rejected (generated code, a plugin interface's
unused parameters) has no issue to carry its marker, so it used to come back at
the top of every queue and be re-read and re-rejected each run. ``--dismiss``
records it in the committed ``.quality-dismissed.json`` with a reason and the
cluster's pre-churn score. A dismissal is a verdict on the code *as it was*: it
lapses by itself when the file set changes (a new fingerprint) or the score
rises above the recorded one (the code got worse), and the cluster comes back
carrying the old reason so the author can judge whether it still holds.

Two failure modes this script refuses to have
---------------------------------------------
**An unread corpus is not an empty one.** If the issue list cannot be fetched, or
comes back truncated, the script exits non-zero *before writing anything*.
Deduping against a corpus that failed to load marks every cluster "not tracked"
and re-files work that already exists — while exiting 0.

**Dedup runs before the cut-off, not after.** Otherwise eight already-tracked
clusters sitting above a fresh one starve it forever.

**A cluster nothing can be read for is not queued.** The scanner marks findings
whose path did not resolve at scan time; a cluster made only of those cannot be
investigated by a command whose method is "open the files at the lines". Held
back and counted, never dropped silently — the count is the symptom of an adapter
emitting unusable paths.

Zero dependencies: stdlib only.
"""
# Vendored from llmdev tools/quality @ 02c1cd5. Re-sync with /quality-setup +update.
# Local edits will be overwritten; upstream fixes belong in llmdev.

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_scan import (  # noqa: E402  (path is set up above)
    DEFAULT_CONFIG, DISABLED_NOTE, KIND_WEIGHTS, NO_LAYERS_NOTE, NO_ROOTS_NOTE, Ctx, git_churn, load_config, prune_dirs,
    source_scan_targets,
)

# Skipped because the repo has no code in that language, declares no Go
# layering, or switched the tool off — not because a tool is missing. Excluded
# from ``analysers_not_ok``, or every single-language repo would list the other
# languages' adapters on every run and the list would stop meaning anything.
NOT_APPLICABLE_NOTES = frozenset({NO_LAYERS_NOTE, DISABLED_NOTE}
                                 | {NO_ROOTS_NOTE.format(lang=lang)
                                    for lang in DEFAULT_CONFIG["roots"]})

# Filesystem timestamps and the scanner's clock do not agree to better than about
# a second (coarse mtime granularity, NFS skew). Anything inside that window is
# noise, and a check that cries stale on noise gets worked around rather than
# heeded.
CLOCK_TOLERANCE_SECONDS = 2.0

# Cut-off. Deliberately small: the point of a weighted ranking is to surface the
# few clusters that matter, not to reopen the firehose one level up.
DEFAULT_TOP_N = 8
DEFAULT_MIN_SCORE = 6.0

# A cluster's score sums its findings' weights, so sheer *volume* can beat
# severity: one barrel file with 64 unused exports (64 x 1.5 = 96) outranks every
# duplication and complexity cluster in a repo. Capping how many clusters of one
# kind may hold a queue slot restores the intended severity spread without giving
# up the transparent, explainable score. Structural is exempt — see select().
MAX_PER_KIND = 3

# Structural clusters bypass the score floor and the per-kind cap, which on a
# repo with a genuinely tangled import graph let them take every slot: eight
# cycles, and no duplication or complexity cluster is ever seen again. Two slots
# are held back whenever there is anything else to put in them.
RESERVED_FOR_OTHERS = 2

KIND_STRUCTURAL = "structural"
KIND_DUPLICATION = "duplication"
KIND_COMPLEXITY = "complexity"
KIND_DEAD_CODE = "dead_code"

CLUSTER_KINDS = (KIND_STRUCTURAL, KIND_DUPLICATION, KIND_COMPLEXITY, KIND_DEAD_CODE)

FINDING_TO_CLUSTER_KIND = {
    "circular_import": KIND_STRUCTURAL,
    "layering_violation": KIND_STRUCTURAL,
    "duplicate": KIND_DUPLICATION,
    "complexity": KIND_COMPLEXITY,
    "maintainability": KIND_COMPLEXITY,
    "dead_code": KIND_DEAD_CODE,
}

MARKER_RE = re.compile(r"\(qfp:([0-9a-f]{10,})\)")

# The rolling queue issue. Its own marker is *excluded* from the dedup corpus:
# the queue body lists every queued cluster's marker, so counting it as coverage
# would empty the queue on the very next run.
ROLLING_MARKER = "(qfp:queue)"
ROLLING_TITLE = f"[QUALITY] Refactoring queue {ROLLING_MARKER}"

# Committed, unlike the hand-off and the issue dump beside it: a dismissal is a
# reviewed judgement, and a reviewer has to be able to see it in a diff.
DISMISSED_FILE = ".quality-dismissed.json"

# One issue body, capped. `tea` passes it as a single argv entry and Linux caps
# those at 128 KiB (MAX_ARG_STRLEN), so an unbounded queue body fails the write
# rather than the render.
MAX_ISSUE_BODY = 60000

# `gh issue list --limit N` truncates *silently*, and the overflow then reads as
# "not tracked" — the same defect as a failed read, with no error to catch. Set
# well above any plausible open-issue count and treated as a hard error when hit.
LIST_LIMIT = 2000


class IssueListError(RuntimeError):
    """The open-issue corpus could not be read, or was read incompletely."""


def is_rolling_issue(issue: dict[str, Any]) -> bool:
    """Title-only, because this script writes that title. See ``tracked_markers``."""
    return ROLLING_MARKER in (issue.get("title") or "")


@dataclass
class Cluster:
    kind: str
    files: list[str]
    findings: list[dict[str, Any]] = field(default_factory=list)
    signature: str = ""
    score: float = 0.0
    # The score before churn scaling. Dismissals compare against this, because
    # the churn multiplier grows with every commit to a file and would lapse a
    # dismissal on code that did not change at all.
    base_score: float = 0.0
    churn: int = 0
    # The dismissal this cluster outgrew, if any — see ``apply_dismissals``.
    previous_dismissal: dict[str, Any] | None = None

    @property
    def fingerprint(self) -> str:
        """Identity hash: kind + files, or kind + signature for structural.
        **Never** line numbers or symbol names — an edit above a complex
        function must not mint a new cluster and re-file an issue that already
        exists. A structural signature is already canonical; its files are not
        (go-layering names whichever file in the package sorts first and
        imports the target), so adding a file must not re-file the breach."""
        if self.kind == KIND_STRUCTURAL:
            payload = "|".join([self.kind, self.signature])
        else:
            payload = "|".join([self.kind, self.signature, *sorted(self.files)])
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()

    @property
    def marker(self) -> str:
        return f"(qfp:{self.fingerprint})"

    @property
    def unresolved_files(self) -> list[str]:
        """Files in this cluster that did not name a real path when the scan ran.

        ``quality_scan.mark_unresolved`` records this so the triage step does not
        wedge on one bad adapter path — but the flag then died here, and the
        hand-off presented such a finding as indistinguishable from a good one.
        The installed command tells its reader to open every file in ``files``,
        so an unmarked bad path sends it to read something that is not there.
        """
        seen: set[str] = set()
        resolved: set[str] = set()
        for item in self.findings:
            name = item.get("file")
            if not name:
                continue
            seen.add(name)
            if not item.get("unresolved"):
                resolved.add(name)
        # A file with *any* resolved finding is readable. The scanner flags per
        # file existence so the two agree today, but a future adapter reporting
        # a path it could not resolve must not condemn a file that is right there.
        return sorted(seen - resolved)

    @property
    def is_unreadable(self) -> bool:
        """Nothing in this cluster can be opened, so there is nothing to analyse."""
        return bool(self.files) and set(self.unresolved_files) == set(self.files)

    def title(self) -> str:
        head = self.files[0] if self.files else "(unknown)"
        more = f" +{len(self.files) - 1} more" if len(self.files) > 1 else ""
        label = {
            KIND_STRUCTURAL: "Structural",
            KIND_DUPLICATION: "Duplication",
            KIND_COMPLEXITY: "Complexity",
            KIND_DEAD_CODE: "Dead code",
        }[self.kind]
        return f"{label}: {head}{more}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "marker": self.marker,
            "kind": self.kind,
            "title": self.title(),
            "score": round(self.score, 2),
            "base_score": round(self.base_score, 2),
            "churn_commits": self.churn,
            "previously_dismissed": self.previous_dismissal,
            "signature": self.signature,
            "files": sorted(self.files),
            # Empty for the overwhelming majority of clusters; present always, so
            # a consumer can rely on the key rather than on its absence.
            "unresolved_files": self.unresolved_files,
            "findings": [
                {"file": f["file"], "line": f.get("line"), "kind": f["kind"],
                 "tool": f["tool"], "detail": f["detail"],
                 "unresolved": bool(f.get("unresolved"))}
                for f in sorted(self.findings, key=lambda f: (f["file"], f.get("line") or 0))
            ],
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


class StaleReportError(RuntimeError):
    """The reports do not describe the tree as it is now."""


def newest_source_mtime(ctx: Ctx) -> float:
    """Most recent mtime under any configured source root, or ``0.0``.

    Internal consistency is not enough. A report set can come from one scan and
    still describe a tree that has since been edited — which is the *normal*
    state, since fixing something is what people do between scans. Clustering
    that produces a confident, fully evidenced refactoring issue for a function
    that was deleted an hour ago, and nothing in the issue looks wrong.
    """
    newest = 0.0
    # `source_scan_targets` is the scanner's own answer to "what gets read",
    # including its whole-checkout fallback for a repo with no declared roots.
    # Deriving this independently left findings that nothing was watching.
    for root, suffixes in source_scan_targets(ctx):
        if root.is_file():
            if root.suffix in suffixes:
                newest = max(newest, root.stat().st_mtime)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            prune_dirs(dirnames, ctx.exclude_dirs)
            for name in filenames:
                # Only files an analyser actually reads: a README or a fixture
                # under a source root changes no finding.
                if not name.endswith(suffixes):
                    continue
                try:
                    newest = max(newest, (Path(dirpath) / name).stat().st_mtime)
                except OSError:
                    continue
    return newest


def assert_reports_are_current(ctx: Ctx, run: dict[str, Any]) -> None:
    """Refuse a scan the tree has moved on from.

    Refusing rather than warning, for the same reason the mixed-set check
    refuses: an issue filed from a stale scan is indistinguishable from a correct
    one until somebody opens the file. Re-scanning costs seconds.
    """
    stamp = run.get("generated_at")
    scanned_at = run.get("generated_at_ts")
    if scanned_at is None:
        if not stamp:
            return
        try:
            scanned_at = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            return
    newest = newest_source_mtime(ctx)
    if newest > float(scanned_at) + CLOCK_TOLERANCE_SECONDS:
        changed = datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds")
        raise StaleReportError(
            f"the scan is from {stamp} but a source file changed at {changed} — "
            f"re-run quality_scan.py so the findings describe the current tree")


def assert_findings_resolve(ctx: Ctx, findings: Sequence[dict[str, Any]]) -> None:
    """Refuse a report set whose findings name files that are gone.

    The freshness check is an mtime *maximum*, and a maximum cannot see a
    deletion: removing a whole file bumps no surviving file's timestamp, so the
    scan still looks current while describing code that no longer exists. That is
    not a corner case — deleting the dead module is the most common way a
    dead-code finding gets fixed.

    Findings the **scanner** marked ``unresolved`` are exempt: that flag means
    the path did not name a real file when the scan ran, so re-scanning
    reproduces it and raising here would wedge the whole triage step on a
    scanner bug — taking every other tool's good findings down with it. See
    ``quality_scan.mark_unresolved``.
    """
    missing = sorted({f["file"] for f in findings
                      if f.get("file") and not f.get("unresolved")
                      and not (ctx.repo / f["file"]).exists()})
    if missing:
        shown = ", ".join(missing[:5]) + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
        raise StaleReportError(
            f"the reports name {len(missing)} file(s) that no longer exist ({shown}) — "
            f"re-run quality_scan.py so the findings describe the current tree")


def load_findings(report_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(findings, run)`` from every tool report, in a stable order.

    **Every report must carry the same ``run_id``.** The reports are separate
    files that a partial scan can refresh individually, and nothing in a single
    finding says how old it is. Clustering a fresh structural report together
    with a three-week-old complexity report produces confident, well-evidenced
    refactoring issues for code that was fixed a fortnight ago — and there is no
    symptom, because every path in them still resolves.

    ``partial/`` is not read: ``glob`` does not recurse, and a partial run writes
    only there precisely so this set stays whole.
    """
    if not report_dir.is_dir():
        raise SystemExit(f"no reports at {report_dir} — run quality_scan.py first")
    out: list[dict[str, Any]] = []
    runs: dict[str, list[str]] = {}
    run_meta: dict[str, Any] = {}
    unstamped: list[str] = []
    not_ok: list[str] = []
    for path in sorted(report_dir.glob("*.json")):
        if path.name in ("hotspots.json", "summary.json", "baseline.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        run = payload.get("run") or {}
        run_id = run.get("run_id")
        if run_id:
            runs.setdefault(run_id, []).append(path.name)
            run_meta = run_meta or dict(run)
        else:
            unstamped.append(path.name)
        if payload.get("status") == "ok":
            out.extend(payload.get("findings") or [])
        elif payload.get("note") not in NOT_APPLICABLE_NOTES:
            not_ok.append(f"{payload.get('tool') or path.stem} ({payload.get('status')})")
    if unstamped:
        raise StaleReportError(
            f"{', '.join(unstamped)} carry no run stamp (written by an older "
            f"scanner) — re-run quality_scan.py to refresh the whole set")
    if len(runs) > 1:
        detail = "; ".join(f"{rid[:8]}: {', '.join(files)}" for rid, files in sorted(runs.items()))
        raise StaleReportError(
            f"reports come from {len(runs)} different scans ({detail}) — "
            f"re-run quality_scan.py so the whole set describes one state")
    if not runs:
        raise SystemExit(f"no tool reports in {report_dir} — run quality_scan.py first")
    # Carried to the stale-dismissal note: an analyser that did not run makes
    # every dismissal of its kind look stale, and nothing else at triage time
    # says it did not run.
    run_meta["analysers_not_ok"] = sorted(not_ok)
    return out, run_meta


# --------------------------------------------------------------------------
# Clustering: findings -> root causes
# --------------------------------------------------------------------------


class _Union:
    """Union-find over clone fragments."""

    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, item: Any) -> Any:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def cluster_findings(findings: Sequence[dict[str, Any]]) -> list[Cluster]:
    """Group findings by shared root cause — one rule per signal class.

    ================  ==========================================  =====================================
    Cluster kind      Grouped by                                  Why
    ================  ==========================================  =====================================
    ``structural``    canonical cycle / breach signature          rotations of one cycle are one problem
    ``duplication``   connected component of the clone graph      N pairwise clones == one copy-pasted helper
    ``complexity``    file (cyclomatic + maintainability merged)  a complex file with a low MI is one job
    ``dead_code``     file                                        one cleanup per file
    ================  ==========================================  =====================================
    """
    structural: dict[str, Cluster] = {}
    per_file: dict[tuple[str, str], Cluster] = {}
    clone_findings: list[dict[str, Any]] = []

    for item in findings:
        kind = FINDING_TO_CLUSTER_KIND.get(item.get("kind", ""))
        if kind is None:
            continue
        if kind == KIND_STRUCTURAL:
            signature = item.get("signature") or item.get("detail", "")
            cluster = structural.setdefault(
                signature, Cluster(kind=KIND_STRUCTURAL, files=[], signature=signature))
            if item["file"] not in cluster.files:
                cluster.files.append(item["file"])
            cluster.findings.append(item)
        elif kind == KIND_DUPLICATION:
            clone_findings.append(item)
        else:
            key = (kind, item["file"])
            cluster = per_file.setdefault(key, Cluster(kind=kind, files=[item["file"]]))
            cluster.findings.append(item)

    clusters = list(structural.values()) + list(per_file.values())
    clusters.extend(_cluster_clones(clone_findings))
    for cluster in clusters:
        cluster.files.sort()
        # The finding's own weight, so a rank-F function outweighs a rank-D one.
        cluster.score = sum(f.get("weight") or KIND_WEIGHTS.get(f["kind"], 1.0)
                            for f in cluster.findings)
        cluster.base_score = cluster.score
    return clusters


def _cluster_clones(findings: Sequence[dict[str, Any]]) -> list[Cluster]:
    """Collapse pairwise clone reports into one cluster per copy-pasted thing.

    Every clone finding carries a ``partner``; the transitive closure over those
    edges is the set of files that share the fragment. Without this, a helper
    pasted into six files produces fifteen "duplicate code" issues that are all
    the same deletion.

    The nodes are *fragments* (file + line range), not files. Over files, two
    unrelated clones that merely shared a file were chained into one cluster —
    and since the marker hashes the file set, any new clone touching a tracked
    cluster's file changed its marker and re-filed work already tracked.
    Overlapping fragments in one file are the same code; components that end up
    with the same file set are merged, so one file pair keeps one marker.
    """
    def fragment(path: str, line: Any, lines: int) -> tuple[str, float, float]:
        # No position (a report from before `partner_line`): the whole file,
        # which reproduces the old per-file grouping rather than guessing.
        if not line:
            return (path, 0, math.inf)
        return (path, line, line + max(lines, 1) - 1)

    union = _Union()
    own: list[tuple[str, float, float]] = []
    for item in findings:
        lines = item.get("clone_lines") or 0
        positioned = item.get("partner_line") is not None
        node = fragment(item["file"], item.get("line") if positioned else None, lines)
        own.append(node)
        union.find(node)
        if item.get("partner"):
            union.union(node, fragment(item["partner"],
                                       item.get("partner_line"), lines))
    by_file: dict[str, list[tuple[str, float, float]]] = {}
    for node in list(union.parent):
        by_file.setdefault(node[0], []).append(node)
    for nodes in by_file.values():
        nodes.sort()
        reach = nodes[0]  # the node reaching furthest in the current overlapping run
        for node in nodes[1:]:
            if node[1] <= reach[2]:
                union.union(reach, node)
                if node[2] > reach[2]:
                    reach = node
            else:
                reach = node
    component_files: dict[Any, set[str]] = {}
    for item, node in zip(findings, own):
        files = component_files.setdefault(union.find(node), set())
        files.update(f for f in (item["file"], item.get("partner")) if f)
    groups: dict[frozenset[str], Cluster] = {}
    for item, node in zip(findings, own):
        files = frozenset(component_files[union.find(node)])
        cluster = groups.setdefault(files, Cluster(kind=KIND_DUPLICATION,
                                                   files=sorted(files)))
        cluster.findings.append(item)
    return list(groups.values())


def apply_churn(clusters: Sequence[Cluster], churn: dict[str, int], weight: float) -> None:
    """Scale each cluster by how often its files actually change.

    The multiplier is the same bounded ``1 + weight * log1p(n)/log1p(max)`` the
    scanner uses for hotspots, taken over the cluster's **busiest** file: a
    refactor is worth doing if *any* file it touches is hot, and averaging would
    let five cold files hide one that changes every week.
    """
    if not churn:
        return
    max_commits = max(churn.values())
    for cluster in clusters:
        commits = max((churn.get(f, 0) for f in cluster.files), default=0)
        cluster.churn = commits
        if commits > 0 and max_commits > 0:
            cluster.score *= 1.0 + weight * (math.log1p(commits) / math.log1p(max_commits))


# --------------------------------------------------------------------------
# Dedup corpus
# --------------------------------------------------------------------------


def _run(cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(list(cmd), cwd=str(cwd), capture_output=True,
                              text=True, timeout=120, check=False)
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return None


def list_open_issues(repo: Path, backend: str) -> list[dict[str, Any]]:
    """Open issues as ``[{number, title, body}]``. Raises rather than returning
    ``[]`` — see the module docstring on unread corpora."""
    if backend == "gh":
        proc = _run(["gh", "issue", "list", "--state", "open", "--limit",
                     str(LIST_LIMIT), "--json", "number,title,body"], repo)
        if proc is None:
            raise IssueListError("gh is not installed or timed out")
        if proc.returncode != 0:
            raise IssueListError(f"gh issue list failed: {proc.stderr.strip()[-300:]}")
        try:
            issues = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise IssueListError(f"gh returned non-JSON: {exc}") from exc
    elif backend == "tea":
        issues = _list_tea_issues(repo)
    else:
        raise IssueListError(f"unknown backend {backend!r}")
    if len(issues) >= LIST_LIMIT:
        # Truncation is indistinguishable from "no more issues" in the output,
        # so it must be an error rather than a silently short corpus.
        raise IssueListError(f"issue list hit the {LIST_LIMIT} limit — raise LIST_LIMIT")
    return issues


TEA_PAGE = 50  # Gitea's default per-page maximum; `--limit` above it is silently capped


def _list_tea_issues(repo: Path) -> list[dict[str, Any]]:
    """Every open issue, page by page. tea's default fields leave out ``body``,
    which is where the markers live, and ``--limit`` is a page size — so the
    plain call returned 50 issues with no bodies and dedup matched nothing."""
    remote = _run(["git", "remote", "get-url", "origin"], repo)
    slug = _repo_slug(remote.stdout if remote and remote.returncode == 0 else "")
    if not slug:
        raise IssueListError("tea needs an `origin` remote to know which repo to read")
    issues: list[dict[str, Any]] = []
    page = 0
    while len(issues) < LIST_LIMIT:
        page += 1
        proc = _run(["tea", "issues", "list", "--state", "open", "--page", str(page),
                     "--limit", str(TEA_PAGE), "--fields", "index,title,body,owner,repo",
                     "--output", "json"], repo)
        if proc is None:
            raise IssueListError("tea is not installed or timed out")
        if proc.returncode != 0:
            raise IssueListError(f"tea issues list failed: {proc.stderr.strip()[-300:]}")
        try:
            raw = json.loads(proc.stdout) or []
        except json.JSONDecodeError as exc:
            raise IssueListError(f"tea returned non-JSON: {exc}") from exc
        for i in raw:
            # With no login for origin's host, tea falls back to its default
            # login and lists *other* repos' issues, exit 0. An unrelated corpus
            # reads as "nothing tracked" and everything is filed again.
            if f"{i.get('owner', '')}/{i.get('repo', '')}".lower() != slug:
                raise IssueListError(
                    f"tea listed issues from {i.get('owner')}/{i.get('repo')}, not {slug} "
                    f"— add a tea login for origin's host")
            issues.append({"number": i.get("index") or i.get("number"),
                           "title": i.get("title", ""), "body": i.get("body", "")})
        # Only an empty page ends the corpus: a server whose MAX_RESPONSE_ITEMS is
        # below TEA_PAGE returns short pages throughout, and stopping on the first
        # one truncated the corpus silently.
        if not raw:
            break
    return issues


def _repo_slug(url: str) -> str:
    """``owner/repo`` from an ssh or https remote URL, lower-cased."""
    path = url.strip().removesuffix("/").removesuffix(".git")
    parts = re.split(r"[/:]", path)
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else ""


def load_issue_corpus(repo: Path, backend: str, issues_json: Path | None) -> list[dict[str, Any]]:
    if issues_json is not None:
        # The seam for trackers this script cannot call itself (e.g. an MCP-only
        # tracker): the caller fetches the open issues and hands them over.
        try:
            payload = json.loads(issues_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IssueListError(f"{issues_json}: {exc}") from exc
        if not isinstance(payload, list):
            raise IssueListError(f"{issues_json}: expected a JSON array of issues")
        return payload
    return list_open_issues(repo, backend)


def tracked_markers(issues: Iterable[dict[str, Any]]) -> set[str]:
    """Fingerprints already covered by an open issue.

    A marker counts from the **title or the body**, so one LLM-authored issue can
    cover several clusters through a ``Covers quality clusters:`` footer. The
    rolling queue issue is excluded from its own corpus — its body lists every
    queued marker, so counting it as coverage would empty the queue on the next
    run.

    That exclusion keys on the **title**, which this script writes and therefore
    owns. Matching the body too meant an analysed issue that merely *mentioned*
    the queue ("supersedes the entry in (qfp:queue)") had all of its own markers
    thrown away, and the work it tracked came straight back in the next queue.
    """
    out: set[str] = set()
    for issue in issues:
        if is_rolling_issue(issue):
            continue
        out.update(MARKER_RE.findall(f"{issue.get('title', '')}\n{issue.get('body') or ''}"))
    return out


# --------------------------------------------------------------------------
# Dismissals
# --------------------------------------------------------------------------


class DismissalError(RuntimeError):
    """The dismissal file is unreadable, or a dismissal cannot be recorded."""


def load_dismissed(repo: Path) -> dict[str, dict[str, Any]]:
    """``{fingerprint: entry}`` from the dismissal file; ``{}`` when there is none.

    A malformed file raises rather than reading as empty. Empty would only
    re-queue clusters, which is the safe direction — but it would do it
    silently, and the next run would re-dismiss them over a file nobody fixed.
    An entry without a reason is refused too: a dismissal nobody can explain
    is a suppression, and a reviewer has nothing to check it against.
    """
    path = repo / DISMISSED_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DismissalError(f"{DISMISSED_FILE}: {exc}") from exc
    if not isinstance(payload, list):
        raise DismissalError(f"{DISMISSED_FILE}: expected a JSON array of dismissals")
    out: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(payload):
        # `kind` is required because the stale check keys on it: an entry with
        # no kind, or a misspelt one, could never be reported and would sit in
        # the file forever after its cluster was gone.
        if not (isinstance(entry, dict) and isinstance(entry.get("fingerprint"), str)
                and entry.get("kind") in CLUSTER_KINDS
                and isinstance(entry.get("base_score"), (int, float))
                and str(entry.get("reason") or "").strip()):
            raise DismissalError(f"{DISMISSED_FILE}: entry {i} needs a fingerprint, a kind "
                                 f"({', '.join(CLUSTER_KINDS)}), a numeric base_score "
                                 f"and a reason")
        out[entry["fingerprint"]] = entry
    return out


def apply_dismissals(clusters: Sequence[Cluster],
                     dismissed: dict[str, dict[str, Any]]) -> tuple[list[Cluster], list[Cluster]]:
    """``(kept, dismissed)``.

    A dismissal holds only while the cluster is no worse than when it was
    judged. Rounded to the two places the file records, so float noise in the
    weights cannot lapse it. A cluster that outgrew its dismissal is kept and
    carries the old entry, so the author re-judges it with the reason in hand
    instead of re-discovering the same false positive from scratch.
    """
    kept: list[Cluster] = []
    out: list[Cluster] = []
    for cluster in clusters:
        entry = dismissed.get(cluster.fingerprint)
        if entry is None:
            kept.append(cluster)
        elif round(cluster.base_score, 2) <= entry["base_score"]:
            out.append(cluster)
        else:
            cluster.previous_dismissal = entry
            kept.append(cluster)
    return kept, out


def stale_dismissals(clusters: Sequence[Cluster], dismissed: dict[str, dict[str, Any]],
                     kinds: Iterable[str]) -> list[str]:
    """Dismissed fingerprints this scan no longer produces, among ``kinds``.

    Reported, never pruned: the triage run is read-only, and an entry can look
    stale only because an analyser did not run this time.
    """
    present = {c.fingerprint for c in clusters}
    kinds = set(kinds)
    return sorted(fp for fp, entry in dismissed.items()
                  if fp not in present and entry["kind"] in kinds)


def record_dismissal(repo: Path, handoff_path: Path, fingerprint: str, reason: str) -> dict[str, Any]:
    """Add or replace one dismissal, taken from the hand-off the author read.

    From the hand-off rather than a fresh clustering, so the recorded score is
    the one belonging to the code that was actually read. Re-dismissing a
    resurfaced cluster replaces its entry, which moves the bar up to the score
    it was re-judged at.
    """
    fingerprint = fingerprint.strip()
    match = MARKER_RE.fullmatch(fingerprint)
    if match:
        fingerprint = match.group(1)
    if not reason.strip():
        raise DismissalError("--reason is required: say why this is not worth fixing")
    try:
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DismissalError(f"cannot read the hand-off {handoff_path}: {exc}") from exc
    cluster = next((c for c in handoff.get("clusters") or []
                    if c.get("fingerprint") == fingerprint), None)
    if cluster is None:
        raise DismissalError(f"{fingerprint} is not a queued cluster in {handoff_path} — "
                             f"only a cluster that was queued, and read, can be dismissed")
    if "base_score" not in cluster:
        raise DismissalError(f"{handoff_path} predates dismissals — re-run the triage first")
    entries = load_dismissed(repo)
    entries[fingerprint] = {
        "fingerprint": fingerprint,
        "kind": cluster["kind"],
        "title": cluster["title"],
        "files": cluster["files"],
        "base_score": cluster["base_score"],
        "reason": reason.strip(),
        "date": datetime.now(timezone.utc).date().isoformat(),
    }
    ordered = sorted(entries.values(), key=lambda e: (e.get("kind", ""), e.get("title", ""),
                                                      e["fingerprint"]))
    (repo / DISMISSED_FILE).write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    return entries[fingerprint]


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def is_queueable(cluster: Cluster, min_score: float) -> bool:
    """Could this cluster hold a slot at all?

    Structural clusters bypass the floor; everything else has to clear it. Shared
    by the queue and the "already tracked" count, because deriving "what was on
    offer" twice is how the headline came to disagree with the table.
    """
    return cluster.kind == KIND_STRUCTURAL or cluster.score >= min_score


def select(clusters: Sequence[Cluster], tracked: set[str], *, top: int,
           min_score: float) -> tuple[list[Cluster], int]:
    """``(queued, already_tracked_count)``.

    Dedup runs **first**: eight already-tracked clusters ranked above a fresh one
    would otherwise starve it forever.

    Structural clusters bypass the score entirely — they sort ahead of everything
    and are exempt from both the floor and the per-kind cap. A lone import cycle
    scores 4.0, which ranks it below every high-volume dead-code cluster and under
    a 6.0 floor; either alone would keep the most severe class the scanner detects
    out of the queue permanently. They do **not** get to take every slot: see
    ``RESERVED_FOR_OTHERS``.

    ``already`` counts only clusters that would otherwise have been *queueable*.
    Counting every tracked cluster made the headline read "41 already tracked" on
    a repo with three open issues, because most clusters were sub-floor noise that
    was never a candidate for anything.

    ``MAX_PER_KIND`` governs *competition between kinds* only: clusters it held
    back fill whatever slots the other kinds leave empty, so a restriction to one
    kind — or a repo with little else wrong — still gets the ``--top`` it asked for.
    """
    fresh = [c for c in clusters if c.fingerprint not in tracked]

    structural = [c for c in fresh if c.kind == KIND_STRUCTURAL]
    others = [c for c in fresh if c.kind != KIND_STRUCTURAL and is_queueable(c, min_score)]
    already = sum(1 for c in clusters
                  if c.fingerprint in tracked and is_queueable(c, min_score))

    structural.sort(key=lambda c: (-c.score, c.signature, c.files))
    others.sort(key=lambda c: (-c.score, c.kind, c.files))

    structural_cap = top
    if others:
        structural_cap = max(1, top - min(RESERVED_FOR_OTHERS, len(others)))
    queued = structural[:structural_cap]
    per_kind: dict[str, int] = {}
    held: list[Cluster] = []
    for cluster in others:
        if len(queued) >= top:
            break
        if per_kind.get(cluster.kind, 0) >= MAX_PER_KIND:
            held.append(cluster)
            continue
        per_kind[cluster.kind] = per_kind.get(cluster.kind, 0) + 1
        queued.append(cluster)
    # The cap only arbitrates between kinds. Once the other kinds have nothing
    # left, a held-back cluster is better than an empty slot: `--top 8` on a repo
    # with three duplication and two complexity clusters queued five.
    queued.extend(held[:top - len(queued)])
    return queued[:top], already


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_preview(queued: Sequence[Cluster], total: int, already: int,
                   unreadable: int = 0, dismissed: int = 0) -> str:
    head = f"{total} clusters · {already} already tracked · {len(queued)} queued"
    if dismissed:
        head += f" · {dismissed} dismissed"
    if unreadable:
        head += f" · {unreadable} unreadable (paths the scanner could not resolve)"
    lines = [head, ""]
    if not queued:
        lines.append("nothing to triage.")
        return "\n".join(lines)
    lines += ["| # | Score | Churn | Kind | Cluster | Marker |",
              "|---:|---:|---:|---|---|---|"]
    for i, cluster in enumerate(queued, 1):
        title = cluster.title()
        if cluster.previous_dismissal:
            title += f" — resurfaced (dismissed at {cluster.previous_dismissal['base_score']})"
        lines.append(f"| {i} | {cluster.score:.1f} | {cluster.churn} | {cluster.kind} | "
                     f"{title} | `{cluster.marker}` |")
    return "\n".join(lines)


def render_queue_body(queued: Sequence[Cluster], total: int, already: int,
                      dismissed: int = 0) -> str:
    """Body of the rolling queue issue.

    A queue entry is a *pointer at* work, not a unit of it — it carries evidence
    and no analysis on purpose. The analysed, scoped issues are authored by
    ``/quality-triage``, and their markers drop entries from here automatically.
    """
    lines = [
        "Ranked maintainability clusters from the code-quality scanner.",
        "",
        "**This is a queue, not a work order.** Each entry is a cluster of findings that",
        "share a root cause, ranked by weighted severity times change frequency. Run",
        "`/quality-triage` to have them investigated in the code and filed as scoped",
        "refactoring issues — an issue carrying a cluster's `(qfp:…)` marker drops that",
        "cluster from this queue on the next run.",
        "",
        f"_{total} clusters total · {already} already tracked by an open issue"
        f"{f' · {dismissed} dismissed in `{DISMISSED_FILE}`' if dismissed else ''}._",
        "",
    ]
    for i, cluster in enumerate(queued, 1):
        lines += [f"### {i}. {cluster.title()}  `{cluster.marker}`",
                  "",
                  f"- **kind** {cluster.kind} · **score** {cluster.score:.1f} · "
                  f"**commits (window)** {cluster.churn}"]
        if cluster.signature:
            lines.append(f"- **signature** `{cluster.signature}`")
        lines.append(f"- **files** {', '.join(f'`{f}`' for f in cluster.files[:8])}"
                     + (f" _+{len(cluster.files) - 8} more_" if len(cluster.files) > 8 else ""))
        lines.append("")
        for item in cluster.to_dict()["findings"][:6]:
            location = f"{item['file']}:{item['line']}" if item["line"] else item["file"]
            lines.append(f"  - `{location}` — {item['detail']} _({item['tool']})_")
        if len(cluster.findings) > 6:
            lines.append(f"  - _… {len(cluster.findings) - 6} more findings_")
        lines.append("")
    lines += ["---",
              "<!-- Rewritten in place by quality_triage.py. Hand edits are lost. -->"]
    body = "\n".join(lines) + "\n"
    if len(body) > MAX_ISSUE_BODY:
        # `tea` takes the body as a single argv entry, and Linux caps one of
        # those at 128 KiB — a large `--top` would fail the write with an
        # errno nobody would connect to the queue length. Truncating loses the
        # tail of a ranked list, which is the cheapest thing here to lose.
        body = (body[:MAX_ISSUE_BODY].rsplit("\n", 1)[0]
                + f"\n\n_…truncated at {MAX_ISSUE_BODY} characters. "
                  f"Lower `--top` to see the whole queue in the tracker._\n")
    return body


# --------------------------------------------------------------------------
# Rolling queue issue
# --------------------------------------------------------------------------


def find_rolling_issue(issues: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    for issue in issues:
        if is_rolling_issue(issue):
            return issue
    return None


def upsert_rolling_issue(repo: Path, backend: str, body: str,
                         existing: dict[str, Any] | None, label: str) -> str:
    # Not inside the repo: a process killed between write and unlink used to
    # leave an untracked scratch file in the working tree, where it shows up in
    # someone else's `git status` and in the next commit's `git add -A`.
    handle = tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8",
                                         delete=False)
    handle.write(body)
    handle.close()
    body_file = Path(handle.name)
    try:
        if backend == "gh":
            if existing:
                cmd = ["gh", "issue", "edit", str(existing["number"]),
                       "--body-file", str(body_file)]
            else:
                cmd = ["gh", "issue", "create", "--title", ROLLING_TITLE,
                       "--body-file", str(body_file), "--label", label]
        elif backend == "tea":
            if existing:
                cmd = ["tea", "issues", "edit", str(existing["number"]),
                       "--description", body]
            else:
                cmd = ["tea", "issues", "create", "--title", ROLLING_TITLE,
                       "--description", body, "--labels", label]
        else:
            raise SystemExit(f"--mode rolling cannot write to backend {backend!r}")
        proc = _run(cmd, repo)
        if proc is None or proc.returncode != 0:
            detail = proc.stderr.strip()[-300:] if proc else "command could not be run"
            raise SystemExit(f"failed to write the queue issue: {detail}")
        # `create` prints the new issue's URL; `edit` prints little or nothing,
        # so fall back to the number we already know.
        printed = (proc.stdout or "").strip()
        if printed:
            return printed
        return f"#{existing['number']} updated" if existing else "created"
    finally:
        body_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def detect_backend(repo: Path) -> str:
    """Best-effort tracker detection, biased toward failing loudly.

    Never falls back to "no tracker" — a dedup corpus that silently does not
    exist is exactly the failure this script refuses to have.
    """
    remote = _run(["git", "remote", "get-url", "origin"], repo)
    url = (remote.stdout.strip().lower() if remote and remote.returncode == 0 else "")
    if "github.com" in url:
        if shutil.which("gh"):
            return "gh"
        raise SystemExit("origin is on github.com but `gh` is not installed")
    # Hosts that are definitely not Gitea. Falling through to `tea` merely because
    # it happened to be installed pointed a GitLab repo's dedup at whatever
    # unrelated Gitea instance tea was configured for — and an empty answer from
    # the wrong tracker reads exactly like "nothing is tracked yet".
    # Codeberg is deliberately absent: it runs Forgejo, which `tea` speaks.
    foreign = ("gitlab.com", "bitbucket.org", "dev.azure.com", "visualstudio.com",
               "sourceforge.net")
    if url and not any(host in url for host in foreign) and shutil.which("tea"):
        return "tea"
    raise SystemExit(
        f"cannot tell which tracker {url or 'this repo'} uses — pass --backend gh|tea, "
        f"or --issues-json <file> with the open issues fetched another way")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--kind", action="append", choices=sorted(CLUSTER_KINDS),
                        help="restrict the queue to one cluster kind (repeatable)")
    parser.add_argument("--mode", choices=["handoff", "rolling"], default="handoff",
                        help="handoff: write the JSON an LLM triages from (default). "
                             "rolling: also maintain one queue issue in the tracker.")
    parser.add_argument("--backend", choices=["gh", "tea"],
                        help="issue tracker CLI (default: detect)")
    parser.add_argument("--issues-json", type=Path,
                        help="open issues as JSON [{number,title,body}] — for trackers "
                             "this script cannot call itself")
    parser.add_argument("--report-dir", help="override report_dir")
    parser.add_argument("--label", default="refactor",
                        help="label for the rolling queue issue (must already exist)")
    parser.add_argument("--json", dest="json_out", default=".quality-clusters.json",
                        help="where to write the hand-off JSON")
    # Named for what it does, not for "go". The installed command has its own
    # `+execute` meaning "file the analysed issues", and one flag that meant two
    # different writes in two different places was a trap.
    parser.add_argument("--write-queue", action="store_true",
                        help="rolling mode: actually write the queue issue. "
                             "Default is a preview.")
    parser.add_argument("--dismiss", metavar="FINGERPRINT",
                        help=f"record a queued cluster from the hand-off as not worth "
                             f"fixing, in {DISMISSED_FILE}, and exit. Needs --reason.")
    parser.add_argument("--reason", help="why the --dismiss cluster is not worth fixing")
    args = parser.parse_args(argv)
    if (args.dismiss is None) != (args.reason is None):
        parser.error("--dismiss and --reason go together")
    if args.dismiss is not None and (args.mode != "handoff" or args.write_queue):
        parser.error("--dismiss only records a dismissal; run the queue separately")
    if args.write_queue and args.mode != "rolling":
        # Silently ignoring it printed a normal hand-off and exited 0, so the
        # user believed the tracker had been updated. The flag was renamed to
        # stop two different writes looking alike; a no-op that looks like
        # success undoes that.
        parser.error("--write-queue only applies to --mode rolling")

    repo = Path(args.repo).resolve()
    if args.dismiss is not None:
        handoff_path = Path(args.json_out)
        if not handoff_path.is_absolute():
            handoff_path = repo / handoff_path
        try:
            entry = record_dismissal(repo, handoff_path, args.dismiss, args.reason)
        except DismissalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"dismissed {entry['title']} (qfp:{entry['fingerprint']}) at score "
              f"{entry['base_score']} in {DISMISSED_FILE} — commit it with the run")
        return 0
    config = load_config(repo)
    ctx = Ctx(repo, config)
    report_dir = Path(args.report_dir).resolve() if args.report_dir else repo / config["report_dir"]
    # Repo-relative, like `--json` and every path in the reports. The installed
    # command writes `.quality-issues.json` at the repo root and this resolved it
    # against the *cwd*, so `--repo elsewhere` read no corpus, or the wrong one —
    # and a corpus that is merely absent is the failure this script exists to
    # refuse, so it must not be reachable by a path mistake.
    issues_json = args.issues_json
    if issues_json is not None and not issues_json.is_absolute():
        issues_json = repo / issues_json

    try:
        findings, run = load_findings(report_dir)
        assert_reports_are_current(ctx, run)
        assert_findings_resolve(ctx, findings)
    except StaleReportError as exc:
        # Refuse rather than warn: an issue filed from a mixed report set is
        # indistinguishable from a correct one until someone opens the file.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    clusters = cluster_findings(findings)
    # Restricting first, so that *everything* downstream describes the same set:
    # the unreadable count is reported next to a restricted queue, and counting
    # it over all kinds told a `--kind structural` run that clusters had been
    # held back from it that it was never going to consider anyway.
    # Filtering here rather than inside select() also keeps `already` meaning
    # "tracked among the clusters on offer" — the restriction is part of what is
    # on offer.
    if args.kind:
        clusters = [c for c in clusters if c.kind in set(args.kind)]
    try:
        dismissals = load_dismissed(repo)
    except DismissalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Over every cluster on offer, readable or not: an unreadable cluster still
    # exists, so its dismissal is not stale.
    stale = stale_dismissals(clusters, dismissals, args.kind or CLUSTER_KINDS)
    # A cluster every one of whose files failed to resolve at scan time cannot be
    # investigated: the command's whole method is "read the files at the lines".
    # Held back rather than dropped silently — the count is the symptom of an
    # adapter emitting unusable paths, and hiding it hides the bug.
    unreadable = [c for c in clusters if c.is_unreadable]
    clusters = [c for c in clusters if not c.is_unreadable]
    churn_cfg = config.get("churn", {})
    apply_churn(clusters, git_churn(ctx), float(churn_cfg.get("weight", 1.0)))

    backend = args.backend or (None if issues_json else detect_backend(repo))
    try:
        issues = load_issue_corpus(repo, backend or "gh", issues_json)
    except IssueListError as exc:
        # Before any write: deduping against a corpus that failed to load marks
        # every cluster "not tracked" and re-files work that already exists.
        print(f"error: could not read the open-issue corpus: {exc}", file=sys.stderr)
        return 1

    tracked = tracked_markers(issues)
    # Before the cut-off, for the same reason dedup is: a dismissed cluster
    # ranked above a fresh one would otherwise take its slot on every run.
    clusters_on_offer = len(clusters)
    clusters, dismissed = apply_dismissals(clusters, dismissals)
    n_dismissed = sum(1 for c in dismissed if is_queueable(c, args.min_score))
    queued, already = select(clusters, tracked, top=args.top, min_score=args.min_score)

    handoff = {
        "repo": str(repo),
        "generated_from": str(report_dir),
        "scan": run,
        "totals": {"findings": len(findings), "clusters": clusters_on_offer,
                   "already_tracked": already, "dismissed": n_dismissed,
                   "queued": len(queued), "unreadable": len(unreadable)},
        "stale_dismissals": stale,
        "unreadable_clusters": [{"kind": c.kind, "files": sorted(c.files),
                                 "tools": sorted({f["tool"] for f in c.findings})}
                                for c in unreadable],
        # Recorded so a hand-off cannot be mistaken for the whole picture.
        "restricted_to_kinds": sorted(set(args.kind)) if args.kind else None,
        "clusters": [c.to_dict() for c in queued],
    }
    out_path = Path(args.json_out)
    if not out_path.is_absolute():
        out_path = repo / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")

    print(render_preview(queued, clusters_on_offer, already, len(unreadable), n_dismissed))
    if stale:
        missing = run.get("analysers_not_ok") or []
        cause = (f"analysers that did not run in this scan: {', '.join(missing)} — keep "
                 f"the entries until a scan with them confirms it" if missing else
                 f"every analyser ran, so the code was fixed or its file set changed")
        print(f"({len(stale)} stale dismissal(s) in {DISMISSED_FILE}: {', '.join(stale)}; "
              f"{cause})")
    if unreadable:
        tools = sorted({f["tool"] for c in unreadable for f in c.findings})
        print(f"({len(unreadable)} cluster(s) held back: every file in them was "
              f"already unresolvable when the scan ran — check {', '.join(tools)})")
    if args.kind:
        print(f"(restricted to {', '.join(sorted(set(args.kind)))} — other kinds "
              f"were not considered)")
    # Print the scan's age: every finding below is only as true as the scan that
    # produced it, and that is not visible from the findings themselves.
    print(f"\nfrom scan {run.get('run_id', '?')} at {run.get('generated_at', '?')}")
    print(f"hand-off written: {ctx.relpath(out_path)}")

    if args.mode == "rolling":
        if backend is None:
            # --issues-json supplies a *read* corpus; it says nothing about how
            # to write one back. Guessing a CLI here would file the queue issue
            # into whichever tracker happened to be installed.
            print("error: --mode rolling needs --backend gh|tea to write the queue issue",
                  file=sys.stderr)
            return 1
        body = render_queue_body(queued, clusters_on_offer, already, n_dismissed)
        if not args.write_queue:
            print("\n(preview — rerun with --write-queue to write the queue issue)")
            return 0
        existing = find_rolling_issue(issues)
        result = upsert_rolling_issue(repo, backend, body, existing, args.label)
        print(f"queue issue: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
