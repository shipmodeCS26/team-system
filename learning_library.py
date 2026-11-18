"""Read-only internal learning views. Private content is never a static application asset."""
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests
from flask import Blueprint, Response, abort, render_template, request
from werkzeug.security import check_password_hash

bp = Blueprint('learning', __name__)
FILES = {'knowledge.json': 'records', 'sources.json': 'sources',
         'tasks.json': 'tasks', 'skool-lessons.json': 'lessons'}
MAX_BYTES = 512 * 1024
MAX_ITEMS = 5000
SHA = re.compile(r'[0-9a-f]{40}')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}')


class LibraryUnavailable(Exception):
    """Safe boundary for configuration, validation and upstream errors."""


def enabled():
    return os.getenv('LEARNING_LIBRARY_ENABLED', 'false').lower() == 'true'


def internal_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not enabled():
            abort(404)
        if not all(os.getenv(k) for k in ('WORKSPACE_USER', 'WORKSPACE_PASSWORD_HASH', 'SECRET_KEY')):
            return unavailable()
        auth = request.authorization
        valid = False
        if auth and auth.type.lower() == 'basic':
            try:
                valid = (hmac.compare_digest((auth.username or '').encode('utf-8'), os.environ['WORKSPACE_USER'].encode('utf-8'))
                         and check_password_hash(os.environ['WORKSPACE_PASSWORD_HASH'], auth.password or ''))
            except (ValueError, TypeError):
                valid = False
        if not valid:
            return Response('Sign in to your Shipmode workspace.', 401,
                            {'WWW-Authenticate': 'Basic realm="Shipmode workspace", charset="UTF-8"'})
        return fn(*args, **kwargs)
    return wrapper


def unavailable():
    return render_template('learning/unavailable.html'), 503


def safe_link(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    return None


def parse(raw):
    if len(raw) > MAX_BYTES:
        raise LibraryUnavailable()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise LibraryUnavailable() from None
    if not isinstance(data, dict):
        raise LibraryUnavailable()
    return data


def read_file(path):
    with path.open('rb') as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise LibraryUnavailable()
    return raw


def github_file(repo, revision, token, filename):
    # The host, directory and filenames are code-owned, never request parameters.
    url = f'https://api.github.com/repos/{repo}/contents/knowledge-base/data/{filename}'
    with requests.get(url, params={'ref': revision},
                      headers={'Authorization': f'Bearer {token}',
                               'Accept': 'application/vnd.github.raw+json'},
                      timeout=(3, 10), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise LibraryUnavailable()
        data = bytearray()
        for chunk in response.iter_content(16384):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise LibraryUnavailable()
        return bytes(data)


def load_bundle():
    revision = os.getenv('LEARNING_LIBRARY_COMMIT', '')
    if not SHA.fullmatch(revision):
        raise LibraryUnavailable()
    directory = os.getenv('LEARNING_LIBRARY_DIR')
    try:
        if directory:
            root = Path(directory).resolve()
            manifest = parse(read_file(root / 'manifest.json'))
            if manifest.get('commit') != revision or not isinstance(manifest.get('files'), dict):
                raise LibraryUnavailable()
            raw_files = {}
            for filename in FILES:
                raw = read_file(root / filename)
                if hashlib.sha256(raw).hexdigest() != manifest['files'].get(filename):
                    raise LibraryUnavailable()
                raw_files[filename] = raw
        else:
            repo = os.getenv('LEARNING_LIBRARY_REPO', '')
            token = os.getenv('LEARNING_LIBRARY_TOKEN', '')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not token:
                raise LibraryUnavailable()
            raw_files = {f: github_file(repo, revision, token, f) for f in FILES}
        bundle = {key: parse(raw_files[f]) for f, key in FILES.items()}
        validate(bundle)
        return bundle, revision
    except (OSError, requests.RequestException, LibraryUnavailable, RecursionError):
        # No URLs, upstream response bodies, paths, records or secrets in responses/logs.
        raise LibraryUnavailable() from None


def validate(bundle):
    groups = {}
    for name in FILES.values():
        items = bundle[name].get(name)
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise LibraryUnavailable()
        mapped = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not IDENTIFIER.fullmatch(item['id']):
                raise LibraryUnavailable()
            if item['id'] in mapped:
                raise LibraryUnavailable()
            mapped[item['id']] = item
        groups[name] = mapped
    # Each file is pinned; the dates are displayed rather than treated as live data.
    for group in ('records', 'sources', 'tasks'):
        if not isinstance(bundle[group].get('as_of'), str):
            raise LibraryUnavailable()
    def strings(item, names):
        if any(not isinstance(item.get(k), str) or len(item[k]) > 12000 for k in names):
            raise LibraryUnavailable()
    def refs(item, key, target):
        values = item.get(key)
        if not isinstance(values, list) or len(values) > MAX_ITEMS or any(not isinstance(x, str) or x not in target for x in values):
            raise LibraryUnavailable()
    for s in groups['sources'].values():
        strings(s, ['title', 'url', 'kind', 'coverage', 'last_reviewed'])
        if not safe_link(s['url']) and not s['url'].startswith('local-attachment:'):
            raise LibraryUnavailable()
    for r in groups['records'].values():
        strings(r, ['title', 'topic', 'learning', 'application', 'guardrail', 'updated_at', 'application_status'])
        if r.get('scope') not in ('shipmode', 'rhen') or r.get('decision') not in ('adopt', 'adapt', 'defer', 'reject') or r.get('visibility') != 'internal':
            raise LibraryUnavailable()
        refs(r, 'source_ids', groups['sources']); refs(r, 'next_task_ids', groups['tasks'])
    for t in groups['tasks'].values():
        strings(t, ['title', 'priority', 'proposed_owner', 'status', 'done_when'])
        if t.get('scope') not in ('shipmode', 'rhen') or t['status'] not in ('open', 'in_progress', 'blocked', 'done', 'cancelled'):
            raise LibraryUnavailable()
        refs(t, 'depends_on', groups['tasks']); refs(t, 'source_ids', groups['sources'])
        if t['status'] == 'done' and not (t.get('completion_evidence') and t.get('completed_at')):
            raise LibraryUnavailable()
    # Iterative cycle detection avoids deep recursion on untrusted task graphs.
    pending = {k: set(t['depends_on']) for k, t in groups['tasks'].items()}
    while pending:
        ready = {k for k, deps in pending.items() if not deps}
        if not ready:
            raise LibraryUnavailable()
        pending = {k: deps - ready for k, deps in pending.items() if k not in ready}
    for l in groups['lessons'].values():
        strings(l, ['title', 'url', 'status'])
        if not safe_link(l['url']) or l['status'] not in ('indexed_only', 'reviewed_summary'):
            raise LibraryUnavailable()
        if l['status'] == 'indexed_only' and l.get('summary') is not None:
            raise LibraryUnavailable()
        if l['status'] == 'reviewed_summary' and not all(isinstance(l.get(k), str) and l[k] for k in ('summary', 'reviewed_at', 'reviewer')):
            raise LibraryUnavailable()


def search(items, query):
    terms = query.casefold().split()
    return [item for item in items if all(t in json.dumps(item, ensure_ascii=False).casefold() for t in terms)]


@bp.get('/knowledgebase/learning')
@internal_only
def library():
    try:
        bundle, revision = load_bundle()
    except LibraryUnavailable:
        return unavailable()
    view = request.args.get('view', 'shipmode')
    if view not in ('shipmode', 'rhen', 'tasks', 'courses'):
        abort(400)
    query = request.args.get('q', '').strip()[:200]
    decision = request.args.get('decision', '')[:40]
    status = request.args.get('status', '')[:80]
    scope = request.args.get('scope', '')[:20]
    task = request.args.get('task', '')
    if task and (view != 'tasks' or not IDENTIFIER.fullmatch(task)):
        abort(400)
    if view in ('shipmode', 'rhen'):
        items = [r for r in bundle['records']['records'] if r['scope'] == view]
        status_key = 'application_status'
    else:
        name = 'tasks' if view == 'tasks' else 'lessons'
        items = bundle[name][name]
        status_key = 'status'
    statuses = sorted({r[status_key] for r in items})
    if task:
        items = [r for r in items if r['id'] == task]
    items = search(items, query)
    if decision:
        items = [r for r in items if r.get('decision') == decision]
    if status:
        items = [r for r in items if r[status_key] == status]
    if scope:
        items = [r for r in items if r.get('scope') == scope]
    page = request.args.get('page', '1')
    if not page.isascii() or not page.isdigit() or len(page) > 5:
        abort(400)
    page = max(1, int(page)); total = len(items); size = 20
    pages = max(1, (total + size - 1) // size); page = min(page, pages)
    return render_template('learning/library.html', view=view, items=items[(page-1)*size:page*size],
                           total=total, page=page, pages=pages, query=query, decision=decision,
                           status=status, scope=scope, task=task, statuses=statuses, safe_link=safe_link,
                           sources={s['id']: s for s in bundle['sources']['sources']},
                           revision=revision, as_of=bundle['records']['as_of'],
                           course_coverage=bundle['lessons'].get('coverage', 'Coverage not specified.'))
