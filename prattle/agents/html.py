"""HTML synthesizer.

Two modes:
- LLM-driven: ask the backend to produce a full HTML document.
- Local fallback: render markdown + diagrams to HTML with a minimal template
  (no LLM call). Used when the backend errors or the LLM returns empty HTML.

Enforces a cooldown so we don't hammer this — it's the expensive agent.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path

import markdown as _md

from ..utils import load_prompt, now_iso, parse_json_tolerant
from .base import Agent, AgentContext
from .mermaid import read_index as read_mermaid_index

log = logging.getLogger("agent.html")

def _mmd_to_svg(code: str, timeout: float = 20.0) -> str | None:
    """Render Mermaid source to SVG via mermaid.ink. Returns SVG string or None on failure."""
    try:
        encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode()
        url = f"https://mermaid.ink/svg/{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "prattle/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            svg = resp.read().decode("utf-8")
        svg = re.sub(r"<\?xml[^?]*\?>", "", svg).strip()
        return svg
    except Exception as e:
        log.debug("mermaid.ink render failed: %s", e)
        return None


def _inject_svg_diagrams(html: str, sketches_dir: Path) -> str:
    """Replace <pre class="mermaid">…</pre> blocks with inline SVG (via Kroki).

    Falls back to leaving the block as-is (CDN rendering) if Kroki is unreachable.
    Also removes the CDN <script> tag when all diagrams are pre-rendered.
    """
    pattern = re.compile(
        r'<pre[^>]+class=["\']mermaid["\'][^>]*>(.*?)</pre>',
        re.DOTALL | re.IGNORECASE,
    )
    rendered_count = 0
    total_count = len(pattern.findall(html))

    def replace_block(m: re.Match) -> str:
        nonlocal rendered_count
        code = m.group(1).strip()
        svg = _mmd_to_svg(code)
        if svg:
            rendered_count += 1
            return f'<figure class="diagram">{svg}</figure>'
        return m.group(0)  # leave as <pre class="mermaid"> for CDN fallback

    html = pattern.sub(replace_block, html)

    # Remove CDN script only if every diagram was pre-rendered.
    if rendered_count == total_count and total_count > 0:
        html = re.sub(
            r'<script[^>]*>[^<]*mermaid[^<]*</script>',
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        log.info("html: pre-rendered %d/%d diagrams as SVG", rendered_count, total_count)
    elif rendered_count > 0:
        log.info("html: pre-rendered %d/%d diagrams (CDN fallback for rest)", rendered_count, total_count)

    return html


_FALLBACK_CSS = """
:root { color-scheme: light dark; }
body {
  max-width: 760px; margin: 2rem auto; padding: 0 1rem;
  font-family: Georgia, "New York", serif; line-height: 1.6;
  color: #1a1a1a; background: #fafaf6;
}
@media (prefers-color-scheme: dark) {
  body { color: #e8e8e3; background: #1c1c1c; }
  blockquote { color: #c0c0bd; border-left-color: #4a4a4a; }
  pre { background: #2a2a2a; }
}
h1, h2, h3, header { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
h1 { font-size: 1.7rem; margin-bottom: 0.25rem; }
header .meta { color: #888; font-size: 0.85rem; }
h2 { margin-top: 2.2rem; border-bottom: 1px solid #ddd5; padding-bottom: 0.25rem; }
blockquote { border-left: 3px solid #aaa; padding-left: 1rem; color: #555; margin-left: 0; }
pre { background: #f0eee6; padding: 0.75rem; overflow-x: auto; border-radius: 4px; }
code { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.92em; }
figure { margin: 2rem 0; }
figcaption { color: #888; font-size: 0.85rem; text-align: center; margin-top: 0.25rem; }
.diagrams { margin-top: 3rem; }
"""


def _render_local(session_name: str, notes_md: str, diagrams: list[dict]) -> str:
    """Local fallback: render without an LLM call."""
    body_html = _md.markdown(notes_md or "*(no notes yet)*", extensions=["extra", "sane_lists"])
    diagram_html_parts: list[str] = []
    for d in diagrams:
        mmd_file = d.get("file")
        title = d.get("title", d.get("id", "diagram"))
        if not mmd_file:
            continue
        # The .mmd files live next to output.html in <session_root>/sketches/<file>
        # so referencing them via inline pre-class=mermaid is the safe path.
        diagram_html_parts.append(
            f'<figure>\n<pre class="mermaid"><!--MMD:{mmd_file}--></pre>\n'
            f"<figcaption>{title}</figcaption>\n</figure>"
        )
    diagrams_section = (
        f'<section class="diagrams"><h2>Diagrams</h2>\n'
        + "\n".join(diagram_html_parts)
        + "</section>"
    ) if diagram_html_parts else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{session_name} — prattle</title>
<style>{_FALLBACK_CSS}</style>
</head>
<body>
<header>
  <h1>{session_name}</h1>
  <div class="meta">Last updated {now_iso()} · prattle · local-render fallback</div>
</header>
{body_html}
{diagrams_section}
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "default" }});
</script>
</body>
</html>
"""


def _inline_mmd_files(html: str, sketches_dir: Path) -> str:
    """Replace <!--MMD:<file>--> markers with the actual .mmd contents."""
    def repl(m: "re.Match[str]") -> str:
        fname = m.group(1)
        p = sketches_dir / fname
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8").rstrip()

    return re.sub(r"<!--MMD:(.+?)-->", repl, html)


def _render_via_cli(ctx: "AgentContext", theme: str) -> bool:
    """Render using prattle-html CLI. Returns True on success."""
    import shutil
    import subprocess as _sp

    cli = shutil.which("html-gen") or str(Path.home() / ".local" / "bin" / "html-gen")
    if not Path(cli).exists():
        log.debug("prattle-html not found at %s", cli)
        return False
    try:
        result = _sp.run(
            [
                sys.executable, cli,
                str(ctx.session_root),
                "--theme", theme,
                "--title", ctx.session_name,
                "--out", str(ctx.html_path),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            size = ctx.html_path.stat().st_size if ctx.html_path.exists() else 0
            log.info("html: wrote %s (%s, theme=%s)", ctx.html_path.name,
                     f"{size//1024}KB", theme)
            return True
        log.warning("prattle-html failed (exit %d): %s", result.returncode,
                    result.stderr.strip()[:200])
    except Exception as e:
        log.warning("prattle-html error: %s", e)
    return False


class HtmlAgent(Agent):
    name = "html"

    def describe_for_router(self) -> str:
        return (
            "**html** — synthesizes the current notes + diagrams into a polished "
            "standalone HTML document. EXPENSIVE; the system enforces a cooldown "
            "(default 60s). Invoke when there's a meaningful accumulation of "
            "new content worth rolling up — typically every few minutes."
        )

    def _cooldown_seconds(self) -> float:
        return float(self.settings.get("cooldown_seconds", 60))

    def _within_cooldown(self, html_path: Path) -> bool:
        if not html_path.exists():
            return False
        age = time.time() - html_path.stat().st_mtime
        return age < self._cooldown_seconds()

    def _theme(self) -> str:
        return str(self.settings.get("theme", "ezekiel"))

    def handle(self, ctx: AgentContext) -> None:
        if self._within_cooldown(ctx.html_path):
            log.info(
                "html: within cooldown (%ds), skipping",
                int(self._cooldown_seconds()),
            )
            return

        ctx.html_path.parent.mkdir(parents=True, exist_ok=True)

        # Try prattle-html CLI first (themed, pre-rendered SVG diagrams).
        if _render_via_cli(ctx, self._theme()):
            return

        # Fallback: local Python renderer + SVG injection.
        notes_md = ctx.notes_path.read_text(encoding="utf-8") if ctx.notes_path.exists() else ""
        diagrams = read_mermaid_index(ctx.sketches_dir)
        html = _render_local(ctx.session_name, notes_md, diagrams)
        html = _inline_mmd_files(html, ctx.sketches_dir)
        html = _inject_svg_diagrams(html, ctx.sketches_dir)
        ctx.html_path.write_text(html, encoding="utf-8")
        log.info("html: wrote %s (%d chars) [fallback]", ctx.html_path.name, len(html))

    def _llm_render(self, ctx: AgentContext, notes_md: str, diagrams: list[dict]) -> str:
        # Hydrate diagrams with code for the LLM
        hydrated = []
        for d in diagrams:
            file = d.get("file")
            code = ""
            if file:
                p = ctx.sketches_dir / file
                if p.exists():
                    code = p.read_text(encoding="utf-8")
            hydrated.append({**d, "code": code})

        system = load_prompt("html")
        user = (
            f"Session: {ctx.session_name}\n"
            f"Timestamp: {now_iso()}\n\n"
            f"=== notes.md ===\n{notes_md or '(empty)'}\n\n"
            f"=== diagrams ===\n{json.dumps(hydrated, ensure_ascii=False, indent=2)}\n\n"
            f"Produce a single polished standalone HTML document per the schema."
        )
        raw = ctx.backend.call(system, user)
        data = parse_json_tolerant(raw)
        return (data.get("html") or "").strip()
