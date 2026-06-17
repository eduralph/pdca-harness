# docs/publishing/tools — documentation publishing

The repository is the single source of truth for documentation. These tools
render the Markdown under `docs/` (and the vendored model spec under
`template/PCDA/quality-cycle/`) into the bespoke hand-rolled pdca.dev site, and CI
publishes it. Nothing here adds or rewrites content; it renders, wires up
navigation, and guards conventions.

## What each piece does

| File | Purpose |
|------|---------|
| `render_site.py` | Renders the published Markdown → static HTML in `./build`, using `../templates/page.html` for chrome and `../site/` for assets. Renders the landing page (`/`) from `docs/index.yml` through `../templates/home.html`. Rewrites relative links (published → output URL; other in-repo files → GitHub URL), generates the `/spec/` index, and (`--check`) fails on any dangling internal link. |
| `lint_docs.py` | Fails the build on Obsidian-only `[[wikilink]]` / `![[embed]]` syntax. |
| `../site/` | The stylesheet (`assets/style.css`), `CNAME` (`pdca.dev`), and `.nojekyll`. Copied verbatim into the output. |
| `../templates/page.html` | Page chrome wrapped around every rendered doc: `{{TITLE}}`, `{{DESCRIPTION}}`, `{{DOC_HEADER}}`, `{{CONTENT}}`, `{{SCRIPTS}}`. |
| `../templates/home.html` | Full-bleed landing template; `render_site.py` fills `{{CONTENT}}` from `docs/index.yml`. |
| `../../../.github/workflows/docs.yml` | Lints, renders, and deploys the whole site to the pdca.dev repo on every push to `main` that touches docs. |
| `../../../.github/workflows/docs-check.yml` | The PR-time counterpart: lints + renders `--check` without deploying. |

## Local build

Two Python dependencies — `markdown-it-py` (the `[linkify]` extra enables
bare-URL autolinking; the renderer also runs without it) and `PyYAML` (reads the
landing page's `index.yml`):

```sh
pip install "markdown-it-py[linkify]" PyYAML     # CI pins ==3.0.0 / ==6.0.2
```

Then, from the repo root:

```sh
python3 docs/publishing/tools/lint_docs.py             # optional; CI runs it anyway
python3 docs/publishing/tools/render_site.py --check   # builds ./build, audits links
python3 -m http.server -d build 8000                   # preview at http://localhost:8000
```

The renderer only fetches `mermaid.min.js` (pinned, into `./.cache/`) if a page
actually contains a ```mermaid fence — there are none today, so a normal build is
fully offline. Set `MERMAID_JS` to a local file to skip the download if you add
one. The scripts are standalone (stdlib + the two deps), no project install
needed.

## Editing in Obsidian

You can open the `docs/` folder as an Obsidian vault — what you edit is what
ships. Keep output portable:

- **Settings → Files & Links → Use [[Wikilinks]]: off.** Write standard
  `[label](relative/path.md)` links. `lint_docs.py` fails the build if a wikilink
  slips through.
- **New link format: Relative path to file.**

## Generated / build files (git-ignored)

`render_site.py` regenerates the site on every build, so the outputs are not
committed (see the root `.gitignore`):

```gitignore
/build/
/.cache/
```

## CI secret and target

`docs.yml` deploys to the repo named in its `env:` block (`DOCS_REPO`,
`SITE_DOMAIN`). Before the first run: enable deploy keys for the org (Settings →
Repository → Deploy keys → Enabled), then add a repository secret
`DOCS_DEPLOY_KEY` — the private half of an ed25519 keypair whose public half is
registered, with write access, as a deploy key on the **eduralph/pdca.dev** repo.
The workflow overwrites that repo's `main` branch (its Pages source) with the
built site, setting `CNAME = pdca.dev`.
