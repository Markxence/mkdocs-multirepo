"""Convert mdBook projects (book.toml + src/SUMMARY.md) into MkDocs-Material
projects (mkdocs.yml + docs/), in place next to the book.toml.

A converted project has a standard mkdocs.yml, so the existing --scan feature
picks it up automatically afterwards.
"""

import os
import re
import hashlib
import click
import yaml
from shutil import copytree, ignore_patterns

# mdBook output dirs and vendored sources we never want to convert.
MDBOOK_IGNORE_DIRS = {".git", "node_modules", "book", ".book", "__pycache__",
                      ".cargo", ".rustup", "target", ".venv", "venv"}

_LINK_RE = re.compile(r"^(\s*)[-*+]\s+\[([^\]]*)\]\(([^)]*)\)")
_BARE_LINK_RE = re.compile(r"^\[([^\]]*)\]\(([^)]*)\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_SUMMARY_HEADING_RE = re.compile(r"^#{1,6}\s+(summary|sommaire)\s*$", re.IGNORECASE)


def convertMdbook(roots, force=False):
    converted = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(os.path.abspath(root)):
            if "book.toml" not in filenames:
                dirnames[:] = [d for d in dirnames if d not in MDBOOK_IGNORE_DIRS]
                continue
            # An mdBook project: don't descend further into it.
            dirnames[:] = []
            result = _convertOne(dirpath, force)
            if result:
                converted.append(result)
    click.echo("Converted " + str(len(converted)) + " mdBook project(s) to MkDocs.")
    return converted


def _convertOne(project_dir, force):
    import tomllib
    book_toml = os.path.join(project_dir, "book.toml")
    with open(book_toml, "rb") as f:
        meta = tomllib.load(f)
    book = meta.get("book", {})
    src_name = book.get("src", "src")
    src_dir = os.path.join(project_dir, src_name)
    summary_path = os.path.join(src_dir, "SUMMARY.md")
    if not os.path.isfile(summary_path):
        click.echo("  skip (no " + src_name + "/SUMMARY.md): " + project_dir)
        return None

    mkdocs_path = os.path.join(project_dir, "mkdocs.yml")
    docs_dir = os.path.join(project_dir, "docs")
    if (os.path.exists(mkdocs_path) or os.path.exists(docs_dir)) and not force:
        click.echo("  skip (mkdocs.yml or docs/ already exists): " + project_dir)
        return None

    # Copy the markdown sources into docs/, dropping SUMMARY.md (it becomes nav).
    copytree(src_dir, docs_dir, ignore=ignore_patterns("SUMMARY.md"), dirs_exist_ok=force)

    nav_nodes, warnings = _parseSummary(summary_path)

    # mdBook renders the first chapter at index.html; mirror that so the portal
    # link "<name>/index.html" resolves to the project's home page.
    index_md = os.path.join(docs_dir, "index.md")
    if not os.path.exists(index_md):
        first = _firstPageNode(nav_nodes)
        if first:
            src_first = os.path.join(docs_dir, first["path"])
            if os.path.isfile(src_first):
                os.rename(src_first, index_md)
                first["path"] = "index.md"

    config = {"site_name": book.get("title") or os.path.basename(project_dir)}
    if book.get("description"):
        config["site_description"] = book["description"]
    authors = book.get("authors")
    if authors:
        config["site_author"] = ", ".join(authors) if isinstance(authors, list) else str(authors)
    theme = {"name": "material"}
    if book.get("language"):
        theme["language"] = book["language"]
    config["theme"] = theme
    nav = [_emitNode(n) for n in nav_nodes if n["path"] or n["children"]]
    if nav:
        config["nav"] = nav

    with open(mkdocs_path, "w", encoding="utf8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    # Warn about mdBook-specific syntax MkDocs won't understand.
    if _hasMdbookSyntax(docs_dir):
        warnings.append("contains mdBook preprocessor syntax ({{#include}} / hidden lines) not converted")
    for w in warnings:
        click.echo("  warning [" + os.path.basename(project_dir) + "]: " + w)

    click.echo("  converted: " + project_dir)
    return project_dir


def _node(title, path=None):
    return {"title": title, "path": path, "children": []}


def _firstPageNode(nodes):
    for n in nodes:
        if n["path"]:
            return n
        child = _firstPageNode(n["children"])
        if child:
            return child
    return None


def _stripdot(path):
    if path.startswith("./"):
        return path[2:]
    return path


def _parseSummary(summary_path):
    nav = []
    warnings = []
    # stack of (indent, children_list); base appends to the current section.
    stack = [(-1, nav)]
    with open(summary_path, encoding="utf8") as f:
        lines = f.readlines()
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if _SUMMARY_HEADING_RE.match(line):
            continue
        if line.strip().startswith("---"):
            continue
        link = _LINK_RE.match(line)
        if link:
            indent = len(link.group(1).expandtabs(4))
            title = link.group(2).strip()
            path = link.group(3).strip()
            if not path:
                warnings.append("draft chapter skipped: " + title)
                continue
            while len(stack) > 1 and indent <= stack[-1][0]:
                stack.pop()
            parent_list = stack[-1][1]
            node = _node(title, _stripdot(path))
            parent_list.append(node)
            stack.append((indent, node["children"]))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            # Part title: a top-level section the following list items nest under.
            node = _node(heading.group(1).strip())
            nav.append(node)
            stack = [(-1, node["children"])]
            continue
        bare = _BARE_LINK_RE.match(line)
        if bare:
            path = bare.group(2).strip()
            if path:
                nav.append(_node(bare.group(1).strip(), _stripdot(path)))
                stack = [(-1, nav)]
            continue
    return nav, warnings


def _emitNode(node):
    children = [c for c in node["children"] if c["path"] or c["children"]]
    if children:
        kids = []
        if node["path"]:
            kids.append({node["title"]: node["path"]})
        for c in children:
            kids.append(_emitNode(c))
        return {node["title"]: kids}
    return {node["title"]: node["path"]}


def _hasMdbookSyntax(docs_dir):
    for dirpath, _dirnames, filenames in os.walk(docs_dir):
        for name in filenames:
            if not name.endswith(".md"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf8", errors="ignore") as f:
                if "{{#" in f.read():
                    return True
    return False
