# Prattle

Voice-to-notes tool for macOS. Transcribes audio in real time, runs an agentic pipeline to extract structured notes, Mermaid diagrams, and polished HTML — all while you talk.

Built on [parakeet-mlx](https://github.com/senstella/parakeet-mlx) (Apple Silicon ASR), [Textual](https://textual.textualize.io/) TUI, and Claude.

## What it does

1. **Listens** — captures mic audio in chunks, transcribes with parakeet-tdt-0.6b-v3
2. **Routes** — an LLM router decides which agents to invoke based on transcript content
3. **Agents**:
   - **Notes** — maintains a running `notes.md` with structured summaries
   - **Mermaid** — generates `.mmd` diagram files + `diagrams.md` for Obsidian
   - **HTML** — renders `output.html` via [html-gen](https://github.com/jonathanhudak/html-gen) with themed Tailwind layout
   - **Obsidian** — syncs notes to your vault
4. **TUI** — live transcript feed, pause/resume, chat injection, new session

## Requirements

- macOS (Apple Silicon recommended)
- Python 3.11+
- `ffmpeg` (for audio capture)
- Anthropic API key

Optional:
- [html-gen](https://github.com/jonathanhudak/html-gen) — themed HTML output
- [obsidian-cli](https://github.com/some-project/obsidian-cli) — Obsidian vault sync

## Install

```bash
git clone https://github.com/jonathanhudak/prattle.git
cd prattle
pip install -r requirements.txt
```

First run downloads the parakeet model (~2.1 GB). Subsequent runs are instant.

## Configure

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:

```toml
session_name = "sessions/my-session"

[backend]
model = "claude-sonnet-4-5"

[listener]
# input_device = 0   # uncomment to specify mic index
chunk_seconds = 8

[router]
min_new_chars = 200
timeout_seconds = 180

[agents.html]
theme = "ezekiel"   # requires html-gen installed
cooldown_seconds = 60
```

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### TUI (recommended)

```bash
python -m prattle tui
python -m prattle tui "my session name"
```

| Key | Action |
|-----|--------|
| `Ctrl+P` | Pause / resume transcription + routing |
| `Ctrl+N` | New session |
| `Ctrl+O` | Open output HTML in browser |
| `Ctrl+R` | Force HTML re-render |
| `Ctrl+Q` | Quit |
| `Enter` | Send chat message to router |

Chat messages are injected into the transcript as high-priority context — use them to steer the agents mid-session.

### Headless

```bash
python -m prattle run
python -m prattle run "my session name"
```

### List input devices

```bash
python -m prattle devices
```

## Session output

```
sessions/my-session/
├── transcript.jsonl    # raw ASR output + chat messages
├── notes.md            # structured notes (updated continuously)
├── diagrams.md         # Mermaid diagrams with code fences (Obsidian-ready)
├── output.html         # themed standalone HTML
└── sketches/
    ├── _index.json
    └── *.mmd
```

## Architecture

```
prattle/
├── cli.py          # argparse entry point, session naming
├── config.py       # Config dataclass, path helpers
├── listener.py     # ASR loop (parakeet-mlx), pause/resume
├── router.py       # LLM router, chat injection, agent dispatch
├── tui.py          # Textual TUI app
└── agents/
    ├── base.py     # Agent ABC, AgentContext
    ├── html.py     # HTML renderer (delegates to html-gen CLI)
    ├── mermaid.py  # Diagram extractor + .mmd writer
    └── obsidian.py # Obsidian vault sync
```

## HTML themes

HTML output uses [html-gen](https://github.com/jonathanhudak/html-gen). Install it, then set in `config.toml`:

```toml
[agents.html]
theme = "ezekiel"   # or "default", "ableton"
```
