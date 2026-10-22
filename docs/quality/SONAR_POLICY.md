# Sonar sustainable policy — Clean as You Code (post Missions 1–3)

**Context:** first main analysis `625aa2e` (`f2255b38`) imported real coverage (`59.2%` overall, `61.9%` line, `50.0%` branch, `8076` to cover, `13119` ncloc) and 313 issues (3 bugs, 5 vulns, 305 smells, `0.1%` duplication). The main quality gate was **not computed** (`projectStatus NONE`, `SonarCloud Code Analysis neutral: Quality Gate not computed`), while the workflow's `sonarqube-scan-action` with `sonar.qualitygate.wait=true` reported `FAILED` (exit 3) for the same commit. PR #5's gate was `OK` (4 new-code conditions) because a PR has a diff window.

See `SONAR_BASELINE.md` for the exact `NONE` vs `FAILED` evidence and `SONAR_FINDINGS.md` for the 8 dispositions (4 confirmed fixed in #9, 4 false positives).

## Objective

Keep the existing **Sonar way** Clean as You Code gate, keep real coverage, and make the next main analysis *computable* without hiding historical debt.

## Current gate (do not weaken)

`api/qualitygates/show?organization=mitchins&id=9` — **Sonar way** (built-in, default, `isCleanAsYouCode: true`, id 9):

| metric | op | threshold | applies to |
|---|---|---|---|
| `new_security_rating` | GT | 1 | new code |
| `new_reliability_rating` | GT | 1 | new code |
| `new_maintainability_rating` | GT | 1 | new code |
| `new_coverage` | LT | 80 | new code |
| `new_duplicated_lines_density` | GT | 3 | new code |
| `new_security_hotspots_reviewed` | LT | 100 | new code |

No overall-code condition. The `80` is **new-code coverage**, not overall. Demanding `80%` overall overnight would require either fabricating tests for legacy `pipeline/stages/*` (13% compose) or excluding active source files — both forbidden. The current overall `59.2%` must remain visible as debt.

Historical debt stays visible:
- `alert_status` remains empty until a baseline exists, but `coverage`, `bugs`, `vulnerabilities`, `code_smells`, `duplicated_lines_density` are still served via `measures/component` and the dashboard. No exclusions, no fake `sonar.coverage.exclusions`, no `sonar.tests` manipulation.

## Why the first main gate was NONE and what to correct

- **New-code definition (instance):** `sonar.leak.period.type=previous_version`, `sonar.leak.period=previous_version` (`api/settings/values`). No project-specific override.
- **Project version:** `not provided` (`project_analyses/search` `projectVersion: "not provided"`). With `previous_version` and no version, the leak period is empty → `new_lines_to_cover` empty → `new_coverage` empty → `projectStatus` `NONE`, `periods: []`.
- **PR vs main:** PR #5 had a diff (`base main`), so its 4 new-code ratings could be evaluated (`OK`). Main had no diff.

**Correction (reversible, preserves security/coverage):** Establish a baseline version so the *next* main analysis has a period.

**Recommendation (requires operator approval before apply):**

1. Set an explicit project version for the next main analysis. Smallest change:
   - Add to `sonar-project.properties`:
     ```
     sonar.projectVersion=0.2.0
     ```
     (keeps the file tracked, as required for CI's `Verify tracked Sonar configuration` step). Next push after merging #9 will create analysis `0.2.0` → subsequent commits will have `new_lines_to_cover` and gate conditions.
   - Alternative via UI: Project Settings → New Code Definition → Previous version → set version `0.2.0` via `sonar.projectVersion` or `api/project_analyses/search` version event. Prefer the file-based version to keep the repo the source of truth.

2. Do **not** change thresholds to clear the red gate. Do **not** switch to a `30-day` or `reference branch` definition unless the team prefers a time-based Clean as You Code window; `previous_version` matches the current implicit intent and needs only a version.

3. After the baseline, verify via `api/measures/search_history?metrics=new_coverage,new_lines_to_cover` that `new_coverage` acquires a value and `api/qualitygates/project_status?branch=main` returns `OK`/`ERROR` with 6 conditions instead of `NONE`.

No Sonar setting change has been applied in this worktree; this document is the recommendation for approval. The change is reversible by removing `sonar.projectVersion` or resetting the new-code definition to `previous_version` without a version.

## Branch protection (inspect, don't yet enforce)

**Actual today:** `gh api repos/mitchins/yt2wiki/branches/main/protection` → `404 Branch not protected`. No required status checks.

**Reported checks for `625aa2e`:**
- `Python tests and coverage: success`
- `SonarQube quality gate: failure` (workflow job, `anyio` fix not yet in main)
- `SonarCloud Code Analysis: neutral` (quality gate not computed)
- `GitGuardian: success` (or `failure` on `COMMANDS=passwd` hint, non-blocking)

**Intended requirements (from `ci.yml`):**

```yaml
sonar:
  if: github.actor != 'dependabot[bot]' && (github.event_name != 'pull_request' || head.repo.full_name == github.repository)
```

- On `push: main` and on same-repo PRs: require `Python tests and coverage` + `SonarQube quality gate`.
- On fork PRs without `SONAR_TOKEN` and on `dependabot[bot]` PRs: only `Python tests and coverage` must be required; `SonarQube quality gate` cannot run (no secret) and must not be a required check, otherwise forks would be permanently red.

**Recommended protection (apply only after verification):**

- Require `Python tests and coverage` (always).
- Require `SonarQube quality gate` **only** when it can run (i.e. keep the existing `if` condition; GitHub's required checks should be set to `Python tests and coverage` as strictly required, and `SonarQube quality gate` as required but with the understanding that its `if` will skip on forks/Dependabot — do not make `SonarCloud Code Analysis` required because it is `neutral` when not computed).
- Do not require a check that is `skipped` on forks; verify by opening a test PR from a fork and confirming the required set is satisfiable before enabling.

No protection change has been applied here. The recommendation should be verified by pushing `fix/sonar-initial-findings` (#9) and observing both jobs on a same-repo PR (`OK`) and, if needed, a Dependabot PR (only `Python tests and coverage` runs).

## Coverage reporting

- Keep `sonar.python.coverage.reportPaths=coverage.xml` (tracked), `sonar.sources=src/yt2wiki`, `sonar.tests=tests`, `sonar.python.version=3.12`.
- CI already enforces `test -s coverage.xml` and uploads artifact `python-test-reports` (14d). Local `python -m pytest --cov=yt2wiki --cov-branch --cov-report=xml:coverage.xml` matches CI.
- New code must reach `>=80%` (`new_coverage LT 80`); overall `59.2%` may rise organically as V9 stages gain tests, but not as a gate.

## Debt ledger

Historical debt remains in Sonar:
- `305` code smells (including `S3776` precognitive `v9/run.py:164`, etc.) — intentionally deferred per non-goals, not bulk-fixed.
- `3` bugs, `5` vulns — 4 confirmed fixed (#9), 4 false positives documented.
- Duplication `0.1%` (`new_duplicated_lines_density GT 3` guards new duplication).

No file exclusions have been added; `sonar.tests` remains `tests`.

## Gate D

Documented final gate = **Sonar way** unchanged (`new_*` 6 conditions, `80` new coverage). No threshold or definition change applied yet; proposed `sonar.projectVersion=0.2.0` is the minimal reversible baseline fix, pending approval. Existing technical debt remains visible in dashboard measures.

A green dashboard alone is not acceptance — the next main analysis after the versioned baseline must show `projectStatus OK`/`ERROR` with 6 conditions, not `NONE`.

