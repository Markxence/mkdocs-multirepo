import yaml
import os
import re
import json
import hashlib
import datetime
import click
from bs4 import BeautifulSoup
from shutil import copy2

# Directories never worth descending into when scanning for MkDocs projects.
SCAN_IGNORE_DIRS = {".git", "node_modules", "site", "__pycache__",
                    ".venv", "venv", ".tox", ".eggs"}

class DefaultHelp(click.Command):
    def __init__(self, *args, **kwargs):
        context_settings = kwargs.setdefault('context_settings', {})
        if 'help_option_names' not in context_settings:
            context_settings['help_option_names'] = ['-h', '--help']
        self.help_flag = context_settings['help_option_names'][0]
        super(DefaultHelp, self).__init__(*args, **kwargs)

    def parse_args(self, ctx, args):
        if not args:
            args = [self.help_flag]
        return super(DefaultHelp, self).parse_args(ctx, args)

@click.command(cls=DefaultHelp)
@click.option("--init", help="Initialize the repos as Git submodules.", is_flag=True, show_default=True)
@click.option("--update", help="Update the repos, i.e., the Git submodules.", is_flag=True, show_default=True)
@click.option("--build", help="Build all MkDocs projects and generate the landing page.", is_flag=True, show_default=True)
@click.option("--scan", "scan_paths", help="Scan a directory for local MkDocs projects (dirs containing mkdocs.yml) and add them as local repos. Repeatable.", multiple=True, type=click.Path(exists=True, file_okay=False))
@click.option("--convert-mdbook", "convert_mdbook", help="Convert mdBook projects (book.toml) found under a directory into MkDocs-Material projects in place. Repeatable.", multiple=True, type=click.Path(exists=True, file_okay=False))

def cli(init, update, build, scan_paths, convert_mdbook):

    # Convert mdBook projects first so a subsequent --scan picks them up.
    if convert_mdbook:
        from .mdbook import convertMdbook
        convertMdbook(list(convert_mdbook))
        if not (init or update or build or scan_paths):
            return

    config = loadConfig()

    # Discover local MkDocs projects from --scan paths and/or the scan_dirs
    # config key, and append them to the repos list as local (non-submodule) entries.
    scan_roots = list(scan_paths) + list(config.get("scan_dirs", []))
    if scan_roots:
        used_names = {repo["name"] for repo in config["repos"] if "name" in repo}
        scanned = scanProjects(scan_roots, used_names)
        click.echo("Scanned " + str(len(scanned)) + " local MkDocs project(s).")
        config["repos"].extend(scanned)

    if init:
        # Initialize the repos as Git submodules
        click.echo("Adding submodules ...")
        cwd = os.path.abspath(os.getcwd())
        for repo in config["repos"]:
            if "local_path" in repo:
                # Local (scanned) project: built in place, not a submodule.
                continue
            # Add repo as git submodule
            repo_dir = os.path.abspath(config["repos_dir"] + os.path.sep + repo["name"])
            os.system("git -c http.sslVerify=false submodule add " + repo["url"] + " " + repo_dir)
            if "branch" in repo:
              repo_branch = repo["branch"]
              click.echo("Using branch " + repo_branch + " in repository " + repo_dir)
              os.chdir(repo_dir)
              os.system("git checkout " + repo_branch)
              os.chdir(cwd)
        click.echo("Done.")

    if update:
        # Update the repos, i.e., the Git submodules
        click.echo("Updating submodules ...")
        os.system("git -c http.sslVerify=false submodule update")
        click.echo("Done.")

    if build:
        # Build MkDocs projects
        # Copy image files and build projects
        click.echo("Building projects ...")
        cwd = os.path.abspath(os.getcwd())
        os.makedirs(config["target_dir"], exist_ok=True)
        built_repos = []
        failed_repos = []
        for repo in config["repos"]:
            if "local_path" in repo:
                # Local (scanned) project: build straight from its directory.
                repo_dir = repo["local_path"]
            else:
                repo_dir = os.path.abspath(config["repos_dir"] + os.path.sep + repo["name"])
            if not "mkdocs_dir" in repo:
                repo["mkdocs_dir"] = "."
            if not "mkdocs_config" in repo:
                repo["mkdocs_config"] = "mkdocs.yml"

            repo_site_dir = os.path.abspath(config["target_dir"] + os.path.sep + repo["name"])
            os.chdir(repo_dir + os.path.sep + repo["mkdocs_dir"])
            rc = os.system("mkdocs build --config-file " + repo["mkdocs_config"] + " --site-dir " + repo_site_dir)
            os.chdir(cwd)

            if rc != 0:
                # Build failed (e.g. missing theme/plugin); skip it so the
                # landing page doesn't link to a project that wasn't built.
                failed_repos.append(repo["name"])
                continue

            # Some projects have no index.md at their docs root (the nav home is
            # a sub-page), so no <name>/index.html is produced. Point the card at
            # the project's first nav page (falling back to the shallowest built
            # index.html) instead of a 404.
            if "index_html" not in repo and not os.path.isfile(os.path.join(repo_site_dir, "index.html")):
                entry = navEntryHtml(repo, repo_site_dir) or findEntryHtml(repo_site_dir)
                if entry:
                    repo["index_html"] = entry

            if "image" in repo:
                repo_target_image = os.path.abspath(config["target_dir"] + os.path.sep + repo["image"])
                os.makedirs(os.path.dirname(repo_target_image), exist_ok=True)
                copy2(repo["image"], repo_target_image)

            built_repos.append(repo)

        if failed_repos:
            click.echo("Skipped " + str(len(failed_repos)) + " project(s) that failed to build: " + ", ".join(failed_repos))

        # Copy extra files
        if "extra_files" in config:
            click.echo("Copying extra files ...")
            for extrafile in config["extra_files"]:
                os.makedirs(os.path.dirname(config["target_dir"] + os.path.sep + extrafile), exist_ok=True)
                copy2(extrafile, config["target_dir"] + os.path.sep + extrafile)

        # Record the first time each project is seen, so cards can show a
        # stable "date added" instead of a filesystem mtime.
        added_state_path = config.get("added_state", ".mr-added.json")
        added_state = loadAddedState(added_state_path)
        today = datetime.date.today().isoformat()
        for repo in built_repos:
            key = repo["local_path"] if "local_path" in repo else repo["name"]
            if key not in added_state:
                # First time seen: seed from the mkdocs.yml mtime, a realistic
                # proxy for when the project was added. Sticky afterwards.
                added_state[key] = mkdocsDate(repo) or today
            repo["date_added"] = added_state[key]
        saveAddedState(added_state_path, added_state)

        # Generate index.html based on template
        click.echo("Generating landing page ...")
        soup = loadTemplate(config["index_tpl"])
        ensureCharset(soup)
        layout = config.get("layout", "list")
        if layout == "cards":
            injectCardStyles(soup)
            injectCardScripts(soup)
        element = soup.find(id=config["element_id"])
        for repo in built_repos:
            index_html = repo["index_html"] if "index_html" in repo else "index.html"
            repo_element = element
            if "element_id" in repo:
                repo_element = soup.find(id=repo["element_id"])
            href = repo["name"] + "/" + index_html
            if layout == "cards":
                appendCard(soup, repo_element, repo, href)
            else:
                appendListItem(soup, repo_element, repo, href)

        # Write index.html
        with open(config["target_dir"] + "/index.html", "w", encoding="utf8") as htmlfile:
            htmlfile.write(str(soup))
            htmlfile.close()
        click.echo("Done.")

def loadConfig():
    configfile = open(r'config.yml')
    try:
        config = yaml.safe_load(configfile)
        # Set defaults
        if not "repos" in config:
            config["repos"] = []
        if not "repos_dir" in config:
            config["repos_dir"] = os.getcwd()
        if not "target_dir" in config:
            config["target_dir"] = "site"
        if not "element_id" in config:
            config["element_id"] = "multirepo" 
        if not "index_tpl" in config:
            config["index_tpl"] = "index.tpl" 

    finally:
        configfile.close()
    return config

def scanProjects(roots, used_names):
    # Walk the given roots and return a repo entry for every directory that
    # directly contains an mkdocs.yml. Such a directory is treated as a project
    # root and is not descended into, so nested docs/mkdocs.yml are ignored.
    projects = []
    seen_hashes = set()
    duplicates = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(os.path.abspath(root)):
            if "mkdocs.yml" in filenames:
                config_path = os.path.join(dirpath, "mkdocs.yml")
                # Don't descend into a project's own subtree.
                dirnames[:] = []
                # Skip copies of the same project (identical mkdocs.yml).
                with open(config_path, "rb") as f:
                    config_hash = hashlib.sha256(f.read()).hexdigest()
                if config_hash in seen_hashes:
                    duplicates += 1
                    continue
                seen_hashes.add(config_hash)
                name = uniqueName(os.path.basename(dirpath) or "project", used_names)
                used_names.add(name)
                projects.append({
                    "name": name,
                    "title": extractMkdocsField(config_path, "site_name") or name,
                    "description": extractMkdocsField(config_path, "site_description") or "",
                    "local_path": dirpath,
                    "mkdocs_config": "mkdocs.yml",
                })
                continue
            # Prune noise directories before descending.
            dirnames[:] = [d for d in dirnames if d not in SCAN_IGNORE_DIRS]
    if duplicates:
        click.echo("Skipped " + str(duplicates) + " duplicate project copy/copies.")
    return projects

def uniqueName(name, used_names):
    if name not in used_names:
        return name
    i = 2
    while name + "-" + str(i) in used_names:
        i += 1
    return name + "-" + str(i)

def extractMkdocsField(config_path, field):
    # Read a top-level scalar field (e.g. site_name, site_description) from an
    # mkdocs.yml without a full YAML parse, which would choke on MkDocs' custom
    # tags (e.g. !!python/name).
    field_re = re.compile(r"^" + re.escape(field) + r":\s*(.+?)\s*$")
    with open(config_path, encoding="utf8") as f:
        for line in f:
            match = field_re.match(line)
            if match:
                return match.group(1).strip("'\"")
    return None

def ensureCharset(soup):
    # Without an explicit charset the browser may render the UTF-8 output as
    # Latin-1, mangling accents and dashes. Inject one if absent.
    head = soup.head
    if head is None:
        return
    if head.find("meta", charset=True) is None:
        head.insert(0, soup.new_tag("meta", charset="utf-8"))

def appendListItem(soup, container, repo, href):
    if container.ul is None:
        container.insert(1, soup.new_tag("ul"))
    list_tag = soup.new_tag("li")
    anchor_tag = soup.new_tag("a", href=href)
    heading_tag = soup.new_tag("span")
    heading_tag.string = repo["title"]
    if "image" in repo:
        anchor_tag.insert(1, soup.new_tag("img", src=repo["image"]))
    anchor_tag.insert(1, heading_tag)
    list_tag.insert(1, anchor_tag)
    if "pdf" in repo:
        pdf_tag = soup.new_tag("a", href=repo["name"] + "/" + repo["pdf"])
        pdf_tag.string = "pdf"
        list_tag.insert(1, pdf_tag)
    container.ul.insert(1, list_tag)

def appendCard(soup, container, repo, href):
    grid = getCardGrid(soup, container)
    display_path = repo["local_path"] if "local_path" in repo else repo["name"]
    home = os.path.expanduser("~")
    if display_path.startswith(home):
        display_path = "~" + display_path[len(home):]
    date_added = repo.get("date_added", "")

    card = soup.new_tag("a", href=href)
    card["class"] = "mr-card"
    # Data attributes drive client-side filtering and sorting.
    card["data-title"] = repo["title"]
    card["data-path"] = display_path
    card["data-date"] = date_added
    card["data-desc"] = repo.get("description", "")

    star = soup.new_tag("button", attrs={"class": "mr-star", "type": "button",
                                         "aria-label": "Marquer comme favori"})
    star.string = "☆"
    card.append(star)

    title_tag = soup.new_tag("h3")
    title_tag.string = repo["title"]
    card.append(title_tag)
    # Filesystem path as a subtitle, so projects sharing a site_name stay
    # distinct and you can see where each one lives.
    sub_tag = soup.new_tag("div")
    sub_tag["class"] = "mr-sub"
    sub_tag.string = display_path
    card.append(sub_tag)
    if repo.get("description"):
        desc_tag = soup.new_tag("p")
        desc_tag.string = repo["description"]
        card.append(desc_tag)
    footer = soup.new_tag("div")
    footer["class"] = "mr-meta"
    if date_added:
        date_tag = soup.new_tag("span")
        date_tag["class"] = "mr-date"
        date_tag.string = "Ajouté le " + date_added
        footer.append(date_tag)
    if "pdf" in repo:
        pdf_tag = soup.new_tag("span")
        pdf_tag["class"] = "mr-pdf"
        pdf_tag.string = "PDF"
        footer.append(pdf_tag)
    card.append(footer)
    grid.append(card)


def getCardGrid(soup, container):
    # Each container gets a portal wrapper holding a pinned favorites section,
    # a toolbar (search + sort), the main card grid, and a pager. Created once;
    # cards are appended to the main grid and moved around client-side.
    portal = container.find("div", class_="mr-portal")
    if portal is not None:
        return portal.find("div", class_="mr-maingrid")

    portal = soup.new_tag("div")
    portal["class"] = "mr-portal"

    favsection = soup.new_tag("div")
    favsection["class"] = "mr-favsection"
    favsection["style"] = "display:none"
    favtitle = soup.new_tag("div")
    favtitle["class"] = "mr-favtitle"
    favtitle.string = "★ Favoris"
    favsection.append(favtitle)
    favgrid = soup.new_tag("div")
    favgrid["class"] = ["mr-grid", "mr-favgrid"]
    favsection.append(favgrid)
    portal.append(favsection)

    toolbar = soup.new_tag("div")
    toolbar["class"] = "mr-toolbar"
    search = soup.new_tag("input", attrs={"type": "search", "class": "mr-search",
                                          "placeholder": "Filtrer par titre, chemin, description..."})
    toolbar.append(search)
    select = soup.new_tag("select")
    select["class"] = "mr-sort"
    for value, label in [("title-asc", "Titre A-Z"), ("title-desc", "Titre Z-A"),
                         ("date-desc", "Plus recents"), ("date-asc", "Plus anciens"),
                         ("path-asc", "Chemin")]:
        opt = soup.new_tag("option", value=value)
        opt.string = label
        select.append(opt)
    toolbar.append(select)
    portal.append(toolbar)

    grid = soup.new_tag("div")
    grid["class"] = ["mr-grid", "mr-maingrid"]
    portal.append(grid)

    pager = soup.new_tag("div")
    pager["class"] = "mr-pager"
    portal.append(pager)

    container.append(portal)
    return grid


class _TolerantLoader(yaml.SafeLoader):
    pass


# mkdocs.yml routinely uses custom tags (!!python/name, !ENV); ignore them so
# we can still read the nav.
_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)
_TolerantLoader.add_multi_constructor("tag:yaml.org,2002:python/name:",
                                      lambda loader, suffix, node: None)


def _firstNavPage(nav):
    # Depth-first first markdown path referenced by an mkdocs nav.
    if isinstance(nav, str):
        return nav
    if isinstance(nav, list):
        for item in nav:
            page = _firstNavPage(item)
            if page:
                return page
    if isinstance(nav, dict):
        for value in nav.values():
            page = _firstNavPage(value)
            if page:
                return page
    return None


def navEntryHtml(repo, site_dir):
    # Resolve a project's first nav page to the built HTML that exists on disk,
    # handling both use_directory_urls conventions.
    if "local_path" not in repo:
        return None
    cfg = os.path.join(repo["local_path"], repo.get("mkdocs_config", "mkdocs.yml"))
    if not os.path.isfile(cfg):
        return None
    with open(cfg, encoding="utf8") as f:
        data = yaml.load(f, _TolerantLoader)
    if not isinstance(data, dict):
        return None
    page = _firstNavPage(data.get("nav"))
    if not page or not page.endswith(".md"):
        return None
    directory, filename = os.path.split(page)
    stem = filename[:-3]
    if stem in ("index", "README"):
        candidates = [os.path.join(directory, "index.html")]
    else:
        candidates = [os.path.join(directory, stem, "index.html"),
                      os.path.join(directory, stem + ".html")]
    for candidate in candidates:
        candidate = candidate.lstrip(os.sep)
        if os.path.isfile(os.path.join(site_dir, candidate)):
            return candidate.replace(os.sep, "/")
    return None


def findEntryHtml(site_dir):
    # Return the shallowest index.html under site_dir (relative, posix path),
    # used as a project's landing link when it has no root index.html.
    best = None
    for dirpath, _dirnames, filenames in os.walk(site_dir):
        if "index.html" in filenames:
            rel = os.path.relpath(os.path.join(dirpath, "index.html"), site_dir)
            key = (rel.count(os.sep), rel)
            if best is None or key < best[0]:
                best = (key, rel)
    return best[1].replace(os.sep, "/") if best else None


def mkdocsDate(repo):
    # Date (YYYY-MM-DD) of the project's mkdocs.yml, used to seed "date added".
    if "local_path" not in repo:
        return None
    cfg = os.path.join(repo["local_path"], repo.get("mkdocs_config", "mkdocs.yml"))
    if not os.path.isfile(cfg):
        return None
    return datetime.date.fromtimestamp(os.path.getmtime(cfg)).isoformat()


def loadAddedState(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf8") as f:
        return json.load(f)


def saveAddedState(path, state):
    with open(path, "w", encoding="utf8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)

def injectCardStyles(soup):
    # Self-contained default styling so the cards layout looks good with no
    # extra files. Skipped if the template already ships an mr-card rule.
    head = soup.head
    if head is None or "mr-card" in soup.get_text():
        return
    style = soup.new_tag("style")
    style.string = (
        ".mr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));"
        "gap:1rem;padding:1rem;max-width:1100px;margin:0 auto;}"
        ".mr-card{position:relative;display:block;padding:1.1rem 1.2rem;border:1px solid #e2e2e2;border-radius:10px;"
        "text-decoration:none;color:inherit;background:#fff;transition:box-shadow .15s,transform .15s;}"
        ".mr-card:hover{box-shadow:0 6px 20px rgba(0,0,0,.10);transform:translateY(-2px);}"
        ".mr-card.is-fav{border-color:#f5b301;box-shadow:0 0 0 1px #f5b301 inset;}"
        ".mr-star{position:absolute;top:.55rem;right:.6rem;background:none;border:none;padding:0;"
        "cursor:pointer;font-size:1.25rem;line-height:1;color:#cfcfcf;transition:color .12s,transform .12s;}"
        ".mr-star:hover{transform:scale(1.18);color:#f5b301;}"
        ".mr-card.is-fav .mr-star{color:#f5b301;}"
        ".mr-favsection{max-width:1100px;margin:0 auto;padding:.4rem 1rem 0;}"
        ".mr-favtitle{font-size:.85rem;font-weight:600;color:#a07800;letter-spacing:.03em;"
        "padding:.4rem .2rem;border-bottom:1px solid #f0e2bf;margin-bottom:.2rem;}"
        ".mr-card h3{margin:0 0 .15rem;padding-right:1.4rem;font-size:1.05rem;color:#1c1c1c;}"
        ".mr-sub{margin:0 0 .5rem;font-size:.72rem;color:#999;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;}"
        ".mr-card p{margin:0;color:#666;font-size:.9rem;line-height:1.4;}"
        ".mr-meta{display:flex;align-items:center;gap:.5rem;margin-top:.7rem;}"
        ".mr-date{font-size:.72rem;color:#aaa;}"
        ".mr-pdf{display:inline-block;font-size:.72rem;font-weight:600;"
        "letter-spacing:.04em;color:#a33;border:1px solid #a33;border-radius:4px;padding:.1rem .4rem;}"
        ".mr-portal{max-width:1100px;margin:0 auto;padding:0 1rem;}"
        ".mr-toolbar{display:flex;gap:.6rem;flex-wrap:wrap;padding:1rem 0 .2rem;}"
        ".mr-search{flex:1 1 240px;padding:.55rem .8rem;border:1px solid #ddd;border-radius:8px;font-size:.9rem;}"
        ".mr-sort{padding:.55rem .8rem;border:1px solid #ddd;border-radius:8px;font-size:.9rem;background:#fff;}"
        ".mr-pager{display:flex;align-items:center;justify-content:center;gap:1rem;padding:1.2rem 0 2rem;}"
        ".mr-pager button{padding:.35rem .8rem;border:1px solid #ddd;border-radius:8px;background:#fff;cursor:pointer;font-size:1rem;}"
        ".mr-pager button:disabled{opacity:.4;cursor:default;}"
        ".mr-pageinfo{font-size:.82rem;color:#777;}"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa;margin:0;}"
    )
    if head is not None:
        head.append(style)

def injectCardScripts(soup):
    # Vanilla client-side filtering, sorting and pagination over the cards.
    if soup.body is None or "mr-portal-js" in soup.get_text():
        return
    script = soup.new_tag("script")
    script["data-id"] = "mr-portal-js"
    script.string = (
        "(function(){var PAGE=12,KEY='mr-favorites';"
        "function loadFavs(){try{return JSON.parse(localStorage.getItem(KEY))||{};}catch(e){return {};}}"
        "function saveFavs(f){try{localStorage.setItem(KEY,JSON.stringify(f));}catch(e){}}"
        "function setup(portal){"
        "var grid=portal.querySelector('.mr-maingrid');"
        "var favgrid=portal.querySelector('.mr-favgrid');"
        "var favsection=portal.querySelector('.mr-favsection');"
        "var cards=Array.prototype.slice.call(grid.querySelectorAll('.mr-card'));"
        "var search=portal.querySelector('.mr-search');"
        "var sort=portal.querySelector('.mr-sort');"
        "var pager=portal.querySelector('.mr-pager');"
        "var page=1;"
        "function lc(s){return (s||'').toLowerCase();}"
        "function keyOf(c){return c.dataset.path||c.getAttribute('href');}"
        "function apply(){"
        "var favs=loadFavs();"
        "var q=lc(search.value);"
        "var f=cards.filter(function(c){var d=c.dataset;"
        "return lc(d.title).indexOf(q)>=0||lc(d.path).indexOf(q)>=0||lc(d.desc).indexOf(q)>=0;});"
        "var s=sort.value;"
        "function cmp(a,b){var A=a.dataset,B=b.dataset;"
        "if(s==='title-asc')return A.title.localeCompare(B.title);"
        "if(s==='title-desc')return B.title.localeCompare(A.title);"
        "if(s==='date-desc')return (B.date||'').localeCompare(A.date||'')||A.title.localeCompare(B.title);"
        "if(s==='date-asc')return (A.date||'').localeCompare(B.date||'')||A.title.localeCompare(B.title);"
        "if(s==='path-asc')return A.path.localeCompare(B.path);return 0;}"
        "f.sort(cmp);"
        "cards.forEach(function(c){var on=!!favs[keyOf(c)];c.classList.toggle('is-fav',on);"
        "var st=c.querySelector('.mr-star');if(st)st.textContent=on?'\\u2605':'\\u2606';"
        "c.style.display='none';});"
        "var favList=f.filter(function(c){return favs[keyOf(c)];});"
        "var normList=f.filter(function(c){return !favs[keyOf(c)];});"
        "favList.forEach(function(c){favgrid.appendChild(c);c.style.display='';});"
        "favsection.style.display=favList.length?'':'none';"
        "var pages=Math.max(1,Math.ceil(normList.length/PAGE));if(page>pages)page=pages;"
        "var start=(page-1)*PAGE;"
        "normList.forEach(function(c,i){grid.appendChild(c);if(i>=start&&i<start+PAGE)c.style.display='';});"
        "pager.innerHTML='';"
        "var prev=document.createElement('button');prev.textContent='\\u2039';prev.disabled=page<=1;"
        "prev.onclick=function(){page--;apply();};"
        "var info=document.createElement('span');info.className='mr-pageinfo';"
        "info.textContent=normList.length+' projet(s)'+(favList.length?' (+ '+favList.length+' favori(s))':'')+' \\u2014 page '+page+'/'+pages;"
        "var next=document.createElement('button');next.textContent='\\u203a';next.disabled=page>=pages;"
        "next.onclick=function(){page++;apply();};"
        "pager.appendChild(prev);pager.appendChild(info);pager.appendChild(next);}"
        "cards.forEach(function(c){var st=c.querySelector('.mr-star');if(!st)return;"
        "st.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();"
        "var favs=loadFavs(),k=keyOf(c);if(favs[k])delete favs[k];else favs[k]=1;saveFavs(favs);apply();});});"
        "search.addEventListener('input',function(){page=1;apply();});"
        "sort.addEventListener('change',function(){page=1;apply();});"
        "apply();}"
        "document.querySelectorAll('.mr-portal').forEach(setup);})();"
    )
    soup.body.append(script)

def loadTemplate(index_file):
    templatefile = open(index_file)
    try:
        contents = yaml.safe_load(templatefile)
        soup = BeautifulSoup(contents, 'html.parser')
    finally:
        templatefile.close()
    return soup
