# Gate 7 — Privacy (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | **RELEASE. THIS IS A RELEASE BLOCKER.** |
| **Owner (playbook §6.1)** | Privacy lead |
| **Pass condition (playbook §6.1, verbatim)** | No raw audio/transcript/embedding in audit/log export |
| **Failure response (playbook §6.1, verbatim)** | Block demo release |

## Why this one blocks the release and not just the deployment

Gate 5 blocks an artifact. This blocks the whole release, including a demo on a laptop in a room of
eight people, because the harm is not undone by rolling back. A waveform, a transcript, or a speaker
embedding that reached a log line, a database column, a crash report, or a screenshot has already
left the boundary. Deleting the row afterwards deletes the record of the exposure, not the exposure.

There is therefore no partial pass, no "ship it and fix the logging after the demo", and no
exception for a private audience (rules.md R-14, R-15, R-16). The correct response to a finding here
is to not show the system.

The system is designed so that most of these rows are *structurally* true rather than checked by
inspection — the audit table's column set is an exact allow-list with a deny-list on new columns,
`gateway/app/audit/chain.py` refuses forbidden field names at import time, and
`datasets/manifest/manifest.schema.json` has no field that can hold a path. This gate exists because
a structural control that nobody verifies against the deployed system is an assumption.

Requires: nothing. Run it first, run it last, and run it again before every showing.

## 1. What was inspected — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Environment inspected (name the host and tier) | `___` |
| Database revision (`alembic current`) | `___` |
| `policy.yaml` SHA-256 | `___` |
| `calibration.json` SHA-256 | `___` |
| Sessions exercised before inspection | `___` |
| Audit rows written during the exercise | `___` |
| Log volume captured | `___` |
| Export artifacts produced | `___` |

Inspection happens **after** exercising the system. An empty database and a quiet log file pass every
check below and prove nothing; the rows that leak are the ones a real session writes.

## 2. Predeclared criteria — fill BEFORE running

Every criterion in this gate has the same threshold, and it is zero. There is nothing to tune, so §2
records only the scope of the inspection — a narrow scope is the only way this gate passes wrongly.

| Criterion | Value |
|---|---|
| Surfaces in scope (database, application logs, access logs, stdout, crash reports, metrics, traces, exports, browser storage, screenshots) | `___` |
| Surfaces explicitly out of scope, and why | `___` |
| Retention windows verified | `___` |
| Tools and commands used | `___` |
| Permitted findings of any severity | zero |

## 3. Results — fill AFTER inspecting

### 3.1 Audit table column set

The exact allow-list, both directions: the deployed table's columns must **equal** the contract's,
not merely contain them. A subset check passes happily after someone adds a column
(`audit/migrations/schema_contract.py::allow_list_violations`).

| Check | Value |
|---|---|
| Deployed columns equal the declared allow-list exactly | `___` |
| Columns present in the database and absent from the contract | `___` |
| Columns present in the contract and absent from the database | `___` |
| Column names tripping the §5.2 forbidden-substring list | `___` |
| `bytea` columns outside the permitted pair | `___` |
| Vector, array, or float-array typed columns | `___` |
| Unlisted columns wider than the §5.2 byte limit | `___` |
| `jsonb` or unbounded-text columns anywhere in the table | `___` |
| Migration files declaring a column the contract does not list | `___` |

### 3.2 No raw audio, no transcript, no embedding (R-14, R-15)

| Check | Value |
|---|---|
| Any column, of any type, holding audio bytes | `___` |
| Any column holding a path, URI, or filename reaching audio | `___` |
| Any column holding transcribed or partial-transcribed speech | `___` |
| Any column holding a speaker embedding or voiceprint | `___` |
| Any column holding a raw feature matrix | `___` |
| Diagnostic sidecar output stored beyond the bounded descriptors | `___` |
| Audio buffers surviving in application memory past a window's scoring | `___` |
| Audio written to a temporary file at any point | `___` |
| Audio present in browser storage, IndexedDB, or a service-worker cache | `___` |

### 3.3 Pseudonymisation (R-16)

| Check | Value |
|---|---|
| Every `call_ref` matches the 64-lowercase-hex pseudonym shape | `___` |
| Any raw `client_call_ref` value present in any column | `___` |
| Any raw `client_call_ref` value present in any log line | `___` |
| Any phone number, MSISDN, or account number in any column | `___` |
| Any caller name in any column | `___` |
| HMAC key sourced from the environment, absent from the repository | `___` |
| Pseudonym mapping stored anywhere reachable from the audit database | `___` |

### 3.4 Logs, traces, and error paths

The database is the easy part. Logs are where this gate usually fails.

| Surface | Findings | Value |
|---|---|---|
| Application logs at the deployed level | `___` | `___` |
| Application logs at debug level | `___` | `___` |
| Request/access logs (query strings and headers) | `___` | `___` |
| WebSocket frame logging | `___` | `___` |
| Unhandled-exception tracebacks (do locals reach the log?) | `___` | `___` |
| Validation-error messages echoing rejected input | `___` | `___` |
| Metrics labels and trace attributes | `___` | `___` |
| Container stdout and stderr | `___` | `___` |
| Crash reports and core dumps | `___` | `___` |
| Browser console output | `___` | `___` |

A validation error that echoes the payload it rejected is the most common way a raw identifier
reaches a log: the code path that refuses the bad input is also the path that prints it.

### 3.5 Exports and the API surface

| Check | Value |
|---|---|
| Every API response field appears in the published contract | `___` |
| Any endpoint returning a field the contract does not declare | `___` |
| Any endpoint returning audio, a transcript, or an embedding | `___` |
| CSV/JSON export column set equals the audit allow-list | `___` |
| Export includes the pseudonym only, never the raw reference | `___` |
| Error responses leaking internal state or input echoes | `___` |
| Version endpoint reporting artifact hashes and no secrets | `___` |

### 3.6 Retention

| Check | Value |
|---|---|
| Every audit row carries a `retention_expires_at` | `___` |
| Rows past their retention window still present | `___` |
| Retention worker deletes whole sessions atomically | `___` |
| Chain verification passes over survivors after a sweep | `___` |
| Retention receipt contains no personal data | `___` |
| Receipt field names checked against the §5.2 substring list | `___` |
| Research audio past its consent `retention_expiry` still present | `___` |
| Withdrawn subjects with surviving samples | `___` |

### 3.7 Evidence integrity

Not strictly a privacy property, but it belongs to the same review: an audit trail that cannot be
verified cannot substantiate any claim made in this report.

| Check | Value |
|---|---|
| Chain verification passes over every session | `___` |
| Sessions failing verification | `___` |
| Genesis hash correct on every session's first event | `___` |
| `event_seq` contiguous within every session | `___` |
| Chain key rotated at any point (must be no — rules.md R-58) | `___` |

### 3.8 User-facing language (R-11)

| Check | Value |
|---|---|
| `spoof_risk` described as a probability, likelihood, or confidence anywhere | `___` |
| Any authorization verb (`approve`, `deny`, `allow`, `block`, `reject`) in UI copy | `___` |
| Any claim that the threshold was tuned, calibrated, or validated | `___` |
| Any figure presented as measured that is a placeholder | `___` |
| Consent and recording notice shown to demo participants | `___` |
| Artifact state disclosed in the UI | `___` |

## 4. Verdict

- [ ] **PASS** — every row above is zero or affirmative, on a scope that covers all of §2's
      surfaces, inspected after the system was exercised.
- [ ] **FAIL** — **BLOCK THE RELEASE, INCLUDING THE DEMO.** Record every finding below. A finding is
      not closed by a code change alone: the exposed data must be accounted for and the gate re-run
      end to end.
- [ ] **NOT RUN** — treated as FAIL. There is no release without this gate, and an unrun privacy
      gate is not a passing privacy gate.

| Field | Value |
|---|---|
| Release authorised | `___` |
| Signed off by (privacy lead) | `___` |
| Countersigned by (team lead) | `___` |
| Date | `___` |

**Findings, one row per finding. A single row here blocks the release.**

| # | Surface | What was found | Data exposed | Where it went | Remediation | Re-run required |
|---|---|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` | `___` | `___` |

## 5. What this gate does not establish

- It sees the surfaces in §2's scope and no others. A narrow scope is how this gate passes while
  something leaks, so the out-of-scope row is as important as the in-scope one.
- It is a point-in-time inspection of one environment. A passing report on one host does not cover a
  differently-configured one, and a log level raised for debugging invalidates it immediately.
- Structural controls are not self-verifying. The allow-list, the deny-list, and the import-time
  refusal in `chain.py` are strong, but they constrain the code in the repository — not a hotfix
  applied on the host, and not a column added by hand.
- It cannot prove absence in unstructured text. Free-text fields are bounded and substring-checked,
  which catches the field names somebody thought of, not every value somebody typed.
- Passing says nothing about whether the data should have been collected. Consent is gate 1.
