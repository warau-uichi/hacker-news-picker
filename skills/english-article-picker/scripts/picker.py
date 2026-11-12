#!/usr/bin/env python3
"""Collect facts, cache evidence and render a model's selection. Python 3.11+."""
import argparse
import concurrent.futures
from contextlib import closing
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

USER_AGENT = "EnglishArticlePicker/1.0 (personal read-only RSS reader)"
SKILL = Path(__file__).resolve().parents[1]
MAX_BYTES = 3_000_000
REQUEST_LOG = None
REQUEST_LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def validate_hatena_user(value):
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("HATENA_USER must be a username using letters, digits, '_' or '-' (not a URL)")
    return value


def user_config(workspace):
    """Read only the supported setting; never source or execute a .env file."""
    path = Path(workspace) / ".env"
    value, source = os.environ.get("HATENA_USER"), "environment"
    if value is None or not value.strip():
        source = ".env"
        if path.exists():
            for line in path.read_text().splitlines():
                match = re.match(r"^\s*(?:export\s+)?HATENA_USER\s*=(.*)$", line)
                if match:
                    parts = shlex.split(match[1], comments=True)
                    if len(parts) > 1:
                        raise ValueError("HATENA_USER in .env must contain one username")
                    value = parts[0] if parts else ""
    username = validate_hatena_user(value) if value and value.strip() else None
    return {"configured": username is not None, "hatena_user": username,
            "source": source if username else None, "env_file": str(path.resolve())}


def workspace_account(workspace):
    folder = Path(workspace) / ".picker"
    path = folder / "account.json"
    if path.exists():
        return json.loads(path.read_text())["hatena_user"]
    # Preserve the owner of an existing installation before .env was introduced.
    for path in sorted((folder / "runs").glob("*/bundle.json"), reverse=True):
        old = load_bundle(path)
        username = old.get("profile", {}).get("hatena_user")
        if username and any(s.get("source") == "hatena" and s.get("status") == "ok"
                            for s in old.get("source_status", [])):
            return username
    return None


def check_account(workspace, username):
    owner = workspace_account(workspace)
    if owner and owner != username:
        raise ValueError(f"This workspace contains history for {owner}. "
                         "Use a different --workspace for another Hatena account.")


def config(args):
    print(json.dumps({"workspace": str(Path(args.workspace).resolve()), **user_config(args.workspace)},
                     ensure_ascii=False))


def setup(args):
    value = args.hatena_user
    if value is None:
        current = user_config(args.workspace)
        if current["configured"]:
            check_account(args.workspace, current["hatena_user"])
            config(args)
            return
        if not sys.stdin.isatty():
            raise ValueError("Hatena username is not configured. Run setup --hatena-user YOUR_USERNAME")
        value = input("Hatena username: ")
    username = validate_hatena_user(value)
    check_account(args.workspace, username)
    path = Path(args.workspace) / ".env"
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    setting = f"HATENA_USER={username}\n"
    found = False
    for i, line in enumerate(lines):
        if re.match(r"^\s*(?:export\s+)?HATENA_USER\s*=", line):
            lines[i], found = setting, True
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(setting)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".env.picker.tmp")
    # Exclusive creation avoids clobbering another setup process or an existing file.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            output.write("".join(lines))
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(json.dumps({"saved_hatena_user": username, **user_config(args.workspace)}, ensure_ascii=False))


def resolve_workspace(args):
    if args.workspace:
        return str(Path(args.workspace).expanduser().resolve())
    if getattr(args, "bundle", None):
        path = Path(args.bundle).resolve()
        if path.parent.parent.name == "runs" and path.parent.parent.parent.name == ".picker":
            return str(path.parent.parent.parent.parent)
    return str(Path.cwd().resolve())


def normalize(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Public HTTP(S) URL required")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/",
                                   urllib.parse.urlencode(query), ""))


def ident(url):
    return hashlib.sha256(normalize(url).encode()).hexdigest()[:16]


def public_url(url):
    url = normalize(url)
    p = urllib.parse.urlsplit(url)
    if p.port not in (None, 80, 443):
        raise ValueError("Only public web ports are supported")
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Non-public destination blocked")
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append({"url": req.full_url, "status": code, "location": newurl})
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url, with_headers=False):
    observation = {"url": url, "method": "GET", "started_at": now(),
                   "transport": "local-python-urllib", "user_agent": USER_AGENT}
    try:
        public_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                 "Accept": "application/json, application/xml, text/html;q=0.9, */*;q=0.5"})
        redirects = Redirects()
        observation["redirects"] = redirects.hops
        with urllib.request.build_opener(redirects).open(req, timeout=20) as r:
            observation.update(status=r.status, resolved_url=r.geturl(),
                               content_type=r.headers.get_content_type())
            data = r.read(MAX_BYTES + 1)
            observation["bytes"] = len(data)
            if len(data) > MAX_BYTES:
                raise ValueError("Response exceeds 3 MB; use a bounded alternate source")
            result = (data, r.geturl(), r.headers.get_content_charset() or "utf-8")
            return (*result, dict(r.headers)) if with_headers else result
    except Exception as exc:
        observation["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, urllib.error.HTTPError):
            observation.update(status=exc.code, resolved_url=exc.geturl())
        raise
    finally:
        observation["finished_at"] = now()
        if REQUEST_LOG is not None:
            with REQUEST_LOCK:
                REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
                with REQUEST_LOG.open("a") as log:
                    log.write(json.dumps(observation, ensure_ascii=False) + "\n")


def check_robots(url):
    url = normalize(url)
    parsed = urllib.parse.urlsplit(url)
    robot_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    rp = urllib.robotparser.RobotFileParser(robot_url)
    try:
        rp.parse(request(robot_url)[0].decode("utf-8", errors="replace").splitlines())
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410):
            raise
        return
    if not rp.can_fetch(USER_AGENT, url):
        raise ValueError("robots.txt disallows this reader")


class Links(HTMLParser):
    """Expose ordinary page links for model-led exploration from local snapshots."""
    def __init__(self, url):
        super().__init__()
        self.url, self.rows, self.active = url, [], None

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            try:
                target = normalize(urllib.parse.urljoin(self.url, attrs["href"]))
            except ValueError:
                self.active = None
                return
            self.active = {"url": target, "text": ""}
        elif tag == "link" and attrs.get("href") and "alternate" in attrs.get("rel", "").split():
            try:
                self.rows.append({"url": normalize(urllib.parse.urljoin(self.url, attrs["href"])),
                                  "text": attrs.get("title", ""), "type": attrs.get("type", "")})
            except ValueError:
                pass

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"] += data

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag == "a" and self.active is not None:
            self.active["text"] = " ".join(self.active["text"].split())
            self.rows.append(self.active)
            self.active = None


def fetch_page(url, folder):
    """Save local HTTP evidence, including failures, without changing delivery state."""
    record = {"url": url, "retrieved_at": now(), "transport": "local-python-urllib"}
    try:
        url = normalize(url)
        check_robots(url)
        data, resolved, charset, headers = request(url, with_headers=True)
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        digest = hashlib.sha256(data).hexdigest()
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        prefix = folder / f"{ident(url)}-{digest[:12]}"
        raw = prefix.with_suffix(".raw")
        raw.write_bytes(data)
        record.update(status="ok", resolved_url=resolved, content_type=content_type,
                      raw_path=str(raw.resolve()), sha256=digest)
        text = data.decode(charset, errors="replace")
        if "html" in content_type.lower():
            links = Links(resolved)
            links.feed(text)
            record["links"] = links.rows
            try:
                text, extractor = article_text(text, resolved)
                record["extractor"] = extractor
                record["scope"] = "unverified-extraction"
            except (ValueError, RuntimeError) as exc:
                # A listing may have no article prose. Its links/raw HTML remain useful.
                record["extraction_error"] = str(exc)
                text = ""
        elif "json" in content_type.lower():
            json.loads(text)
            record["scope"] = "structured-data"
        elif "xml" in content_type.lower() or "text/plain" in content_type.lower():
            record["scope"] = "source-text"
        else:
            record["extraction_error"] = "Unsupported content type; inspect raw evidence"
            text = ""
        if text:
            output = prefix.with_suffix(".txt")
            record["truncated"] = len(text) > 200000
            output.write_text(text[:200000])
            record["text_path"] = str(output.resolve())
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, urllib.error.HTTPError):
            record["http_status"] = exc.code
            record["failed_url"] = exc.geturl()
    return record


def fetch(args):
    folder = Path(args.bundle).parent
    records = [fetch_page(url, folder / "pages") for url in args.urls]
    bundle = load_bundle(args.bundle)
    bundle.setdefault("page_fetches", []).extend(records)
    dump(args.bundle, bundle)
    output = folder / "page-fetches.json"
    dump(output, bundle["page_fetches"])
    print(json.dumps({"index": str(output), "results": [
        {k: r[k] for k in ("url", "status", "text_path", "error", "http_status", "failed_url") if k in r}
        for r in records]}, ensure_ascii=False))


def get_json(url):
    return json.loads(request(url)[0])


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg"}:
            self.skip += 1
        if not self.skip and tag in {"p", "div", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(markup):
    p = Text()
    p.feed(markup)
    return "\n".join(s for line in "".join(p.parts).splitlines() if (s := " ".join(line.split())))


def article_text(markup, url):
    """Extract article prose separately from RSS snippets and HN comment markup."""
    try:
        import trafilatura
    except ModuleNotFoundError as exc:
        if exc.name != "trafilatura":
            raise
        raise RuntimeError(
            "Trafilatura is required for HTML bodies. Install with this Python: "
            f"{sys.executable} -m pip install -r {SKILL / 'requirements.txt'}"
        ) from exc
    options = {"output_format": "txt", "include_comments": False,
               "include_tables": True, "include_formatting": True,
               "deduplicate": False}
    text = trafilatura.extract(markup, url=url, **options)
    if not text or len(text.strip()) < 100:
        raise ValueError("Too little article text extracted; review the page or import verified text")
    return text, {"name": "trafilatura", "version": trafilatura.__version__, "options": options}


def feed(data, base_url=""):
    root = ET.fromstring(data)
    xml_base = "{http://www.w3.org/XML/1998/namespace}base"
    base_url = urllib.parse.urljoin(base_url, root.get(xml_base, ""))
    rows = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in {"item", "entry"}:
            continue
        fields = {}
        tags = []
        fragments = []
        item_base = urllib.parse.urljoin(base_url, item.get(xml_base, ""))
        for child in item:
            name = child.tag.split("}")[-1]
            value = "".join(child.itertext()).strip()
            if name == "link" and child.get("href") and child.get("rel", "alternate") == "alternate":
                fields["link"] = child.get("href")
            elif name == "link" and value:
                fields["link"] = value
            elif name in {"subject", "category"}:
                tags.append(child.get("term") or value)
            elif name not in fields:
                fields[name] = value
            if name in {"description", "summary", "content", "encoded"}:
                # RSS escaped HTML/CDATA and Atom XHTML both retain their anchors.
                markup = (child.text or "") + "".join(ET.tostring(c, encoding="unicode") for c in child)
                fragments.append((urllib.parse.urljoin(item_base, child.get(xml_base, "")), markup))
        if fields.get("link"):
            post_url = urllib.parse.urljoin(item_base, fields["link"])
            links = []
            for fragment_base, markup in fragments:
                parser = Links(fragment_base or post_url)
                parser.feed(markup)
                for link in parser.rows:
                    if link not in links:
                        links.append(link)
            rows.append({"url": post_url, "title": fields.get("title", fields["link"]),
                         "published": fields.get("published") or fields.get("pubDate") or fields.get("date"),
                         "updated": fields.get("updated"), "tags": tags,
                         "links": links,
                         "snippet": plain(fields.get("description") or fields.get("summary") or fields.get("content") or "")})
    return rows


def fetch_feed(url, folder, limit):
    record = {"url": url, "retrieved_at": now(), "transport": "local-python-urllib", "limit": limit}
    try:
        data, resolved, _ = request(url)
        record.update(resolved_url=resolved, retrieved_at=now())
        digest = hashlib.sha256(data).hexdigest()
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        prefix = folder / f"{ident(url)}-{digest[:12]}"
        raw = prefix.with_suffix(".xml")
        raw.write_bytes(data)
        record.update(raw_path=str(raw.resolve()), sha256=digest)
        rows = feed(data, resolved)
        if not rows:
            raise ValueError("Empty or unrecognized feed")
        selected = rows[:limit]
        entries = prefix.with_suffix(".json")
        dump(entries, selected)
        record.update(status="ok", entries_path=str(entries.resolve()),
                      available_entries=len(rows), collected_entries=len(selected), limited=len(rows) > limit)
        return selected, record
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, urllib.error.HTTPError):
            record.update(http_status=exc.code, failed_url=exc.geturl())
        return [], record


class Bookmarks(HTMLParser):
    """Read Hatena's rendered bookmark rows; ignore template scripts."""
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.title, self.script = [], None, False, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.script = True
        if self.script:
            return
        if tag == "li" and a.get("data-target-url"):
            self.row = {"url": a["data-target-url"], "title": "", "tags": ["読み物"]}
        if tag == "h3" and a.get("class") == "centerarticle-entry-title":
            self.title = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.script = False
        if tag == "h3" and self.title:
            if self.row:
                self.rows.append(self.row)
            self.row, self.title = None, False

    def handle_data(self, data):
        if self.title and self.row and not self.script:
            self.row["title"] += data.strip()


def scores(record):
    result = []
    # GitHub's old and versioned fields coexist; preserve their assessment metadata.
    for label, value in [("cvss", record.get("cvss")),
                         *list((record.get("cvss_severities") or {}).items())]:
        if not isinstance(value, dict):
            continue
        score = value.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 10:
            vector = value.get("vector_string") or value.get("vector")
            if score == 0 and not vector:
                continue  # GitHub uses this for an unassigned assessment, not an actual zero.
            assessment = {"score": score, "version": vector.split("/")[0] if vector else label,
                          "vector": vector, "source": record.get("html_url")}
            if assessment not in result:
                result.append(assessment)
    return result


def security_flags(record, threshold):
    flags = []
    if any(s["score"] >= threshold for s in record.get("cvss", [])):
        flags.append("cvss>=9")
    if record.get("kev"):
        flags.append("known-exploited")
    return flags


def advisory_rows(cutoff, pages):
    query = urllib.parse.urlencode({"severity": "critical", "modified": ">=" + cutoff,
                                    "per_page": 100, "sort": "updated", "direction": "desc"})
    url = "https://api.github.com/advisories?" + query
    visited = set()
    for _ in range(pages):
        if url in visited:
            raise ValueError("GitHub returned a repeated page cursor")
        visited.add(url)
        data, _, _, headers = request(url, with_headers=True)
        rows = json.loads(data)
        if not isinstance(rows, list):
            raise ValueError("Unexpected GitHub API response")
        yield from rows
        link = next((v for k, v in headers.items() if k.lower() == "link"), "")
        matches = re.findall(r'<([^>]+)>;\s*rel="next"', link)
        if not matches:
            return
        url = matches[0]
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.netloc != "api.github.com" or p.path != "/advisories":
            raise ValueError("Unexpected GitHub pagination URL")
    raise ValueError("Advisory page cap reached; coverage may be incomplete. Increase --advisory-pages.")


def db(workspace):
    folder = Path(workspace) / ".picker"
    folder.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(folder / "state.sqlite3")
    con.execute("CREATE TABLE IF NOT EXISTS deliveries (url TEXT PRIMARY KEY, run TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS interests (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
    con.commit()
    return con


def failure(bundle, source, error):
    bundle["source_status"].append({"source": source, "status": "failed", "at": now(),
                                    "error": f"{type(error).__name__}: {error}"})


def add(bundle, row, source, lane="article", metrics=None):
    url = normalize(row["url"])
    key = ident(url)
    target = bundle["candidates"].setdefault(key, {"id": key, "url": url, "title": row.get("title") or url,
                                                    "sources": [], "metrics": [], "lanes": [], "body": None})
    if source not in target["sources"]:
        target["sources"].append(source)
    if lane not in target["lanes"]:
        target["lanes"].append(lane)
    target.update({k: row[k] for k in ["published", "updated", "tags", "snippet", "cvss", "kev", "security_id"] if k in row})
    if row.get("feed_entry"):
        entries = target.setdefault("feed_entries", [])
        if row["feed_entry"] not in entries:
            entries.append(row["feed_entry"])
    if metrics:
        target["metrics"].append(metrics)
    return target


def bookmark_rows(rows):
    """Merge RSS and tag-page observations without discarding richer RSS metadata."""
    merged = {}
    for row in rows:
        url = normalize(row["url"])
        target = merged.setdefault(url, {"url": url, "tags": []})
        target["tags"] = sorted(set(target["tags"]) | set(row.get("tags", [])))
        target.update({k: row[k] for k in ("title", "snippet", "published") if row.get(k)})
    return merged


def bookmark_fingerprint(row):
    # Saving time is not publication time, and alone is not a change of interest.
    content = {k: row.get(k, "") for k in ("title", "snippet")}
    content["tags"] = sorted(row.get("tags", []))
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def interest_state(workspace):
    with closing(db(workspace)) as con:
        row = con.execute("SELECT payload FROM interests WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {"revision": 0, "last_run": None, "bookmarks": {}, "hypotheses": {}}


def prepare_interests(workspace, bundle):
    """Prepare an immutable comparison; collection never advances the analysis checkpoint."""
    state = interest_state(workspace)
    previous = state["bookmarks"]
    baseline_run = state["last_run"]
    # Upgrade existing installations using just the latest successful collection.
    if not state["revision"]:
        paths = sorted((Path(workspace) / ".picker/runs").glob("*/bundle.json"), reverse=True)
        for path in paths:
            if path.parent.name >= bundle["run"]:
                continue
            old = load_bundle(path)
            if "interest_context" in old:
                continue  # An uncommitted new-format collection is not an analyzed checkpoint.
            if any(s["source"] == "hatena" and s["status"] == "ok" for s in old.get("source_status", [])):
                previous = {u: {"row": r, "fingerprint": bookmark_fingerprint(r),
                                "status": "baseline-unreviewed"}
                            for u, r in bookmark_rows(old.get("bookmarks", [])).items()}
                baseline_run = old["run"]
                break
    snapshot = {u: dict(r) for u, r in previous.items()}
    for url, row in bookmark_rows(bundle["bookmarks"]).items():
        old = previous.get(url)
        if old and "published" not in row:
            # Tag-page-only rows lack the RSS comment/date; absence is not an edit.
            row = {**old["row"], **row, "tags": sorted(set(old["row"].get("tags", [])) | set(row["tags"]))}
        fingerprint = bookmark_fingerprint(row)
        if not old or old["fingerprint"] != fingerprint:
            snapshot[url] = {"row": row, "fingerprint": fingerprint, "status": "pending",
                             "change": "initial" if not baseline_run else ("changed" if old else "new")}
    delta = []
    for url, record in sorted(snapshot.items()):
        if record["status"] not in {"pending", "deferred"}:
            continue
        candidate = add(bundle, record["row"], "hatena-interest", "bookmark")
        candidate["bookmarked"] = True
        delta.append({"id": candidate["id"], "url": url, "title": candidate["title"],
                      "change": record.get("change", "retry"), "retry": record["status"] == "deferred"})
    statuses = {s["source"]: s["status"] for s in bundle["source_status"]}
    bundle["interest_context"] = {
        "revision": state["revision"], "previous_run": baseline_run,
        "collection_ok": statuses.get("hatena") == "ok" and statuses.get("hatena-reading") == "ok",
        "coverage": "Observed RSS/tag-page URLs only; gaps beyond these windows are not guaranteed covered.",
        "delta": delta, "snapshot": snapshot, "hypotheses": state["hypotheses"]}
    bundle.setdefault("profile", {})["inferred_interests"] = [
        h for h in state["hypotheses"].values() if h["status"] == "active"]


def update_interests(args):
    """Validate the model's bookmark analysis; commit independently of article delivery."""
    bundle = load_bundle(args.bundle)
    context = bundle["interest_context"]
    analysis = json.loads(Path(args.analysis).read_text())
    digest = hashlib.sha256(json.dumps(analysis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    state = interest_state(args.workspace)
    if state["last_run"] == bundle["run"]:
        if state.get("analysis_hash") != digest:
            raise ValueError("This run already has a different committed interest analysis")
        if args.commit:
            bundle["interest_context"]["hypotheses"] = state["hypotheses"]
            bundle["profile"]["inferred_interests"] = [h for h in state["hypotheses"].values() if h["status"] == "active"]
            dump(args.bundle, bundle)
        print(json.dumps({"committed": True, "revision": state["revision"], "already_applied": True}))
        return
    if state["revision"] != context["revision"]:
        raise ValueError("Interest state advanced; collect again before updating")
    if not context["collection_ok"]:
        raise ValueError("Hatena collection incomplete; keep the checkpoint and retry collection")
    reviews = analysis.get("bookmarks", [])
    expected = {d["id"] for d in context["delta"]}
    if len({r["id"] for r in reviews}) != len(reviews) or {r["id"] for r in reviews} != expected:
        raise ValueError("Analyze each delta bookmark exactly once (or defer with a reason)")
    snapshot = json.loads(json.dumps(context["snapshot"]))
    reviewed = set()
    at = now()
    for review in reviews:
        row = bundle["candidates"][review["id"]]
        entry = snapshot[row["url"]]
        if review.get("status") == "reviewed":
            checked_text(row, {**review, "summary": review.get("observation"),
                               "reason": review.get("inference"), "category": "other"})
            if row.get("body_error"):
                raise ValueError("Failed body fetch cannot support a new interest")
            if review.get("scope") not in {"full", "partial"}:
                raise ValueError("Explicit full/partial reading scope required")
            reviewed.add(review["id"])
        elif review.get("status") == "deferred":
            if not isinstance(review.get("reason"), str) or not review["reason"].strip():
                raise ValueError("Deferred bookmarks need an explanation")
        else:
            raise ValueError("Bookmark analysis status must be reviewed or deferred")
        entry.update({"status": review["status"], "analysis": review, "analyzed_at": at,
                      "body": row.get("body") if review["status"] == "reviewed" else None})
    hypotheses = json.loads(json.dumps(state["hypotheses"]))
    changes = analysis.get("hypotheses", [])
    if len({h["id"] for h in changes}) != len(changes):
        raise ValueError("Duplicate hypothesis updates")
    if not reviewed and changes:
        raise ValueError("Interest changes require newly reviewed bookmark bodies")
    for change in changes:
        key = change["id"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Hypothesis ID required")
        evidence_ids = change.get("bookmark_ids", [])
        if not evidence_ids or not set(evidence_ids) <= reviewed:
            raise ValueError("Hypotheses must cite reviewed delta bookmarks")
        if change.get("status") not in {"active", "retired"} or (change["status"] == "retired" and key not in hypotheses):
            raise ValueError("Only existing hypotheses can be retired")
        if change.get("confidence") not in {"tentative", "supported"}:
            raise ValueError("Hypothesis confidence must be tentative or supported")
        for field in ("text", "rationale"):
            if not isinstance(change.get(field), str) or not change[field].strip():
                raise ValueError(f"Hypothesis needs {field}")
        evidence = [{"url": bundle["candidates"][i]["url"], "run": bundle["run"],
                     "fingerprint": snapshot[bundle["candidates"][i]["url"]]["fingerprint"]}
                    for i in evidence_ids]
        prior = hypotheses.get(key, {})
        evidence = prior.get("evidence", []) + [e for e in evidence if e not in prior.get("evidence", [])]
        if change["confidence"] == "supported" and len({e["url"] for e in evidence}) < 2:
            raise ValueError("Supported hypotheses require at least two distinct bookmark URLs")
        hypotheses[key] = {**change, "origin": "bookmark-inference", "updated_at": at,
                           "created_at": prior.get("created_at", at), "evidence": evidence}
    updated = {"revision": state["revision"] + 1, "last_run": bundle["run"], "updated_at": at,
               "bookmarks": snapshot, "hypotheses": hypotheses, "analysis_hash": digest}
    # Persist a reviewable audit record even for previews; never touch delivery history here.
    audit = Path(args.bundle).parent / "interest-analysis.json"
    dump(audit, {"run": bundle["run"], "input": analysis, "result": updated})
    if args.commit:
        with closing(db(args.workspace)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT payload FROM interests WHERE id=1").fetchone()
            revision = json.loads(current[0])["revision"] if current else 0
            if revision != state["revision"]:
                raise ValueError("Interest state advanced; collect again before updating")
            con.execute("INSERT OR REPLACE INTO interests VALUES (1, ?)",
                        (json.dumps(updated, ensure_ascii=False),))
        bundle["interest_context"]["hypotheses"] = hypotheses
        bundle["profile"]["inferred_interests"] = [h for h in hypotheses.values() if h["status"] == "active"]
        dump(args.bundle, bundle)
    print(json.dumps({"analysis": str(audit), "committed": args.commit, "revision": updated["revision"],
                      "reviewed": len(reviewed), "deferred": len(reviews) - len(reviewed)}, ensure_ascii=False))


def collect(args):
    profile = json.loads((SKILL / "references/profile.json").read_text())
    settings = user_config(args.workspace)
    if not settings["configured"]:
        raise ValueError("HATENA_USER is not configured. Run setup --hatena-user YOUR_USERNAME first")
    profile["hatena_user"] = settings["hatena_user"]
    check_account(args.workspace, profile["hatena_user"])
    stamp = getattr(args, "stamp", None) or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = Path(args.workspace) / ".picker/runs" / stamp
    folder.mkdir(parents=True)
    bundle = {"run": stamp, "created_at": now(), "profile": profile, "candidates": {},
              "bookmarks": [], "source_status": [], "coverage": "Configured feeds only; not an exhaustive security or news monitor."}
    bundle["network"] = {"transport": "local-python-urllib",
                         "request_log": str(REQUEST_LOG.resolve()) if REQUEST_LOG else None}

    def source(name, fn):
        try:
            fn()
            bundle["source_status"].append({"source": name, "status": "ok", "at": now()})
        except Exception as e:
            failure(bundle, name, e)

    username = urllib.parse.quote(profile["hatena_user"], safe="")
    def hatena():
        rows = feed(request(f"https://b.hatena.ne.jp/{username}/bookmark.rss")[0])
        if not rows:
            raise ValueError("Empty Hatena RSS; cannot establish bookmark differences")
        bundle["bookmarks"].extend(rows)
    source("hatena", hatena)
    if bundle["source_status"][-1]["status"] == "ok":
        dump(Path(args.workspace) / ".picker/account.json", {"hatena_user": profile["hatena_user"]})

    def reading():
        p = Bookmarks()
        p.feed(request(f"https://b.hatena.ne.jp/{username}/%E8%AA%AD%E3%81%BF%E7%89%A9/")[0].decode("utf-8"))
        if not p.rows:
            raise ValueError("No rendered reading bookmarks; parser/source may have changed")
        bundle["bookmarks"].extend(p.rows)
    source("hatena-reading", reading)

    def hn():
        story_ids = []
        for listing in ("topstories", "newstories"):
            story_ids.extend(get_json(f"https://hacker-news.firebaseio.com/v0/{listing}.json")[:args.hn_limit])
        story_ids = list(dict.fromkeys(story_ids))
        def one(i):
            return i, get_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json"), now()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {pool.submit(one, i): i for i in story_ids}
            for job in concurrent.futures.as_completed(jobs):
                try:
                    i, r, at = job.result()
                    if r and r.get("type") == "story" and r.get("url") and not (r.get("dead") or r.get("deleted")):
                        add(bundle, {"url": r["url"], "title": r["title"]}, "hn",
                            metrics={"platform": "HN", "score": r.get("score"), "comments": r.get("descendants"),
                                     "url": f"https://news.ycombinator.com/item?id={i}",
                                     "submitted_at": r.get("time"), "retrieved_at": at})
                except Exception as e:
                    failure(bundle, f"hn-item-{jobs[job]}", e)
    source("hn", hn)

    def lobsters():
        for r in get_json("https://lobste.rs/hottest.json"):
            if r.get("url"):
                add(bundle, {"url": r["url"], "title": r["title"], "tags": r.get("tags", [])}, "lobsters",
                    metrics={"platform": "Lobsters", "score": r.get("score"), "comments": r.get("comment_count"),
                             "url": r.get("comments_url"), "submitted_at": r.get("created_at"), "retrieved_at": now()})
    source("lobsters", lobsters)

    bundle["feed_fetches"] = []
    for spec in profile["feeds"]:
        rows, record = fetch_feed(spec["url"], folder / "feeds", args.feed_limit)
        record["source"] = spec["name"]
        bundle["feed_fetches"].append(record)
        bundle["source_status"].append({k: record[k] for k in (
            "source", "status", "url", "retrieved_at", "collected_entries", "available_entries", "limit",
            "limited", "error", "http_status", "failed_url") if k in record})
        bundle["source_status"][-1]["at"] = record["retrieved_at"]
        for row in rows:
            row["feed_entry"] = {"feed_url": spec["url"], "resolved_feed_url": record["resolved_url"],
                                 "post_url": row["url"], "retrieved_at": record["retrieved_at"],
                                 "raw_path": record["raw_path"], "links": row["links"]}
            add(bundle, row, spec["name"], spec["lane"])
    dump(folder / "feed-fetches.json", bundle["feed_fetches"])

    # Supplementary discovery and security listings are fetched from this host too.
    # Their links are evidence for exploration, not automatically recommendations.
    bundle["page_fetches"] = []
    for spec in profile.get("discovery_pages", []):
        record = fetch_page(spec["url"], folder / "pages")
        record["source"] = spec["name"]
        bundle["page_fetches"].append(record)
        bundle["source_status"].append({"source": spec["name"], "status": record["status"],
                                        "at": record["retrieved_at"], "url": spec["url"],
                                        **{k: record[k] for k in ("error", "http_status", "failed_url") if k in record}})
    dump(folder / "page-fetches.json", bundle["page_fetches"])

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).date().isoformat()

    def kev():
        r = get_json("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
        bundle["kev_catalog"] = {"version": r.get("catalogVersion"), "retrieved_at": now()}
        for v in r["vulnerabilities"]:
            if v["dateAdded"] >= cutoff:
                add(bundle, {"url": "https://www.cve.org/CVERecord?id=" + v["cveID"],
                             "title": v["vulnerabilityName"], "security_id": v["cveID"], "kev": True,
                             "published": v["dateAdded"], "snippet": v.get("shortDescription", "") + "\n" + v.get("requiredAction", "")},
                    "cisa-kev", "security")
    source("cisa-kev", kev)

    def github():
        # No ecosystem/product restriction: all recent critical advisories must remain eligible.
        for r in advisory_rows(cutoff, args.advisory_pages):
            if r.get("withdrawn_at"):
                continue
            add(bundle, {"url": r["html_url"], "title": r["summary"], "security_id": r.get("cve_id") or r["ghsa_id"],
                         "published": r.get("published_at"), "updated": r.get("updated_at"),
                         "cvss": scores(r), "snippet": r.get("description", "")}, "github-advisories", "security")
    source("github-advisories", github)

    with db(args.workspace) as con:
        seen = {r[0] for r in con.execute("SELECT url FROM deliveries")}
        alerts = dict(con.execute("SELECT id, fingerprint FROM alerts"))
    prepare_interests(args.workspace, bundle)
    saved = set(bundle["interest_context"]["snapshot"])
    for key, r in bundle["candidates"].items():
        r["already_recommended"] = r["url"] in seen
        r["bookmarked"] = r["url"] in saved
        r["mandatory_reasons"] = security_flags(r, profile["critical_cvss"])
        fingerprint = hashlib.sha256(json.dumps({k: r.get(k) for k in
                                    ("title", "snippet", "cvss", "kev", "updated")}, sort_keys=True).encode()).hexdigest()
        r["security_fingerprint"] = fingerprint
        r["security_changed"] = alerts.get(key) != fingerprint
    # Stable ordering avoids fetch completion order acting as a recommendation signal.
    bundle["candidates"] = dict(sorted(bundle["candidates"].items()))
    dump(folder / "bundle.json", bundle)
    overview = {"run": stamp, "bookmarks": bundle["bookmarks"], "source_status": bundle["source_status"],
                "network": bundle["network"], "page_fetches_index": str(folder / "page-fetches.json"),
                "feed_fetches_index": str(folder / "feed-fetches.json"),
                "interest_context": {k: v for k, v in bundle["interest_context"].items() if k != "snapshot"},
                "coverage": bundle["coverage"], "candidates": [{k: r.get(k) for k in
                ("id", "title", "url", "sources", "lanes", "metrics", "published", "updated", "already_recommended", "bookmarked", "mandatory_reasons", "security_changed")}
                for r in bundle["candidates"].values()]}
    dump(folder / "overview.json", overview)
    print(json.dumps({"bundle": str(folder / "bundle.json"), "overview": str(folder / "overview.json"), "candidates": len(bundle["candidates"]),
                      "bookmarks": len(bundle["bookmarks"]), "errors": [s for s in bundle["source_status"] if s["status"] == "failed"]}, ensure_ascii=False))


def load_bundle(path):
    return json.loads(Path(path).read_text())


def discussion(args):
    """Cache a bounded breadth-first sample, retaining parents and source links."""
    bundle = load_bundle(args.bundle)
    for key in args.ids:
        row = bundle["candidates"][key]
        snapshots = []
        for metric in row.get("metrics", []):
            if metric.get("platform") != "HN":
                continue
            snapshot = {"url": metric["url"], "retrieved_at": now(), "comments": [], "errors": []}
            try:
                url = urllib.parse.urlsplit(metric["url"])
                if url.hostname != "news.ycombinator.com" or url.path != "/item":
                    raise ValueError("Expected an HN item URL")
                story_id = int(urllib.parse.parse_qs(url.query)["id"][0])
                story = get_json(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json")
                if not story or story.get("type") != "story":
                    raise ValueError("HN story unavailable")
                if normalize(story.get("url", "")) not in {normalize(row["url"]), normalize(row.get("resolved_url", row["url"]))}:
                    raise ValueError("HN story URL does not match article")
                queue = [(cid, story_id, 0) for cid in story.get("kids", [])]
                snapshot["reported_count"] = story.get("descendants")
                visited = set()
                while queue and len(visited) < args.limit:
                    cid, parent, depth = queue.pop(0)
                    if cid in visited:
                        continue
                    visited.add(cid)
                    try:
                        comment = get_json(f"https://hacker-news.firebaseio.com/v0/item/{int(cid)}.json")
                        if not comment:
                            raise ValueError("Comment unavailable")
                        if comment.get("id") != cid or comment.get("parent") != parent:
                            raise ValueError("Comment parent mismatch")
                        queue.extend((child, cid, depth + 1) for child in comment.get("kids", []))
                        if comment.get("deleted") or comment.get("dead") or not comment.get("text"):
                            continue
                        snapshot["comments"].append({"id": cid, "parent": parent, "depth": depth,
                            "by": comment.get("by"), "time": comment.get("time"),
                            "text": plain(comment["text"]), "url": f"https://news.ycombinator.com/item?id={cid}"})
                    except Exception as error:
                        snapshot["errors"].append(f"{cid}: {type(error).__name__}: {error}")
                snapshot["truncated"] = bool(queue)
                snapshot["status"] = "partial" if queue or snapshot["errors"] else "complete"
            except Exception as error:
                snapshot["status"] = "failed"
                snapshot["errors"].append(f"{type(error).__name__}: {error}")
            payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            output = Path(args.bundle).parent / "discussions" / f"{key}-{digest[:12]}.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text(payload)
            snapshots.append({"path": str(output.resolve()), "sha256": digest})
            print(json.dumps({"id": key, "path": str(output), "status": snapshot["status"],
                              "readable_comments": len(snapshot["comments"])}, ensure_ascii=False))
        row["discussions"] = snapshots
    dump(args.bundle, bundle)


def discussion_notes(row, choice):
    if not any(m.get("platform") == "HN" for m in row.get("metrics", [])):
        return []
    if not row.get("discussions"):
        raise ValueError("Fetch HN discussion before finalizing an HN recommendation")
    comments, snapshots = {}, []
    for ref in row["discussions"]:
        payload = Path(ref["path"]).read_text()
        if hashlib.sha256(payload.encode()).hexdigest() != ref["sha256"]:
            raise ValueError("Discussion snapshot changed; fetch again")
        snap = json.loads(payload)
        snapshots.append(snap)
        comments.update({c["id"]: c for c in snap["comments"]})
    points = choice.get("discussion", [])
    if comments and not points:
        raise ValueError("Review cached HN comments and provide discussion notes")
    result = ["HNの議論（確認したコメントの範囲）：", ""]
    for point in points:
        ids = point.get("comment_ids", [])
        if not point.get("summary") or not ids or any(cid not in comments for cid in ids):
            raise ValueError("Discussion notes require existing cached comment IDs")
        links = "・".join(f"[コメント {cid}]({comments[cid]['url']})" for cid in ids)
        result.extend([f"- {point['summary']}（{links}）", ""])
    for snap in snapshots:
        result.extend([f"議論確認：{len(snap['comments'])}件／状態 {snap['status']}／{snap['retrieved_at']}取得（[スレッド]({snap['url']})）", ""])
        if snap["errors"]:
            result.extend(["取得上の制約：" + "; ".join(snap["errors"]), ""])
    if not comments:
        result.extend(["議論要約：確認できるコメントなし（取得失敗とコメントなしは上の状態で区別）。", ""])
    return result


def body(args):
    bundle = load_bundle(args.bundle)
    for key in args.ids:
        r = bundle["candidates"][key]
        try:
            if args.text_file:
                if len(args.ids) != 1 or not args.evidence_url or not args.scope:
                    raise ValueError("Text import requires one ID, --evidence-url and --scope")
                public_url(args.evidence_url)
                text = Path(args.text_file).read_text()
                provenance = args.evidence_url
                scope = args.scope
                extractor = {"name": "text-file"}
            else:
                check_robots(r["url"])
                data, provenance, charset = request(r["url"])
                r["resolved_url"] = normalize(provenance)
                text, extractor = article_text(data.decode(charset, errors="replace"), provenance)
                scope = "unverified-extraction"
            if len(text.strip()) < 100:
                raise ValueError("Too little text; not a verified article body")
            truncated = len(text) > 200000
            text = text[:200000]
            digest = hashlib.sha256(text.encode()).hexdigest()
            output = Path(args.bundle).parent / "bodies" / f"{key}-{digest[:12]}.txt"
            output.parent.mkdir(exist_ok=True)
            output.write_text(text)
            r["body"] = {"path": str(output.resolve()), "sha256": digest, "scope": scope,
                         "evidence_url": provenance, "retrieved_at": now(),
                         "extractor": extractor, "truncated": truncated}
            r.pop("body_error", None)
            with db(args.workspace) as con:
                if con.execute("SELECT 1 FROM deliveries WHERE url=?", (r.get("resolved_url", r["url"]),)).fetchone():
                    r["already_recommended"] = True
            print(json.dumps({"id": key, "body": r["body"]}, ensure_ascii=False))
        except Exception as e:
            r["body_error"] = f"{type(e).__name__}: {e}"
            print(json.dumps({"id": key, "error": r["body_error"]}, ensure_ascii=False))
    dump(args.bundle, bundle)


def import_candidates(args):
    bundle = load_bundle(args.bundle)
    for r in json.loads(Path(args.file).read_text()):
        # Only provenance supplied through evidence; supplemental metrics are never fabricated here.
        if not r.get("source"):
            raise ValueError("Supplemental candidates require source")
        c = add(bundle, r, r["source"], r.get("lane", "article"))
        if "coverage" in r:
            validate_coverage(c, r["coverage"])
            c["coverage"] = r["coverage"]
        c["mandatory_reasons"] = security_flags(c, bundle["profile"]["critical_cvss"])
        c["security_fingerprint"] = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
        with db(args.workspace) as con:
            c["already_recommended"] = bool(con.execute("SELECT 1 FROM deliveries WHERE url=?", (c["url"],)).fetchone())
            old = con.execute("SELECT fingerprint FROM alerts WHERE id=?", (c["id"],)).fetchone()
        c["security_changed"] = not old or old[0] != c["security_fingerprint"]
        c["bookmarked"] = (c["url"] in bundle.get("interest_context", {}).get("snapshot", {}) or
                           any(normalize(b["url"]) == c["url"] for b in bundle["bookmarks"]))
    dump(args.bundle, bundle)


def checked_text(r, choice):
    b = r.get("body")
    if not b:
        raise ValueError(f"{r['id']}: no body evidence")
    text = Path(b["path"]).read_text()
    if hashlib.sha256(text.encode()).hexdigest() != b["sha256"]:
        raise ValueError("Body snapshot changed; fetch/import again")
    evidence = choice.get("evidence", [])
    if not evidence or not choice.get("body_reviewed"):
        raise ValueError("Model must review the body and provide exact evidence excerpts")
    for quote in evidence:
        if not isinstance(quote, str) or len(quote) < 12 or " ".join(quote.split()) not in " ".join(text.split()):
            raise ValueError("Evidence excerpt is absent from the cached body")
    for field in ("summary", "reason"):
        if not isinstance(choice.get(field), str) or not choice[field].strip():
            raise ValueError(f"Missing {field}")
    if choice.get("category") not in {"reading", "experiment", "technology", "other"}:
        raise ValueError("Unsupported category")


def metric_time(value):
    """Accept API Unix seconds or timezone-aware ISO timestamps; never guess a zone."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
    except (ValueError, OverflowError, OSError):
        pass
    return None


def attention_signal(metric):
    """Classify one observation, independent of render time and other platforms."""
    observed = metric_time(metric.get("retrieved_at"))
    submitted = metric_time(metric.get("submitted_at"))
    hours = (observed - submitted).total_seconds() / 3600 if observed and submitted else None
    if hours is not None and hours < 0:
        hours = None
    score = metric.get("score")
    policy = json.loads((SKILL / "references/attention-policy.json").read_text())
    platform = policy["platforms"].get(metric.get("platform"))
    if hours is None or not isinstance(score, (int, float)) or isinstance(score, bool) or not platform:
        return {"hours": hours, "label": "話題度判定は未確認"}
    for rule in platform["recent"]:
        if hours <= rule["max_hours"] and score >= rule["min_score"]:
            return {"hours": hours, "label": rule["label"]}
    if hours > 24:
        for rule in platform["cumulative"]:
            if score >= rule["min_score"]:
                return {"hours": hours, "label": rule["label"]}
        return {"hours": hours, "label": "累積の反応は小さめ"}
    return {"hours": hours, "label": "話題ラベルの目安未満"}


def attention(r):
    if not r["metrics"]:
        return "注目度：未確認（ゼロ・不人気という意味ではありません）"
    lines = []
    for m in r["metrics"]:
        observed = metric_time(m.get("retrieved_at"))
        dt = observed.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M JST") if observed else "日時未確認"
        points = m.get("score") if m.get("score") is not None else "未確認"
        comments = m.get("comments") if m.get("comments") is not None else "未確認"
        signal = attention_signal(m)
        age = f"投稿後{signal['hours']:.1f}時間" if signal["hours"] is not None else "投稿経過時間は未確認"
        submitted = metric_time(m.get("submitted_at"))
        stamp = "／投稿 " + submitted.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M JST") if submitted else ""
        lines.append(f"[{m['platform']} {points}pt／{comments}コメント]({m['url']})・{age}・{signal['label']}（取得時点：{dt}{stamp}）")
    return "注目度：" + " / ".join(lines)


PICKUP_SITES = {
    "Hatena": ("はてなブックマーク", {"b.hatena.ne.jp"}),
    "HN": ("HN", {"news.ycombinator.com"}),
    "Lobsters": ("Lobsters", {"lobste.rs"}),
    "MetaFilter": ("MetaFilter", {"metafilter.com", "www.metafilter.com"}),
    "Kottke": ("Kottke", {"kottke.org", "www.kottke.org", "also.kottke.org"}),
}


def validate_coverage(row, checks):
    """Check the evidence contract; the model must verify each post's outgoing link."""
    if not isinstance(checks, list):
        raise ValueError("coverage must be a list")
    targets = {normalize(row[k]) for k in ("url", "resolved_url") if row.get(k)}
    for check in checks:
        if check.get("site") not in PICKUP_SITES:
            raise ValueError("Unsupported coverage site")
        if check.get("status") not in {"found", "not_found", "failed", "unchecked"}:
            raise ValueError("Unsupported coverage status")
        if metric_time(check.get("retrieved_at")) is None:
            raise ValueError("Coverage requires a timezone-aware observation time")
        if check["status"] == "found":
            post = normalize(check.get("post_url", ""))
            if urllib.parse.urlsplit(post).hostname not in PICKUP_SITES[check["site"]][1]:
                raise ValueError("Coverage evidence must link to the named site")
            if normalize(check.get("article_url", "")) not in targets:
                raise ValueError("Coverage must refer to the same article, not just the same topic")
        elif not isinstance(check.get("note"), str) or not check["note"].strip():
            raise ValueError("Unconfirmed coverage requires a search scope or failure reason")


def pickup_summary(row):
    checks = row.get("coverage", [])
    validate_coverage(row, checks)
    found, times = {}, []
    for metric in row.get("metrics", []):
        site = metric.get("platform")
        if site in {"HN", "Lobsters"} and metric_time(metric.get("retrieved_at")):
            url = normalize(metric["url"])
            if urllib.parse.urlsplit(url).hostname in PICKUP_SITES[site][1]:
                found.setdefault(site, []).append(url)
                times.append(metric_time(metric["retrieved_at"]))
    for check in checks:
        if check["status"] == "found":
            found.setdefault(check["site"], []).append(normalize(check["post_url"]))
        times.append(metric_time(check["retrieved_at"]))
    if not found and not checks:
        return "複数サイトでの掲載：未確認"
    links = []
    for site, (label, _) in PICKUP_SITES.items():
        for n, url in enumerate(dict.fromkeys(found.get(site, [])), 1):
            links.append(f"[{label}{'（別投稿）' if n > 1 else ''}]({url})")
    result = f"複数サイトでの掲載：対象5サイト中、掲載を確認できたのは{len(found)}サイト"
    if links:
        result += "（" + "・".join(links) + "）"
    missing = []
    for site, (label, _) in PICKUP_SITES.items():
        if site in found:
            continue
        records = [c for c in checks if c["site"] == site]
        if not records:
            missing.append(label + "：未確認")
        else:
            labels = {"not_found": "確認範囲で未発見", "failed": "取得失敗", "unchecked": "未確認"}
            missing.extend(label + "：" + labels[c["status"]] + "（" + c["note"] + "）" for c in records)
    if times:
        stamps = sorted({t.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M JST") for t in times})
        result += "／確認時点：" + "〜".join([stamps[0], stamps[-1]] if len(stamps) > 1 else stamps)
    if missing:
        result += "。" + "／".join(missing)
    return result


def finalize(args):
    bundle = load_bundle(args.bundle)
    interests = None
    if bundle.get("interest_context", {}).get("collection_ok"):
        interests = interest_state(args.workspace)
        if interests["last_run"] != bundle["run"]:
            raise ValueError("Commit bookmark interest analysis before finalizing recommendations")
    selection = json.loads(Path(args.selection).read_text())
    choices = selection.get("recommendations", [])
    if len(choices) > 10:
        raise ValueError("At most 10 recommendations; don't pad the list")
    ids = [c["id"] for c in choices]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate article selections")
    with db(args.workspace) as con:
        live_seen = dict(con.execute("SELECT url, run FROM deliveries"))
    # Validate everything before creating the digest or changing delivery history.
    for c in choices:
        r = bundle["candidates"][c["id"]]
        if r["url"] in live_seen and live_seen[r["url"]] != bundle["run"]:
            raise ValueError("Article was delivered by another run after collection")
        if r.get("resolved_url") in live_seen and live_seen[r["resolved_url"]] != bundle["run"]:
            raise ValueError("Resolved article URL has already been delivered")
        if r.get("already_recommended") or r.get("bookmarked") or "article" not in r["lanes"]:
            raise ValueError("Selected article is saved, already delivered or security-only")
        checked_text(r, c)
        discussion_notes(r, c)
        pickup_summary(r)
    lines = ["# 英語記事の推薦", "", "好みを優先して選定。注目度は各コミュニティ内の反応です。", ""]
    for n, c in enumerate(choices, 1):
        r = bundle["candidates"][c["id"]]
        # Article titles/links/metrics come only from collected records, not model JSON.
        title = r["title"].replace("[", "(").replace("]", ")").replace("\n", " ")
        lines.extend([f"## {n}. [{title}]({r['url']})", "", c["summary"], "",
                      "推薦理由：" + c["reason"], "", attention(r), "", pickup_summary(r), "",
                      "本文確認範囲：" + r["body"]["scope"], ""])
        if c.get("how_to_read"):
            lines.extend(["見どころ・操作：" + c["how_to_read"], ""])
        lines.extend(discussion_notes(r, c))
        if r.get("published"):
            lines.extend(["収集元の公開日表記：" + r["published"], ""])
    changes = [h for h in (interests or {}).get("hypotheses", {}).values()
               if any(e["run"] == bundle["run"] for e in h["evidence"])]
    if changes:
        lines.extend(["## 今回のブックマークから見えた関心（仮説）", ""])
        for h in changes:
            label = "見直し" if h["status"] == "retired" else ("暫定" if h["confidence"] == "tentative" else "複数記事が支持")
            urls = list(dict.fromkeys(e["url"] for e in h["evidence"] if e["run"] == bundle["run"]))
            links = "・".join(f"[保存記事{i}]({url})" for i, url in enumerate(urls, 1))
            lines.extend([f"- {h['text']}（{label}）。{h['rationale']} 根拠：{links}", ""])
    security_notes = {c["id"]: c for c in selection.get("security", [])}
    for key in security_notes:
        if key not in bundle["candidates"] or "security" not in bundle["candidates"][key]["lanes"]:
            raise ValueError("Security selection must refer to a collected security candidate")
    pending = [r for r in bundle["candidates"].values() if "security" in r["lanes"] and
               (r.get("mandatory_reasons") or r["id"] in security_notes) and r.get("security_changed", True)]
    lines.extend(["## セキュリティ", ""])
    groups = {}
    for r in pending:
        groups.setdefault(r.get("security_id") or r["id"], []).append(r)
    for group in groups.values():
        r = group[0]
        lines.extend([f"- [{r['title']}]({r['url']})", "",
                      "  掲載条件：" + ", ".join(sorted({reason for item in group for reason in item.get("mandatory_reasons", [])}) or ["モデルが影響確認対象として選定"]), ""])
        for item in group:
            if item is not r:
                lines.extend([f"  関連する公式記録：[{item['title']}]({item['url']})", ""])
            for s in item.get("cvss", []):
                lines.extend([f"  CVSS {s['version']}: {s['score']}（[出典]({s['source']})）", ""])
        note = "\n\n  ".join(security_notes.get(item["id"], {}).get("note", "") for item in group).strip()
        lines.extend(["  " + (note or "一次情報の内容と自分の環境への影響を確認してください。モデルによる解説は未作成です。"), ""])
    if not pending:
        lines.extend(["収集済み範囲で新規・更新の必須速報なし。網羅的な安全確認を意味しません。", ""])
    errors = [s for s in bundle["source_status"] if s["status"] == "failed"]
    if errors:
        lines.extend(["## 取得できなかった情報源", ""] + [f"- {s['source']}: {s['error']}" for s in errors] + [""])
    if selection.get("limitations"):
        lines.extend(["確認上の制約：" + selection["limitations"], ""])
    out = Path(args.bundle).parent / "digest.md"
    out.write_text("\n".join(lines))
    result = {"run": bundle["run"], "created_at": now(), "articles": [dict(bundle["candidates"][c["id"]], assessment=c) for c in choices],
              "security": pending, "source_status": bundle["source_status"], "selection": selection}
    dump(out.with_suffix(".json"), result)
    # Explicit --commit records successful delivery, not just a draft render.
    if args.commit:
        with db(args.workspace) as con:
            for c in choices:
                r = bundle["candidates"][c["id"]]
                con.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?)", (r["url"], bundle["run"]))
                if r.get("resolved_url"):
                    con.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?)", (r["resolved_url"], bundle["run"]))
            for r in pending:
                con.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?)", (r["id"], r["security_fingerprint"]))
        # Human-readable/exported results only; SQLite is the history authority.
        archive = Path(args.workspace) / "research/digests" / (bundle["run"] + "-manual")
        dump(archive.with_suffix(".json"), result)
        archive.with_suffix(".md").write_text(out.read_text())
    print(json.dumps({"digest": str(out), "recommendations": len(choices), "security": len(groups), "committed": args.commit}))


def main():
    global REQUEST_LOG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Directory containing .env and local history (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("config", help="Show whether a Hatena username is configured; no network access")
    p.set_defaults(func=config)
    p = sub.add_parser("setup", help="Save a Hatena username in the workspace .env")
    p.add_argument("--hatena-user")
    p.set_defaults(func=setup)
    p = sub.add_parser("collect")
    p.add_argument("--hn-limit", type=int, default=30)
    p.add_argument("--feed-limit", type=int, default=20)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--advisory-pages", type=int, default=10)
    p.set_defaults(func=collect)
    p = sub.add_parser("body")
    p.add_argument("bundle")
    p.add_argument("ids", nargs="+")
    p.add_argument("--text-file")
    p.add_argument("--evidence-url")
    p.add_argument("--scope", choices=["full", "partial"])
    p.set_defaults(func=body)
    p = sub.add_parser("fetch", help="Fetch supplementary pages locally; save text, links and HTTP evidence")
    p.add_argument("bundle")
    p.add_argument("urls", nargs="+")
    p.set_defaults(func=fetch)
    p = sub.add_parser("discussion")
    p.add_argument("bundle")
    p.add_argument("ids", nargs="+")
    p.add_argument("--limit", type=int, choices=range(1, 201), default=60)
    p.set_defaults(func=discussion)
    p = sub.add_parser("import")
    p.add_argument("bundle")
    p.add_argument("file")
    p.set_defaults(func=import_candidates)
    p = sub.add_parser("finalize")
    p.add_argument("bundle")
    p.add_argument("selection")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=finalize)
    p = sub.add_parser("interests")
    p.add_argument("bundle")
    p.add_argument("analysis")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=update_interests)
    args = parser.parse_args()
    args.workspace = resolve_workspace(args)
    if args.command == "collect" and not (1 <= args.hn_limit <= 200 and 1 <= args.feed_limit <= 100 and 1 <= args.days <= 90 and 1 <= args.advisory_pages <= 10):
        parser.error("Collection bounds exceeded")
    if args.command == "collect":
        args.stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        REQUEST_LOG = Path(args.workspace) / ".picker/runs" / args.stamp / "requests.jsonl"
    elif args.command in {"body", "discussion", "fetch"}:
        REQUEST_LOG = Path(args.bundle).parent / "requests.jsonl"
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
