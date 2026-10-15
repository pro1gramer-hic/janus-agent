# Janus Agent: plan

Status: **plan validated** (all choices in section 4 accepted). Implementation in progress.

Janus Agent turns Janus (an IBM Bob hackathon workflow) into a standalone AI agent that
works with any language model. The source repository is used read-only; reused code keeps
its MIT copyright notice (see `NOTICE`, to be added).

---

## 1. What each part of Janus does

| Part | What it does |
|---|---|
| `janus/scan_config.py` | Parses Python with `ast` to find every env var read (`os.environ[...]` = strict, `os.getenv(x)` = nullable, `os.getenv(x, default)` = default), then compares with `.env.example` and every `*.env` deploy config (never `.env`). Severity follows the crash risk. |
| `janus/scan_secrets.py` | Finds hardcoded secrets: assignments to secret-looking names in Python (AST) and in `.env/.yml/.json/.toml/...` files (regex), plus known formats (AWS key, GitHub token, private key block, password in URL). Values are masked in the report. |
| `janus/scan_docs.py` | Checks README claims against the code: env var names (with a "did you mean" via `difflib`), endpoints (FastAPI/Flask routes from AST), ports, start commands, app references, and "no secrets in code" claims (cross-checked with the secrets scanner). Also lists undocumented routes. |
| `janus/__main__.py` | `python -m janus check <path>`: runs the 3 scanners, sorts findings by severity, writes `<path>/.janus/report.json`, exits 1 on critical/high. No AI. |
| `tests/test_scanners.py` | 6 tests: every planted issue in `example_app` is found, crashes are critical, secrets are never printed, the docs "truth" suggestions are right, a clean project has zero findings. |
| `example_app/` (git HEAD) | "Taskly", a FastAPI app with planted drift. The scanners report **11 findings** (3 config, 2 secrets, 6 docs). It also has a CI log for failure triage (no scanner yet). |
| `docs/traps.md` | Answer key of the planted traps (in French). Traps 4 (untested `calculate_priority`) and 5 (CI log triage) have no scanner. |
| `.bob/custom_modes.yaml` | The Bob "Janus" mode: role, budget rules (only open cited lines), safety rules (never read `.env`, never invent values, fail-fast secrets, never weaken tests), the check workflow (3 parallel subagents A config+secrets / B docs / C tests, re-scan, run tests, add `conftest.py`) and the learn workflow (rules + dated `DECISIONS.md` via `date +%F`). |
| `.bob/commands/`, `.bob/skills/` | Thin wrappers (`/janus-plan`, `/janus-check`, `/janus-learn`) pointing to the mode's workflows. `janus-plan` uses grep to map impact and writes `.janus/contract.md`. |
| `.bob/rules/janus-learned.md` | Rules Bob wrote after a run (5 rules: no hardcoded secrets, env vars everywhere, README in sync, endpoints documented, `conftest.py` with fake env vars). |
| `README.md` | Pitch and quick start. Mentions 5 scanners, but only 3 exist ("impact" and "triage" are in progress). |

Note: the working tree of Janus-bob contains an **already fixed** `example_app`
(uncommitted changes, plus `DECISIONS.md` and `conftest.py`). The 11 traps only exist in
git `HEAD`, so the copy will come from `git archive HEAD` (read-only), not from the files on disk.

## 2. Reuse, rewrite, leave

**Reused as is (with an MIT header and import paths changed to `janus_agent.scanners`)**
- `scan_config.py`, `scan_secrets.py`, `scan_docs.py`: logic unchanged, so the 11 findings stay identical.
- `tests/test_scanners.py`: the 6 tests, only the import path changes.
- `example_app/` from git `HEAD`, with its 11 traps and its fake secrets.

**Small additions to the reused code (to validate)**
- `scan_secrets.py`: add provider key formats (`sk-...`, `sk-ant-...`, `gsk_...`, `AIza...`, `xox...`, JWT) to `KNOWN_FORMATS`, so the scanner can check the agent repo itself before each push. It does not change the 11 findings, since none of these formats exist in `example_app` (checked).

**Rewritten**
- `__main__.py` becomes `cli.py` (`janus-agent plan | check | learn | scan`).
- The Bob mode, skills and commands: their rules become Python code (hard rules) plus prompt templates (judgment). See section 3.
- `docs/traps.md`: translated to English, moved out of `example_app` so it can never reach the model.

**Left behind**
- `.bob/` (mode, skills, commands, `artifacts/`): IBM Bob specific. `artifacts/` holds unrelated portfolio HTML pages.
- `bob_sessions/` screenshots, `.bobignore`, `requirements.txt` (replaced by `pyproject.toml`).
- `.bob/rules/janus-learned.md`: its content becomes an example in the README, not seed rules.

## 3. Proposed architecture

```
janus_agent/
  cli.py              argparse: plan, check, learn, scan (no AI)
  config.py           TOML config + env vars, precedence: CLI > env > *.local.toml > janus-agent.toml
  scanners/           the 3 reused scanners + run_scan() that builds the report
  llm/
    base.py           ChatRequest / ChatResponse / Usage, Provider protocol
    structured.py     ask_structured(): native tool call or strict JSON, validate, retry (max 3)
    schema.py         tiny JSON-schema subset validator (type, required, enum, items)
  providers/
    __init__.py       auto-discovers every module in this package (adding one = adding one file)
    openai_compat.py  POST {base_url}/chat/completions (OpenAI, Ollama, LM Studio, vLLM, Groq, AMD...)
    anthropic.py      POST /v1/messages
    fake.py           scripted replies, for tests and offline demos
  security/
    redact.py         masks secret-like values before anything leaves the machine
    paths.py          forbidden files (.env, .env.*, keys, certs), path traversal checks
    commands.py       allowlist of commands the orchestrator may run (tests only)
    test_guard.py     refuses edits that weaken or delete tests
  context.py          builds the model input per finding: the redacted finding + cited lines ± N
  edits.py            edit operations, validation, unified diff, apply
  workspace.py        shadow copy of the project where fixes are applied and verified
  orchestrator.py     the check loop, task split, file-conflict detection
  subagents.py        task definitions: config_secrets, docs, tests
  planner.py          plan command
  learner.py          learn command
  memory.py           .janus/rules.md and DECISIONS.md
  costs.py            token and cost accounting
  prompts/*.md        prompt templates
```

### 3.1 One model interface
```python
class Provider(Protocol):
    name: str
    supports_tools: bool
    def complete(self, req: ChatRequest) -> ChatResponse: ...
# ChatRequest: system, messages, tool (optional JSON schema), max_tokens, temperature
# ChatResponse: text, tool_args (dict | None), usage (input, output), cost (float | None)
```
- HTTP with `httpx` only, no vendor SDK: one small dependency, same code path for every
  OpenAI-compatible server.
- Each provider module defines a class with a `name` and registers itself. The registry
  imports every module in `providers/`, so a new provider is one new file.
- Config example (`janus-agent.example.toml`, no keys):
  ```toml
  [model]
  provider = "openai_compat"          # or "anthropic"
  model = "llama3.1:8b"
  base_url = "http://localhost:11434/v1"
  api_key_env = "OPENAI_API_KEY"      # name of the env var, never the key itself
  tool_calling = "auto"               # auto | native | json
  [pricing]                           # optional, USD per million tokens
  input_per_mtok = 0.0
  output_per_mtok = 0.0
  [check]
  max_rounds = 3
  test_command = ["python", "-m", "pytest", "-q"]
  ```
  Env vars override: `JANUS_PROVIDER`, `JANUS_MODEL`, `JANUS_BASE_URL`, `JANUS_API_KEY_ENV`.

### 3.2 Small models: structured output with fallback
- Every model answer the agent needs is a JSON object with a schema (edits, rules, contract).
- `native` mode: the schema is sent as a forced tool call.
- `json` mode: the schema goes in the prompt, the reply is parsed (code fences stripped,
  first JSON object only), then validated.
- `auto`: tries native; if the server rejects tools or answers without a tool call, it
  switches to JSON for the rest of the run.
- Invalid reply: the validation error is sent back, at most 3 attempts in total, then the
  task fails cleanly (reported, nothing applied).

### 3.3 What the model sees (never the whole project)
- The redacted finding (no `masked_value` at all, since even 4 characters are a leak).
- The cited lines ± 3 lines of context, redacted. Some finding types add targeted extra lines
  (for an undocumented endpoint, the README "endpoints" lines found by regex).
- Env files are shown as key lists only, with values replaced by `<redacted>`.
- The learned rules from `.janus/rules.md` and the built-in safety rules, in every call.
- For the tests task: the last lines of the pytest output (redacted) and the list of env vars
  read strictly by the code (from the scanner).

### 3.4 Edit operations (the model never writes files directly)
| Op | Fields | Rules |
|---|---|---|
| `replace` | file, old, new | `old` must occur exactly once. Redaction placeholders in `old` are mapped back to the real text. A placeholder in `new` is refused, so a secret can never be copied elsewhere. |
| `create` | file, content | Only for a new file (`tests/conftest.py`, new test files). Refused if the file exists. |
| `env_add` | file, key, comment | Writes `KEY=  # set in the secret manager`. **No value field exists**, so a production value can never be invented. |
| `env_remove` | file, key | Only in `.env.example`. |

Each task may only edit its own allowed files. Every edit is checked by `security/paths.py`
and `security/test_guard.py`.

### 3.5 The check loop
```
scan (Python) ─► split findings into tasks ─► run tasks in parallel (threads)
     ▲                                                     │
     │                                    collect edits, refuse conflicts
     │                                                     │
     └── re-scan + run tests in the shadow copy ◄── apply edits to the shadow copy
until 0 findings and tests green, or max_rounds reached
then: show the full diff ─► confirm (or --apply) ─► write to the real project
```
- **Task split** by fix target, not by finding file:
  A `config_secrets` owns `.env.example`, deploy configs and the files holding secrets;
  B `docs` owns the README; C `tests` owns `tests/` (fed by test failures from the previous round).
- **Dependent finding**: `security_claim_false` is held back while secret findings exist,
  because moving the secrets makes the claim true. It is sent to `docs` only if it survives
  the re-scan (the Bob mode had the same step 6).
- **Conflicts**: the owned file sets are checked before launch; at collection time, two tasks
  editing the same file are refused and the findings go back to the next round, run one
  after the other.
- **Independent verification**: success means 0 findings from the scanners and a 0 exit
  code from the tests. What the model says is only logged.
  A moved secret must also show up as a strict read (`os.environ["X"]`) in the config
  scanner's usages, or the task counts as failed.
- **Dry run by default**: everything happens in a temp copy of the project (without `.env*`
  files), so the full loop, including tests, runs without touching the real files. At the
  end, the diff is shown and written only after `y` (or `--apply`). Before writing, the
  original files are hashed again, and the write is refused if they changed during the run.
- **Commands**: the model gets no shell tool. The orchestrator runs only the scanners
  (in-process) and the test command, checked against an allowlist (`python -m pytest`,
  `pytest`). The test process gets the environment minus provider keys (`*_API_KEY`, `JANUS_*`).

### 3.6 plan
1. Scan, then build a Python index: functions, call sites (AST), routes, env vars, README
   sections, tests that reference each symbol.
2. Model call 1: extract the identifiers the described change touches (JSON). Fallback with no
   model reply: keyword match against the index.
3. Python resolves them to files, lines, call sites and missing tests.
4. Model call 2: write the contract (JSON: files, call sites, tests to add, config keys,
   README lines, security risks). Python drops any file or line not in the index, so nothing
   can be invented. The result is written to `.janus/contract.md`.

### 3.7 learn
- Reads `.janus/runs/last.json` (written by check: findings before/after, edits applied).
- One model call: a short, general rule per fixed finding type (JSON), with the existing
  rules sent along to avoid duplicates. Python also deduplicates by normalized title.
- `DECISIONS.md`: one line per fix. Date from `datetime.date.today()` (the system clock), the
  "drift" and "change" columns built by Python from the report and edits, the "why" column
  from the model.

### 3.8 Costs
Usage is read from each response (`usage.prompt_tokens`/`completion_tokens` or
`input_tokens`/`output_tokens`, plus `cost` when the server returns it, as OpenRouter does).
Prices come from `[pricing]` in the config, with no built-in price table that would go stale.
A table per task is printed at the end of each run: calls, tokens in, tokens out, cost (or "unknown").

### 3.9 Tests
- `tests/test_scanners.py`: the 6 original tests.
- Unit tests with `providers/fake.py`: native and JSON modes, retry after invalid JSON (and
  give-up after 3), redaction (a recording fake asserts that no planted secret ever appears
  in a request), edit validation, refused conflicts, test guard, path guard, `.env` never
  read, dry run writes nothing, `learn` uses the real date (date mocked in the test).
- End to end: `check` on a temp copy of `example_app` with scripted fake replies goes from
  11 findings to 0 with the app tests green. Round 1 fixes config, secrets and docs; the app
  tests then fail on `KeyError: 'JWT_SECRET'`; round 2 sends that failure to the tests task,
  which creates `conftest.py`.
- Real model: Ollama 0.40.4 with `qwen2.5-coder:3b` (CPU only) is available for manual
  live tests. A pytest live test is marked `skip` unless `JANUS_LIVE_TEST=1` and a configured
  endpoint are present, so CI never needs a model.
  First probe through `/v1/chat/completions`:
  - native tool call: **failed**. No `tool_calls` came back; the model wrote the call as text
    in a code fence, with a wrong shape (`edits.items`). About 25 s.
  - JSON mode (`response_format: {"type": "json_object"}` + schema in the prompt): valid,
    correct JSON on the first try. About 6 s.
  - `usage` is returned, so token counts work.
  Consequences: in `auto` mode, a reply without a real tool call switches to JSON mode;
  JSON mode sends `response_format` when the server accepts it; the validator and the
  retries are required, not optional.
- Test strings that look like provider keys are built at runtime (`"sk-" + "a" * 40`), so
  no literal in the repo matches a real key format and GitHub push protection stays quiet.

## 4. Risks and choices (validated)

1. **Package and command name**: package `janus_agent` (avoids clashing with the original
   `janus`), command `janus-agent`. Python ≥ 3.11 (for `tomllib`); this machine has 3.12.
2. **Fourth command `scan`** (scanners only, free, for CI and for the pre-push check). The
   brief asks for three; I suggest adding it.
3. **Shadow copy for the dry run** (3.5): it lets the full loop and the tests run before any
   write. Downside: copying a large project takes time and disk. `.venv`, `node_modules`,
   `.git` and the other `SKIP_DIRS` are skipped. **Recommended.**
4. **`httpx` instead of vendor SDKs**: fewer dependencies, one code path. Downside: new API
   features have to be wired by hand.
5. **Everything goes through the model**, even mechanical fixes such as adding a key to
   `.env.example`. That matches the goal (an AI agent) and `env_add` keeps it safe. A
   deterministic fixer would be cheaper; it could come later as an option.
6. **Rules location**: `.janus/rules.md` as asked, but `.janus/` is also where reports and
   run logs live, and many projects will ignore it in git, losing the rules. Proposal: keep
   `.janus/rules.md` by default, make the path configurable (`rules_path`), and say in the
   README to add `!.janus/rules.md` after `.janus/*` in the analyzed project's `.gitignore`.
7. **Scope stays at 3 scanners**: "impact" (untested functions, trap 4) and "triage"
   (CI log, trap 5) are not implemented in Janus either. Adding them would change the
   11-finding baseline. Proposal: future work, listed in the README limits.
8. **Test guard limits**: existing test functions must keep an identical AST. Removing a
   test, adding `skip`/`xfail`, or touching pytest config (`pytest.ini`, `addopts`,
   `pytest_collection_modifyitems`) is refused. A model could still write weak *new* tests;
   that is not detectable in general (known limit).
9. **Redaction is heuristic** (known formats, secret-named assignments, values the scanner
   flagged, long high-entropy strings). It can miss an unusual secret or mask a harmless
   hash. Every request also goes through a final check against the values the scanner found.
10. **Running the analyzed project's tests runs its code.** That is inherent to the job; the
    test process gets no provider keys and runs in the shadow copy.
11. **Small models** may fail to produce valid edits even after 3 attempts. The agent then
    reports the task as failed and applies nothing, which is safe but not useful. Quality
    depends on the model.
12. **License of the new repository**: I suggest MIT, same holder (`pro1gramer-hic`), plus
    `NOTICE` crediting Janus (MIT, Copyright (c) 2026 pro1gramer-hic, repository
    `pro1gramer-hic/Janus-bob`).
13. **Order of work** after validation: `.gitignore` (step 2), then small commits: scanners
    and their tests, `example_app`, model interface and providers, structured output,
    security layer, edits and workspace, orchestrator, plan, learn, costs, end-to-end test,
    README. Push only at the end of a validated step.

## 5. Progress

- [x] Step 1: Janus-bob read in full, this plan written.
- [x] Step 2: `.gitignore` hardened.
- [ ] Implementation (see 4.13).
