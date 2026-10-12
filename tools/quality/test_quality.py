#!/usr/bin/env python3
"""Tests for the invariants that are expensive to get wrong.

Run with ``python3 tools/quality/test_quality.py`` — stdlib ``unittest``, no
pytest, so it works in any repo the engine is vendored into.

Each test here pins a property whose violation is *silent*: a wrong answer that
still exits 0. That is the selection criterion — the adapters themselves are
covered by running the scanner, not by mocking every tool's output format.
"""
# Vendored from llmdev tools/quality @ 02c1cd5. Re-sync with /quality-setup +update.
# Local edits will be overwritten; upstream fixes belong in llmdev.

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_scan as qs
import quality_triage as qt


def _finding(file, kind, detail="d", tool="t", line=None, **extra):
    return qs.finding(file, kind, detail, tool=tool, line=line, extra=extra or None)


class CycleIdentity(unittest.TestCase):
    def test_signature_is_rotation_independent(self):
        # A cycle re-reported from a different entry point is the same cycle. If
        # it is not, every baseline diff invents a "new" structural issue.
        self.assertEqual(qs.cycle_signature(["a", "b", "c"]),
                         qs.cycle_signature(["c", "a", "b"]))

    def test_scc_is_iterative(self):
        # A deep chain must not blow the recursion limit: a crashed gate and a
        # passing gate look identical to anyone reading only the exit code.
        edges = {f"m{i}": {f"m{i + 1}"} for i in range(3000)}
        edges["m3000"] = {"m0"}
        cycles = qs.find_cycles(edges)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(len(cycles[0]), 3001)


class ImportGraph(unittest.TestCase):
    def test_src_layout_resolves(self):
        # `src/myapp/x.py` is imported as `myapp.x`, not `src.myapp.x`. Indexing
        # only the root-prefixed form resolves *nothing*, and the analyser then
        # reports zero cycles — which reads exactly like a clean codebase.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            for pkg in ("myapp", "myapp/utils", "myapp/services"):
                (root / pkg).mkdir(parents=True)
                (root / pkg / "__init__.py").touch()
            (root / "myapp/utils/u.py").write_text("from myapp.services import s\n")
            (root / "myapp/services/s.py").write_text("from myapp.utils import u\n")
            edges, _ = qs.build_python_graph([root], qs.DEFAULT_CONFIG["exclude_dirs"])
            self.assertEqual(len(qs.find_cycles(edges)), 1)

    def test_one_import_line_is_one_edge(self):
        # `from pkg import mod` imports both `pkg` and `pkg.mod`. Counting both
        # doubles a file's layering score for one line of code.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            (root / "pkg").mkdir(parents=True)
            (root / "pkg" / "__init__.py").touch()
            (root / "pkg" / "mod.py").touch()
            (root / "main.py").write_text("from pkg import mod\n")
            edges, _ = qs.build_python_graph([root], qs.DEFAULT_CONFIG["exclude_dirs"])
            self.assertEqual(len(edges["src.main"]), 1)

    def test_type_checking_imports_are_not_import_time_edges(self):
        # `if TYPE_CHECKING:` is the standard way to break a cycle; counting it
        # reported the fix as the problem.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            (root / "app").mkdir(parents=True)
            (root / "app" / "__init__.py").touch()
            (root / "app" / "a.py").write_text(
                "import typing\nif typing.TYPE_CHECKING:\n    from app.b import B\n")
            (root / "app" / "b.py").write_text("from app.a import f\n")
            edges, _ = qs.build_python_graph([root], qs.DEFAULT_CONFIG["exclude_dirs"])
            self.assertEqual(qs.find_cycles(edges), [])


class PathRerooting(unittest.TestCase):
    def test_base_is_chosen_by_checking_the_filesystem(self):
        # jscpd reports each finding relative to the scan target it came from.
        # Resolving against the wrong base gives a plausible path that does not
        # exist, and triage then reads whatever happens to be there.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "web" / "src").mkdir(parents=True)
            (repo / "web" / "src" / "a.ts").touch()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertEqual(qs._reroot(ctx, "src/a.ts", [repo / "web"]), "web/src/a.ts")

    def test_absolute_paths_pass_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "x.py").touch()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertEqual(qs._reroot(ctx, str(repo / "x.py"), []), "x.py")

    def test_unresolvable_path_is_not_invented(self):
        # Better a visibly raw string than a repo-relative path that resolves to
        # a different, existing file.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = qs.Ctx(Path(tmp), qs.load_config(Path(tmp)))
            self.assertEqual(qs._reroot(ctx, "nowhere/x.ts", []), "nowhere/x.ts")

    def test_ambiguous_match_is_not_guessed(self):
        # Existence is not uniqueness. Taking the first matching base attributed
        # a clone that lives in `web/` to an innocent, non-duplicated
        # `api/src/a.js` — a plausible path pointing at the wrong file, which is
        # worse than no path because triage follows it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for side in ("api", "web"):
                (repo / side / "src").mkdir(parents=True)
                (repo / side / "src" / "a.js").touch()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertEqual(qs._reroot(ctx, "src/a.js", [repo / "api", repo / "web"]),
                             "src/a.js")

    def test_same_file_via_two_bases_is_not_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "a.js").touch()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertEqual(qs._reroot(ctx, "src/a.js", [repo, repo]), "src/a.js")


class Layering(unittest.TestCase):
    ORDER = ["utils", "core", "services", "cli"]

    def test_abstract_importing_concrete_is_a_violation(self):
        edges = {"app.utils.x": {"app.services.y"}, "app.services.y": set()}
        self.assertEqual(qs.find_layering_violations(edges, self.ORDER),
                         [("app.utils.x", "app.services.y")])

    def test_concrete_importing_abstract_is_fine(self):
        edges = {"app.cli.x": {"app.utils.y"}, "app.utils.y": set()}
        self.assertEqual(qs.find_layering_violations(edges, self.ORDER), [])

    def test_slash_paths_are_segmented(self):
        # Go import paths are slashed. A dot-only split classified every Go
        # package as unlayered, so the check ran, reported zero, and read as a
        # clean result.
        edges = {"example.com/m/utils": {"example.com/m/services"},
                 "example.com/m/services": set()}
        self.assertEqual(len(qs.find_layering_violations(edges, self.ORDER)), 1)

    def test_test_modules_are_exempt(self):
        # A suite mirroring the source layout picks up a layer name from its
        # path; on the first real repo this ran against, every layering finding
        # was that false positive.
        edges = {"tests.unit.utils.test_x": {"app.services.y"}, "app.services.y": set()}
        exempt = frozenset(["tests.unit.utils.test_x"])
        self.assertEqual(qs.find_layering_violations(edges, self.ORDER, exempt), [])
        self.assertEqual(len(qs.find_layering_violations(edges, self.ORDER)), 1)

    def test_unlayered_modules_are_ignored(self):
        edges = {"app.misc": {"app.services.y"}, "app.services.y": set()}
        self.assertEqual(qs.find_layering_violations(edges, self.ORDER), [])


class Summaries(unittest.TestCase):
    def test_skipped_tool_banks_no_count(self):
        # Otherwise installing the tool later reads as a 0 -> N regression, and
        # the honest fix is indistinguishable from silencing the gate.
        results = [qs.ToolResult("radon", "skipped"), qs.ToolResult("vulture", "ok", [])]
        self.assertNotIn("radon", qs.summarize(results)["tool_finding_counts"])
        self.assertEqual(qs.summarize(results)["tool_finding_counts"]["vulture"], 0)

    def test_errored_tool_banks_no_count(self):
        results = [qs.ToolResult("knip", "error", note="boom")]
        self.assertEqual(qs.summarize(results)["tool_finding_counts"], {})

    def test_new_tool_is_coverage_not_regression(self):
        current = {"tool_finding_counts": {"radon": 40}, "structural_signatures": []}
        baseline = {"tool_finding_counts": {}, "structural_signatures": []}
        self.assertEqual(qs.diff_baseline(current, baseline), [])

    def test_shrinking_a_cycle_is_not_a_new_one(self):
        current = {"tool_finding_counts": {},
                   "structural_signatures": ["circular_import:pkg.a -> pkg.b"]}
        baseline = {"tool_finding_counts": {},
                    "structural_signatures": ["circular_import:pkg.a -> pkg.b -> pkg.c"]}
        self.assertEqual(qs.diff_baseline(current, baseline, gate="structural"), [])
        current["structural_signatures"] = ["circular_import:pkg.a -> pkg.d"]
        self.assertEqual(len(qs.diff_baseline(current, baseline, gate="structural")), 1)

    def test_count_increase_is_a_regression(self):
        current = {"tool_finding_counts": {"radon": 41}, "structural_signatures": []}
        baseline = {"tool_finding_counts": {"radon": 40}, "structural_signatures": []}
        self.assertEqual(len(qs.diff_baseline(current, baseline)), 1)

    def test_structural_gate_ignores_counts(self):
        current = {"tool_finding_counts": {"radon": 99}, "structural_signatures": [],
                   "files_below_mi_floor": 10}
        baseline = {"tool_finding_counts": {"radon": 1}, "structural_signatures": [],
                    "files_below_mi_floor": 0}
        self.assertEqual(qs.diff_baseline(current, baseline, gate="structural"), [])
        self.assertEqual(len(qs.diff_baseline(current, baseline, gate="all")), 2)

    def test_mi_floor_is_a_file_count(self):
        current = {"tool_finding_counts": {}, "structural_signatures": [],
                   "files_below_mi_floor": 4}
        baseline = {"tool_finding_counts": {}, "structural_signatures": [],
                    "files_below_mi_floor": 3}
        self.assertEqual(len(qs.diff_baseline(current, baseline)), 1)

    def test_absent_mi_count_is_not_a_count_regression(self):
        # radon did not run; None must not compare as "worse than 3".
        current = {"tool_finding_counts": {}, "structural_signatures": [],
                   "files_below_mi_floor": None}
        baseline = {"tool_finding_counts": {}, "structural_signatures": [],
                    "files_below_mi_floor": 3}
        self.assertEqual([p for p in qs.diff_baseline(current, baseline) if "->" in p], [])

    def test_lost_mi_coverage_is_reported(self):
        # ...but it is not a clean result either. The baseline measured the
        # maintainability index and this run did not, so the gate is comparing
        # less than it was, and only this line says so.
        current = {"tool_finding_counts": {}, "structural_signatures": [],
                   "files_below_mi_floor": None}
        baseline = {"tool_finding_counts": {}, "structural_signatures": [],
                    "files_below_mi_floor": 3}
        problems = qs.diff_baseline(current, baseline)
        self.assertEqual(len(problems), 1)
        self.assertIn("coverage lost", problems[0])

    def test_lost_tool_coverage_is_a_regression(self):
        # The mirror of "a new tool is coverage, not a regression". Counts can
        # only regress upward, so uninstalling radon was the cheapest way to
        # make a complexity finding disappear from a green gate.
        current = {"tool_finding_counts": {"vulture": 3}, "structural_signatures": []}
        baseline = {"tool_finding_counts": {"radon": 40, "vulture": 3},
                    "structural_signatures": []}
        problems = qs.diff_baseline(current, baseline)
        self.assertEqual(len(problems), 1)
        self.assertIn("radon", problems[0])

    def test_partial_run_does_not_report_lost_coverage(self):
        # A partial run knows nothing about the tools it did not run; reporting
        # all of them as lost would make `--only` unusable under a gate.
        current = {"tool_finding_counts": {"import-analysis": 0},
                   "structural_signatures": [], "run": {"partial": True}}
        baseline = {"tool_finding_counts": {"import-analysis": 0, "radon": 40},
                    "structural_signatures": []}
        self.assertEqual(qs.diff_baseline(current, baseline), [])


class Churn(unittest.TestCase):
    @staticmethod
    def _repo(root: Path) -> None:
        for cmd in (["git", "init", "-q", "."],
                    ["git", "config", "user.email", "t@example.com"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)

    @staticmethod
    def _commit(root: Path, message: str) -> None:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=root,
                       check=True, capture_output=True)

    def test_churn_is_keyed_off_the_scanned_root_not_the_git_root(self):
        # `git log --name-only` prints paths relative to the git top-level while
        # findings are relative to the scanned root, so a subproject scan matched
        # nothing and scored every file as never touched. Nothing fails: the
        # ranking just silently loses the multiplier that makes it a risk list.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._repo(root)
            (root / "sub" / "src").mkdir(parents=True)
            (root / "sub" / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (root / "outside.py").write_text("y = 1\n", encoding="utf-8")
            self._commit(root, "one")
            (root / "sub" / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
            self._commit(root, "two")
            ctx = qs.Ctx(root / "sub", qs.load_config(root / "sub"))
            churn = qs.git_churn(ctx)
            self.assertEqual(churn.get("src/a.py"), 2)
            # Paths outside the scanned root cannot be named by any finding, and
            # keeping them would let an unrelated file set the `max` the whole
            # multiplier is normalised against.
            self.assertNotIn("outside.py", churn)
            self.assertNotIn("sub/src/a.py", churn)

    def test_churn_at_the_git_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._repo(root)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            self._commit(root, "one")
            ctx = qs.Ctx(root, qs.load_config(root))
            self.assertEqual(qs.git_churn(ctx).get("a.py"), 1)

    def test_a_non_repo_yields_no_churn(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = qs.Ctx(Path(tmp), qs.load_config(Path(tmp)))
            self.assertEqual(qs.git_churn(ctx), {})


class Hotspots(unittest.TestCase):
    def test_churn_multiplier_is_bounded(self):
        results = [qs.ToolResult("t", "ok", [_finding("a.py", "complexity")])]
        plain = qs.rank_hotspots(results)[0]["score"]
        hot = qs.rank_hotspots(results, {"a.py": 500}, 1.0)[0]["score"]
        self.assertEqual(plain, 2.0)
        # One hyperactive file must not swamp the ranking: the factor caps at
        # 1 + weight, so churn reorders equals rather than replacing severity.
        self.assertLessEqual(hot, plain * 2.0)
        self.assertGreater(hot, plain)

    def test_ranking_is_deterministic(self):
        results = [qs.ToolResult("t", "ok", [
            _finding("b.py", "complexity"), _finding("a.py", "complexity")])]
        self.assertEqual([h["file"] for h in qs.rank_hotspots(results)], ["a.py", "b.py"])


class Clustering(unittest.TestCase):
    def test_pairwise_clones_collapse_to_one_cluster(self):
        # Three files sharing one fragment produce six directed findings; that is
        # one refactor, not six issues.
        findings = []
        pairs = [("a.ts", "b.ts"), ("b.ts", "c.ts"), ("a.ts", "c.ts")]
        for left, right in pairs:
            findings.append(_finding(left, "duplicate", partner=right))
            findings.append(_finding(right, "duplicate", partner=left))
        clusters = qt.cluster_findings(findings)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].files, ["a.ts", "b.ts", "c.ts"])

    def test_complexity_and_maintainability_merge_per_file(self):
        clusters = qt.cluster_findings([
            _finding("x.py", "complexity"), _finding("x.py", "maintainability")])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].score, 4.5)

    def test_rotated_cycle_is_one_cluster(self):
        signature = qs.cycle_signature(["a", "b"])
        clusters = qt.cluster_findings([
            _finding("a.py", "circular_import", signature=signature),
            _finding("b.py", "circular_import", signature=signature)])
        self.assertEqual(len(clusters), 1)

    def test_fingerprint_ignores_line_numbers(self):
        # An edit above a complex function must not mint a new cluster and
        # re-file an issue that already exists.
        one = qt.cluster_findings([_finding("x.py", "complexity", line=10)])[0]
        two = qt.cluster_findings([_finding("x.py", "complexity", line=920)])[0]
        self.assertEqual(one.fingerprint, two.fingerprint)

    @staticmethod
    def _clone(left, left_line, right, right_line, lines=20):
        return [_finding(left, "duplicate", line=left_line, partner=right,
                         partner_line=right_line, clone_lines=lines),
                _finding(right, "duplicate", line=right_line, partner=left,
                         partner_line=left_line, clone_lines=lines)]

    def test_unrelated_clone_through_a_shared_file_keeps_the_marker(self):
        # A new clone b<->z elsewhere in b.py is a different refactor. Chained by
        # file, it changed the tracked a<->b cluster's marker and re-filed it.
        helper = self._clone("a.py", 10, "b.py", 50)
        before = qt.cluster_findings(helper)
        after = qt.cluster_findings(helper + self._clone("b.py", 200, "z.py", 1))
        self.assertEqual(len(after), 2)
        self.assertIn(before[0].fingerprint, {c.fingerprint for c in after})

    def test_one_fragment_pasted_three_times_is_still_one_cluster(self):
        # jscpd's ranges for the same code differ slightly per pair; overlap joins them.
        clusters = qt.cluster_findings(self._clone("a.py", 10, "b.py", 50)
                                       + self._clone("b.py", 52, "c.py", 1))
        self.assertEqual([c.files for c in clusters], [["a.py", "b.py", "c.py"]])

    def test_two_clones_between_one_file_pair_share_one_marker(self):
        clusters = qt.cluster_findings(self._clone("a.py", 10, "b.py", 50)
                                       + self._clone("a.py", 300, "b.py", 400))
        self.assertEqual(len(clusters), 1)

    def test_go_layering_marker_survives_a_new_importing_file(self):
        # go-layering names whichever file sorts first; a new `account.go` must
        # not turn a tracked breach into an untracked one.
        sig = "layering_violation:m/domain -> m/db"
        one = qt.cluster_findings([_finding("domain/user.go", "layering_violation",
                                            signature=sig)])[0]
        two = qt.cluster_findings([_finding("domain/account.go", "layering_violation",
                                            signature=sig)])[0]
        self.assertEqual(one.fingerprint, two.fingerprint)


class Selection(unittest.TestCase):
    def _clusters(self):
        out = [qt.Cluster(kind=qt.KIND_STRUCTURAL, files=["s.py"], signature="a -> b",
                          findings=[_finding("s.py", "circular_import")], score=4.0)]
        for i in range(6):
            out.append(qt.Cluster(kind=qt.KIND_DEAD_CODE, files=[f"d{i}.py"],
                                  findings=[_finding(f"d{i}.py", "dead_code")],
                                  score=90.0 - i))
        return out

    def test_structural_bypasses_floor_and_cap(self):
        queued, _ = qt.select(self._clusters(), set(), top=8, min_score=6.0)
        self.assertEqual(queued[0].kind, qt.KIND_STRUCTURAL)
        self.assertLess(queued[0].score, 6.0)

    def test_per_kind_cap_holds(self):
        # Ten dead-code clusters outscore the duplication one; the cap still lets
        # it in, and the held-back dead code fills the slots nothing else wants.
        clusters = self._clusters() + [qt.Cluster(
            kind=qt.KIND_DUPLICATION, files=["dup.py"],
            findings=[_finding("dup.py", "duplicate")], score=7.0)]
        queued, _ = qt.select(clusters, set(), top=5, min_score=6.0)
        self.assertIn(qt.KIND_DUPLICATION, [c.kind for c in queued])
        self.assertEqual(len([c for c in queued if c.kind == qt.KIND_DEAD_CODE]),
                         qt.MAX_PER_KIND)

    def test_below_floor_is_dropped(self):
        low = qt.Cluster(kind=qt.KIND_COMPLEXITY, files=["l.py"],
                         findings=[_finding("l.py", "complexity")], score=2.0)
        queued, _ = qt.select([low], set(), top=8, min_score=6.0)
        self.assertEqual(queued, [])

    def test_structural_does_not_take_every_slot(self):
        # Exempt from the floor and the cap is not the same as exempt from the
        # queue's purpose: on a tangled repo, ten cycles filled all eight slots
        # and no duplication or complexity cluster was ever seen again.
        clusters = [qt.Cluster(kind=qt.KIND_STRUCTURAL, files=[f"s{i}.py"],
                               signature=f"a{i} -> b{i}",
                               findings=[_finding(f"s{i}.py", "circular_import")],
                               score=4.0) for i in range(10)]
        clusters += [qt.Cluster(kind=qt.KIND_DUPLICATION, files=[f"d{i}.py"],
                                findings=[_finding(f"d{i}.py", "duplicate")],
                                score=30.0) for i in range(3)]
        queued, _ = qt.select(clusters, set(), top=8, min_score=6.0)
        self.assertEqual(len(queued), 8)
        self.assertEqual(len([c for c in queued if c.kind == qt.KIND_DUPLICATION]),
                         qt.RESERVED_FOR_OTHERS)

    def test_structural_may_fill_the_queue_when_nothing_competes(self):
        clusters = [qt.Cluster(kind=qt.KIND_STRUCTURAL, files=[f"s{i}.py"],
                               signature=f"a{i} -> b{i}",
                               findings=[_finding(f"s{i}.py", "circular_import")],
                               score=4.0) for i in range(10)]
        queued, _ = qt.select(clusters, set(), top=8, min_score=6.0)
        self.assertEqual(len(queued), 8)

    def test_already_counts_only_queueable_clusters(self):
        # A tracked cluster that was below the floor anyway never competed for a
        # slot; counting it made the headline claim dozens of tracked clusters on
        # a repo with three open issues.
        noise = qt.Cluster(kind=qt.KIND_COMPLEXITY, files=["n.py"],
                           findings=[_finding("n.py", "complexity")], score=1.0)
        real = qt.Cluster(kind=qt.KIND_COMPLEXITY, files=["r.py"],
                          findings=[_finding("r.py", "complexity")], score=40.0)
        tracked = {noise.fingerprint, real.fingerprint}
        _, already = qt.select([noise, real], tracked, top=8, min_score=6.0)
        self.assertEqual(already, 1)

    def test_dedup_runs_before_the_cutoff(self):
        # Tracked clusters ranked above a fresh one must not consume its slot,
        # or the fresh one is starved forever.
        clusters = self._clusters()
        tracked = {c.fingerprint for c in clusters[1:4]}
        queued, already = qt.select(clusters, tracked, top=8, min_score=6.0)
        self.assertEqual(already, 3)
        self.assertEqual(len([c for c in queued if c.kind == qt.KIND_DEAD_CODE]),
                         qt.MAX_PER_KIND)


class Dedup(unittest.TestCase):
    def test_marker_counts_from_title_or_body(self):
        self.assertEqual(qt.tracked_markers([{"title": "x (qfp:aaaaaaaaaaaa)", "body": ""}]),
                         {"aaaaaaaaaaaa"})
        self.assertEqual(qt.tracked_markers([{"title": "x", "body": "covers (qfp:bbbbbbbbbbbb)"}]),
                         {"bbbbbbbbbbbb"})

    def test_one_issue_can_cover_several_clusters(self):
        body = "**Covers quality clusters:** `(qfp:aaaaaaaaaaaa)` `(qfp:bbbbbbbbbbbb)`"
        self.assertEqual(qt.tracked_markers([{"title": "t", "body": body}]),
                         {"aaaaaaaaaaaa", "bbbbbbbbbbbb"})

    def test_queue_issue_is_excluded_from_its_own_corpus(self):
        # The queue lists every queued cluster's marker; counting it as coverage
        # would empty the queue on the very next run.
        body = f"{qt.ROLLING_MARKER}\n(qfp:cccccccccccc)"
        self.assertEqual(qt.tracked_markers([{"title": qt.ROLLING_TITLE, "body": body}]), set())

    def test_issue_mentioning_the_queue_keeps_its_own_markers(self):
        # The exclusion keys on the title, which this script writes. Matching the
        # body threw away every marker on an analysed issue that merely referred
        # to the queue, and the work it tracked came straight back next run.
        issue = {"title": "Extract the shared parser (qfp:dddddddddddd)",
                 "body": f"Supersedes the entry in {qt.ROLLING_MARKER}."}
        self.assertEqual(qt.tracked_markers([issue]), {"dddddddddddd"})

    def test_unreadable_corpus_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(qt.IssueListError):
                qt.load_issue_corpus(Path(tmp), "gh", path)

    def test_non_array_corpus_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.json"
            path.write_text('{"issues": []}', encoding="utf-8")
            with self.assertRaises(qt.IssueListError):
                qt.load_issue_corpus(Path(tmp), "gh", path)

    def _tea(self, owner_repo, pages):
        """``list_open_issues`` over tea, with origin ``owner_repo`` and the
        given per-page payloads."""
        calls = []

        def fake_run(cmd, cwd):
            if cmd[0] == "git":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"git@git.example.com:{owner_repo}.git\n", stderr="")
            calls.append(cmd)
            page = int(cmd[cmd.index("--page") + 1])
            out = pages[page - 1] if page <= len(pages) else []
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

        with _patched(qt, _run=fake_run):
            return qt.list_open_issues(Path("."), "tea"), calls

    def test_tea_reads_every_page_with_bodies(self):
        # tea's default fields have no `body` and `--limit` is a page size:
        # the plain call returned 50 bodiless issues and dedup matched nothing.
        def issue(n, body=""):
            return {"index": str(n), "title": "t", "body": body,
                    "owner": "alex", "repo": "app"}
        full = [issue(n) for n in range(qt.TEA_PAGE)]
        issues, calls = self._tea("alex/app", [full, [issue(99, "(qfp:aaaaaaaaaaaa)")]])
        self.assertEqual(len(issues), qt.TEA_PAGE + 1)
        self.assertEqual(qt.tracked_markers(issues), {"aaaaaaaaaaaa"})
        self.assertIn("body", calls[0][calls[0].index("--fields") + 1])

    def test_tea_keeps_reading_past_a_short_page(self):
        # A server capping pages below TEA_PAGE returns short pages throughout;
        # stopping on the first one dropped every marker after it.
        def issue(n, body=""):
            return {"index": str(n), "title": "t", "body": body,
                    "owner": "alex", "repo": "app"}
        issues, _ = self._tea("alex/app", [[issue(1)], [issue(2, "(qfp:bbbbbbbbbbbb)")]])
        self.assertEqual(qt.tracked_markers(issues), {"bbbbbbbbbbbb"})

    def test_tea_refuses_another_repos_issues(self):
        # No login for origin's host: tea lists the default login's repos, exit 0.
        with self.assertRaises(qt.IssueListError):
            self._tea("alex/app", [[{"index": "1", "title": "t", "body": "",
                                     "owner": "alex", "repo": "other"}]])


class FixtureFilter(unittest.TestCase):
    def _repo(self, tmp):
        repo = Path(tmp)
        (repo / "tests").mkdir()
        (repo / "tests" / "conftest.py").write_text(
            "import pytest\n\n@pytest.fixture\ndef no_env():\n    pass\n", encoding="utf-8")
        (repo / "tests" / "test_a.py").write_text(
            "import pytest\n\n@pytest.fixture(name='aliased')\n"
            "def _impl():\n    pass\n\ndef test_x(no_env, aliased):\n    pass\n",
            encoding="utf-8")
        (repo / "app.py").write_text("no_env = 1\n", encoding="utf-8")
        return repo

    def test_visible_fixture_param_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            kept, dropped = qs.filter_fixture_params(
                [_finding("tests/test_a.py", "dead_code", "unused variable 'no_env'")], repo)
            self.assertEqual((kept, dropped), ([], 1))

    def test_explicit_name_replaces_the_function_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            kept, dropped = qs.filter_fixture_params([
                _finding("tests/test_a.py", "dead_code", "unused variable 'aliased'"),
                _finding("tests/test_a.py", "dead_code", "unused variable '_impl'"),
            ], repo)
            # `aliased` is injectable, `_impl` is not: an alias replaces the
            # function name rather than adding to it.
            self.assertEqual(dropped, 1)
            self.assertEqual(len(kept), 1)

    def test_same_name_outside_a_test_file_survives(self):
        # An allowlist entry would be name-global and would silence this too.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            kept, dropped = qs.filter_fixture_params(
                [_finding("app.py", "dead_code", "unused variable 'no_env'")], repo)
            self.assertEqual((len(kept), dropped), (1, 0))

    def test_unused_attribute_survives(self):
        # An attribute is never injected by name the way a fixture parameter is,
        # so suppressing one would hide real dead code that merely shares a
        # fixture's name.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            kept, dropped = qs.filter_fixture_params(
                [_finding("tests/test_a.py", "dead_code", "unused attribute 'no_env'")], repo)
            self.assertEqual((len(kept), dropped), (1, 0))

    def test_unused_import_in_a_test_file_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            kept, dropped = qs.filter_fixture_params(
                [_finding("tests/test_a.py", "dead_code", "unused import 'no_env'")], repo)
            self.assertEqual((len(kept), dropped), (1, 0))


class ReportSetIntegrity(unittest.TestCase):
    """The defect these pin: a partial run used to leave the canonical report
    set describing two different states at once, and every consumer believed it."""

    def _write(self, directory: Path, tool: str, run_id: str, findings):
        result = qs.ToolResult(tool, "ok", list(findings))
        run = {"run_id": run_id, "generated_at": "2026-01-01T00:00:00+00:00",
               "partial": False, "tools": [tool]}
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{tool}.json").write_text(
            json.dumps(result.to_dict(run)), encoding="utf-8")

    def test_mixed_runs_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            self._write(reports, "import-analysis", "aaaa", [])
            self._write(reports, "radon", "bbbb", [_finding("x.py", "complexity")])
            with self.assertRaises(qt.StaleReportError):
                qt.load_findings(reports)

    def test_one_run_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            self._write(reports, "import-analysis", "aaaa", [])
            self._write(reports, "radon", "aaaa", [_finding("x.py", "complexity")])
            findings, run = qt.load_findings(reports)
            self.assertEqual(len(findings), 1)
            self.assertEqual(run["run_id"], "aaaa")

    def test_unstamped_report_is_refused(self):
        # Written by an older scanner: its age is unknowable, so it cannot be
        # trusted to describe the same state as its stamped neighbours.
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            self._write(reports, "radon", "aaaa", [])
            (reports / "vulture.json").write_text(
                json.dumps({"tool": "vulture", "status": "ok", "findings": []}),
                encoding="utf-8")
            with self.assertRaises(qt.StaleReportError):
                qt.load_findings(reports)

    def test_partial_run_does_not_touch_the_canonical_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            full = [qs.ToolResult("import-analysis", "ok", []),
                    qs.ToolResult("radon", "ok", [_finding("x.py", "complexity")])]
            qs.write_reports(full, [], reports)
            before = (reports / "summary.json").read_text()
            qs.write_reports([qs.ToolResult("import-analysis", "ok", [])], [], reports,
                             partial=True)
            self.assertEqual((reports / "summary.json").read_text(), before)
            self.assertTrue((reports / qs.PARTIAL_SUBDIR / "import-analysis.json").is_file())
            # And the full set still loads as one run.
            findings, _ = qt.load_findings(reports)
            self.assertEqual(len(findings), 1)

    def test_a_full_run_clears_a_report_no_analyser_owns_any_more(self):
        # Nothing removed them, so a report left behind by a renamed or removed
        # analyser — the normal outcome of re-syncing a vendored engine — made
        # every later load fail as a mixed run, *permanently*. The error told the
        # reader to re-run the scanner, which was the one thing that could not
        # clear it. A full run is the whole truth about this directory, so it is
        # the one entitled to say what no longer belongs.
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            qs.write_reports([qs.ToolResult("radon", "ok", [])], [], reports)
            self._write(reports, "oldtool", "deadbeef", [_finding("x.py", "complexity")])
            with self.assertRaises(qt.StaleReportError):
                qt.load_findings(reports)
            qs.write_reports([qs.ToolResult("radon", "ok", [])], [], reports)
            self.assertFalse((reports / "oldtool.json").exists())
            qt.load_findings(reports)  # no longer wedged

    def test_pruning_leaves_the_summary_and_a_foreign_file_alone(self):
        # The baseline lives here and is the one file that must survive; and a
        # file this script does not recognise is not its to delete — doing so
        # would be a worse failure than the wedge being prevented.
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            qs.write_reports([qs.ToolResult("radon", "ok", [])], [], reports)
            (reports / "baseline.json").write_text("{}", encoding="utf-8")
            (reports / "notes.json").write_text('{"mine": true}', encoding="utf-8")
            qs.write_reports([qs.ToolResult("radon", "ok", [])], [], reports)
            for name in ("baseline.json", "summary.json", "hotspots.json", "notes.json"):
                self.assertTrue((reports / name).is_file(), name)

    def test_a_partial_run_prunes_nothing(self):
        # It ran a subset by definition; deleting "what it did not produce" would
        # delete the canonical set the `partial/` split exists to protect.
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            qs.write_reports([qs.ToolResult("radon", "ok", []),
                              qs.ToolResult("vulture", "ok", [])], [], reports)
            qs.write_reports([qs.ToolResult("import-analysis", "ok", [])], [], reports,
                             partial=True)
            self.assertTrue((reports / "radon.json").is_file())
            self.assertTrue((reports / "vulture.json").is_file())

    def test_scan_older_than_the_tree_is_refused(self):
        # Internal consistency is not enough: the normal state between scans is
        # that somebody fixed something, and a stale scan then files a fully
        # evidenced issue against code that no longer exists.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            source = repo / "src" / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            past = {"generated_at": "2000-01-01T00:00:00+00:00"}
            with self.assertRaises(qt.StaleReportError):
                qt.assert_reports_are_current(ctx, past)
            future = {"generated_at": "2100-01-01T00:00:00+00:00"}
            qt.assert_reports_are_current(ctx, future)  # must not raise

    def test_non_source_edits_do_not_invalidate_a_scan(self):
        # A README or fixture under a source root changes no finding. Treating it
        # as staleness makes the check cry wolf, and a check that cries wolf gets
        # worked around instead of heeded.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "a.py").touch()
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            before = qt.newest_source_mtime(ctx)
            note = repo / "src" / "NOTES.md"
            note.touch()
            os.utime(note, (before + 10_000, before + 10_000))
            self.assertEqual(qt.newest_source_mtime(ctx), before)

    def test_sub_second_skew_is_tolerated(self):
        # `generated_at` is truncated to seconds, so a scan can look fractionally
        # older than a file it had already read.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            source = repo / "src" / "a.py"
            source.touch()
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            mtime = source.stat().st_mtime
            qt.assert_reports_are_current(ctx, {"generated_at_ts": mtime - 0.9})
            with self.assertRaises(qt.StaleReportError):
                qt.assert_reports_are_current(
                    ctx, {"generated_at_ts": mtime - qt.CLOCK_TOLERANCE_SECONDS - 1})

    def test_unparseable_timestamp_does_not_block(self):
        # A stamp we cannot read is a reason to stay quiet, not to wedge the
        # command: the mixed-run check already covers the dangerous case.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = qs.Ctx(Path(tmp), qs.load_config(Path(tmp)))
            qt.assert_reports_are_current(ctx, {"generated_at": "not-a-date"})
            qt.assert_reports_are_current(ctx, {})

    def test_partial_update_baseline_is_refused(self):
        # The guard is on coverage, not on which flag produced it: a partial
        # summary written as the baseline deletes the skipped tools' counts, and
        # a tool absent from the baseline is never diffed again.
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["--repo", tmp, "--only", "import-analysis", "--update-baseline"],
                         ["--repo", tmp, "--gate", "structural", "--update-baseline"]):
                with self.assertRaises(SystemExit) as caught:
                    qs.main(argv)
                self.assertEqual(caught.exception.code, 2)  # argparse usage error


class GateHonesty(unittest.TestCase):
    """A gate may only report on what it actually checked."""

    def _repo(self, tmp):
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"python": ["src"]}, "churn": {"enabled": False}}),
            encoding="utf-8")
        return repo

    def _with_broken(self, name, argv):
        original = qs.RUNNERS[name]

        def boom(ctx):
            raise RuntimeError("adapter exploded")

        qs.RUNNERS[name] = boom
        try:
            return qs.main(argv)
        finally:
            qs.RUNNERS[name] = original

    def test_crashed_analyser_fails_the_structural_gate(self):
        # `--gate structural` runs exactly two analysers. If one raises, exiting
        # 0 reports "no new cycles" about a graph that was never built — and an
        # exit code cannot tell the difference.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.assertEqual(qs.main(["--repo", str(repo), "--update-baseline", "--quiet"]), 0)
            code = self._with_broken("import-analysis",
                                     ["--repo", str(repo), "--gate", "structural", "--quiet"])
            self.assertEqual(code, 1)

    def test_crashed_analyser_fails_the_full_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            qs.main(["--repo", str(repo), "--update-baseline", "--quiet"])
            self.assertEqual(
                self._with_broken("import-analysis",
                                  ["--repo", str(repo), "--gate", "all", "--quiet"]), 1)

    def test_crashed_analyser_cannot_bank_a_baseline(self):
        # `summarize` banks no count for an errored tool, and `diff_baseline`
        # skips tools absent from the baseline — so a baseline taken on a broken
        # day makes the gate permanently blind to that tool.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            code = self._with_broken("import-analysis",
                                     ["--repo", str(repo), "--update-baseline", "--quiet"])
            self.assertEqual(code, 1)
            self.assertFalse((repo / qs.DEFAULT_CONFIG["report_dir"] / "baseline.json").is_file())

    def test_skipped_analyser_still_passes_the_gate(self):
        # The counterpart: "not installed" is honest and must stay non-blocking,
        # or every gate run needs the whole toolchain present.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            qs.main(["--repo", str(repo), "--update-baseline", "--quiet"])
            self.assertEqual(qs.main(["--repo", str(repo), "--gate", "all", "--quiet"]), 0)

    def test_write_queue_outside_rolling_mode_is_refused(self):
        # It used to be ignored: a normal hand-off, exit 0, and the user
        # believing the tracker had been updated.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                qt.main(["--repo", tmp, "--write-queue", "--issues-json", tmp])
            self.assertEqual(caught.exception.code, 2)

    def test_unknown_only_is_refused(self):
        # A typo used to select no runner at all: nothing analysed, nothing
        # written, exit 0 — including under `--gate all`.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                qs.main(["--repo", tmp, "--only", "radonn", "--quiet"])
            self.assertEqual(caught.exception.code, 2)


class ScanTargets(unittest.TestCase):
    def test_no_roots_falls_back_to_the_whole_repo(self):
        # A repo with no declared roots is not a repo with no code: jscpd still
        # scans the checkout, so the freshness check has to watch it too.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = qs.Ctx(Path(tmp), qs.load_config(Path(tmp)))
            targets = qs.source_scan_targets(ctx)
            self.assertEqual([root for root, _ in targets], [Path(tmp).resolve()])
            self.assertIn(".py", targets[0][1])
            self.assertIn(".ts", targets[0][1])

    def test_declared_roots_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertEqual(qs.source_scan_targets(ctx),
                             [((repo / "src").resolve(), (".py",))])

    def test_deleted_file_is_caught_even_though_no_mtime_moved(self):
        # An mtime maximum cannot see a deletion: removing a whole file bumps no
        # surviving file's timestamp, so the scan still looks current while
        # describing code that is gone. Deleting the dead module is the most
        # common way a dead-code finding gets fixed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ctx = qs.Ctx(repo, qs.load_config(repo))
            qt.assert_findings_resolve(ctx, [])
            with self.assertRaises(qt.StaleReportError):
                qt.assert_findings_resolve(ctx, [_finding("gone.py", "dead_code")])

    def test_unresolvable_finding_does_not_wedge_triage(self):
        # Re-scanning produces the same unresolvable path, so refusing here would
        # block the whole triage step on a scanner bug — every other tool's good
        # findings with it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ctx = qs.Ctx(repo, qs.load_config(repo))
            qt.assert_findings_resolve(
                ctx, [_finding("src/a.js", "duplicate", unresolved=True)])

    def test_scanner_marks_paths_that_never_resolved(self):
        # Recorded at scan time, where the answer is knowable, and for *every*
        # adapter — not just the one that happens to have a re-rooting helper.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "real.py").touch()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            good = _finding("real.py", "complexity")
            bad = _finding("src/lib.rs", "dead_code")   # e.g. cargo's own cwd
            qs.mark_unresolved(ctx, [qs.ToolResult("t", "ok", [good, bad])])
            self.assertNotIn("unresolved", good)
            self.assertTrue(bad["unresolved"])
            # And triage proceeds: the good finding stays reachable.
            qt.assert_findings_resolve(ctx, [good, bad])

    def test_one_bad_adapter_path_does_not_hide_a_deletion(self):
        # The exemption must not become a blanket one: a finding the scanner
        # *did* resolve, whose file is now gone, still refuses.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ctx = qs.Ctx(repo, qs.load_config(repo))
            with self.assertRaises(qt.StaleReportError):
                qt.assert_findings_resolve(
                    ctx, [_finding("src/a.js", "duplicate", unresolved=True),
                          _finding("gone.py", "dead_code")])

    def test_unrooted_repo_can_still_go_stale(self):
        # Regression: findings existed (jscpd fell back to the checkout) while
        # the freshness check watched the empty root list and never fired, so
        # triage queued a cluster for files that had been deleted.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "a.js").write_text("const x = 1;\n", encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            with self.assertRaises(qt.StaleReportError):
                qt.assert_reports_are_current(ctx, {"generated_at_ts": 0.0})


class KindRestriction(unittest.TestCase):
    """`--kind` has to exist in the engine, not only in the command's docs."""

    def _repo(self, tmp):
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"python": ["src"]}, "churn": {"enabled": False}}),
            encoding="utf-8")
        # The findings below must name real files: triage refuses a report set
        # that points at code which no longer exists.
        (repo / "src" / "c.py").write_text("y = 2\n", encoding="utf-8")
        reports = repo / qs.DEFAULT_CONFIG["report_dir"]
        qs.write_reports([
            qs.ToolResult("import-analysis", "ok", [
                _finding("src/a.py", "circular_import", signature="a -> b")]),
            # Four findings on one file: 4 x 2.0 clears the 6.0 floor, so the
            # unrestricted run has something other than structural to show.
            qs.ToolResult("radon", "ok", [
                _finding("src/c.py", "complexity", line=i) for i in range(4)]),
        ], [], reports)
        (repo / "issues.json").write_text("[]", encoding="utf-8")
        return repo, reports

    def test_every_cluster_kind_is_selectable(self):
        # The choices must cover what `cluster_findings` can actually produce,
        # or a restriction silently excludes a kind that exists.
        self.assertEqual(set(qt.CLUSTER_KINDS), set(qt.FINDING_TO_CLUSTER_KIND.values()))

    def test_restriction_drops_the_other_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo(tmp)
            out = repo / "clusters.json"
            code = qt.main(["--repo", str(repo), "--issues-json", str(repo / "issues.json"),
                            "--json", str(out), "--kind", "structural"])
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["restricted_to_kinds"], ["structural"])
            self.assertEqual({c["kind"] for c in payload["clusters"]}, {"structural"})

    def test_no_restriction_keeps_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo(tmp)
            out = repo / "clusters.json"
            qt.main(["--repo", str(repo), "--issues-json", str(repo / "issues.json"),
                     "--json", str(out)])
            payload = json.loads(out.read_text())
            self.assertIsNone(payload["restricted_to_kinds"])
            self.assertIn("complexity", {c["kind"] for c in payload["clusters"]})


class Config(unittest.TestCase):
    def test_missing_config_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(qs.load_config(Path(tmp))["report_dir"],
                             qs.DEFAULT_CONFIG["report_dir"])

    def test_user_config_merges_per_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / qs.CONFIG_NAME).write_text(
                json.dumps({"thresholds": {"mi_floor": 15}}), encoding="utf-8")
            config = qs.load_config(Path(tmp))
            self.assertEqual(config["thresholds"]["mi_floor"], 15)
            # A partial section must not wipe the rest of it.
            self.assertIn("complexity_rank", config["thresholds"])


class RootScoping(unittest.TestCase):
    """Roots have to mean the same thing for every adapter."""

    def _repo(self, tmp, config):
        repo = Path(tmp)
        (repo / qs.CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
        return repo

    def test_checkout_under_a_tests_directory_still_reports_layering(self):
        # The exemption looked at the *absolute* path, so a checkout living
        # anywhere under a directory called `test`/`tests` — a CI workspace, a
        # scratch clone — exempted every module in the repo. The rule then
        # reported zero, which reads exactly like a clean codebase.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "tests" / "proj"
            (repo / "src" / "core").mkdir(parents=True)
            (repo / "src" / "cli").mkdir(parents=True)
            (repo / "src" / "core" / "a.py").write_text("import src.cli.b\n", encoding="utf-8")
            (repo / "src" / "cli" / "b.py").write_text("x = 1\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"python": ["src"]}, "layers": {"order": ["core", "cli"]}}),
                encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            result = qs.run_import_analysis(ctx)
            self.assertEqual([f["kind"] for f in result.findings], ["layering_violation"])

    def test_two_roots_with_the_same_basename_do_not_collide(self):
        # `root.name` is not an identity: `api/src` and `web/src` produced the
        # same canonical module names, one root overwrote the other in the module
        # table, and a real cycle inside the loser was reported as zero findings.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for side in ("api", "web"):
                (repo / side / "src" / "core").mkdir(parents=True)
            (repo / "api" / "src" / "core" / "a.py").write_text(
                "import core.b\n", encoding="utf-8")
            (repo / "api" / "src" / "core" / "b.py").write_text(
                "import core.a\n", encoding="utf-8")
            (repo / "web" / "src" / "core" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "web" / "src" / "core" / "b.py").write_text("y = 1\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"python": ["api/src", "web/src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            result = qs.run_import_analysis(ctx)
            self.assertEqual(result.metrics["modules"], 4)
            self.assertEqual({f["file"] for f in result.findings},
                             {"api/src/core/a.py", "api/src/core/b.py"})

    def test_a_cycle_through_a_package_init_is_found(self):
        # `pkg/__init__.py` importing its own submodule, which imports back: the
        # commonest real cycle. Resolving the __init__'s relative import against
        # the package's *parent* dropped the edge and reported zero cycles.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src" / "pkg").mkdir(parents=True)
            (repo / "src" / "pkg" / "__init__.py").write_text(
                "from .a import f\nVERSION = 1\n", encoding="utf-8")
            (repo / "src" / "pkg" / "a.py").write_text(
                "from . import VERSION\ndef f(): return VERSION\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"python": ["src"]}}), encoding="utf-8")
            result = qs.run_import_analysis(qs.Ctx(repo, qs.load_config(repo)))
            self.assertEqual({f["file"] for f in result.findings},
                             {"src/pkg/__init__.py", "src/pkg/a.py"})

    def test_an_import_inside_a_function_is_not_a_cycle(self):
        # A lazy import is how a cycle gets *broken*; reporting it as one blames
        # the fix (and fails the structural gate on it).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src" / "pkg").mkdir(parents=True)
            (repo / "src" / "pkg" / "__init__.py").write_text(
                "from .a import f\nVERSION = 1\n", encoding="utf-8")
            (repo / "src" / "pkg" / "a.py").write_text(
                "def f():\n    from . import VERSION\n    return VERSION\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"python": ["src"]}}), encoding="utf-8")
            result = qs.run_import_analysis(qs.Ctx(repo, qs.load_config(repo)))
            self.assertEqual(result.findings, [])

    def test_a_cycle_in_a_later_root_is_not_hidden(self):
        # The mirror of the test above, and the half the root prefix did not
        # reach: the *canonical* names were disambiguated, but the bare aliases
        # they are looked up by ("core.b") stayed in one global first-wins
        # table. So every intra-root import in root two resolved into root one,
        # and a real cycle in the later root reported zero findings — a silent
        # clean from the one analyser that sits on the merge gate.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for side in ("api", "web"):
                (repo / side / "src" / "core").mkdir(parents=True)
            (repo / "api" / "src" / "core" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "api" / "src" / "core" / "b.py").write_text("y = 1\n", encoding="utf-8")
            (repo / "web" / "src" / "core" / "a.py").write_text(
                "import core.b\n", encoding="utf-8")
            (repo / "web" / "src" / "core" / "b.py").write_text(
                "import core.a\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"python": ["api/src", "web/src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            result = qs.run_import_analysis(ctx)
            self.assertEqual({f["file"] for f in result.findings},
                             {"web/src/core/a.py", "web/src/core/b.py"})

    def test_a_cross_root_import_still_resolves(self):
        # Own-root-first must not mean own-root-only: an import naming another
        # root's module by its canonical path is a real edge, and dropping it
        # would trade one silent miss for another.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for side in ("api", "web"):
                (repo / side / "src").mkdir(parents=True)
            (repo / "api" / "src" / "a.py").write_text(
                "import web.src.b\n", encoding="utf-8")
            (repo / "web" / "src" / "b.py").write_text(
                "import api.src.a\n", encoding="utf-8")
            edges, _ = qs.build_python_graph(
                [repo / "api" / "src", repo / "web" / "src"],
                qs.DEFAULT_CONFIG["exclude_dirs"], repo)
            self.assertEqual(len(qs.find_cycles(edges)), 1)

    def test_repo_as_its_own_root_keeps_a_usable_prefix(self):
        # `roots: ["."]` makes the repo-relative prefix empty; the directory name
        # is what stands in for it, or a package `__init__.py` gets no name.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            (repo / "pkg").mkdir()
            (repo / "pkg" / "__init__.py").touch()
            (repo / "pkg" / "a.py").write_text("import pkg.b\n", encoding="utf-8")
            (repo / "pkg" / "b.py").write_text("import pkg.a\n", encoding="utf-8")
            edges, files = qs.build_python_graph([repo], qs.DEFAULT_CONFIG["exclude_dirs"], repo)
            self.assertTrue(all(name for name in files))
            self.assertEqual(len(qs.find_cycles(edges)), 1)
            # ...and not the checkout's directory name, which a CI clone or a
            # worktree does not share with the checkout the baseline was banked in.
            self.assertFalse(any(repo.name in name for name in files))

    def test_findings_outside_the_declared_roots_are_dropped(self):
        # staticcheck and clippy analyse a whole module/workspace by
        # construction, so the roots only mean something if they are applied to
        # the output as well as to the decision to run at all.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, {"roots": {"go": ["internal"]}})
            (repo / "internal").mkdir()
            (repo / "examples").mkdir()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertTrue(qs.under_roots(ctx, "internal/a.go", "go"))
            self.assertFalse(qs.under_roots(ctx, "examples/a.go", "go"))
            # A language with no roots declared constrains nothing.
            self.assertTrue(qs.under_roots(ctx, "examples/a.rs", "rust"))

    def test_only_files_the_freshness_check_watches_are_reported(self):
        # jscpd is handed root directories and picks its own formats, so it
        # reported clones in a README under a source root — a finding no
        # staleness check was watching.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, {"roots": {"python": ["src"]}})
            (repo / "src").mkdir()
            (repo / "src" / "a.py").touch()
            (repo / "src" / "README.md").touch()
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertTrue(qs.is_watched_source(ctx, "src/a.py"))
            self.assertFalse(qs.is_watched_source(ctx, "src/README.md"))
            self.assertFalse(qs.is_watched_source(ctx, "docs/a.py"))


class PartialAnalyserOutput(unittest.TestCase):
    def test_failed_mi_run_banks_no_count(self):
        # `radon cc` succeeding and `radon mi` failing recorded
        # `files_below_mi_floor: 0` — a false zero the gate can never read as a
        # regression, so the MI half went green while it was not being measured.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))

            # A realistic `radon cc` payload: an empty one over a root that has
            # Python files in it is itself a failure now, and would mask what
            # this test is about.
            cc_payload = json.dumps({str(repo / "src" / "a.py"): []})

            def fake_run(cmd, cwd=None, timeout=600):
                if "mi" in cmd:
                    return None                       # radon mi could not run
                return subprocess.CompletedProcess(cmd, 0, stdout=cc_payload, stderr="")

            with _patched(qs, _have_module=lambda name: True, _run=fake_run):
                result = qs.run_radon(ctx)
            self.assertEqual(result.status, "ok")
            self.assertNotIn("files_below_mi_floor", result.metrics)
            self.assertIsNone(qs.summarize([result])["files_below_mi_floor"])
            self.assertIn("radon mi", result.note)


class VacuousGate(unittest.TestCase):
    def test_structural_gate_with_no_structural_analyser_is_refused(self):
        # Both structural analysers are language-specific. On a TypeScript-only
        # repo this gate compared nothing and exited 0 for as long as anyone left
        # it on a merge path.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "web").mkdir()
            (repo / "web" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
            # The node tools are disabled so a host that has them installed
            # cannot fail the baseline this test needs.
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"typescript": ["web"]}, "churn": {"enabled": False},
                 "tools": {"disabled": ["jscpd", "knip", "madge"]}}),
                encoding="utf-8")
            self.assertEqual(qs.main(["--repo", str(repo), "--update-baseline", "--quiet"]), 0)
            self.assertEqual(
                qs.main(["--repo", str(repo), "--gate", "structural", "--quiet"]), 1)

    def test_a_crashed_structural_analyser_reports_the_crash(self):
        # The vacuous-gate message must not swallow the more specific one: a
        # crash is not a configuration problem, and telling the user to declare
        # roots they already have sends them somewhere useless.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"python": ["src"]}, "churn": {"enabled": False}}),
                encoding="utf-8")
            qs.main(["--repo", str(repo), "--update-baseline", "--quiet"])

            def boom(ctx):
                raise RuntimeError("adapter exploded")

            original = qs.RUNNERS["import-analysis"]
            qs.RUNNERS["import-analysis"] = boom
            try:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = qs.main(["--repo", str(repo), "--gate", "structural", "--quiet"])
            finally:
                qs.RUNNERS["import-analysis"] = original
            self.assertEqual(code, 1)
            self.assertIn("analyser failed", err.getvalue())
            self.assertNotIn("pass vacuously", err.getvalue())

    def _one_tool_repo(self, tmp):
        """A Python repo with a banked full baseline, so a gate has something
        to compare against."""
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (repo / qs.CONFIG_NAME).write_text(json.dumps(
            {"roots": {"python": ["src"]}, "churn": {"enabled": False}}),
            encoding="utf-8")
        self.assertEqual(qs.main(["--repo", str(repo), "--update-baseline", "--quiet"]), 0)
        return repo

    def test_a_partial_full_gate_says_what_it_compared(self):
        # `--gate all --only X --quiet` exited 0 having printed **nothing**,
        # having compared one tool of ten: the lost-coverage rule is suspended
        # for a partial run, and `--quiet` suppresses the "partial run" line.
        # A gate that says nothing about the coverage it dropped is the same
        # silent-green shape as one that never compared anything.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._one_tool_repo(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = qs.main(["--repo", str(repo), "--gate", "all",
                                "--only", "import-analysis", "--quiet"])
            self.assertEqual(code, 0)
            self.assertIn("import-analysis", err.getvalue())
            self.assertIn("not being gated", err.getvalue())

    def test_a_partial_full_gate_that_compares_nothing_is_refused(self):
        # The baseline has a count for import-analysis and nothing else, so a
        # run restricted to a tool the baseline never saw compares literally
        # zero things — rule 1 skips tools absent from the baseline.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._one_tool_repo(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = qs.main(["--repo", str(repo), "--gate", "all",
                                "--only", "radon", "--quiet"])
            self.assertEqual(code, 1)
            self.assertIn("compared nothing", err.getvalue())

    def test_a_full_gate_is_not_warned_about(self):
        # The warning is about coverage `--only` dropped. A whole scan drops
        # none, and a gate that cries wolf on every correct run gets bypassed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._one_tool_repo(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = qs.main(["--repo", str(repo), "--gate", "all", "--quiet"])
            self.assertEqual(code, 0)
            self.assertNotIn("not being gated", err.getvalue())

    def test_note_cannot_break_the_report_table(self):
        # A tool note is arbitrary stderr; an unescaped pipe reshapes the table.
        row = qs.render_markdown([qs.ToolResult("t", "error", note="a | b\nc")], [])
        self.assertIn("a \\| b c", row)


class ExcludeDirs(unittest.TestCase):
    def test_user_entries_extend_the_defaults(self):
        # The setup command says "add anything repo-specific here". A plain
        # assignment silently dropped `dist`, `build` and `vendor` from every
        # config whose author took that literally.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / qs.CONFIG_NAME).write_text(
                json.dumps({"exclude_dirs": ["fixtures"]}), encoding="utf-8")
            excluded = qs.load_config(Path(tmp))["exclude_dirs"]
            self.assertIn("fixtures", excluded)
            for default in ("node_modules", "dist", "vendor", "site-packages"):
                self.assertIn(default, excluded)


class UnreadableClusters(unittest.TestCase):
    def _cluster(self, *findings):
        return qt.cluster_findings(list(findings))[0]

    def test_unresolved_flag_reaches_the_handoff(self):
        # The scanner records it so triage does not wedge — but it died at the
        # hand-off, and the installed command's whole method is "open the files".
        cluster = self._cluster(_finding("a.py", "complexity"),
                                _finding("a.py", "maintainability", unresolved=True))
        payload = cluster.to_dict()
        self.assertEqual([f["unresolved"] for f in payload["findings"]], [False, True])
        # ...and a file with one resolved finding on it is still readable.
        self.assertEqual(payload["unresolved_files"], [])

    def test_cluster_with_nothing_readable_is_held_back(self):
        cluster = self._cluster(_finding("gone.py", "complexity", unresolved=True))
        self.assertTrue(cluster.is_unreadable)
        # One resolvable file is enough to make the cluster worth investigating.
        mixed = qt.cluster_findings([_finding("a.py", "duplicate", partner="gone.js"),
                                     _finding("gone.js", "duplicate", partner="a.py",
                                              unresolved=True)])[0]
        self.assertFalse(mixed.is_unreadable)
        self.assertEqual(mixed.unresolved_files, ["gone.js"])


class QueueBody(unittest.TestCase):
    def test_body_is_bounded(self):
        # `tea` passes the body as one argv entry, which Linux caps at 128 KiB.
        clusters = []
        for i in range(400):
            cluster = qt.Cluster(kind=qt.KIND_COMPLEXITY, files=[f"src/f{i}.py"])
            cluster.findings = [_finding(f"src/f{i}.py", "complexity", detail="x" * 200)]
            clusters.append(cluster)
        body = qt.render_queue_body(clusters, len(clusters), 0)
        self.assertLessEqual(len(body), qt.MAX_ISSUE_BODY + 200)
        self.assertIn("truncated", body)


class NodeAdapters(unittest.TestCase):
    """The multi-root wiring, without needing npm."""

    def _repo(self, tmp, roots):
        repo = Path(tmp)
        for rel in roots:
            (repo / rel).mkdir(parents=True)
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"typescript": roots}}), encoding="utf-8")
        return repo

    def test_knip_covers_every_root(self):
        # Running only in roots[0] gave a second root no coverage at all, and
        # said so nowhere in the report.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["api", "web"])
            ctx = qs.Ctx(repo, qs.load_config(repo))
            seen = []

            def fake_run(cmd, cwd=None, timeout=600):
                seen.append(Path(cwd).name)
                payload = {"files": [f"{Path(cwd).name}.ts"], "issues": []}
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload),
                                                   stderr="")

            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("knip"), repo),
                          _run=fake_run):
                result = qs.run_knip(ctx)
            self.assertEqual(sorted(seen), ["api", "web"])
            self.assertEqual(sorted(f["file"] for f in result.findings),
                             ["api/api.ts", "web/web.ts"])
            self.assertEqual(result.metrics["bases"], ["api", "web"])

    def test_knip_6_unused_files_are_reported(self):
        # knip 6 moved unused files from a top-level list into each issue's
        # `files`; reading only the old shape dropped every orphan file.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["web"])
            ctx = qs.Ctx(repo, qs.load_config(repo))
            payload = {"issues": [{"file": "orphan.ts", "files": [{"name": "orphan.ts"}],
                                   "exports": []}]}
            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("knip"), repo),
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(payload),
                                                          stderr="")):
                result = qs.run_knip(ctx)
            self.assertEqual([(f["file"], f["detail"]) for f in result.findings],
                             [("web/orphan.ts", "file is not imported anywhere")])

    def test_knip_tolerates_output_printed_by_the_projects_config(self):
        # knip loads webpack/jest configs, and whatever they print lands on stdout
        # ahead of the report — a strict parse turned that into a permanent error.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["web"])
            ctx = qs.Ctx(repo, qs.load_config(repo))
            # `unlisted` is a dependency that is *used*; it is not dead code.
            payload = {"issues": [{"file": "orphan.ts", "files": [{"name": "orphan.ts"}],
                                   "unlisted": [{"name": "left-pad"}]}]}
            stdout = "Certificates generated in ~/.certs\n{not json either\n" + json.dumps(payload)
            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("knip"), repo),
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr="")):
                result = qs.run_knip(ctx)
            self.assertEqual(result.status, "ok")
            self.assertEqual([f["file"] for f in result.findings], ["web/orphan.ts"])

    def test_knip_runs_from_the_package_json_above_the_root(self):
        # knip will not start without a package.json in its cwd, and the usual
        # layout keeps it above `src/` — so running in the root was an error on
        # nearly every TypeScript repo. It analyses the whole package from
        # there; findings outside the declared roots are dropped.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["src"])
            (repo / "package.json").write_text("{}", encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            seen = []

            def fake_run(cmd, cwd=None, timeout=600):
                seen.append(Path(cwd))
                payload = {"files": ["src/a.ts", "scripts/b.ts"], "issues": []}
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload),
                                                   stderr="")

            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("knip"), repo),
                          _run=fake_run):
                result = qs.run_knip(ctx)
            self.assertEqual(seen, [ctx.repo])
            self.assertEqual([f["file"] for f in result.findings], ["src/a.ts"])

    # knip reports an object, madge an array; anything else is "no usable output".
    _OK_STDOUT = {"run_knip": '{"files": [], "issues": []}', "run_madge": "[]"}

    def _one_failing_root(self, tmp, runner):
        """``runner`` over roots ``api`` (works) and ``web`` (produces nothing)."""
        repo = self._repo(tmp, ["api", "web"])
        ctx = qs.Ctx(repo, qs.load_config(repo))
        good = self._OK_STDOUT[runner.__name__]

        def fake_run(cmd, cwd=None, timeout=600):
            if Path(cwd).name == "web":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            return subprocess.CompletedProcess(cmd, 0, stdout=good, stderr="")

        with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("bin"), repo),
                      _run=fake_run):
            return runner(ctx)

    def test_one_failing_root_does_not_lose_the_others(self):
        # ...but it must not pass silently either: the note and `bases_failed`
        # are the only places a half-scan is visible.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._one_failing_root(tmp, qs.run_madge)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.metrics["bases"], ["api"])
            self.assertEqual(result.metrics["bases_failed"], ["web"])
            self.assertIn("web", result.note)

    def test_a_half_scanned_root_set_banks_no_count(self):
        # knip and madge run once per declared root, so "ok" covers anything
        # from one root to all of them. Banking the count anyway made *breaking*
        # an analyser in one root of a monorepo read as an improvement — counts
        # only ever regress upward, so a smaller number is never questioned.
        # Withholding it routes the tool into the lost-coverage rule instead,
        # which is the only thing that says the gate now compares less.
        for runner in (qs.run_knip, qs.run_madge):
            with self.subTest(runner=runner.__name__), tempfile.TemporaryDirectory() as tmp:
                result = self._one_failing_root(tmp, runner)
                self.assertTrue(result.incomplete)
                self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_a_half_scanned_structural_root_set_banks_no_signatures(self):
        # madge is structural, and `diff_baseline` only ever reports *new*
        # signatures — so cycles vanishing with the root that could not be read
        # is invisible on its own. The tool has to drop out of the summary
        # wholesale for the coverage loss to surface anywhere.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["api", "web"])
            ctx = qs.Ctx(repo, qs.load_config(repo))

            def fake_run(cmd, cwd=None, timeout=600):
                if Path(cwd).name == "web":
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps([["a.ts", "b.ts"]]), stderr="")

            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("madge"), repo),
                          _run=fake_run):
                result = qs.run_madge(ctx)
            self.assertTrue(result.findings)  # the findings are real; they stay
            self.assertEqual(qs.summarize([result])["structural_signatures"], [])

    def test_a_half_scanned_run_cannot_be_banked_as_a_baseline(self):
        # The mirror of the crashed-adapter refusal: an incomplete tool is
        # withheld from the summary, and a tool absent from the baseline is
        # never diffed again — so banking now blinds the gate to it for good.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["api", "web"])
            (repo / qs.CONFIG_NAME).write_text(json.dumps(
                {"roots": {"typescript": ["api", "web"]}, "churn": {"enabled": False},
                 "tools": {"disabled": ["knip", "jscpd"]}}), encoding="utf-8")

            def fake_run(cmd, cwd=None, timeout=600):
                if cwd is not None and Path(cwd).name == "web":
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

            err = io.StringIO()
            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("madge"), repo),
                          _run=fake_run), contextlib.redirect_stderr(err):
                code = qs.main(["--repo", str(repo), "--update-baseline", "--quiet"])
            self.assertEqual(code, 1)
            self.assertIn("did not cover every declared root", err.getvalue())
            self.assertFalse((repo / "reports" / "code-quality" / "baseline.json").exists())

    def test_every_root_failing_is_an_error_not_a_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["api"])
            ctx = qs.Ctx(repo, qs.load_config(repo))
            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("madge"), repo),
                          _run=lambda cmd, cwd=None, timeout=600: None):
                self.assertEqual(qs.run_madge(ctx).status, "error")

    def test_jscpd_drops_a_clone_in_an_unwatched_file(self):
        # A clone between a source file and a README under the same root is a
        # finding the freshness check can never see go stale.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            for name in ("a.py", "b.py", "README.md"):
                (repo / "src" / name).touch()
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))

            def fake_run(cmd, cwd=None, timeout=600):
                out = Path(cmd[cmd.index("--output") + 1])
                duplicates = [
                    {"lines": 20,
                     "firstFile": {"name": str(repo / "src" / "a.py"), "start": 1},
                     "secondFile": {"name": str(repo / "src" / "b.py"), "start": 1}},
                    {"lines": 30,
                     "firstFile": {"name": str(repo / "src" / "a.py"), "start": 1},
                     "secondFile": {"name": str(repo / "src" / "README.md"), "start": 1}},
                ]
                (out / "jscpd-report.json").write_text(
                    json.dumps({"duplicates": duplicates}), encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with _patched(qs, _find_node_bin=lambda ctx, tool: (Path("jscpd"), repo),
                          _run=fake_run):
                result = qs.run_jscpd(ctx)
            self.assertEqual(result.metrics["skipped_unwatched_pairs"], 1)
            self.assertEqual({f["file"] for f in result.findings},
                             {"src/a.py", "src/b.py"})


class AdapterFailuresAreNotZeros(unittest.TestCase):
    """The rule every adapter has to obey: a tool that measured nothing must not
    report that as a clean result. Each of these banked a false zero, and a false
    zero is invisible — the gate can only ever read a count regressing *upward*.
    """

    def _python_repo(self, tmp, files=("a.py",)):
        repo = Path(tmp)
        (repo / "src").mkdir()
        for name in files:
            (repo / "src" / name).write_text("x = 1\n", encoding="utf-8")
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
        return repo, qs.Ctx(repo, qs.load_config(repo))

    def test_radon_reading_nothing_is_an_error(self):
        # radon exits 0 for a path it cannot read and says so only by returning
        # `{}`, so the return code is not the discriminator: covering nothing
        # while there is something to cover is. Banked as `radon: 0`, a gate
        # whose baseline was taken while radon worked passes green ever after.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._python_repo(tmp)
            with _patched(qs, _have_module=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")):
                result = qs.run_radon(ctx)
            self.assertEqual(result.status, "error")
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_radon_reading_nothing_from_an_empty_root_is_fine(self):
        # The mirror: a root with no Python in it *should* come back empty, and
        # calling that a broken tool would make the scan unusable.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            with _patched(qs, _have_module=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")):
                result = qs.run_radon(ctx)
            self.assertEqual(result.status, "ok")

    def test_radon_says_which_files_it_could_not_parse(self):
        # A per-file parse error is coverage this run did not get. It must not
        # withhold the whole count (a repo shipping broken-syntax fixtures could
        # then never bank a baseline), but it must not be silent either.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._python_repo(tmp, files=("a.py", "b.py"))
            payload = json.dumps({
                str(repo / "src" / "a.py"): [],
                str(repo / "src" / "b.py"): {"error": "invalid syntax"},
            })
            mi = json.dumps({str(repo / "src" / "a.py"): {"mi": 100.0}})

            def fake_run(cmd, cwd=None, timeout=600):
                out = mi if "mi" in cmd else payload
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

            with _patched(qs, _have_module=lambda name: True, _run=fake_run):
                result = qs.run_radon(ctx)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.metrics["unparseable_files"], 1)
            self.assertIn("src/b.py", result.note)

    def test_vulture_failure_without_a_parse_error_is_an_error(self):
        # Exit codes are 0 (clean), 3 (found dead code), 1/2 (failed). A failure
        # that is not a per-file parse error must not bank `vulture: 0`.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._python_repo(tmp)
            with _patched(qs, _have_module=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(
                                  cmd, 1, stdout="", stderr="Error: no such file")):
                result = qs.run_vulture(ctx)
            self.assertEqual(result.status, "error")
            self.assertIn("no such file", result.note)
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_a_timed_out_tool_is_an_error_not_skipped(self):
        # `skipped` banks no count and does not block --update-baseline, so a
        # timeout silently produced a baseline without the tool.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._python_repo(tmp)

            def timeout(*args, **kwargs):
                raise subprocess.TimeoutExpired("vulture", 600)

            with _patched(qs, _have_module=lambda name: True), \
                    _patched(qs.subprocess, run=timeout):
                result = qs.run_vulture(ctx)
            self.assertEqual(result.status, "error")
            self.assertIn("timed out", result.note)

    def test_vulture_parse_errors_are_named_not_silent(self):
        # Real vulture (2.16) reports an unparseable file on stderr, skips it and
        # carries on: exit 3 if it found dead code elsewhere, 1 if not. Both must
        # say which files went unread, and neither may withhold the count.
        err = 'src/b.py:3: multiple exception types must be parenthesized at "except A, B:"'
        out = "src/a.py:1: unused variable 'x' (100% confidence)"
        for code, stdout, expected in ((3, out, 1), (1, "", 0)):
            with self.subTest(exit=code), tempfile.TemporaryDirectory() as tmp:
                repo, ctx = self._python_repo(tmp, files=("a.py", "b.py"))
                with _patched(qs, _have_module=lambda name: True,
                              _run=lambda cmd, cwd=None, timeout=600:
                                  subprocess.CompletedProcess(cmd, code, stdout=stdout,
                                                              stderr=err)):
                    result = qs.run_vulture(ctx)
                self.assertEqual(result.status, "ok")
                self.assertEqual(len(result.findings), expected)
                self.assertEqual(result.metrics["unparseable_files"], 1)
                self.assertIn("src/b.py", result.note)

    def test_vulture_findings_exit_code_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._python_repo(tmp)
            out = "src/a.py:1: unused variable 'x' (100% confidence)"
            with _patched(qs, _have_module=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 3, stdout=out, stderr="")):
                result = qs.run_vulture(ctx)
            self.assertEqual(result.status, "ok")
            self.assertEqual(len(result.findings), 1)

    def _go_repo(self, tmp, roots):
        repo = Path(tmp)
        for rel in roots:
            (repo / rel).mkdir(parents=True, exist_ok=True)
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"go": roots}}), encoding="utf-8")
        return repo, qs.Ctx(repo, qs.load_config(repo))

    def test_gocyclo_half_scanned_root_set_banks_no_count(self):
        # Same per-root rule knip and madge follow: `continue` on a root that
        # could not be run banked a full count for a partial scan.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["api", "web"])

            def fake_run(cmd, cwd=None, timeout=600):
                if cmd[-1].endswith("web"):
                    return None
                return subprocess.CompletedProcess(
                    cmd, 1, stdout=f"20 pkg Fn {repo}/api/x.go:3:1\n", stderr="")

            with _patched(qs, _have_binary=lambda name: True, _run=fake_run):
                result = qs.run_gocyclo(ctx)
            self.assertEqual(result.status, "ok")      # its findings are real
            self.assertEqual(len(result.findings), 1)
            self.assertTrue(result.incomplete)         # its count is not comparable
            self.assertEqual(result.metrics["bases_failed"], ["web"])
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_gocyclo_over_threshold_exit_is_not_a_failure(self):
        # gocyclo exits 1 precisely when something is over the threshold.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["api"])
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(
                                  cmd, 1, stdout=f"20 pkg Fn {repo}/api/x.go:3:1\n",
                                  stderr="")):
                result = qs.run_gocyclo(ctx)
            self.assertEqual(result.status, "ok")
            self.assertFalse(result.incomplete)
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {"gocyclo": 1})

    def test_gocyclo_every_root_failing_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["api"])
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600: None):
                self.assertEqual(qs.run_gocyclo(ctx).status, "error")

    def test_gocyclo_failing_on_a_path_is_not_a_clean_zero(self):
        # gocyclo exits 1 for "something is over the threshold" **and** for
        # "I could not read that path", so the exit code cannot tell them
        # apart. Reading it alone, an installed gocyclo pointed at an
        # unreadable root came back `ok` with zero findings — bankable as a
        # baseline, and afterwards only ever readable as an improvement.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["api"])
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(
                                  cmd, 1, stdout="",
                                  stderr="open api: no such file or directory")):
                result = qs.run_gocyclo(ctx)
            self.assertEqual(result.status, "error")
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_gocyclo_one_root_failing_on_a_path_banks_no_count(self):
        # The same discriminator per root: one unreadable root out of two makes
        # the count cover less than it claims, exactly as a vanished binary does.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["api", "web"])

            def fake_run(cmd, cwd=None, timeout=600):
                if cmd[-1].endswith("web"):
                    return subprocess.CompletedProcess(cmd, 1, stdout="",
                                                       stderr="cannot read web")
                return subprocess.CompletedProcess(
                    cmd, 1, stdout=f"20 pkg Fn {repo}/api/x.go:3:1\n", stderr="")

            with _patched(qs, _have_binary=lambda name: True, _run=fake_run):
                result = qs.run_gocyclo(ctx)
            self.assertEqual(result.status, "ok")      # api's finding is real
            self.assertEqual(len(result.findings), 1)
            self.assertTrue(result.incomplete)
            self.assertEqual(result.metrics["bases_failed"], ["web"])
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_gocyclo_clean_run_is_a_real_zero(self):
        # Exit 0 means "nothing over the threshold", which is a measurement.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["api"])
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")):
                result = qs.run_gocyclo(ctx)
            self.assertEqual(result.status, "ok")
            self.assertFalse(result.incomplete)
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {"gocyclo": 0})

    def test_staticcheck_compile_error_is_not_a_dead_code_finding(self):
        # A module that does not build reports `{"code": "compile", ...}`. Turned
        # into a finding it clusters, ranks and reaches the triage queue as
        # "unused code: syntax error"; ignored, it banks `staticcheck: 0`.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["."])
            entry = json.dumps({"code": "compile", "severity": "error",
                                "message": "syntax error: unexpected }",
                                "location": {"file": str(repo / "main.go"), "line": 7}})
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 1, stdout=entry + "\n",
                                                          stderr="")):
                result = qs.run_staticcheck(ctx)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.findings, [])
            self.assertIn("compile", result.note)
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_staticcheck_build_failure_with_no_output_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["."])
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 1, stdout="",
                                                          stderr="build failed")):
                result = qs.run_staticcheck(ctx)
            self.assertEqual(result.status, "error")
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def _rust_repo(self, tmp):
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"rust": ["src"]}}), encoding="utf-8")
        return repo, qs.Ctx(repo, qs.load_config(repo))

    @staticmethod
    def _clippy_stream(*entries):
        return "\n".join(json.dumps(e) for e in entries) + "\n"

    def test_clippy_build_failure_after_findings_banks_no_count(self):
        # The old discriminator was `not findings and returncode and "error" in
        # stderr`. In a workspace where one crate lints cleanly and the next
        # fails to compile, `findings` is non-empty — so the failure was never
        # noticed and a partial count was banked as a full one. Counts only ever
        # regress upward, so nothing downstream would question the smaller one.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._rust_repo(tmp)
            stream = self._clippy_stream(
                {"reason": "compiler-message", "message": {
                    "level": "warning", "code": {"code": "dead_code"},
                    "message": "function is never used",
                    "spans": [{"is_primary": True,
                               "file_name": str(repo / "src" / "a.rs"),
                               "line_start": 3}]}},
                {"reason": "compiler-message", "message": {
                    "level": "error", "message": "cannot find value `x` in this scope",
                    "spans": []}},
                {"reason": "build-finished", "success": False})
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=900:
                              subprocess.CompletedProcess(cmd, 101, stdout=stream,
                                                          stderr="error: could not compile")):
                result = qs.run_clippy(ctx)
            self.assertEqual(result.status, "error")
            self.assertIn("cannot find value", result.note)
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_clippy_clean_run_is_a_real_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._rust_repo(tmp)
            stream = self._clippy_stream({"reason": "build-finished", "success": True})
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=900:
                              subprocess.CompletedProcess(cmd, 0, stdout=stream, stderr="")):
                result = qs.run_clippy(ctx)
            self.assertEqual(result.status, "ok")
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {"clippy": 0})

    def test_clippy_never_starting_is_an_error_not_a_zero(self):
        # No `build-finished` at all and a failure exit: cargo never got as far
        # as running the check, so nothing knows what clippy would have said.
        # The old test for this leaned on the English word "error" appearing in
        # stderr, which the tool is free to reword.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._rust_repo(tmp)
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=900:
                              subprocess.CompletedProcess(cmd, 101, stdout="",
                                                          stderr="failed to select a version")):
                result = qs.run_clippy(ctx)
            self.assertEqual(result.status, "error")
            self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_clippy_component_not_installed_is_skipped(self):
        # `cargo` exists, the clippy component does not. That is "not
        # installed", which banks no count *and* does not fail a gate — an
        # `error` here would turn a repo that simply never added clippy red.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._rust_repo(tmp)
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=900:
                              subprocess.CompletedProcess(
                                  cmd, 101, stdout="",
                                  stderr="error: no such command: `clippy`")):
                result = qs.run_clippy(ctx)
            self.assertEqual(result.status, "skipped")
            self.assertIn("rustup component add clippy", result.note)

    def test_staticcheck_clean_run_is_a_real_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._go_repo(tmp, ["."])
            with _patched(qs, _have_binary=lambda name: True,
                          _run=lambda cmd, cwd=None, timeout=600:
                              subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")):
                result = qs.run_staticcheck(ctx)
            self.assertEqual(result.status, "ok")
            self.assertEqual(qs.summarize([result])["tool_finding_counts"],
                             {"staticcheck": 0})


class DeclaredRootsThatDoNotExist(unittest.TestCase):
    """A typo'd root produced a clean report — the most dangerous output here."""

    def _repo(self, tmp):
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("import src.b\n", encoding="utf-8")
        (repo / "src" / "b.py").write_text("import src.a\n", encoding="utf-8")
        (repo / qs.CONFIG_NAME).write_text(              # "scr" is the typo
            json.dumps({"roots": {"python": ["scr"]}}), encoding="utf-8")
        return repo

    def test_skip_note_distinguishes_absent_from_undeclared(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            ctx = qs.Ctx(repo, qs.load_config(repo))
            result = qs.run_import_analysis(ctx)
            self.assertEqual(result.status, "skipped")
            self.assertIn("scr", result.note)
            self.assertIn("do not exist", result.note)

    def test_undeclared_language_still_reads_as_undeclared(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / qs.CONFIG_NAME).write_text("{}", encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            self.assertEqual(ctx.no_roots_note("python"), "no python roots configured")
            self.assertEqual(ctx.all_missing_roots(), {})

    def test_a_missing_root_is_reported_and_cannot_be_banked(self):
        # Every adapter for the language skips, so the baseline has no entry for
        # any of them — and a tool absent from the baseline is skipped in the
        # diff entirely. Banking here makes the gate blind to a whole language.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = qs.main(["--repo", str(repo), "--update-baseline", "--quiet"])
            self.assertEqual(code, 1)
            self.assertIn("scr", err.getvalue())
            self.assertFalse((repo / "reports" / "code-quality" / "baseline.json").exists())


class KindRestrictionAndTheCap(unittest.TestCase):
    """`--top` says how much work to queue; the per-kind cap says how to spread
    it across kinds. Restricted to one kind there is nothing to spread."""

    def _dead_code(self, n, score=90.0):
        return [qt.Cluster(kind=qt.KIND_DEAD_CODE, files=[f"d{i}.py"],
                           findings=[_finding(f"d{i}.py", "dead_code")],
                           score=score - i) for i in range(n)]

    def test_the_cap_does_not_leave_slots_empty(self):
        # `--top 8` with only one kind on offer queued three clusters and left
        # five slots empty: the cap arbitrates between kinds, and there were none.
        queued, _ = qt.select(self._dead_code(6), set(), top=8, min_score=6.0)
        self.assertEqual(len(queued), 6)

    def _repo_with_dead_code(self, tmp, files):
        """A scanned repo whose reports hold one dead-code cluster per file."""
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / qs.CONFIG_NAME).write_text(
            json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
        reports = repo / "reports" / "code-quality"
        reports.mkdir(parents=True)
        findings = []
        for name in files:
            (repo / "src" / name).write_text("x = 1\n", encoding="utf-8")
            # Five findings apiece: dead code is weighted 1.5, so a single one
            # would sit under the score floor and never compete for a slot.
            findings += [_finding(f"src/{name}", "dead_code") for _ in range(5)]
        run = {"run_id": "r1", "generated_at": "2030-01-01T00:00:00+00:00",
               "generated_at_ts": 1893456000.0, "partial": False, "tools": ["t"]}
        (reports / "t.json").write_text(json.dumps(
            {"tool": "t", "status": "ok", "note": "", "incomplete": False,
             "metrics": {}, "run": run, "findings": findings}), encoding="utf-8")
        (repo / "issues.json").write_text("[]", encoding="utf-8")
        return repo

    def _triage(self, repo, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = qt.main(["--repo", str(repo), "--issues-json", "issues.json",
                            "--json", str(repo / "clusters.json"), *extra])
        self.assertEqual(code, 0)
        return out.getvalue(), json.loads((repo / "clusters.json").read_text())

    def test_a_single_kind_restriction_lifts_the_cap_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_dead_code(tmp, [f"d{i}.py" for i in range(5)])
            printed, handoff = self._triage(repo, "--kind", "dead_code", "--top", "5")
            self.assertEqual(len(handoff["clusters"]), 5)
            self.assertNotIn("held back", printed)

    def test_an_unrestricted_run_fills_top_from_one_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_dead_code(tmp, [f"d{i}.py" for i in range(5)])
            printed, handoff = self._triage(repo, "--top", "5")
            self.assertEqual(len(handoff["clusters"]), 5)
            self.assertNotIn("held back", printed)

    def test_the_restriction_is_applied_before_anything_is_counted(self):
        # The unreadable count sat beside a restricted queue while being computed
        # over every kind, so a `--kind structural` run was told clusters had
        # been held back from it that it would never have considered.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo / qs.CONFIG_NAME).write_text(
                json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
            reports = repo / "reports" / "code-quality"
            reports.mkdir(parents=True)
            run = {"run_id": "r1", "generated_at": "2030-01-01T00:00:00+00:00",
                   "generated_at_ts": 1893456000.0, "partial": False, "tools": ["t"]}
            # One dead-code cluster whose only file never resolved: unreadable,
            # and of a kind a structural-only run never asks for.
            gone = _finding("src/vanished.py", "dead_code")
            gone["unresolved"] = True
            (reports / "t.json").write_text(json.dumps(
                {"tool": "t", "status": "ok", "note": "", "incomplete": False,
                 "metrics": {}, "run": run, "findings": [gone]}), encoding="utf-8")
            (repo / "issues.json").write_text("[]", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = qt.main(["--repo", str(repo), "--kind", "structural",
                                "--issues-json", "issues.json",
                                "--json", str(repo / "clusters.json")])
            self.assertEqual(code, 0)
            self.assertNotIn("unreadable", out.getvalue())
            handoff = json.loads((repo / "clusters.json").read_text())
            self.assertEqual(handoff["totals"]["unreadable"], 0)


class ContradictoryFlags(unittest.TestCase):
    def test_update_baseline_and_gate_are_refused_together(self):
        # `--update-baseline` returned before the comparison ran, so
        # `--gate all --update-baseline` was a gate that could not fail and that
        # rewrote the thing it was meant to compare against.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / qs.CONFIG_NAME).write_text("{}", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    qs.main(["--repo", str(repo), "--gate", "all",
                             "--update-baseline", "--quiet"])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("contradictory", err.getvalue())
            # `--gate structural` is refused one rule earlier, for covering less
            # than a baseline may describe; either way it never banks one.
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    qs.main(["--repo", str(repo), "--gate", "structural",
                             "--update-baseline", "--quiet"])
            self.assertIn("needs a full scan", err.getvalue())


class IssueCorpusPath(unittest.TestCase):
    def test_relative_issues_json_resolves_against_the_repo(self):
        # The installed command writes `.quality-issues.json` at the repo root.
        # Resolved against the *cwd*, `--repo elsewhere` read no corpus at all —
        # and a corpus that is merely absent marks every cluster "not tracked"
        # and re-files work that already exists.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            repo = _repo_with_one_cluster(tmp)
            # The marker of whatever the scan actually produced: hand-building
            # one from a guessed file list tests the guess, not the path.
            first = json.loads((repo / ".quality-clusters.json").read_text())
            marker = first["clusters"][0]["marker"]
            (repo / "issues.json").write_text(
                json.dumps([{"number": 1, "title": "tracked", "body": marker}]),
                encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(elsewhere)                       # a different cwd on purpose
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = qt.main(["--repo", str(repo), "--issues-json", "issues.json",
                                    "--json", str(Path(elsewhere) / "clusters.json")])
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)
            self.assertIn("1 already tracked", out.getvalue())


def _repo_with_one_cluster(tmp):
    """A repo with one structural finding already scanned into its reports."""
    repo = Path(tmp)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("import src.b\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("import src.a\n", encoding="utf-8")
    (repo / qs.CONFIG_NAME).write_text(
        json.dumps({"roots": {"python": ["src"]}}), encoding="utf-8")
    qs.main(["--repo", str(repo), "--quiet"])
    # One triage pass against an empty corpus, so a caller can read the marker
    # the engine actually minted rather than hand-building one from a guess.
    (repo / "empty.json").write_text("[]", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        qt.main(["--repo", str(repo), "--issues-json", str(repo / "empty.json")])
    return repo


class Dismissals(unittest.TestCase):
    """A rejected cluster has no issue to carry its marker, so without a record
    it came back at the top of every queue and was re-read every run. The
    record must lapse when the code gets worse, or it becomes a blind spot."""

    def _cluster(self, name, base, churned=None, kind=None):
        cluster = qt.Cluster(kind=kind or qt.KIND_COMPLEXITY, files=[name],
                             findings=[_finding(name, "complexity")],
                             score=churned if churned is not None else base)
        cluster.base_score = base
        return cluster

    def _entry(self, cluster, base, kind=None):
        return {cluster.fingerprint: {"fingerprint": cluster.fingerprint,
                                      "kind": kind or cluster.kind,
                                      "base_score": base, "reason": "generated code"}}

    def test_dismissed_cluster_does_not_take_a_slot(self):
        # Filtered before the cut-off: a dismissed cluster ranked above a fresh
        # one would otherwise starve it on every run, as tracked ones would.
        loud, quiet = self._cluster("loud.py", 50.0), self._cluster("quiet.py", 10.0)
        kept, dismissed = qt.apply_dismissals([loud, quiet], self._entry(loud, 50.0))
        queued, _ = qt.select(kept, set(), top=1, min_score=6.0)
        self.assertEqual([c.files for c in queued], [["quiet.py"]])
        self.assertEqual(dismissed, [loud])

    def test_churn_alone_does_not_lapse_a_dismissal(self):
        # The churn multiplier grows with every commit; comparing the churned
        # score would bring back code that did not change at all.
        cluster = self._cluster("c.py", 10.0, churned=25.0)
        kept, dismissed = qt.apply_dismissals([cluster], self._entry(cluster, 10.0))
        self.assertEqual((kept, dismissed), ([], [cluster]))

    def test_worse_code_resurfaces_with_the_old_reason(self):
        cluster = self._cluster("c.py", 14.0)
        kept, dismissed = qt.apply_dismissals([cluster], self._entry(cluster, 10.0))
        self.assertEqual((kept, dismissed), ([cluster], []))
        self.assertEqual(cluster.to_dict()["previously_dismissed"]["reason"], "generated code")

    def test_float_noise_does_not_lapse_a_dismissal(self):
        cluster = self._cluster("c.py", 0.1 + 0.2)
        kept, _ = qt.apply_dismissals([cluster], self._entry(cluster, 0.3))
        self.assertEqual(kept, [])

    def test_a_changed_file_set_is_a_new_cluster(self):
        pair = qt.Cluster(kind=qt.KIND_DUPLICATION, files=["a.py", "b.py"])
        trio = qt.Cluster(kind=qt.KIND_DUPLICATION, files=["a.py", "b.py", "c.py"])
        kept, _ = qt.apply_dismissals([trio], self._entry(pair, 99.0))
        self.assertEqual(kept, [trio])

    def test_stale_entries_are_reported_only_for_kinds_on_offer(self):
        here, gone = self._cluster("here.py", 5.0), self._cluster("gone.py", 5.0)
        entries = {**self._entry(here, 5.0), **self._entry(gone, 5.0)}
        self.assertEqual(qt.stale_dismissals([here], entries, qt.CLUSTER_KINDS),
                         [gone.fingerprint])
        # A `--kind structural` run never saw the complexity clusters.
        self.assertEqual(qt.stale_dismissals([], entries, [qt.KIND_STRUCTURAL]), [])

    def test_an_unexplained_or_malformed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertEqual(qt.load_dismissed(repo), {})
            path = repo / qt.DISMISSED_FILE
            path.write_text(json.dumps([{"fingerprint": "ab", "base_score": 1.0,
                                         "reason": " "}]), encoding="utf-8")
            with self.assertRaises(qt.DismissalError):
                qt.load_dismissed(repo)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(qt.DismissalError):
                qt.load_dismissed(repo)
            # A misspelt kind would never be reported stale, so it is refused.
            path.write_text(json.dumps([{"fingerprint": "ab", "kind": "dead-code",
                                         "base_score": 1.0, "reason": "x"}]),
                            encoding="utf-8")
            with self.assertRaises(qt.DismissalError):
                qt.load_dismissed(repo)

    def test_stale_note_names_the_analysers_that_did_not_run(self):
        # A missing tool makes every dismissal of its kind look stale; without
        # naming it, the triager deletes valid dismissals from a committed file.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo_with_one_cluster(tmp)
            (repo / qt.DISMISSED_FILE).write_text(json.dumps([{
                "fingerprint": "0123456789abcdef", "kind": qt.KIND_DUPLICATION,
                "base_score": 9.0, "reason": "generated"}]), encoding="utf-8")
            reports = repo / "reports" / "code-quality"
            stamp = json.loads((reports / "import-analysis.json").read_text())["run"]
            (reports / "jscpd.json").write_text(json.dumps(
                {"tool": "jscpd", "status": "skipped", "run": stamp, "findings": []}),
                encoding="utf-8")
            (reports / "switched-off.json").write_text(json.dumps(
                {"tool": "switched-off", "status": "skipped", "note": qs.DISABLED_NOTE,
                 "run": stamp, "findings": []}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qt.main(["--repo", str(repo), "--issues-json", str(repo / "empty.json")])
            self.assertIn("1 stale dismissal(s)", out.getvalue())
            note = out.getvalue()
            self.assertIn("did not run in this scan:", note)
            self.assertIn("jscpd (skipped)", note)
            # Languages this repo does not use, and tools it switched off, are
            # not missing tools.
            for tool in ("clippy", "detekt", "gocyclo", "go-layering", "knip", "madge",
                         "switched-off"):
                self.assertNotIn(tool, note)

    def test_dismiss_round_trip_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo_with_one_cluster(tmp)
            handoff = json.loads((repo / ".quality-clusters.json").read_text())
            marker = handoff["clusters"][0]["marker"]
            empty = str(repo / "empty.json")
            with contextlib.redirect_stdout(io.StringIO()):
                code = qt.main(["--repo", str(repo), "--dismiss", marker,
                                "--reason", "intentional plugin cycle"])
            self.assertEqual(code, 0)
            [entry] = json.loads((repo / qt.DISMISSED_FILE).read_text())
            self.assertEqual(entry["reason"], "intentional plugin cycle")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = qt.main(["--repo", str(repo), "--issues-json", empty])
            self.assertEqual(code, 0)
            self.assertIn("0 queued · 1 dismissed", out.getvalue())

    def test_only_a_queued_cluster_can_be_dismissed(self):
        # Dismissing from a guess records a score nobody read the code at.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo_with_one_cluster(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = qt.main(["--repo", str(repo), "--dismiss", "0123456789abcdef",
                                "--reason", "x"])
            self.assertEqual(code, 1)
            self.assertFalse((repo / qt.DISMISSED_FILE).exists())

    def test_dismiss_needs_a_reason(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            qt.main(["--dismiss", "0123456789abcdef"])


class DetektAdapter(unittest.TestCase):
    """detekt reports far more than maintainability, and its exit codes overload
    "found something" with "broke". Both are silent failure modes: the first
    fills the queue with style noise nobody will read, the second banks a zero.
    """

    REPORT = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<checkstyle version="4.3">\n'
        '<file name="app/Big.kt">\n'
        '  <error line="12" column="9" severity="warning" message="too long"'
        ' source="detekt.LongMethod" />\n'
        '  <error line="12" column="9" severity="warning" message="cc 31"'
        ' source="detekt.CyclomaticComplexMethod" />\n'
        '  <error line="3" column="7" severity="warning" message="35 functions"'
        ' source="detekt.TooManyFunctions" />\n'
        '  <error line="88" column="4" severity="warning" message="magic number"'
        ' source="detekt.MagicNumber" />\n'
        '  <error line="90" column="4" severity="warning" message="line too long"'
        ' source="detekt.MaxLineLength" />\n'
        '  <error line="91" column="4" severity="warning" message="too many returns"'
        ' source="detekt.ReturnCount" />\n'
        '</file>\n'
        '</checkstyle>\n'
    )

    def _repo(self, tmp, config=None):
        repo = Path(tmp)
        (repo / "app").mkdir()
        (repo / "app" / "Big.kt").write_text("class Big\n", encoding="utf-8")
        cfg = {"roots": {"kotlin": ["app"]}}
        if config is not None:
            cfg["thresholds"] = {"detekt_config": config}
        (repo / qs.CONFIG_NAME).write_text(json.dumps(cfg), encoding="utf-8")
        return repo, qs.Ctx(repo, qs.load_config(repo))

    def _fake_run(self, returncode=2, report=None, stderr=""):
        """Stand in for the detekt binary, writing the report it was asked for."""
        body = self.REPORT if report is None else report

        def run(cmd, cwd=None, timeout=600):
            if body is not None:
                target = next(a.split(":", 1)[1] for a in cmd if a.startswith("xml:"))
                Path(target).write_text(body, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)
        return run

    def test_only_maintainability_rules_reach_the_queue(self):
        # MagicNumber/MaxLineLength/ReturnCount are 88% of a real run on a DSP
        # codebase. Letting them through buries the findings worth acting on.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp)
            with _patched(qs, _have_binary=lambda name: True, _run=self._fake_run()):
                result = qs.run_detekt(ctx)
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            sorted(f["kind"] for f in result.findings),
            ["complexity", "complexity", "maintainability"])
        self.assertEqual(result.metrics["dropped_out_of_scope"], 3)

    def test_rules_map_onto_the_engines_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp)
            with _patched(qs, _have_binary=lambda name: True, _run=self._fake_run()):
                result = qs.run_detekt(ctx)
        by_rule = {f["detail"].split(":")[0]: f for f in result.findings}
        self.assertEqual(by_rule["LongMethod"]["kind"], "complexity")
        self.assertEqual(by_rule["TooManyFunctions"]["kind"], "maintainability")
        self.assertEqual(by_rule["LongMethod"]["line"], 12)
        self.assertEqual(by_rule["LongMethod"]["file"], "app/Big.kt")

    def test_exit_2_means_findings_not_failure(self):
        # detekt exits 2 whenever it found anything at all. Treating that as an
        # error makes every repo with a single finding unscannable.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp)
            with _patched(qs, _have_binary=lambda name: True,
                          _run=self._fake_run(returncode=2)):
                result = qs.run_detekt(ctx)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.findings)

    def test_a_crash_is_an_error_not_a_zero(self):
        # Exit 1 is a crash and 3 is a bad config. Either banked as `detekt: 0`
        # is a false clean the gate can never read as a regression.
        for code in (1, 3):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                repo, ctx = self._repo(tmp)
                with _patched(qs, _have_binary=lambda name: True,
                              _run=self._fake_run(returncode=code, report=None,
                                                  stderr="boom")):
                    result = qs.run_detekt(ctx)
                self.assertEqual(result.status, "error")
                self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_a_missing_report_is_an_error(self):
        # Ran, exited 0, wrote nothing: indistinguishable from a clean repo
        # except by the absent artefact.
        def run(cmd, cwd=None, timeout=600):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp)
            with _patched(qs, _have_binary=lambda name: True, _run=run):
                result = qs.run_detekt(ctx)
        self.assertEqual(result.status, "error")
        self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_a_declared_config_that_does_not_exist_is_refused(self):
        # Falling back to detekt's defaults would file issues against thresholds
        # the project explicitly replaced, and say nothing about having done so.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp, config="config/detekt.yml")
            with _patched(qs, _have_binary=lambda name: True, _run=self._fake_run()):
                result = qs.run_detekt(ctx)
        self.assertEqual(result.status, "error")
        self.assertIn("detekt.yml", result.note)

    def test_a_projects_own_config_is_used_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp)
            (repo / "detekt.yml").write_text("build:\n", encoding="utf-8")
            seen = {}

            def run(cmd, cwd=None, timeout=600):
                seen["cmd"] = cmd
                target = next(a.split(":", 1)[1] for a in cmd if a.startswith("xml:"))
                Path(target).write_text(self.REPORT, encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="")

            with _patched(qs, _have_binary=lambda name: True, _run=run):
                result = qs.run_detekt(ctx)
        self.assertEqual(result.status, "ok")
        self.assertIn("--config", seen["cmd"])
        self.assertIn("--build-upon-default-config", seen["cmd"])
        self.assertEqual(result.metrics["config"], "detekt.yml")

    def test_no_kotlin_roots_skips_rather_than_zeroes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / qs.CONFIG_NAME).write_text(json.dumps({"roots": {}}), encoding="utf-8")
            ctx = qs.Ctx(repo, qs.load_config(repo))
            with _patched(qs, _have_binary=lambda name: True, _run=self._fake_run()):
                result = qs.run_detekt(ctx)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(qs.summarize([result])["tool_finding_counts"], {})

    def test_kotlin_sources_are_watched_for_staleness(self):
        # jscpd can report a clone in a .kt file once kotlin is a declared root.
        # If the suffix is not watched, triage queues a cluster that nothing
        # invalidates when the code behind it changes.
        with tempfile.TemporaryDirectory() as tmp:
            repo, ctx = self._repo(tmp)
            self.assertTrue(qs.is_watched_source(ctx, "app/Big.kt"))
            self.assertIn(".kt", qs.SOURCE_SUFFIXES["kotlin"])


class _patched:
    """Swap module attributes for the duration of a block."""

    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.saved[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self.module

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(self.module, name, value)
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
