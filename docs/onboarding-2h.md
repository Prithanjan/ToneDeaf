# Onboarding — two hours to proficiency

**Status:** study aid, not normative. Nothing here overrides [rules.md](../rules.md),
[technical-design.md](../technical-design.md), or [memory.md](../memory.md). If this file and one of
those disagree, they win and this file is stale.
**Audience:** a new contributor (human or agent) who has never opened this repo.

---

## The timetable

| Time | Module | Question it answers |
|---|---|---|
| 0:00–0:10 | The one idea | What does this product actually claim, and what does it refuse to claim? |
| 0:10–0:25 | Repo map | Which directory owns what, and who owns the directory? |
| 0:25–0:35 | The document set | Which of the eight root docs answers my question? |
| 0:35–0:55 | Gateway request path | What happens between a microphone and an audit row? |
| 0:55–1:05 | Auth | Four hops: Cognito → JWT → session → ticket → WSS |
| 1:05–1:15 | Secrets | Five secrets, four env vars, two providers. Why the mismatch? |
| 1:15–1:30 | Database | One table, an exact allow-list, an HMAC hash chain |
| 1:30–1:45 | Tests | 294 gateway tests, four CI gates, one known hole |
| 1:45–2:00 | Infra, cost, phases | Six CDK stacks, runtime-zero, and what is actually blocked |

---

## Exercises, in order

Each block is one command. Run them from the repo root unless told otherwise.

**E1 — prove the constants are self-consistent.** Exits silently on success; the module runs
`_self_check()` at import.

```bash
.venv-ws/Scripts/python.exe -c "import sys; sys.path.insert(0,'gateway'); import app.constants as c; print(c.WS_FRAME_BYTES, c.WINDOW_BYTES, c.HOPS_PER_WINDOW)"
```

**E2 — prove the schema contract is self-consistent.** Same trick: `_self_check()` at import,
stdlib only, no database needed.

```bash
.venv-ws/Scripts/python.exe -c "import sys; sys.path.insert(0,'audit/migrations'); import schema_contract as s; print(len(s.COLUMNS), 'columns;', len(s.CHAINED_COLUMNS), 'chained')"
```

**E3 — run the full gateway suite.** Expect `294 passed`.

```bash
cd gateway && ../.venv-ws/Scripts/python.exe -m pytest tests/ -q
```

**E4 — read the CREATE TABLE the migration will emit, without a database.**

```bash
.venv-ws/Scripts/python.exe -c "import sys; sys.path.insert(0,'audit/migrations'); sys.path.insert(0,'audit/migrations/versions'); import importlib; m=importlib.import_module('0001_audit_event'); print(m._create_table_sql())"
```

**E5 — prove `approve`/`deny` exist nowhere.** Expect no hits outside prose that forbids them.

```bash
grep -rniE "\b(approve|deny)\b" --include=*.py --include=*.ts --include=*.yaml --include=*.proto gateway scorer pwa policy contracts audit
```

**E6 — count what is actually in each directory.** The table in `memory.md` §1 drifts; this is truth.

```bash
for d in gateway scorer pwa infra audit .github evaluation scripts contracts datasets policy ml docs; do printf "%-12s %s\n" "$d" "$(find "$d" -type f -not -path '*/__pycache__/*' 2>/dev/null | wc -l)"; done
```

**E7 — find every rule citation in the source.** This is how the code and `rules.md` stay joined.

```bash
grep -rno "R-[0-9][0-9]" --include=*.py gateway/app | sort -t: -k3 | uniq -c -f2 | head -40
```

---

## Corrections to `memory.md` — verify before you trust the ledger

`memory.md` §1 says of its own state table: *"re-measure before trusting this table."* Taking it at
its word, as of 2026-08-26:

| Claim in `memory.md` | Reality | Where |
|---|---|---|
| U-3 "`infra/` is empty" | **False** — 22 files, six CDK stacks present | §6 U-3 vs §1, §5 E-7 |
| U-5 "`.github/` is still empty" | **False** — 10 files, 8 workflows present | §6 U-5 vs §1, §5 E-8 |
| E-7 "twenty files" in `infra/` | 22 measured (the two extras are `infra/iam/rendered/*`) | §5 E-7 vs §1 |
| §13 item 3 cites `audit/tests/test_schema_denylist.py` | No such file. Real names: `test_deny_list.py`, `test_schema_allow_list.py` | §13 |
| §7 "no `.gitignore` yet" | Exists, 6,862 bytes (§12 already retracted this; §7 was not updated) | §7 vs §12 |
| — | **Undocumented:** a vendored `.pytest834/` tree sits at repo root and is in no §11 scratch list | §11 |

Two conflicts **between** normative documents, which R-54 says must be named rather than blended:

1. **Four secrets or five?** [phases.md](../phases.md) §1.2 step 0.7 and the Phase 0 DoD both say
   *"Four Secrets Manager placeholder entries."*
   [aws-setup-instructions.md](../aws-setup-instructions.md) §6 says *"There are FIVE, not four"* and
   names `infra/cdk/lib/secrets-stack.ts` as the tie-breaker. **Five is right** — see Module 5. The
   `phases.md` rows need updating.
2. **A broken command in the runbook.** `aws-setup-instructions.md` §8 tells you to run
   `pytest -q audit/tests/test_schema_denylist.py` against real RDS. That path does not exist; it is
   `audit/tests/test_deny_list.py`. As written the one-shot task exits non-zero and the deny-list is
   never verified against the deployed schema — a green-looking skip on the exact check that matters.

---

## Self-check — you are proficient when you can answer these without looking

1. Why is a WSS binary frame exactly **648** bytes, and why is the 8-byte header big-endian while
   the 640-byte payload is little-endian?
2. Four hops of overlap: how many 20 ms frames must arrive before the *first* window is scored, and
   how many before the second?
3. `spoof_risk = 0.91` arrives on one window. What action does the client see, and why?
4. Same score, three consecutive times, `purpose_code = support_enquiry` vs `beneficiary_change`.
   What differs, and which file decides?
5. The Scorer times out on window 7. What is written to the audit table for window 7?
6. Name the two things `verify_chain` catches that a per-row signature would not.
7. Why does `spoof_risk` use `numeric(5,4)` and not `double precision`?
8. Why are there **three** ECR repositories rather than two?
9. `git push` to `main`. Can that start GPU spend? What is the mechanism that prevents it?
10. Why must `sih26104/audit-chain-key` never be rotated after the first real session?
11. What does `diagnostics.observe(...)` return, and what does the caller do with it?
12. Why is `is_aws` allowed in `main.py` but forbidden in `stream.py`?
