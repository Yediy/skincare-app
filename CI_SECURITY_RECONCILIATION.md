# CI Security Reconciliation

Closeout for the pass whose sole mission was: get the actual GitHub Actions
`CI` workflow green, without weakening security, and without touching
product features. See `SECURITY_VULNERABILITY_STATUS.md` for the tracked
exception list this pass produced.

## Starting state

The ticket for this pass referenced GitHub Actions run `34278814337`
(commit `e563d06`), reporting `test`/`dependency-scan`/`docker-build-and-scan`
all failing. By the time this pass actually started, `master` had moved
three commits past that (`74899e7`, `815f843`, `7a3017e`), and the *actual*
latest run at that new HEAD (`34309610960`, commit `7a3017e`) was still
failing all three of the same jobs -- but for reasons that had nothing to
do with the ticket's own RLS-role-model description, which turned out to
already be fixed (see below). This pass re-diagnosed from the real, current
CI logs rather than trusting the ticket's stale description.

```
Workflow: CI
Run ID:   34309610960
Commit:   7a3017e9fbd36b1f83f4bc65a307b4f62335b352

secret-scan             PASS
test                    FAIL  (7 failed, 4 errors, 123 passed)
dependency-scan         FAIL  (51 known vulnerabilities, 6 packages)
docker-build-and-scan   FAIL  (Trivy: 63 HIGH + 5 CRITICAL, ~2.1GB image)
```

## Test mismatch root cause

The ticket predicted a Postgres RLS / restricted-role test-fixture problem.
That work was already done and already green in CI (see commit
`815f843`'s predecessor "security: restricted runtime DB role and row-level
security", run `34196991687`, PASS) -- `backend/tests/conftest.py` already
cleanly separates `TEST_DATABASE_URL` (superuser, used only for migrations
and `TRUNCATE` cleanup) from `APP_DATABASE_URL` (the restricted
`skincare_app` role every real request goes through, RLS included). No
change was needed there.

The actual failure was unrelated: three test files
(`tests/cv/test_capture_assessment.py`, `tests/domain/test_analysis_service.py`,
`tests/integration/test_end_to_end_analysis.py`) built a "real photo with a
face in it" fixture by reaching into a path inside the developer's local
virtualenv:

```
Path(__file__).resolve().parent.parent.parent
    / ".venv/lib/python3.11/site-packages/matplotlib/mpl-data/sample_data/grace_hopper.jpg"
```

This worked locally because a developer's `backend/.venv` exists and
happens to have `matplotlib` installed (pulled in transitively by
`mediapipe`, which uses it for optional plotting utilities this app never
calls). It never worked in GitHub Actions, because the `test` job's
"Install dependencies" step runs `pip install -r requirements.txt` directly
into the `actions/setup-python`-managed interpreter at
`/opt/hostedtoolcache/Python/.../site-packages` -- **no `.venv` is ever
created in CI**, so `backend/.venv/...` never exists there, regardless of
which matplotlib version resolves. This is why local runs saw
114/114 (or, at current HEAD, 134/134) passing while CI saw the same tests
fail with `FileNotFoundError` / `cv2.error: ... !_src.empty()`.

Fix: vendor the fixture image into the repo itself
(`backend/tests/fixtures/grace_hopper.jpg`, a public-domain US Navy photo
via Wikimedia Commons -- see `backend/tests/fixtures/README.md` for
provenance), and reference it by a path relative to the test file, not
through any installed package's internal data directory or any assumption
of a local venv's existence. Verified in a from-scratch virtualenv (no
`.venv` reuse, dependencies installed exactly as `requirements-dev.txt`
specifies) against real Postgres/Redis: **134 passed, 0 failed, 0 errors,
0 skipped**.

## Dependency findings

`pip-audit -r backend/requirements.txt` at the start of this pass: **51
known vulnerabilities across 6 packages** (python-multipart, Pillow,
cryptography, protobuf, starlette, ecdsa).

| Package | Old | New | Reason | Compatibility impact | Test result |
|---|---|---|---|---|---|
| python-multipart | 0.0.9 | 0.0.32 | 7 CVEs, fixes through 0.0.31; took latest | None found | Full suite green |
| Pillow | 10.4.0 | 12.3.0 | ~15 CVEs, fixes through 12.3.0; image parsing is real attack surface here (user-uploaded photos) | None found | Full suite green, incl. real CV pipeline against a real photo |
| starlette | 0.37.2 (transitive via fastapi) | 1.6.0 | 8 CVEs; upgraded as a compatible pair with fastapi (see below), not pinned against an incompatible old fastapi | None found | All HTTP/auth/CORS/docs-gating tests green |
| fastapi | 0.111.0 | 0.141.1 | Needed to allow a secure starlette; fastapi 0.141.1 requires `starlette>=0.46.0` (no upper bound) and `pydantic>=2.9.0` | Required a pydantic bump alongside it (below) | All HTTP tests green |
| pydantic | 2.7.4 | 2.13.5 | Required by the fastapi upgrade (`pydantic>=2.9.0`) | None found; pydantic-settings 2.15.0 (already latest) unaffected | Full suite green, incl. `Settings` validation tests |
| cryptography | 42.0.8 | **removed** | Was a direct pin only because `python-jose[cryptography]` needed it; no code in `app/` imports it directly (verified: no `hazmat`/`Fernet`/`from cryptography` anywhere in `app/`). Removed entirely along with python-jose (see JWT decision below) rather than merely bumped. | N/A -- dependency eliminated | Confirmed via a from-scratch venv install: `cryptography` is not installed at all from the final `requirements.txt` |
| protobuf | 4.25.9 | **unchanged** (documented exception) | See "MediaPipe / protobuf decision" below | N/A | N/A |

`pip-audit -r backend/requirements.txt` after the dependency-upgrade
commit: **1 known vulnerability** (protobuf, `PYSEC-2026-1805` /
CVE-2026-0994). `pip check` and a genuinely fresh `python -m venv` +
`pip install -r backend/requirements.txt` install both confirm no
broken/conflicting requirements, and confirm `cryptography` and `ecdsa`
are not installed at all (not merely unused -- absent).

Unlike Trivy, `pip-audit` has no severity threshold to filter on here --
it fails on *any* known vulnerability, so the accepted protobuf exception
(same CVE as the Trivy one, see the MediaPipe/protobuf decision below)
still failed the real `dependency-scan` job on this pass's first push
(run `34357736117`). Fixed by adding the same kind of per-CVE-justified
exception pip-audit itself provides for exactly this case --
`--ignore-vuln PYSEC-2026-1805` on both pip-audit invocations in
`.github/workflows/ci.yml` -- rather than weakening the gate generally;
`pip-audit` now reports "No known vulnerabilities found, 1 ignored" for
the production dependency set, with the ignore itself dated and
justified in both `ci.yml`'s comments and
`SECURITY_VULNERABILITY_STATUS.md`.

## JWT library decision

`python-jose[cryptography]==3.5.0` was **replaced** with `PyJWT==2.13.0`.
`ecdsa==0.19.2` (pulled in unconditionally by `python-jose` -- it is a
hard, non-extra-gated dependency even with the `[cryptography]` extra
selected, confirmed by reading python-jose's own package metadata) carries
CVE PYSEC-2026-1325 (the Minerva timing side-channel), with **no fixed
version available** from the `ecdsa` maintainers. This application only
ever signs/verifies with HS256 (`app/config.py: jwt_algorithm: str =
"HS256"`), so the elliptic-curve code path `ecdsa` exists for was never
actually exercised here -- but pip-audit correctly flags it as an
installed, vulnerable dependency regardless of whether this app's code
path reaches it.

`app/security/tokens.py` and `app/security/auth.py` were updated to import
from `jwt` (PyJWT) instead of `jose`. `decode_access_token` still passes an
explicit `algorithms=[settings.jwt_algorithm]` allowlist to `jwt.decode` --
this is what actually blocks algorithm confusion (a token claiming
`"alg": "none"`, or one signed with a different algorithm than configured),
not merely "using a different library."

Preserved and newly, directly unit-tested (there was no prior direct unit
test of `app/security/tokens.py` at all -- only indirect coverage through
full HTTP-flow tests): `sub`, `family_id`, `iat`, `exp`, `type` claims;
expired-token rejection; tampered-signature rejection; a token signed with
a different secret; algorithm-confusion rejection (a token signed HS512
against an HS256-configured verifier); wrong-`type`-claim rejection. See
`backend/tests/unit/test_access_tokens.py`. Revoked-family and
account-invalidation behavior were already covered at the HTTP level by
`backend/tests/auth/test_refresh_rotation.py` and
`test_account_invalidation.py` and needed no changes.

## MediaPipe / protobuf decision

**protobuf was left at 4.25.9 (unchanged), and this is tracked as an open,
documented, non-silent exception** -- see
`SECURITY_VULNERABILITY_STATUS.md` and `backend/.trivyignore`. Summary of
the investigation:

1. **Does a newer MediaPipe support a secure protobuf?** Yes --
   `mediapipe>=0.10.30` drops the `protobuf<5,>=4.25.3` constraint entirely
   (verified by downloading and inspecting the wheel metadata for
   `0.10.21` through `1.0.1`; `0.10.30` is the first version on PyPI
   without a protobuf upper bound at all).
2. **Does that newer MediaPipe work without code changes?** No.
   `0.10.30`+ also **removed** the legacy `mediapipe.solutions` module
   entirely (confirmed empty by inspecting wheel contents for
   `mediapipe/python/solutions/face_mesh.py` across versions -- present in
   `0.10.21`, absent in `0.10.30` and `0.10.35`). This app's
   `FaceLandmarkExtractor` (`app/cv/face_landmarks.py`) is built directly
   on `mp.solutions.face_mesh.FaceMesh`. The replacement (`mediapipe.tasks`
   Tasks API) is a different API shape and additionally requires a
   separately-distributed `.task` model asset file (~a few MB, not bundled
   in the pip wheel at all -- confirmed by inspecting the wheel for any
   `.task`/`.tflite` file: none) that this repo does not currently vendor,
   with its own provenance/licensing/update-process to establish.
3. **Is there a packaging combination with both a legacy-API MediaPipe and
   a patched protobuf?** No -- PyPI has no published mediapipe version
   between `0.10.21` (last with the legacy API, still requires
   `protobuf<5`) and `0.10.30` (first without the pin, no legacy API);
   `0.10.22`-`0.10.29` were never published to PyPI.
4. **Is the CVE reachable here regardless?** No. CVE-2026-0994 is a
   recursion-depth bypass specifically in
   `google.protobuf.json_format.ParseDict()` when parsing attacker-supplied
   deeply-nested `Any` messages -- a DoS, not RCE/data exposure. This app
   never parses attacker-supplied protobuf-JSON at any endpoint; mediapipe's
   only use of protobuf here is its own internal binary model
   configuration.

Given (2)-(4), migrating the single most safety-sensitive code path in this
product (real-time facial landmark detection feeding directly into the
SafetyEngine's allergy/pregnancy enforcement -- see
`SECURITY_AND_SAFETY_NOTES.md`) to vendor a new, unreviewed external binary
model asset and rewrite its detection code, in order to close a
DoS-only CVE this app's actual request-handling code never reaches, was
judged not to be the responsible trade-off for this pass. It is tracked
with a review date (2026-12-09) rather than silently dropped: re-check then
whether MediaPipe has restored legacy-API compatibility with a patched
protobuf, or reassess the Tasks-API migration as a deliberate, separately
-planned piece of work with its own test/validation pass (not a rider on a
CI-green pass).

## Container findings

| | Before | After |
|---|---|---|
| Base image | `python:3.11-slim` (floating tag, resolved to Debian 13/trixie at build time) | `python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534` (pinned; still trixie) |
| Native libs (`libgl1`, `libglib2.0-0`) | Present | **Present, unchanged** -- re-verified empirically this pass (not merely re-asserted the prior comment): a build with `libgl1` removed fails `import cv2` with `ImportError: libGL.so.1: cannot open shared object file`; a build with only `libgl1` (no `libglib2.0-0`) fails with `ImportError: libgthread-2.0.so.0: ...`. Root cause: `mediapipe` unconditionally depends on `opencv-contrib-python` (the non-headless build) regardless of this app's own `opencv-python-headless` pin -- both get installed side by side, and the non-headless one's `cv2` module is what actually loads, needing GUI-stack libs even though nothing here ever opens a window. |
| pip/setuptools/wheel in runtime layer | Present (never used post-build) | **Stripped** from the final image after the builder stage copies dependencies in -- this app never installs packages at runtime. Removed `wheel` (CVE-2026-24049) and `jaraco.context` (CVE-2026-23949, vendored inside `setuptools`) findings entirely, along with a stray `distutils-precedence.pth` that referenced the removed `_distutils_hack` (caught by testing the built image, not assumed -- it printed a `ModuleNotFoundError` at every Python startup until the `.pth` file itself was also removed). |
| Image size | ~2.1GB | ~2.05GB (modest -- the CV/ML stack -- mediapipe, opencv, scipy, numpy, jax/jaxlib pulled in by mediapipe -- dominates the image and was not touched; stripping the packaging toolchain is a small fraction of that) |
| Runtime user | `appuser` (non-root, `NOSUPERUSER`-equivalent, uid 1000) | Unchanged, reconfirmed (`docker run ... whoami` -> `appuser`) |
| Trivy: fixable HIGH | 3 (`python-multipart`, historically; at this pass's start of container work: `jaraco.context`, `protobuf`, `wheel`) | 1 (`protobuf`, tracked exception; `jaraco.context`/`wheel` eliminated) |
| Trivy: fixable CRITICAL | 0 | 0 |
| Trivy: unfixed OS HIGH | 60 | 60 (Debian security-tracker backlog, not a decision this repo makes -- see `SECURITY_VULNERABILITY_STATUS.md`) |
| Trivy: unfixed OS CRITICAL | 5 | 5 |

**Base image alternative evaluated:** `python:3.11-slim-bookworm` (Debian
12) was built and scanned empirically rather than assumed safer for being
older/"stable". Result: **68 HIGH + 7 CRITICAL** (worse than trixie's 63
HIGH + 5 CRITICAL at the time of that comparison). Kept `python:3.11-slim`
(trixie).

**Gating policy** (`backend/.github/workflows/ci.yml`,
`docker-build-and-scan` job): two Trivy steps. The gating step uses
`ignore-unfixed: true` plus `backend/.trivyignore` (one justified entry,
`CVE-2026-0994`, with a review date) and fails the build
(`exit-code: 1`) on anything else. A second, always-run, non-gating step
scans without the ignore file (`exit-code: 0`) so both the unfixed-OS
backlog and the one tracked exception stay visible in every CI run's logs
instead of disappearing.

## Remaining unfixed vulnerabilities

See `SECURITY_VULNERABILITY_STATUS.md` for the full, canonical, living
list. Summary: one tracked Python-dependency exception (protobuf,
CVE-2026-0994, non-reachable DoS, review date 2026-12-09) and the Debian
security-tracker's unfixed-upstream OS backlog (60 HIGH + 5 CRITICAL at
last measurement), which is out of this repo's control and stays visible
via the non-gating Trivy report rather than being hidden.

## Final GitHub Actions result

```
Workflow run ID: 34394585364
Commit SHA:      8d17808084d060a729259fef6843e1135e65cf5b

secret-scan             PASS
test                     PASS
dependency-scan         PASS
docker-build-and-scan   PASS
```

One real workflow run, one commit, all four jobs green:
https://github.com/Yediy/skincare-app/actions/runs/34394585364

This took two pushes to land, not one -- the first
(commit `71ea085`, run `34357736117`) got `secret-scan`/`test`/
`docker-build-and-scan` green but missed that `pip-audit` has no
severity/unfixed-status filter the way Trivy's `ignore-unfixed` does, so
the same accepted protobuf exception that Trivy correctly excluded still
failed `dependency-scan` there. Fixed with the matching `--ignore-vuln`
exception (commit `8d17808`) rather than declaring success on the first,
partially-green run.
