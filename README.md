# nsdocs

A lightweight static site generator for project wikis. Markdown in, plain HTML out.

- No template engines, no JS framework, one Python file.
- Dark/light mode, code copy buttons, GitHub-style callouts, tables, sidebar nav.
- Ships as a CLI and a GitHub Action.

## Install

```bash
pip install git+https://github.com/CStaks/nsdocs.git
```

Requires Python 3.9+ and `pyyaml`.

## Usage

1. Put Markdown files in `docs/wiki-src/`.
2. Add a `nsdocs.yml` config (see schema below).
3. Run:

```bash
nsdocs                 # build to docs/wiki/
nsdocs --check         # CI mode: fail if pages are stale
nsdocs --clean         # remove orphaned .html files
```

## Config

`nsdocs.yml` in the repo root:

```yaml
site_name: "novyra"
brand_name: "novyra"
site_url: "https://CStaks.github.io/novyra/"
accent_color: "#238FC9"
license_text: "novyra OS documentation."

repo:
  url: "https://github.com/CStaks/novyra"
  branch: "main"

assets:
  css: "../wiki.css"
  favicon_dark: "../primary-dark-logo.ico"
  favicon_light: "../primary-light-logo.ico"

features:
  theme_toggle: true
  clean_stale_files: true
  edit_button: true
  copy_button: true

nav:
  "Getting started":
    - getting-started.md
    - installation.md
```

Pages can set a title and description with front matter:

```markdown
---
name: Installation
description: How to install novyra
---
```

## GitHub Action

```yaml
- uses: CStaks/nsdocs@v1
  with:
    config: nsdocs.yml
    check: false
```

## License

MIT (CStaks). If you redistribute substantial portions of this code, keep the license notice and credit this repository.
