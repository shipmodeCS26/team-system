import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

from werkzeug.security import generate_password_hash
import app as server
import learning_library as library

REVISION = 'a' * 40
HASH = generate_password_hash('test-only-password', method='pbkdf2:sha256:1000')
AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'reviewer:test-only-password').decode()}


def sample():
    return {
        'records': {'as_of': '2026-11-18', 'records': [
            {'id': 'SM01', 'scope': 'shipmode', 'title': 'Reusable test method', 'topic': 'research',
             'decision': 'adopt', 'source_ids': ['S01'], 'learning': 'Learn from behavior.',
             'application': 'Review a buying decision.', 'application_status': 'method_ready',
             'next_task_ids': ['T01'], 'guardrail': 'Not validated demand.',
             'updated_at': '2026-11-18', 'visibility': 'internal'},
            {'id': 'RH01', 'scope': 'rhen', 'title': 'Synthetic brand application', 'topic': 'copy',
             'decision': 'adapt', 'source_ids': ['S01'], 'learning': 'Use approved facts.',
             'application': 'Synthetic-only example.', 'application_status': 'plan_only',
             'next_task_ids': [], 'guardrail': 'Not deployed.',
             'updated_at': '2026-11-18', 'visibility': 'internal'}]},
        'sources': {'as_of': '2026-11-18', 'sources': [
            {'id': 'S01', 'title': 'Synthetic source', 'url': 'https://example.com/source',
             'kind': 'project', 'coverage': 'Test fixture only.', 'last_reviewed': '2026-11-18'}]},
        'tasks': {'as_of': '2026-11-18', 'tasks': [
            {'id': 'T01', 'scope': 'shipmode', 'title': 'Synthetic task', 'priority': 'P1',
             'proposed_owner': 'Reviewer', 'owner_accepted': False, 'status': 'open',
             'depends_on': [], 'done_when': 'Checked evidence.', 'source_ids': ['S01']}]},
        'lessons': {'coverage': 'Partial synthetic index.', 'lessons': [
            {'id': 'SK01', 'title': 'Unreviewed lesson', 'url': 'https://example.com/lesson',
             'status': 'indexed_only', 'summary': None}]}}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'APP_MODE': 'demo', 'LEARNING_LIBRARY_ENABLED': 'true',
                    'WORKSPACE_USER': 'reviewer', 'WORKSPACE_PASSWORD_HASH': HASH, 'SECRET_KEY': 'test-only-secret',
                    'INVENTORY_SHEETS_ENABLED': 'false', 'SHOPIFY_ENABLED': 'false', 'SSK_API_ENABLED': 'false'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        server.app.config['TESTING'] = True
        self.client = server.app.test_client()
        self.bundle = sample()

    def get(self, path='/knowledgebase/learning', auth=True):
        with patch.object(library, 'load_bundle', return_value=(self.bundle, REVISION)):
            return self.client.get(path, headers=AUTH if auth else {})

    def test_feature_off_fetches_nothing(self):
        os.environ['LEARNING_LIBRARY_ENABLED'] = 'false'
        with patch.object(library, 'load_bundle') as fetch:
            self.assertEqual(self.client.get('/knowledgebase/learning', headers=AUTH).status_code, 404)
            fetch.assert_not_called()

    def test_auth_required_even_in_demo(self):
        for mode in ['demo', 'live']:
            os.environ['APP_MODE'] = mode
            with patch.object(library, 'load_bundle') as fetch:
                response = self.client.get('/knowledgebase/learning')
                self.assertEqual(response.status_code, 401)
                self.assertIn('Basic ', response.headers['WWW-Authenticate'])
                fetch.assert_not_called()
                self.assertNotIn(b'Reusable test method', response.data)

    def test_wrong_password_and_username_are_rejected(self):
        for identity in [b'reviewer:wrong', b'other:test-only-password']:
            header = {'Authorization': 'Basic ' + base64.b64encode(identity).decode()}
            self.assertEqual(self.client.get('/knowledgebase/learning', headers=header).status_code, 401)

    def test_missing_auth_configuration_fails_closed(self):
        for key in ['WORKSPACE_USER', 'WORKSPACE_PASSWORD_HASH', 'SECRET_KEY']:
            value = os.environ.pop(key)
            with patch.object(library, 'load_bundle') as fetch:
                self.assertEqual(self.client.get('/knowledgebase/learning', headers=AUTH).status_code, 503)
                fetch.assert_not_called()
            os.environ[key] = value

    def test_scopes_search_and_empty_state(self):
        page = self.get()
        self.assertIn(b'Reusable test method', page.data)
        self.assertNotIn(b'Synthetic brand application', page.data)
        page = self.get('/knowledgebase/learning?view=rhen')
        self.assertIn(b'Synthetic brand application', page.data)
        self.assertNotIn(b'Reusable test method', page.data)
        self.assertIn(b'No matching results', self.get('/knowledgebase/learning?q=missing').data)
        self.assertIn(b'Reusable test method', self.get('/knowledgebase/learning?q=behavior&decision=adopt&status=method_ready').data)

    def test_sources_task_dependencies_and_coverage(self):
        page = self.get()
        self.assertIn(b'https://example.com/source', page.data)
        self.assertIn(b'Not validated demand.', page.data)
        page = self.get('/knowledgebase/learning?view=tasks')
        self.assertIn(b'Synthetic task', page.data)
        self.assertIn(b'Assignment not confirmed', page.data)
        page = self.get('/knowledgebase/learning?view=courses')
        self.assertIn(b'Partial synthetic index.', page.data)
        self.assertIn(b'Summary pending review.', page.data)

    def test_task_links_open_only_the_exact_reference(self):
        self.bundle['tasks']['tasks'].append(dict(self.bundle['tasks']['tasks'][0], id='T02', title='Downstream task', depends_on=['T01']))
        page = self.get('/knowledgebase/learning?view=tasks&task=T01')
        self.assertIn(b'<h2>Synthetic task</h2>', page.data)
        self.assertNotIn(b'<h2>Downstream task</h2>', page.data)
        self.assertIn(b'task=T01', self.get().data)
        self.assertEqual(self.get('/knowledgebase/learning?view=tasks&task=bad%2Fid').status_code, 400)

    def test_content_and_query_are_escaped_and_csp_retained(self):
        self.bundle['records']['records'][0]['title'] = '<script>alert(1)</script>'
        self.bundle['sources']['sources'][0]['url'] = 'javascript:alert(1)'
        page = self.get('/knowledgebase/learning')
        self.assertIn(b'&lt;script&gt;', page.data)
        self.assertNotIn(b'<script>', page.data)
        self.assertNotIn(b'href="javascript:', page.data)
        self.assertNotIn(b'style=', page.data)
        self.assertIn("script-src 'self'", page.headers['Content-Security-Policy'])
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        page = self.get('/knowledgebase/learning?q=%22%3E%3Cscript%3E')
        self.assertNotIn(b'<script>', page.data)

    def test_outage_does_not_expose_details(self):
        with patch.object(library, 'load_bundle', side_effect=library.LibraryUnavailable('sensitive-detail')):
            page = self.client.get('/knowledgebase/learning', headers=AUTH)
        self.assertEqual(page.status_code, 503)
        self.assertNotIn(b'sensitive-detail', page.data)

    def test_invalid_view_and_page(self):
        for query in ['view=unknown', 'page=bad', 'page=123456', 'page=-1']:
            self.assertEqual(self.get('/knowledgebase/learning?' + query).status_code, 400)

    def test_pagination(self):
        self.bundle['records']['records'] = [dict(self.bundle['records']['records'][0], id=f'X{i}', title=f'Record {i}') for i in range(22)]
        page = self.get('/knowledgebase/learning?page=2')
        self.assertIn(b'Record 20', page.data)
        self.assertNotIn(b'<h2>Record 0</h2>', page.data)
        self.assertIn(b'Previous', page.data)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'LEARNING_LIBRARY_COMMIT': REVISION}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, bundle=None):
        bundle = bundle or sample()
        hashes = {}
        for filename, key in library.FILES.items():
            raw = json.dumps(bundle[key]).encode(); (self.root / filename).write_bytes(raw)
            hashes[filename] = hashlib.sha256(raw).hexdigest()
        (self.root / 'manifest.json').write_text(json.dumps({'commit': REVISION, 'files': hashes}))
        os.environ['LEARNING_LIBRARY_DIR'] = str(self.root)

    def test_packager_rejects_public_checkout_output(self):
        self.save()
        script = Path(library.__file__).parent / 'package_learning_library.py'
        for output in [script.parent / 'static' / 'private-test-artifact', script.parent / 'private-test-artifact']:
            result = subprocess.run([sys.executable, str(script), '--source', str(self.root),
                                     '--output', str(output), '--commit', REVISION], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'outside the application checkout', result.stderr)
            self.assertFalse(output.exists())

    def test_pinned_artifact_reads_all_files(self):
        self.save(); data, revision = library.load_bundle()
        self.assertEqual(revision, REVISION)
        self.assertEqual(len(data['records']['records']), 2)

    def test_wrong_version_and_tampering_fail(self):
        self.save();os.environ['LEARNING_LIBRARY_COMMIT'] = 'b' * 40
        with self.assertRaises(library.LibraryUnavailable):library.load_bundle()
        os.environ['LEARNING_LIBRARY_COMMIT'] = REVISION
        (self.root / 'knowledge.json').write_text('{}')
        with self.assertRaises(library.LibraryUnavailable):library.load_bundle()

    def test_missing_settings_and_malformed_source(self):
        with self.assertRaises(library.LibraryUnavailable):library.load_bundle()
        self.save(); (self.root / 'manifest.json').write_text('not json')
        with self.assertRaises(library.LibraryUnavailable):library.load_bundle()

    def test_size_limit(self):
        with self.assertRaises(library.LibraryUnavailable):library.parse(b'x' * (library.MAX_BYTES + 1))
        self.save(); (self.root / 'knowledge.json').write_bytes(b'x' * (library.MAX_BYTES + 1))
        with self.assertRaises(library.LibraryUnavailable):library.load_bundle()

    def test_bad_shapes_references_and_cycles_rejected(self):
        broken=[]
        b=sample();b['records']['records'][0]['source_ids']=['missing'];broken.append(b)
        b=sample();b['tasks']['tasks'][0]['depends_on']=['T01'];broken.append(b)
        b=sample();b['records']['records'][0]['scope']='other';broken.append(b)
        b=sample();b['lessons']['lessons'][0]['summary']='invented summary';broken.append(b)
        b=sample();b['sources']['sources'][0]['url']='javascript:alert(1)';broken.append(b)
        b=sample();b['records']['records'].append(copy.deepcopy(b['records']['records'][0]));broken.append(b)
        for bundle in broken:
            with self.assertRaises(library.LibraryUnavailable):library.validate(bundle)

    def test_safe_links(self):
        for url in ['javascript:alert(1)', '//example.com', 'https://user:pass@example.com', 'https://example.com/\n']:
            self.assertIsNone(library.safe_link(url))
        self.assertEqual(library.safe_link('https://example.com/ok'), 'https://example.com/ok')

    def test_github_fetch_pins_each_read_and_disables_redirects(self):
        os.environ.update(LEARNING_LIBRARY_REPO='example/private-library',LEARNING_LIBRARY_TOKEN='test-only-token')
        bundle=sample();responses=[]
        for filename,key in library.FILES.items():
            response=MagicMock();response.__enter__.return_value=response;response.status_code=200
            response.iter_content.return_value=[json.dumps(bundle[key]).encode()];responses.append(response)
        with patch.object(library.requests,'get',side_effect=responses) as get:
            result, revision=library.load_bundle()
        self.assertEqual(len(get.call_args_list),4)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs['params'],{'ref':REVISION})
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.args[0].startswith('https://api.github.com/repos/example/private-library/contents/knowledge-base/data/'))

    def test_remote_redirect_and_transport_error_fail_closed(self):
        os.environ.update(LEARNING_LIBRARY_REPO='example/private-library',LEARNING_LIBRARY_TOKEN='test-only-token')
        response=MagicMock();response.__enter__.return_value=response;response.status_code=302
        with patch.object(library.requests,'get',return_value=response):
            with self.assertRaises(library.LibraryUnavailable):library.load_bundle()
        with patch.object(library.requests,'get',side_effect=library.requests.RequestException('secret')):
            with self.assertRaises(library.LibraryUnavailable):library.load_bundle()


if __name__ == '__main__':unittest.main()
