# Documentation publishing

> **Tooling documentation, not part of the rendered template.** This describes how
> the Markdown under `docs/` (plus the vendored model spec) is rendered to the
> **pdca.dev** site. It is excluded from a rendered project's own docs.

## Overview

The `eduralph/pdca-harness` repo is the **single source of truth** for the
project's documentation. The pipeline renders that Markdown into a bespoke,
hand-rolled static site, and CI publishes the whole site to the **pdca.dev** repo.

```
docs/**/*.md  +  template/PCDA/quality-cycle/*.md
        │
        ▼  render_site.py + page.html + style.css      (render + chrome)
   static HTML in ./build
        │
        ▼  CI (docs.yml)                                (overwrite main)
   eduralph/pdca.dev  →  served at pdca.dev via GitHub Pages
```

**pdca.dev is a generated mirror** — never hand-edited. The landing page is
authored as structured content (`docs/index.yml` — words only, no markup); the
stylesheet and templates live here under `publishing/`. CI overwrites pdca.dev's
`main` branch with the built site.

## Site map

| URL | Source |
|-----|--------|
| `/` | `docs/index.yml` through `templates/home.html` (the bespoke landing) |
| `/guide/` | `docs/README.md` (the walkthrough index) |
| `/guide/<nn>-*.html` | `docs/<nn>-*.md` (the walkthrough steps 00–07) |
| `/spec/` | generated index of the quality-cycle model |
| `/spec/<nn>-*.html` | `template/PCDA/quality-cycle/<nn>-*.md` (the vendored model) |

## Layout

```
<repo root>/
├── .github/workflows/
│   ├── docs.yml          ← CI: lint → render → publish to pdca.dev (push to main)
│   └── docs-check.yml    ← CI: lint → render --check on every PR (no deploy)
└── docs/
    ├── README.md         ← GitHub-facing index of docs/     → /guide/
    ├── 00-introduction.md … 07-publish-and-act.md           → /guide/<nn>-*.html
    ├── index.yml         ← landing page as structured content → /
    └── publishing/       ← build tooling + site inputs (not published)
        ├── site/
        │   ├── assets/style.css
        │   ├── CNAME      ← pdca.dev
        │   └── .nojekyll
        ├── templates/
        │   ├── page.html  ← chrome around every rendered doc ({{TITLE}}, {{CONTENT}}, …)
        │   └── home.html  ← full-bleed landing template, filled from index.yml
        └── tools/
            ├── render_site.py   ← renders docs + spec → ./build, copies site/, audits links
            └── lint_docs.py     ← guard: no Obsidian [[wikilinks]]
```

## How a page is built

`render_site.py` discovers the published sources (the top-level `docs/*.md` and the
flat `template/PCDA/quality-cycle/*.md` set), computes each one's output URL,
renders Markdown to HTML, **rewrites relative links** — to a published page → its
site URL; to any other in-repo file (e.g. `../template/`, `../copier.yml`) → a
GitHub blob/tree URL so it still resolves — and wraps the result in
`templates/page.html`. The landing page renders to `/` from `docs/index.yml`
through `templates/home.html`; the `/spec/` index is generated from the model's
numbered files. A `--check` pass then fails the build on any dangling internal
link.

See [tools/README.md](tools/README.md) for build and deploy instructions.

## Two properties, do not conflate

| Property | What it is | Source |
|----------|-----------|--------|
| `pdca.dev` | The site: landing + guide + spec | this repo via `render_site.py` (generated mirror) |
| `github.com/eduralph/pdca-harness` | The repository | source of truth for everything, including the site |
