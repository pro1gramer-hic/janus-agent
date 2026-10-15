# Janus Agent

Looks before. Checks after. Never forgets. Works with any language model.

Janus Agent keeps a repository honest. Before a change, it tells you what the change will
touch. After the change, it checks that configuration, environment variables, the README,
secrets and tests still match the code, fixes what drifted, and verifies the fix with the
scanners and the project's own tests. Then it turns each fix into a project rule.

It is the standalone version of [Janus](https://github.com/pro1gramer-hic/Janus-bob), a
workflow built for the IBM Bob hackathon. It does not need IBM Bob: it talks to any
OpenAI-compatible server (OpenAI, Ollama, LM Studio, vLLM, Groq, self-hosted servers) or to
Anthropic, and adding a provider is one file.

## How it works

| Command | Model? | What it does |
|---|---|---|
| `janus-agent scan` | no | Runs the three scanners (config, docs, secrets) and writes `.janus/report.json`. Free. |
| `janus-agent plan "<change>"` | yes | Maps the functions, call sites, routes, env vars, README lines and tests a change will touch, and writes `.janus/contract.md`. |
| `janus-agent check` | yes | Scans, fixes with parallel sub-agents, re-scans and runs the tests, repeats up to `max_rounds`, then shows the diff and writes only after confirmation. |
| `janus-agent learn` | yes | Turns each fix of the last applied check into a rule in `.janus/rules.md` and a dated line in `DECISIONS.md`. |

```
scan (Python, free) ─► split findings by target file ─► sub-agents in parallel
      ▲                  config_secrets │ docs │ tests        (own context each)
      │                                 ▼
      │                      validate every edit, refuse conflicts
      │                                 ▼
      └── scanners + tests decide ◄── apply in a temporary copy of the project
until 0 findings and green tests, or max_rounds ─► diff ─► confirm or --apply ─► write
```

- **Scanners stay free.** All reading of the project is plain Python (AST and regex). The
  model never sees the project: only the redacted findings and the cited lines.
- **The model is never believed.** A check succeeds only when the scanners report zero
  findings and the test command exits with 0.
- **Small models work.** If a model does not make native tool calls, the agent switches to
  strict JSON, validates every reply against a schema, and retries an invalid reply with the
  errors (3 attempts by default). Each finding comes with a fix hint computed in Python;
  the model decides, the agent verifies.
- **Memory.** Rules learned by `learn` are sent with every model call.
- **Costs are visible.** Every run prints tokens sent and received per task, and a cost
  estimate when `[pricing]` is set (or when the server reports the cost).

## Install

Python 3.11 or newer.

```bash
git clone https://github.com/pro1gramer-hic/janus-agent.git
cd janus-agent
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'      # or: pip install -e .   (without the test tools)
janus-agent --version
```

## Configure a model

Copy the example and edit it:

```bash
cp janus-agent.example.toml janus-agent.toml
```

The agent reads `janus-agent.toml` and `janus-agent.local.toml` from the current
directory, then `~/.config/janus-agent/config.toml`, or the file given with `--config`.
It never reads configuration from the project it analyzes. Environment variables override
the files, and command line flags override both:

| Setting | Env variable | Flag |
|---|---|---|
| provider | `JANUS_PROVIDER` | `--provider` |
| model | `JANUS_MODEL` | `--model` |
| server address | `JANUS_BASE_URL` | `--base-url` |
| name of the variable holding the key | `JANUS_API_KEY_ENV` | |
| tool calling (`auto`, `native`, `json`) | `JANUS_TOOL_CALLING` | `--tool-calling` |

**API keys never go in a committed file.** Set `api_key_env` to the name of the
environment variable that holds the key. A literal `api_key` is refused in
`janus-agent.toml`; it is only accepted in `janus-agent.local.toml`, which `.gitignore`
excludes.

Without any configuration, the agent uses a local Ollama server
(`http://localhost:11434/v1`, model `qwen2.5-coder:3b`): nothing leaves the machine.

### Ollama (local, free)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve                      # keep it running
ollama pull qwen2.5-coder:3b
```
```toml
[model]
provider = "openai_compat"
model = "qwen2.5-coder:3b"
base_url = "http://localhost:11434/v1"
```

### LM Studio (local)

Start the local server in LM Studio, then:
```toml
[model]
provider = "openai_compat"
model = "qwen2.5-coder-7b-instruct"     # the name LM Studio shows
base_url = "http://localhost:1234/v1"
```
From WSL, LM Studio runs on Windows: use the Windows host address or WSL mirrored networking.

### OpenAI

```toml
[model]
provider = "openai_compat"
model = "gpt-4.1-mini"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
```

### Groq

```toml
[model]
provider = "openai_compat"
model = "llama-3.3-70b-versatile"
base_url = "https://api.groq.com/openai/v1"
api_key_env = "GROQ_API_KEY"
```

### vLLM and other self-hosted OpenAI-compatible servers (AMD, ...)

```toml
[model]
provider = "openai_compat"
model = "your-model-name"
base_url = "http://your-server:8000/v1"
api_key_env = "MY_SERVER_KEY"     # leave out if the server needs no key
```

### Anthropic

```toml
[model]
provider = "anthropic"
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"
# fallbacks = "default"   # optional server-side fallback when a request is declined
```

### Prices

```toml
[pricing]
input_per_mtok = 2.0     # USD per million input tokens
output_per_mtok = 10.0
```

### Add a provider

Create one file in `janus_agent/providers/` with a subclass of
`janus_agent.llm.base.Provider` that has a `name` and a `complete(request)` method. It is
discovered automatically; set `provider = "<name>"` in the config.

## Examples

The repository ships `example_app/`, a small FastAPI app with planted drift.

```bash
janus-agent scan example_app
# 11 issue(s): a missing env var that crashes production, 2 hardcoded secrets,
# 6 false README claims...

janus-agent plan "give urgent tasks a bigger priority boost" --path example_app
# writes example_app/.janus/contract.md: calculate_priority is defined in
# app/services.py, used in app/main.py:19 and app/reports.py:5, and has no test

janus-agent check example_app            # dry run: shows the diff, asks before writing
janus-agent check example_app --apply    # writes without asking

janus-agent learn example_app --apply    # rules in .janus/rules.md, lines in DECISIONS.md
```

Exit codes: `0` success, `1` problems left (or a blocking finding for `scan`), `2` usage or
configuration error, `3` changes proposed but not written.

## Security

The model may be remote, so the rules are stricter than in Janus:

- `.env` and every `.env.*` file except `.env.example` are never read, copied or edited.
  Neither are private keys, certificates or `*.local.toml` files.
- Everything sent to the model is redacted first: known key formats, values assigned to
  secret-looking names, long random tokens, and every value the secrets scanner found.
  Env files are shown as keys only. A last check refuses to send a request that still
  contains a known secret.
- A production value is never invented: the `env_add` operation has no value field, it
  writes `KEY=  # set in the secret manager`.
- A moved secret must be read with `os.environ["NAME"]` so the app fails at startup if it
  is missing. The agent checks this after every round.
- Tests are never weakened: removing, renaming, changing or skipping an existing test,
  lowering the number of assertions, or touching test configuration is refused.
- Dry run by default: all fixes and test runs happen in a temporary copy of the project.
  The diff is shown and written only after confirmation or with `--apply`, and only if the
  original files did not change in the meantime. Secret values are hidden in the diff.
  `--apply` never writes changes that make previously passing tests fail.
- The model gets no shell. The agent runs only the scanners (in-process) and the test
  command, which must match an allowlist (`python -m pytest`, `pytest`). The test process
  gets the environment without API keys, tokens or passwords.
- Each sub-agent may only edit its own files; two sub-agents editing the same file is
  refused.

### About the fake secrets in example_app

`example_app/app/config.py` contains two **fake, intentional** secrets
(`JWT_SECRET`, `ADMIN_PASSWORD`). They exist so the secrets scanner has something to find.
They do not match any real provider key format. The full list of planted issues is in
[docs/traps.md](docs/traps.md). Tests that need strings shaped like provider keys build
them at runtime, so no literal in this repository looks like a real key.

## Tests

```bash
pip install -e '.[test]'
pytest                      # unit and end-to-end tests, no network, no cost
```

- The six scanner tests from Janus.
- Unit tests with a scripted fake provider: structured output, JSON fallback and retries,
  redaction, path rules, edit validation, test guard, providers' HTTP payloads.
- End to end: `check` on a copy of `example_app` goes from 11 findings to 0 with the app's
  tests green, writes nothing in dry-run mode, never sends a planted secret, and refuses
  conflicting edits and weakened tests.
- Optional live test against a real OpenAI-compatible model:
  ```bash
  JANUS_LIVE_TEST=1 JANUS_MODEL=qwen2.5-coder:3b JANUS_BASE_URL=http://localhost:11434/v1 pytest -m live -s
  ```

## Known limits

- Three scanners only: configuration, docs and secrets, for Python projects (FastAPI and
  Flask routes). The "impact" (untested functions) and "failure triage" (CI logs)
  scanners of the original plan are not implemented: traps 4 and 5 of `example_app` are not
  detected.
- Redaction is heuristic. It can miss an unusual secret format or hide a harmless hash.
- The test guard protects existing tests, but cannot judge the quality of new tests.
- Running the project's tests runs the project's code (in a temporary copy, without keys).
- Small local models are slow on CPU and make mistakes; the agent refuses bad edits and
  reports what is left, but cannot make a weak model right. Tested with Ollama and
  `qwen2.5-coder:3b` running on CPU only: `example_app` went from 11 findings to 0 with green
  tests in about 6 minutes, but an earlier run stopped at 4 findings. Results vary between
  runs.
- Only Ollama was tested against a real server. The OpenAI-compatible and Anthropic
  request formats are covered by unit tests, not by live calls.
- `.janus/` holds reports and run logs. If your project ignores `.janus/` in git but you
  want to keep the rules, add `!.janus/rules.md` after `.janus/*` in its `.gitignore`, or
  set `[memory] rules_path` to another file.

## License

MIT. Includes code from Janus (MIT, Copyright (c) 2026 pro1gramer-hic); see
[NOTICE](NOTICE).
