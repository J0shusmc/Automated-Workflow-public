# Automated Workflow

A local, Python-native visual workflow builder. Think n8n's canvas, but every
tile is a real Python script or an LLM agent, and data flows left to right
between them like notebook cells.

> ⚠️ **Some assembly required.**
>
> **Released as-is.** I built this for my own machine and never polished it for
> the public. People asked for it, so here it is. It is not supported, but
> everything you need to get it running is below. Point your AI assistant
> (Claude, ChatGPT, Codex, a local model, whatever you like) at this README
> and let it help with setup. See [Setting up with an AI assistant](#setting-up-with-an-ai-assistant).

---

## Contents

- [What's in the box](#whats-in-the-box)
- [Quick start](#quick-start)
- [Hooking up an LLM ("Claudia")](#hooking-up-an-llm-claudia)
  - [Local: llama.cpp, Ollama, LM Studio, vLLM](#option-a-local-llm)
  - [Paid / hosted: OpenAI, OpenRouter, Groq, Anthropic, etc.](#option-b-paid--hosted-llm)
  - [Codex CLI agents](#option-c-codex-cli)
- [The included workflows](#the-included-workflows)
- [Configuration reference](#configuration-reference)
- [How the canvas works](#how-the-canvas-works)
- [Running it as a service](#running-it-as-a-service)
- [Security notes](#security-notes)
- [Known rough edges](#known-rough-edges)
- [Setting up with an AI assistant](#setting-up-with-an-ai-assistant)

---

## What's in the box

```
automated_workflow/      The server (pure Python stdlib, no pip installs needed)
web/                     The canvas UI (plain HTML/CSS/JS)
data/workflows/          My 9 workflows, ready to load
workflow-scripts/        Scripts the workflows call (email, PayPal, stock DB)
scripts/                 Image/video generation + export helpers
insider_refresh.py       SEC Form 4 insider-purchase tracker
policy_tracker.py        Government policy-catalyst tracker
generated-projects/      Agents and scripts the builder generated
data/files/              Sample inventory and sales CSVs for the demo workflows
docs/                    Extra notes on Shorts, Kalshi, and the Codex terminal
```

The workflows are definitions plus scripts only. **No run history, logs,
databases, or outputs are included.** Every workflow starts fresh, and the first
run creates its own data files.

## Quick start

Requirements: **Linux or macOS**, Python **3.10+**. The server uses `pty`, so
native Windows won't work. Use WSL.

```bash
git clone <this repo>
cd Automated-Workflow
python3 -m venv venv
cp .env.example .env          # then edit it (see below)
./venv/bin/python -m automated_workflow
```

Open **http://127.0.0.1:8787**. To reach it from other machines on your LAN,
run it with `--host 0.0.0.0` and open `http://<your-ip>:8787`.

The `venv` exists so workflow nodes have an interpreter at `venv/bin/python`.
If a node's configured interpreter is missing, the server falls back to the
Python it is running under and says so in the node log.

---

## Hooking up an LLM ("Claudia")

Claudia is the built-in agent. She powers **Agent** nodes, the "build an agent"
and "create a script with Claudia" features, and the local-LLM script editor.
Any server that speaks one of these APIs works:

| API style | Endpoint shape | Used by |
|---|---|---|
| OpenAI chat completions | `.../v1/chat/completions` | llama.cpp, LM Studio, vLLM, OpenAI, OpenRouter, Groq, Together, Anthropic's OpenAI-compatible endpoint, etc. |
| Ollama native | `.../api/chat` | Ollama |
| llama.cpp raw | `.../completion` | llama.cpp server |

**Endpoint resolution order** (first one that answers wins):

1. The `endpoint` set on that specific Agent node (in the node's settings)
2. `AUTOMATED_WORKFLOW_LLM_URL` from `.env` or the environment
3. Auto-detected local defaults:
   `127.0.0.1:8080` (llama.cpp), `:8081`, `:11434` (Ollama), `:1234` (LM Studio), `:8000` (vLLM)

**Model name**: the agent node's `model` field, else `AUTOMATED_WORKFLOW_LLM_MODEL`,
else `local-model`. Ollama gets `llama3.1` when nothing is set.

### Option A: local LLM

Pick one and start it. The app will usually find it automatically.

**llama.cpp** (what I used; port 8080 is reserved for it):
```bash
llama-server -m /path/to/model.gguf --port 8080 -c 8192 -ngl 99
```

**Ollama**:
```bash
ollama pull llama3.1
ollama serve                     # listens on 11434
```
```dotenv
AUTOMATED_WORKFLOW_LLM_MODEL=llama3.1
```

**LM Studio**: load a model, start the local server (port 1234), and set
`AUTOMATED_WORKFLOW_LLM_MODEL` to the model identifier LM Studio shows.

**vLLM**:
```bash
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8000
```

I ran a Qwen3-VL 30B Q4 GGUF. Anything instruct-tuned that can return clean
JSON works. Bigger models do noticeably better at the "build a script/agent"
features.

#### Start/Stop/Kill buttons (optional)

The UI's LLM Start/Stop buttons run `sudo systemctl start|stop <service>`, and
the **Kill Claudia LLM** node (used in the YouTube Shorts workflow to free VRAM
before video generation) runs `pkill -f <pattern>`. Point them at your setup:

```dotenv
AUTOMATED_WORKFLOW_LLM_SERVICE=llama-server         # your systemd unit name
AUTOMATED_WORKFLOW_LLM_KILL_PATTERN=llama-server    # pkill -f pattern for your LLM process
```

If you use Ollama or LM Studio, ignore these buttons and manage the server yourself.

### Option B: paid / hosted LLM

Any OpenAI-compatible provider works. Set the full chat-completions URL, a model,
and your key in `.env`. The key is sent as `Authorization: Bearer <key>` and is
**never** written into workflow JSON.

```dotenv
# OpenAI
AUTOMATED_WORKFLOW_LLM_URL=https://api.openai.com/v1/chat/completions
AUTOMATED_WORKFLOW_LLM_MODEL=gpt-4o-mini
AUTOMATED_WORKFLOW_LLM_API_KEY=sk-...

# OpenRouter (hundreds of models, one key)
AUTOMATED_WORKFLOW_LLM_URL=https://openrouter.ai/api/v1/chat/completions
AUTOMATED_WORKFLOW_LLM_MODEL=anthropic/claude-sonnet-4.5
AUTOMATED_WORKFLOW_LLM_API_KEY=sk-or-...

# Groq
AUTOMATED_WORKFLOW_LLM_URL=https://api.groq.com/openai/v1/chat/completions
AUTOMATED_WORKFLOW_LLM_MODEL=llama-3.3-70b-versatile
AUTOMATED_WORKFLOW_LLM_API_KEY=gsk_...

# Anthropic (OpenAI-compatible endpoint)
AUTOMATED_WORKFLOW_LLM_URL=https://api.anthropic.com/v1/chat/completions
AUTOMATED_WORKFLOW_LLM_MODEL=<a current Claude model id>
AUTOMATED_WORKFLOW_LLM_API_KEY=sk-ant-...
```

Model names change often, so check your provider's docs for current IDs.
Notes:
- With a hosted URL set, the app still tries the local defaults if the hosted
  call fails. That's harmless, but expect "endpoint failed" lines in the
  activity log when nothing is running locally.
- Temperature is capped at 0.45 on purpose. The builder prompts want
  deterministic JSON back.
- Restart the server after editing `.env`.

### Option C: Codex CLI

Some features shell out to the [OpenAI Codex CLI](https://github.com/openai/codex)
instead of Claudia:

- the **Codex workflow terminal** (an interactive Codex session with your workflow as context)
- "build agent / create script / edit script **with Codex**"
- Agent nodes whose `model` is `codex`

Install it (`npm i -g @openai/codex`), run `codex` once to sign in, and make
sure `codex` is on your `PATH` (or at `~/.local/bin/codex`).

**Heads-up:** several included Agent nodes were built with Codex
(`model: codex`). Without Codex, open those nodes and switch the builder/model
to Claudia (your LLM). Their system prompts work with any model.

---

## The included workflows

All schedules ship **disabled**. Turn on recurrence per workflow in the UI once
it runs cleanly by hand.

| Workflow | What it does | Needs |
|---|---|---|
| **Corporate Insider Refresh** | Pulls SEC EDGAR Form 4 filings, stores insider *purchases* in SQLite, and flags ones matching `insider_rules.json` (≥ $100k, officers/directors, cluster buys). Then runs the policy tracker. | `JEV_SEC_USER_AGENT` |
| **Policy Catalyst Refresh** | Scrapes White House, Commerce, DoD contracts, and DOE releases for terms like "equity stake" and "contract award", and matches them to tickers (`policy_tracker_rules.json`). | `JEV_SEC_USER_AGENT` |
| **Database Updater** | Daily stock-price database update/audit via the bundled `stockdb` package. | `POLYGON_API_KEY`, stockdb deps |
| **Intraday Update** | Intraday bars into the same stock database. | same as above |
| **Online Sales** | Reads sale-notification emails (IMAP), verifies the payment with the PayPal API, then emails the buyer their product files from `data/products/<product>`. | Email + PayPal + SMTP creds |
| **Initial-Contact** | Reads website contact-form emails, screens out spam (country, blocked names/domains, message length), and sends an auto-reply. | Email + SMTP creds |
| **Youtube Shorts** | Prompt → LLM scene writer → Z-Image-Turbo still → kills the LLM to free VRAM → LTX-2.3 image-to-video → 1080×1920 export. | GPU, model weights, LTX-2 |
| **Invetory Monitor** | Sample CSV → agent that reports low stock. | an LLM (or Codex) |
| **Untitled workflow** | Sample sales CSV → agent that says what to drop and what to bring in. | an LLM (or Codex) |

### Setting up each workflow

Every `.env.example` uses placeholders like `ENTER_EMAIL_HERE`,
`ENTER_APP_PASSWORD_HERE`, and `ENTER_PAYPAL_CLIENT_ID_HERE`. The scripts
**refuse to run** while any `ENTER_..._HERE` value is still in place and list
exactly which ones to fill in. That's on purpose: you (or your AI) have to set
them up before anything touches a real inbox or payment account.

**SEC workflows (Insider / Policy).** SEC requires a real contact in the
User-Agent. Put this in the project `.env`:
```dotenv
JEV_SEC_USER_AGENT='YourAppName ENTER_EMAIL_HERE'
```
Run once by hand: `./venv/bin/python insider_refresh.py --days 3`. Output lands in
`data/insiders.sqlite3`, `data/insider_purchases.csv`, and `data/policy_catalysts.*`.

**Stock database (Database Updater / Intraday).**
```bash
cd workflow-scripts/stock-database
python3 -m venv .venv && .venv/bin/pip install -e .
echo "POLYGON_API_KEY=your-key" > .env
```
A free Polygon.io key works. `yfinance` is also used as a fallback source.

**Online Sales.**
```bash
cd workflow-scripts/online-sales
cp .env.example .env     # fill in EMAIL_*, PAYPAL_*, SMTP_*
```
- For Gmail, use an **App Password** (Google Account → Security → App passwords), not your real password.
- PayPal: create an app at developer.paypal.com. Start with `PAYPAL_ENV=sandbox`.
- `*_TEST_MODE=true` (the default in the example) means nothing is marked read,
  charged, or emailed. Flip those to `false` only after a dry run looks right.
- Put each product's files in `data/products/<product-name>/` and map sale
  names to folders with `PRODUCT_FILE_MAP` and `PRODUCT_PRICE_MAP`.
- Stdlib only, no pip install needed.

**Initial-Contact.**
```bash
cd workflow-scripts/initial-contact
cp .env.example .env     # fill in EMAIL_*, SMTP_*, CONTACT_BUSINESS_NAME, etc.
```
Set `EMAIL_SUBJECT_FILTER` to the subject your website's contact form sends.
Stdlib only.

**Youtube Shorts (heavy).** This expects a large NVIDIA GPU. Details are in
[docs/youtube-shorts.md](docs/youtube-shorts.md).
1. Image: `python3 -m venv .venv-shorts && .venv-shorts/bin/pip install torch diffusers transformers accelerate`,
   then download **Z-Image-Turbo** weights into `models/Z-Image-Turbo/`.
2. Video: clone Lightricks' **LTX-2** repo into `external/LTX-2` (or set
   `LTX_ROOT`), follow its install into `external/LTX-2/.venv`, put the LTX-2.3
   checkpoints in `models/LTX-2.3-HQ/`, and put the Gemma text encoder and
   upscaler under `external/LTX-2/models/`.
3. Export needs `ffmpeg` on your PATH.

**Inventory / Sales demo agents.** These only need an LLM. The sample CSVs in
`data/files/` are fake data.

> **Kalshi BTC request intake** (`automated_workflow/kalshi_requests.py`,
> [docs/kalshi-btc-setup.md](docs/kalshi-btc-setup.md)) is unfinished
> scaffolding. Its workflow JSON is not included, and delivery is intentionally
> disabled.

---

## Configuration reference

Project `.env` (loaded at startup; real environment variables win):

| Variable | Default | Purpose |
|---|---|---|
| `AUTOMATED_WORKFLOW_LLM_URL` | — | Preferred LLM endpoint (full URL) |
| `AUTOMATED_WORKFLOW_LLM_MODEL` | `local-model` | Model name sent to the endpoint |
| `AUTOMATED_WORKFLOW_LLM_API_KEY` | — | Bearer token for hosted providers |
| `AUTOMATED_WORKFLOW_LLM_SERVICE` | `llama-server` | systemd unit for the Start/Stop buttons |
| `AUTOMATED_WORKFLOW_LLM_KILL_PATTERN` | `llama-server` | `pkill -f` pattern for the Kill Claudia node |
| `AUTOMATED_WORKFLOW_NODE_TIMEOUT_SECONDS` | `0` (none) | Per-node timeout |
| `AUTOMATED_WORKFLOW_PRODUCT_ROOT` | `data/products` | Product folders for Online Sales |
| `JEV_SEC_USER_AGENT` | — | Required by SEC EDGAR |
| `LTX_ROOT` | `external/LTX-2` | LTX-2 checkout for video generation |
| `KALSHI_MAIL_ENV_FILE` | `workflow-scripts/initial-contact/.env` | Mail creds for the Kalshi scanner |

Each folder under `workflow-scripts/` reads its own `.env`, and a
`.env.example` sits next to each one.

Ports: the app runs on **8787**, **8080** is left for the LLM, and the image
backend auto-detects from **8090** upward.

## How the canvas works

- **Create** nodes: Python code, Input, Output display, Agent (Claudia),
  Data file, Product picker, Breakpoint, and Kill Claudia.
- **Add Action** opens a file picker for any `.py` on disk. The node keeps a
  *reference* to that script, runs it from its project folder, and auto-detects
  that project's `.venv`/`venv`.
- Drag from an output handle to an input handle to link nodes. Runs go in
  dependency order, each node in its own process.
- Inline Python gets upstream values in `inputs` (a dict) and sets `result`:
  ```python
  result = {"image_url": "https://example.com/image.png"}
  ```
- Script nodes get upstream values bridged to CLI args (for example an upstream
  prompt becomes `--prompt`).
- Workflows save to `data/workflows/<name>-<id>/workflow.json`. Paths inside are
  relative to the project root, so the folder can live anywhere.
- Schedules: interval or daily-at-time, with weekday selection and silent or
  verbose display.

More background is in [PROJECT_VISION.md](PROJECT_VISION.md).

## Running it as a service

Linux with systemd user services:
```bash
./scripts/install-startup-service.sh
systemctl --user restart automated-workflow
journalctl --user -u automated-workflow -f
```
The installer fills in your project path. The service binds `0.0.0.0:8787` and
loads the project `.env`.

## Security notes

Read these before exposing the app to a network:

- **There is no authentication.** Anyone who can reach the port can run
  arbitrary Python on your machine. Keep it on `127.0.0.1`, or put it behind a
  VPN or reverse proxy with auth.
- Workflow nodes run as trusted local code with your user's permissions.
- The LLM Start/Stop buttons can send a sudo password to `sudo -S`. Prefer a
  sudoers rule for that one command, or don't use the buttons.
- Keep `.env` files out of git. The included `.gitignore` already covers them.
- The email workflows read real inboxes and send real mail. Use test mode first.

## Known rough edges

- Two tests fail as shipped: `test_canvas_wiring_produces_held_draft` expects the
  unreleased Kalshi workflow file, and one insider-refresh rerun test fails.
  Run the tests with `./venv/bin/python -m unittest discover -s tests`.
- Some nodes still carry an `x/y` layout tuned for my screen.
- The workflow names are mine ("Invetory Monitor", "Untitled workflow"). Rename freely.
- No Windows support outside WSL.

---

## Setting up with an AI assistant

Paste this into your assistant of choice from inside the repo:

> I cloned Automated Workflow, a local Python visual workflow builder. Read
> `README.md` and help me:
> 1. Create the venv and `.env`, and start the server on port 8787.
> 2. Connect an LLM. I want to use **[local: Ollama / llama.cpp / LM Studio]**
>    or **[hosted: OpenAI / OpenRouter / Groq / Anthropic]**. Set
>    `AUTOMATED_WORKFLOW_LLM_URL`, `_MODEL`, and (for hosted) `_API_KEY`, then
>    verify with a test Agent node.
> 3. Pick which included workflows I want (see "The included workflows"), set
>    up only those `workflow-scripts/*/.env` files, and run each once in test
>    mode before I enable any schedule.
> 4. If I don't have the Codex CLI, switch the Codex-built agent nodes to Claudia.
> Don't commit any `.env` file or put secrets in workflow JSON.

## License

MIT. See [LICENSE](LICENSE). Do whatever you like with it.
