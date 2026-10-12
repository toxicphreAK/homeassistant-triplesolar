---
description: "Investigate ranked maintainability clusters and file analysed refactoring issues"
argument-hint: '[--top 8]  ·  +execute  ·  +rescan  ·  +kind:structural  ·  +queue  ·  +no-ask'
allowed-tools: Bash, Read, Grep, Glob
---

<!--
INSTALLED BY /quality-setup. Project-local on purpose: it names this repo's
engine path, tracker and labels, which no other repo shares.

Placeholders the installer substitutes. Every one of them appears in the body
below — if any is left in this file, the install is incomplete.

The names are written WITHOUT their braces here on purpose, and no brace pair
appears anywhere in this comment. Two reasons, and both of them bit:
a substitution pass that rewrites this list too turns the record of what was
substituted into gibberish ("python3 interpreter that can import..."); and the
installer's leftover-placeholder check greps this file for the placeholder
shape, so a comment that merely *talks about* that shape reports itself as an
unsubstituted placeholder in every correct install.

  PY          interpreter that can import this repo's dev tooling
  ENGINE      repo-relative path to tools/quality/
  REPORTS     repo-relative report_dir from .quality.json
  TRACKER     gh | tea | devpilot  (named in step 0 and step 4)
  LIST_CMD    the exact call that dumps open issues as
              [{number,title,body}] to .quality-issues.json — MCP trackers
              only; for gh/tea the engine lists them itself, and step 0 is
              deleted at install time
  DEDUP_ARG   `--backend gh` / `--backend tea`, or
              `--issues-json .quality-issues.json` for an MCP tracker.
              Goes on EVERY quality_triage.py call.
  FILE_CMD    the exact command/tool call that files one issue
  LABELS      labels to put on a filed refactoring issue
  QUEUE_LABEL ONE existing label, for the rolling queue issue's --label
-->

Arguments: **`$ARGUMENTS`**

**GitHub account:** every `gh` call here (directly or through `quality_triage.py`) runs as
`toxicphreAK` via `GH_TOKEN=$(gh auth token --user toxicphreAK)`, whatever account `gh` has
active. Keep that prefix on every command.

You are turning ranked maintainability clusters into **analysed** refactoring
issues. The engine has already done the deterministic half — cluster, fingerprint,
dedup, rank, cut off. Your half is the part a script cannot do: read the code, find
out *why* the duplication or complexity is there, and write a work order somebody
(likely `/implement`) can execute.

## Ground rules

- **This command does not file tool output.** A cluster is evidence that something
  is wrong, not a description of what is wrong. If you file "3 files contain a
  47-line clone", you have moved the firehose one level up and helped nobody.
- **Never file a cluster you have not read the code for.** An unread cluster belongs
  in the queue, not in an issue. Read the actual files at the actual lines — the
  tool's `detail` string is a pointer, not a finding.
- **Consolidate ruthlessly.** Clusters that share one root cause are **one** issue
  carrying **all** their markers. The same duplicated modal boilerplate showing up
  as two clone components is one copy-paste to undo.
- **Discard false positives out loud, and record them.** A barrel file's "unused"
  exports, a plugin interface's unused parameters, generated code. Say what you
  dismissed and why in the report, not in an issue, and record it with `--dismiss`
  (step 4) so the next run does not queue it again. Dismiss only what you read and
  judged. A dismissal used to clear a queue you did not get through hides code
  that nobody looked at.
- **Read-only with respect to the codebase.** This command files work; it does not
  do it. Never refactor here — that is the filed issue's job, and doing it now
  produces an unreviewed diff nobody asked for.
- **A refactor with no test is a rewrite.** Behaviour preservation has to be
  checkable *before* the refactor starts. If nothing covers the code, the issue's
  first acceptance criterion is a characterization test, not the refactor.

## Flags

| Flag | Effect |
|---|---|
| `+execute` | file the analysed issues. Without it this is a preview and nothing is written. |
| `+rescan` | re-run the scanner first |
| `+kind:<k>` | restrict to one cluster kind — pass through as `--kind <k>`. Repeatable. Valid: `structural`, `duplication`, `complexity`, `dead_code` |
| `--top N` | how many clusters to queue (default 8) — pass through as `--top N` |
| `+queue` | also refresh the rolling queue issue in the tracker (see step 6) |
| `+no-ask` | skip the confirmation stop; still respects `+execute` |

No single kind takes more than three of the `--top` slots while another kind has a
candidate left, so one barrel file's sixty unused exports cannot crowd out every
duplication and complexity cluster in the repo. `+kind:<k>` is the way to work
through one kind in depth.

`+execute` and the engine's own `--write-queue` are different writes: the first
files analysed issues through `GH_TOKEN=$(gh auth token --user toxicphreAK) gh issue create --repo toxicphreAK/homeassistant-triplesolar --title '<title>' --body-file <body.md> --label enhancement`, the second rewrites the single
rolling queue issue. Neither implies the other.

## Step 1 — Produce the queue

If `+rescan` was passed, or `reports/code-quality/` has no reports yet:

```bash
.venv/bin/python tools/quality/quality_scan.py
```

Do not hand-check whether the reports are stale — the next command does that for
you, against the tree rather than against a guess, and refuses rather than warns.

Then cluster and dump the hand-off (`--top` and `--kind` carry the flags above;
omit them for the defaults):

```bash
GH_TOKEN=$(gh auth token --user toxicphreAK) .venv/bin/python tools/quality/quality_triage.py --top <N> [--kind <k>] --backend gh
```

That writes `.quality-clusters.json`. If it exits non-zero, **stop** and fix the
cause — it refuses for exactly three reasons, and each makes its output
untrustworthy:

- **The open-issue corpus could not be read.** Triaging against a corpus that
  failed to load marks every cluster "not tracked" and re-files existing work.
- **The tool reports come from different scans.** A partial run (`--only`, or the
  structural gate) refreshes some tools and not others; clustering fresh findings
  with stale ones produces well-evidenced issues for code that is already fixed.
  Re-run `quality_scan.py` with no `--only` so the whole set describes one state.
- **The scan is older than the code.** Somebody fixed something since it ran, so
  some findings describe code that no longer exists. Re-scan.

It may also *hold back* clusters, which is not a failure: a cluster whose every
file failed to resolve when the scan ran cannot be investigated, so it is counted
in the preview and kept out of the queue. A non-zero count there means an adapter
is emitting paths that do not exist — worth reporting, not worth blocking on.

It prints the scan's id and timestamp above the hand-off path. Check that date —
every finding below it is only as true as that scan.

Show the user the preview table it printed: cluster count, how many are already
tracked, dismissed, or held back as unreadable, and the queued clusters with
score, churn, kind and marker. A row marked **resurfaced** was dismissed before
and has got worse since. A note about **stale dismissals** names entries in
`.quality-dismissed.json` that this scan no longer produces; step 5 deals with
them.

## Step 2 — Confirm

Unless `+no-ask`, stop here and ask whether to proceed. If `+execute` was not
passed, stop here regardless — this was a preview.

## Step 3 — Investigate (only after confirmation)

Read `.quality-clusters.json`. For **each** queued cluster:

1. **Read the files** in `files`, at the lines in `findings`. Skip any path
   listed in the cluster's `unresolved_files` (or whose finding has
   `"unresolved": true`) — the scanner could not resolve it, so whatever is at
   that path is not what the tool was describing. Say so in the report.
2. **Pin the root cause.** Not the symptom — the reason. A clone group is usually
   one helper that was copy-pasted before it was extracted. A complexity cluster is
   usually one function doing three jobs, or one function carrying a flag argument
   that splits it into two. A dead-code cluster is usually one abandoned migration.
   Write it as one sentence with a `file:line` behind it.
3. **Check the churn.** The cluster carries `churn_commits`. A hot file justifies a
   bigger refactor; a cold one might justify none. Say which.
4. **Find the test coverage.** Name the tests that pin the current behaviour. If
   there are none, the refactor is unsafe as specified and the issue must say so.
5. **Group** clusters sharing a cause. **Discard** false positives.
6. **A resurfaced cluster** carries `previously_dismissed`: the reason, and the
   `base_score` it was dismissed at. It came back because its `base_score` is
   higher now, so something was added. Find what, and decide whether the old
   reason still covers it. If it does, dismiss it again. If not, file it.

## Step 4 — File the analysed issues

One issue per root cause. Use this project's tracker (gh):

```
GH_TOKEN=$(gh auth token --user toxicphreAK) gh issue create --repo toxicphreAK/homeassistant-triplesolar --title '<title>' --body-file <body.md> --label enhancement
```

Body skeleton — it is deliberately the same shape `/implement` consumes, so a
filed issue is directly executable. (Outer fence is `~~~` so the `bash` block
inside it nests correctly.)

~~~markdown
## Summary

<what is wrong and what it costs, 1–2 sentences>. **Root cause:** <the why>.

## Root cause evidence

- `path/to/file.py:NN` — <what is actually there>
- `path/to/other.ts:NN` — <the copy, or the second job>

Touched <N> times in the last <window> (churn), so this is <interest-bearing / cold>.

## Scope

**In:** <the change>
**Out:** <the adjacent tidy-up this issue is not>

## Refactoring steps

- [ ] <step>
- [ ] <step>

## Acceptance criteria

- [ ] Behaviour unchanged: `<the exact test command that proves it>` passes
- [ ] `<scanner command>` no longer reports this cluster
- [ ] No new findings introduced: the quality gate is green

## Verify

```bash
<one command a reviewer can run>
```

---
<!-- quality-dedup: do not remove. This is how quality_triage.py knows these
     clusters are already tracked. -->
**Covers quality clusters:** `(qfp:<hash1>)` `(qfp:<hash2>)`
~~~

The `(qfp:…)` footer is **mandatory** and must list **every** cluster the issue
covers. Without it the cluster comes straight back in the next queue and gets filed
twice. With the wrong hash in it, a cluster disappears from the queue forever.

Label filed issues `enhancement`.

Then record every cluster you discarded in step 3, one call each:

```bash
GH_TOKEN=$(gh auth token --user toxicphreAK) .venv/bin/python tools/quality/quality_triage.py --dismiss '<marker>' --reason '<why it is not worth fixing>' --backend gh
```

It takes the cluster from `.quality-clusters.json`, so run it before step 5
rewrites that file, and only for clusters that file actually queued. It writes
`.quality-dismissed.json`, which is **committed**. A reviewer has to be able to
see what was waved through and why. The reason should be specific enough to
check: "generated by protoc, see Makefile:40" can be checked, "not important"
cannot.

A dismissal lapses by itself when the cluster's file set changes or its
pre-churn score rises, so it only suppresses the code as you read it. Do not
dismiss as a way to defer real work. That is what an issue is for, and the gate
still measures the code either way.

## Step 5 — Refresh and report

Re-run the triage so the clusters you just filed drop out:

```bash
GH_TOKEN=$(gh auth token --user toxicphreAK) .venv/bin/python tools/quality/quality_triage.py --top <N> [--kind <k>] --backend gh
```

Carry the **same** `--kind` you used in step 1. Dropping it widens the refresh to
kinds this run never considered, so "what is still queued" reports work you did
not triage as though you had passed over it.

Report as a receipt, not a record:

- the issues filed, with the clusters each covers and the root cause in one line
- what is still queued
- what you dismissed as a false positive, and why, and that
  `.quality-dismissed.json` changed and should be committed with the issues
- any **stale dismissals** the refresh printed. The note says whether every
  analyser ran. Only if it did, delete those entries from `.quality-dismissed.json`.
  If it names analysers that did not run, keep the entries, because a missing
  tool makes its dismissals look stale on this machine only. A stale entry often
  belongs to a cluster that is back under a new fingerprint because a file joined
  it, so check the queue for that first.
- anything you could not analyse and why

Then delete the scratch file `.quality-clusters.json`. It sits at the repo root and is
rebuilt from scratch on every run, so a leftover is only ever a stale copy
waiting to be committed by someone's `git add -A`.

## Step 6 — Refresh the rolling queue (only with `+queue`)

The engine can also maintain **one** standing issue listing everything still
queued, so the backlog is visible in the tracker between triage runs:

```bash
GH_TOKEN=$(gh auth token --user toxicphreAK) .venv/bin/python tools/quality/quality_triage.py --top <N> --mode rolling --write-queue --label enhancement --backend gh
```

It rewrites that issue's body in place and finds it by title, so:

- never hand-edit it — the next run overwrites it;
- it is a **queue, not a work order**. Entries carry evidence and no analysis; the
  analysed issues are the ones you filed in step 4, and their `(qfp:…)` markers
  drop the matching entries from here automatically.

`--label` takes **one** existing label, which is why `enhancement` is separate
from the `enhancement` you put on filed issues. Without `--write-queue` this is a
preview. Skip the whole step if this project prefers to keep the backlog in
`.quality-clusters.json` and out of the tracker.

The engine can only *write* this issue through `gh` or `tea`. On an MCP-only
tracker `--backend gh` supplies a read corpus and says nothing about how to
write one back, so there this step is deleted at install time rather than left
here to fail.

## Handing off to implementation

A filed issue here is a normal tracker issue with acceptance criteria, so
`/implement <number>` picks it up with no extra ceremony. Do **not** implement
them from this command: a refactoring diff wants its own fresh context and its own
review, and batching five of them into one session is how a "no behaviour change"
refactor quietly changes behaviour.
