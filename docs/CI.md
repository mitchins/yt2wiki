# CI: pytest coverage and SonarQube

The GitHub Actions workflow at `.github/workflows/ci.yml` runs on PRs to
`main`, pushes to `main`, and manual dispatches.

- **Python tests and coverage:** Ubuntu 24.04, Python 3.12, no GPU/ML extras,
  local FFmpeg and Tesseract. Installs `.[dev]` and `pytesseract`, runs the
  deterministic test suite with branch coverage, and archives `coverage.xml`
  and `junit.xml` for 14 days. The large research/eval package is not part
  of the application repository; its dependent tests may skip intentionally.
- **SonarQube quality gate:** a separate, dependent job downloads the test
  reports and runs the SHA-pinned official SonarSource scanner. It reads
  `sonar-project.properties` from the repository root and receives
  `SONAR_TOKEN` from the GitHub Actions repository secret. Quality-gate
  failures fail this job. It does not run pytest with that credential.
- **Fork PRs:** tests still run, but Sonar analysis is skipped because secrets
  are not exposed to untrusted forks. Sonar runs on same-repository PRs and
  `main` pushes.
- All external Actions use full commit SHA pins and verified Node 24
  runtimes; GitHub-hosted runners meet the required runtime version. Pin
  updates must be deliberate and reviewed.

## Required one-time setup

1. **Track your existing local `sonar-project.properties`:** validate its
   `sonar.projectKey`, SonarQube Cloud organisation or self-hosted server URL,
   `sonar.sources=src/yt2wiki`, and `sonar.tests=tests` against your actual
   Sonar project. Commit the file *without secrets* on the CI feature branch.
   CI intentionally fails the Sonar job if the file is absent. Do not guess
   the organisation/key, and do not commit local `.env` files.
2. Set repository Actions secret `SONAR_TOKEN` (already prepared by the
   operator). Sonar scanner reads it at scan time only; GitHub CI does not
   read the local `.env`. On a self-hosted SonarQube Server, ensure the
   scanner can reach the configured `sonar.host.url`.
3. Review the first full CI run for actual tests, coverage report paths, and
   imported Sonar coverage. Check that Sonar's issue/coverage overview is
   populated (not just a green scanner exit).
4. After the checks work, configure branch protection to require
   **Python tests and coverage** and **SonarQube quality gate**; if your
   Sonar project also publishes its own quality-gate check, choose the
   appropriate checks without blocking legitimate fork PRs on an unavailable
   secret. Do not configure an arbitrary absolute coverage threshold until
   the observed baseline is known.

No GPU model weights, network inference or downloaded videos are needed by CI.

## Local equivalent

```bash
python -m pip install -e '.[dev]' pytesseract
python -m pytest tests -q --cov=yt2wiki --cov-branch \
  --cov-report=term-missing --cov-report=xml:coverage.xml --junitxml=junit.xml
```
