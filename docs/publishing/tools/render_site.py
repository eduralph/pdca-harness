#!/usr/bin/env python3
"""render_site.py -- render the pdca-harness docs into the static pdca.dev site.

The pdca-harness repo is the single source of truth: documentation is hand-written
Markdown. The landing page is authored as structured content in `docs/index.yml`.
This script renders that into a bespoke hand-rolled HTML site -- the chrome lives
in `docs/publishing/templates/` (`page.html` for docs, `home.html` for the
full-bleed landing) and the look in `docs/publishing/site/assets/style.css`. CI
publishes the whole site (landing + guide + spec) to the pdca.dev repo's `main`
branch (see docs.yml); pdca.dev is a generated mirror, not hand-edited.

Output structure (served at the apex domain pdca.dev):

    /                       docs/index.yml (structured content) -> home.html
    /guide/                 docs/README.md                (the walkthrough index)
    /guide/<nn>-*.html      docs/<nn>-*.md                (the walkthrough steps)
    /spec/                  generated index of the quality-cycle model
    /spec/<nn>-*.html       template/PCDA/quality-cycle/<nn>-*.md

Design notes:
  * Deterministic: same tree in, same site out.
  * Link rewriting: relative Markdown links between published docs are resolved to
    their output URLs; relative links to OTHER repo files (e.g. ../template/,
    ../copier.yml) are rewritten to GitHub blob/tree URLs so they still work on the
    site. The `--check` pass then fails the build on any dangling local link.
  * Mermaid: a ```mermaid fence renders client-side via a pinned mermaid.min.js,
    fetched into the build at build time (never committed). No diagrams today; the
    support is here so adding one needs no tooling change.

Usage:
    python3 docs/publishing/tools/render_site.py            # build -> ./build
    python3 docs/publishing/tools/render_site.py --check    # build + fail on dangling links
    python3 docs/publishing/tools/render_site.py -o /tmp/out
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

import yaml
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

# --- locations (this file is docs/publishing/tools/render_site.py) -----------
TOOLS = Path(__file__).resolve().parent
PUBLISHING = TOOLS.parent
DOCS_ROOT = PUBLISHING.parent          # docs/
REPO_ROOT = DOCS_ROOT.parent           # repo root
SITE_DIR = PUBLISHING / "site"
TEMPLATE = PUBLISHING / "templates" / "page.html"
HOME_TEMPLATE = PUBLISHING / "templates" / "home.html"
HOME_DATA = DOCS_ROOT / "index.yml"

# The vendored quality-cycle model, published as /spec/ (repo-relative posix).
SPEC_REL = "template/PCDA/quality-cycle"
SPEC_ROOT = REPO_ROOT / SPEC_REL

# Relative links to repo files that aren't published pages are rewritten to these
# so they resolve on the live site instead of 404ing.
GITHUB_BLOB = "https://github.com/eduralph/pdca-harness/blob/main/"
GITHUB_TREE = "https://github.com/eduralph/pdca-harness/tree/main/"

# Human label per published section, shown in the page's doc-header.
SECTION_CLASS = {
    "guide": "Walkthrough",
    "spec": "Quality-cycle model",
}

MERMAID_VERSION = "11.4.1"
MERMAID_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_NUM_RE = re.compile(r"^(\d+)")
_INLINE_FMT_RE = re.compile(r"[*_`\[\]]")


# --- small helpers -----------------------------------------------------------

def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def read_title(text: str, fallback: str) -> str:
    for line in strip_frontmatter(text).splitlines():
        m = _H1_RE.match(line)
        if m:
            return m.group(1).strip()
    return fallback


def numeric_key(name: str):
    m = _NUM_RE.match(name)
    return (int(m.group(1)) if m else 9999, name)


def prettify(stem: str) -> str:
    return " ".join(w.capitalize() for w in stem.replace("_", " ").replace("-", " ").split())


def description_of(text: str) -> str:
    """First real paragraph, flattened to plain text for the meta description."""
    body = strip_frontmatter(text)
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "-", "*", "|", "```", "<")):
            continue
        plain = _INLINE_FMT_RE.sub("", re.sub(r"\(([^)]*)\)", "", line))
        plain = re.sub(r"\s+", " ", plain).strip(" .")
        if plain:
            return (plain[:177] + "...") if len(plain) > 180 else plain
    return "PDCA Harness documentation."


# --- output-URL model --------------------------------------------------------

def out_url(rel: str) -> str | None:
    """Map a repo-relative source path (posix) to its site-absolute URL, or None
    if the file is not published."""
    if rel == "docs/README.md":
        return "/guide/"
    if rel.startswith("docs/") and rel.endswith(".md") and "/" not in rel[len("docs/"):]:
        return f"/guide/{rel[len('docs/'):-len('.md')]}.html"
    if rel.startswith(SPEC_REL + "/") and rel.endswith(".md"):
        stem = rel[len(SPEC_REL) + 1:-len(".md")]
        if "/" in stem:
            return None  # only the flat quality-cycle/*.md set
        return f"/spec/{stem}.html"
    return None


def url_to_path(out: Path, url: str) -> Path:
    if url.endswith("/"):
        return out / url.strip("/") / "index.html"
    return out / url.lstrip("/")


def section_of(url: str) -> str | None:
    parts = [p for p in url.split("/") if p]
    return parts[0] if parts else None


def normalize(base_dir: str, target: str) -> str:
    """Resolve a relative posix `target` against repo-relative `base_dir`."""
    base = base_dir.split("/") if base_dir else []
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if base:
                base.pop()
        else:
            base.append(part)
    return "/".join(base)


# --- markdown rendering ------------------------------------------------------

def make_md() -> MarkdownIt:
    md = MarkdownIt("gfm-like")
    try:
        import linkify_it  # noqa: F401
    except ImportError:
        md.disable("linkify")

    def render_fence(self, tokens, idx, options, env):
        tok = tokens[idx]
        if (tok.info or "").strip().lower() == "mermaid":
            env["has_mermaid"] = True
            return f'<div class="mermaid">\n{escapeHtml(tok.content)}</div>\n'
        return self.fence(tokens, idx, options, env)

    md.add_render_rule("fence", render_fence)
    return md


def rewrite_links(tokens, src_rel: str, url_map: dict[str, str], dangling: list[str]):
    """Rewrite relative links/images in the token stream: published docs -> output
    URL; other in-repo files -> GitHub blob/tree URL. Record unresolved ones."""
    src_dir = src_rel.rsplit("/", 1)[0] if "/" in src_rel else ""

    def resolve(href: str) -> str:
        if re.match(r"^[a-z]+:", href) or href.startswith(("//", "#", "mailto:")):
            return href
        target, _, frag = href.partition("#")
        if not target:
            return href  # pure in-page anchor
        key = normalize(src_dir, target).rstrip("/")
        if key in url_map:
            return url_map[key] + (("#" + frag) if frag else "")
        repo_path = REPO_ROOT / key
        if repo_path.is_dir():
            return GITHUB_TREE + key
        if repo_path.is_file():
            return GITHUB_BLOB + key + (("#" + frag) if frag else "")
        dangling.append(f"{src_rel} -> {href}")
        return href

    def walk(toks):
        for t in toks:
            if t.type == "link_open":
                h = t.attrGet("href")
                if h is not None:
                    t.attrSet("href", resolve(h))
            elif t.type == "image":
                s = t.attrGet("src")
                if s is not None:
                    t.attrSet("src", resolve(s))
            if t.children:
                walk(t.children)

    walk(tokens)


_TASK_RE = re.compile(r"<li>\[([ xX])\]\s")


def finish_tasklists(html_str: str) -> str:
    def sub(m):
        checked = " checked" if m.group(1).lower() == "x" else ""
        return f'<li class="task"><input type="checkbox" disabled{checked}> '
    return _TASK_RE.sub(sub, html_str)


# --- page assembly -----------------------------------------------------------

class Renderer:
    def __init__(self, out: Path):
        self.out = out
        self.md = make_md()
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.home_template = HOME_TEMPLATE.read_text(encoding="utf-8")
        self.url_map: dict[str, str] = {}
        self.titles: dict[str, str] = {}   # url -> title
        self.dangling: list[str] = []
        self.needs_mermaid = False
        self.cur_desc = "PDCA Harness documentation."

    # -- discovery --
    def discover(self) -> list[tuple[str, Path, str]]:
        """Return (rel, path, url) for every published source; populate url_map."""
        candidates = sorted(DOCS_ROOT.glob("*.md")) + sorted(SPEC_ROOT.glob("*.md"))
        sources: list[tuple[str, Path, str]] = []
        for path in candidates:
            rel = path.relative_to(REPO_ROOT).as_posix()
            url = out_url(rel)
            if url is None:
                continue
            self.url_map[rel] = url
            sources.append((rel, path, url))
        # aliases so prose can link to the section folders / dirs
        self.url_map["docs"] = "/guide/"
        self.url_map[SPEC_REL] = "/spec/"
        return sources

    # -- emit one templated page --
    def emit(self, url: str, title: str, content_html: str, doc_class: str,
             has_mermaid: bool):
        header = ""
        if doc_class:
            header = (
                '<div class="wrap">\n  <header class="doc-header">\n'
                f'    <p class="doc-class">{html.escape(doc_class)}</p>\n'
                "  </header>\n</div>"
            )
        scripts = ""
        if has_mermaid:
            self.needs_mermaid = True
            scripts = (
                '<script src="/assets/mermaid.min.js"></script>\n'
                "<script>mermaid.initialize({startOnLoad:true,theme:\"neutral\"});</script>"
            )
        page = (
            self.template
            .replace("{{TITLE}}", html.escape(title))
            .replace("{{DESCRIPTION}}", html.escape(self.cur_desc))
            .replace("{{DOC_HEADER}}", header)
            .replace("{{CONTENT}}", content_html)
            .replace("{{SCRIPTS}}", scripts)
        )
        dest = url_to_path(self.out, url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page, encoding="utf-8")
        self.titles[url] = title

    def render_markdown(self, rel: str, text: str) -> tuple[str, bool]:
        env: dict = {}
        tokens = self.md.parse(strip_frontmatter(text), env)
        rewrite_links(tokens, rel, self.url_map, self.dangling)
        body = self.md.renderer.render(tokens, self.md.options, env)
        return finish_tasklists(body), bool(env.get("has_mermaid"))

    # -- the bespoke landing page (docs/index.yml -> /) --
    def inline(self, text: str) -> str:
        return self.md.renderInline(text or "").strip()

    def home_content(self, data: dict) -> str:
        hero = data.get("hero") or {}
        ctas = []
        for c in hero.get("ctas") or []:
            cls = "btn primary" if c.get("primary") else "btn"
            arrow = ' <span class="arr" aria-hidden="true">→</span>' if c.get("arrow") else ""
            ctas.append(f'    <a class="{cls}" href="{html.escape(c["href"])}">'
                        f'{self.inline(c["label"])}{arrow}</a>')
        cta_block = ('  <div class="cta-row">\n' + "\n".join(ctas) + "\n  </div>\n") if ctas else ""
        hero_html = (
            '<div class="wrap">\n  <div class="hero">\n'
            f'    <span class="status"><span class="dot" aria-hidden="true"></span>{self.inline(hero.get("status", ""))}</span>\n'
            '    <hr class="thread-rule">\n'
            f'    <h1>{self.inline(hero.get("title", ""))}</h1>\n'
            f'    <p class="lede">{self.inline(hero.get("lede", ""))}</p>\n'
            f'    <p class="pron">{self.inline(hero.get("pron", ""))}</p>\n'
            f"{cta_block}"
            "  </div>\n</div>\n"
        )

        sections = []
        for s in data.get("sections") or []:
            parts = [f'    <p class="eyebrow">{self.inline(s.get("eyebrow", ""))}</p>',
                     f'    <h2>{self.inline(s.get("heading", ""))}</h2>']
            for para in s.get("body") or []:
                parts.append(f'    <p class="muted">{self.inline(para)}</p>')
            if s.get("props"):
                lis = "\n".join(
                    f'      <li><span class="term">{self.inline(p["term"])}</span>'
                    f'<span class="desc">{self.inline(p["desc"])}</span></li>'
                    for p in s["props"]
                )
                parts.append(f'    <ul class="props">\n{lis}\n    </ul>')
            if s.get("note"):
                quote = (f' <span class="quote">{self.inline(s["quote"])}</span>'
                         if s.get("quote") else "")
                parts.append(f'    <p class="name-note">{self.inline(s["note"])}{quote}</p>')
            sections.append("  <section>\n" + "\n".join(parts) + "\n  </section>")
        sections_html = '<div class="wrap">\n' + "\n".join(sections) + "\n</div>\n"

        return hero_html + "\n" + sections_html

    def render_home(self):
        if not HOME_DATA.exists():
            return
        data = yaml.safe_load(HOME_DATA.read_text(encoding="utf-8")) or {}
        meta = data.get("meta") or {}
        title = meta.get("title", "PDCA Harness")
        desc = meta.get("description", "PDCA Harness documentation.")
        page = (
            self.home_template
            .replace("{{TITLE}}", html.escape(title))
            .replace("{{DESCRIPTION}}", html.escape(desc))
            .replace("{{OG_TITLE}}", html.escape(meta.get("og_title", title)))
            .replace("{{OG_DESCRIPTION}}", html.escape(meta.get("og_description", desc)))
            .replace("{{CONTENT}}", self.home_content(data))
        )
        (self.out / "index.html").write_text(page, encoding="utf-8")

    # -- per-source page --
    def render_source(self, rel: str, path: Path, url: str):
        text = path.read_text(encoding="utf-8")
        section = section_of(url)
        title = read_title(text, prettify(path.stem))
        self.cur_desc = description_of(text)
        body, has_mermaid = self.render_markdown(rel, text)
        self.emit(url, title, body, SECTION_CLASS.get(section, ""), has_mermaid)

    # -- generated /spec/ index (the model has no README of its own) --
    def build_spec_index(self):
        items = sorted(
            ((u, t) for u, t in self.titles.items() if section_of(u) == "spec"),
            key=lambda it: numeric_key(it[0].strip("/").split("/")[-1]),
        )
        if not items:
            return
        lis = "\n".join(f'    <li><a href="{u}">{html.escape(t)}</a></li>' for u, t in items)
        self.cur_desc = ("The vendored PDCA quality-cycle model — the project-agnostic "
                         "reference spec the harness implements.")
        body = ("<h1>The quality-cycle model</h1>\n"
                "<p>The project-agnostic reference model the harness implements — "
                "Plan / Do / Check / Act, the 5/5/1 Check, the bundle state machine, "
                "and how to adapt it to a repo. Vendored into every rendered project "
                "under <code>PCDA/quality-cycle/</code>.</p>\n"
                f"<h2>Documents</h2>\n<ul>\n{lis}\n</ul>\n")
        self.emit("/spec/", "The quality-cycle model", body, "", False)

    # -- static assets + mermaid --
    def copy_site(self):
        for item in SITE_DIR.iterdir():
            dest = self.out / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                dest.write_bytes(item.read_bytes())

    def ensure_mermaid(self):
        if not self.needs_mermaid:
            return
        dest = self.out / "assets" / "mermaid.min.js"
        dest.parent.mkdir(parents=True, exist_ok=True)
        cache = Path(os.environ.get("MERMAID_JS")
                     or REPO_ROOT / ".cache" / f"mermaid-{MERMAID_VERSION}.min.js")
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            print(f"render_site: fetching mermaid {MERMAID_VERSION} ...", file=sys.stderr)
            with urllib.request.urlopen(MERMAID_URL) as r:  # noqa: S310 (pinned URL)
                cache.write_bytes(r.read())
        dest.write_bytes(cache.read_bytes())

    # -- link audit --
    def audit(self) -> list[str]:
        broken = []
        attr_re = re.compile(r'(?:href|src)="([^"]+)"')
        for html_file in sorted(self.out.rglob("*.html")):
            base = html_file.parent
            for ref in attr_re.findall(html_file.read_text(encoding="utf-8")):
                target, _, _ = ref.partition("#")
                if not target or re.match(r"^[a-z]+:", target) or target.startswith(("//", "mailto:")):
                    continue
                p = (self.out / target.lstrip("/")) if target.startswith("/") else (base / target)
                if target.endswith("/"):
                    p = p / "index.html"
                if not p.exists():
                    broken.append(f"{html_file.relative_to(self.out)} -> {ref}")
        return broken

    # -- orchestration --
    def run(self, check: bool) -> int:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)
        sources = self.discover()
        # pre-register titles so generated lists can reference any page
        for rel, path, url in sources:
            self.titles[url] = read_title(path.read_text(encoding="utf-8"), prettify(path.stem))
        for rel, path, url in sources:
            self.render_source(rel, path, url)
        self.build_spec_index()
        self.render_home()
        self.copy_site()
        self.ensure_mermaid()

        pages = len(list(self.out.rglob("*.html")))
        where = self.out.relative_to(REPO_ROOT) if self.out.is_relative_to(REPO_ROOT) else self.out
        print(f"render_site: wrote {pages} page(s) to {where}")

        if self.dangling:
            for d in sorted(set(self.dangling)):
                print(f"render_site: WARNING unresolved link: {d}", file=sys.stderr)
        if check:
            broken = self.audit()
            for b in broken:
                print(f"render_site: ERROR dangling link in output: {b}", file=sys.stderr)
            if broken or self.dangling:
                print(f"\nrender_site: {len(broken)} dangling output link(s), "
                      f"{len(set(self.dangling))} unresolved source link(s).", file=sys.stderr)
                return 1
            print("render_site: link audit OK")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the pdca-harness docs into the static site.")
    ap.add_argument("-o", "--out", default=str(REPO_ROOT / "build"),
                    help="output directory (default: ./build at the repo root)")
    ap.add_argument("--check", action="store_true",
                    help="fail the build on any dangling internal link")
    args = ap.parse_args()
    sys.exit(Renderer(Path(args.out).resolve()).run(args.check))


if __name__ == "__main__":
    main()
