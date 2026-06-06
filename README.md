# mkdocs-multirepo

[![PyPI version](https://badge.fury.io/py/mkdocs-multirepo.svg)](https://badge.fury.io/py/mkdocs-multirepo)

A bit like [monorepo](https://github.com/spotify/mkdocs-monorepo-plugin), but keeps MkDocs projects separate.

> **Note: This tool is in beta.** 

## Use Case

This CLI tool allows you to build multiple MkDocs documentation projects and generate a landing page for them:

![Landing Page Example](demo.png)

Unlike [monorepo](https://github.com/spotify/mkdocs-monorepo-plugin), multirepo doesn't merge projects into one. 

Instead, multirepo adds the MkDocs projects as Git submodules, builds them individually, and generates an HTML landing page based on a template file.

This has a number of advantages, for example:

- Keeps the individual mkdocs.yml settings of each project. This means that, e.g., each project can have its own color set or theme.
- Avoids problems with relative paths in the projects.
- Keeps search indexes small instead of creating a giant merged index.

## Installation

1. Install via `pip install mkdocs-multirepo`.
2. Create a directory and put two files named `config.yml` and `index.tpl` in it.
3. Configure the files as described below.
4. Change to the directory created in step 1.
5. Run `git init`.
6. Run `mkdocs-multirepo --init`.

## Usage

```
Usage: mkdocs-multirepo [OPTIONS]

Options:
  --init      Initialize the repos as Git submodules.  [default: False]
  --update    Update the repos, i.e., the Git submodules.  [default: False]
  --build     Build all MkDocs projects and generate the landing page.
              [default: False]
  --scan PATH Scan a directory for local MkDocs projects (directories
              containing an mkdocs.yml) and add them as local repos.
              Repeatable.
  --convert-mdbook PATH
              Convert mdBook projects (book.toml) found under a directory
              into MkDocs-Material projects in place. Repeatable.
```

### Scanning for local projects

Instead of (or in addition to) declaring remote repos in `config.yml`, you can let multirepo discover MkDocs projects already present on your filesystem. A project is any directory that directly contains an `mkdocs.yml`.

```
mkdocs-multirepo --scan /path/to/projects --build
```

This walks the given path, and for every `mkdocs.yml` found:

- adds it as a **local repo** (built in place, no Git submodule, so `--init`/`--update` skip it),
- derives `name` from the directory name (deduplicated on collision),
- derives `title` from the project's `site_name`,
- uses no image (the landing-page entry is rendered without an icon).

Directories named `.git`, `node_modules`, `site`, `__pycache__`, `.venv`, `venv`, `.tox`, `.eggs` are skipped, and a project's own subtree is not descended into (so a nested `docs/mkdocs.yml` is ignored). The `--scan` option can be given multiple times.

You can also declare scan roots in `config.yml` instead of on the command line:

```yml
scan_dirs:
  - /path/to/projects
  - /another/path
```

When scanning, `config.yml` only needs the general settings (`target_dir`, `index_tpl`, etc.); the `repos` list is optional.

### Converting mdBook projects

If some of your documentation is written with [mdBook](https://rust-lang.github.io/mdBook/) rather than MkDocs, multirepo can convert it so that `--scan` picks it up:

```
mkdocs-multirepo --convert-mdbook /path/to/projects
```

This walks the path, and for every mdBook project (a directory containing a `book.toml`) writes an MkDocs-Material project **in place**, next to the `book.toml`:

- `book.toml` `title`/`description`/`authors`/`language` map to `site_name`/`site_description`/`site_author`/`theme.language`.
- `theme` is set to `material`.
- `src/SUMMARY.md` is parsed into a hierarchical `nav` (part headings become sections, nested list items become sub-pages).
- `src/` markdown is copied into `docs/`, and the first chapter is renamed to `index.md` so the project has a home page.

Projects that already have a `mkdocs.yml` or `docs/` directory are skipped (not overwritten). mdBook preprocessor syntax (`{{#include}}`, hidden code lines) is **not** translated; affected projects are reported so you can fix them by hand. Conversion requires Python 3.11+ (uses `tomllib`).

A typical end-to-end run converts then scans then builds:

```
mkdocs-multirepo --convert-mdbook ~/projects --scan ~/projects --build
```

Scanned projects whose `mkdocs.yml` is byte-for-byte identical (the same project copied to several locations) are deduplicated automatically; only the first occurrence is kept.

### Landing-page layout

Set `layout` in `config.yml` to choose how projects are rendered on the landing page:

```yml
layout: cards   # or: list (default)
```

- `list` (default): the classic `<ul>/<li>` markup, backward compatible with existing templates and stylesheets.
- `cards`: a responsive grid of cards, each showing the project's `site_name` as title, its directory name as a subtitle (so projects sharing a `site_name` stay distinct), and its `site_description` as body text. A self-contained stylesheet is injected automatically, so no extra CSS file is required.

A `<meta charset="utf-8">` tag is injected into the generated page when missing, so accented titles and dashes render correctly.

## Configuration

### Sample Project

See `mkdocs_multirepo/demo` for a sample project. 

> **Note:** Search is not functional. You must implement it yourself, e.g., using [Docsearch](https://docsearch.algolia.com/).

### config.yml

Use the `config.yml` file to configure the build process. Example:

```yml
repos:
  - name: repo-1
    title: My Repository 1
    image: images/icon-repo-1.png
    url: https://github.com/giansalex/mkdocs-sample.git
    branch: master
  - name: repo-2
    title: My Repository 2
    image: images/icon-repo-2.png
    url: https://github.com/hristo-mavrodiev/mkdocs-sample.git
    branch: master
    index_html: install/index.html
element_id: multirepo
target_dir: site
repos_dir: src
index_tpl: index.tpl
extra_files:
    - styles.css
    - images/search.svg
```

Each entry under `repos` configures an MkDocs project:

- `name`: Used to create the Git submodule directory and also the output directory within `target_dir`.
- `title`: Text for the landing page list item.
- `image`: Image for the landing page list item.
- `url`: URL of the repository.
- `branch`: Branch of the repository. Default: empty (which is `master` for most repositories).
- `mkdocs_dir`: Directory (within repo) where the MkDocs directory structure is located. Default: `.`.
- `mkdocs_config`: MkDocs config file used during `mkdocs build`. Default: `mkdocs.yml`.
- `index_html`: Index HTML file for this repository. Default: `index.html`.
- `pdf`: Link to a PDF file within the repository, if desired.
- `element_id`: ID of the DOM element on the landing page where the links to this repo project should be created. Default: use general setting (see below).

Additionally, the following general settings are available:

- `element_id`: ID of the DOM element on the landing page where the links to the projects should be created. Default: `multirepo`.
- `target_dir`: Output directory. Default: `site`.
- `repos_dir`: Target directory for repositories (submodules). Default: `repositories`.
- `extra_files`: Additional files to be placed in the output directory.
- `index_tpl`: Path to the landing page template (see below). Default: `index.tpl`.

### index.tpl

Use the `index.tpl` file to configure the landing page. Example:

```yml
<html>
    <head>
        <title>Multirepo Demo Page</title>
        <link rel="stylesheet" type="text/css" href="styles.css">
    </head>
    <body>
        <section id="multirepo"></section>
    </body>
</html>
```

The template must be written in HTML and must contain a node with an ID called "multirepo" or as defined using the `element_id` setting.

From this template, a landing page named `index.html` will be generated and placed into `target_dir`.

Sample output:

```html
<html>
    <head>
        <title>Multirepo Demo Page</title>
        <link href="styles.css" rel="stylesheet" type="text/css" />
    </head>
    <body>
        <section id="multirepo">
            <ul>
                <li><a href="repo-1/index.html"><img src="images/icon-repo-1.png" /><span>My Repository 1</span></a></li>
                <li><a href="repo-2/01_index.html"><img src="images/icon-repo-2.png" /><span>My Repository 2</span></a></li>
            </ul>
        </section>
    </body>
</html>
```
