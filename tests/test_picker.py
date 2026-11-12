import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/english-article-picker/scripts/picker.py"
spec = importlib.util.spec_from_file_location("picker", SCRIPT)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PickerTests(unittest.TestCase):
    def test_missing_user_stops_collection_before_network_or_history_creation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(p.os.environ, {}, clear=True), \
             patch.object(p, 'request') as request:
            args = SimpleNamespace(workspace=tmp)
            with self.assertRaisesRegex(ValueError, 'setup --hatena-user'):
                p.collect(args)
            request.assert_not_called()
            self.assertFalse((Path(tmp) / '.picker').exists())

    def test_setup_persists_user_and_second_setup_never_prompts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(p.os.environ, {}, clear=True), \
             patch.object(p, 'input', create=True) as prompt, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path = root / '.env'
            path.write_text('# local settings\nOTHER_SETTING=keep-me\n')
            args = SimpleNamespace(workspace=root, hatena_user='sample-user')
            p.setup(args)
            saved = path.read_text()
            self.assertIn('OTHER_SETTING=keep-me', saved)
            self.assertIn('HATENA_USER=sample-user', saved)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(p.user_config(root)['hatena_user'], 'sample-user')
            args.hatena_user = None
            p.setup(args)
            prompt.assert_not_called()
            self.assertEqual(path.read_text(), saved)
            self.assertFalse((root / '.picker/state.sqlite3').exists())

    def test_user_setting_precedence_and_safe_dotenv_parsing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(p.os.environ, {}, clear=True):
            root = Path(tmp)
            path = root / '.env'
            path.write_text('IGNORED=$(do-not-run)\nexport HATENA_USER="file-user" # comment\n')
            self.assertEqual(p.user_config(root)['source'], '.env')
            with patch.dict(p.os.environ, {'HATENA_USER': 'environment-user'}):
                self.assertEqual(p.user_config(root)['hatena_user'], 'environment-user')
                self.assertEqual(p.user_config(root)['source'], 'environment')
            with patch.dict(p.os.environ, {'HATENA_USER': ''}):
                self.assertEqual(p.user_config(root)['hatena_user'], 'file-user')
            for value in ['https://b.hatena.ne.jp/someone/', 'one two', '$(touch /tmp/unsafe)', '"unclosed']:
                path.write_text('HATENA_USER=' + value + '\n')
                with self.assertRaises(ValueError):
                    p.user_config(root)

    def test_setup_asks_only_when_unconfigured_in_interactive_terminal(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(p.os.environ, {}, clear=True), \
             patch.object(p.sys.stdin, 'isatty', return_value=False), \
             patch.object(p, 'input', create=True) as prompt:
            args = SimpleNamespace(workspace=tmp, hatena_user=None)
            with self.assertRaisesRegex(ValueError, 'setup --hatena-user'):
                p.setup(args)
            prompt.assert_not_called()
            with patch.object(p.sys.stdin, 'isatty', return_value=True), \
                 patch.object(p, 'input', return_value='new-reader', create=True) as interactive, \
                 contextlib.redirect_stdout(io.StringIO()):
                p.setup(args)
            interactive.assert_called_once()
            self.assertEqual(p.user_config(tmp)['hatena_user'], 'new-reader')

    def test_account_switch_preserves_existing_env_and_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(p.os.environ, {}, clear=True), \
             patch.object(p, 'request') as request:
            root = Path(tmp)
            env = root / '.env'
            env.write_text('HATENA_USER=original-reader\n')
            # Migrate the account binding from a successful pre-.env run.
            p.dump(root / '.picker/runs/old/bundle.json', {
                'profile': {'hatena_user': 'original-reader'},
                'source_status': [{'source': 'hatena', 'status': 'ok'}]})
            with self.assertRaisesRegex(ValueError, 'different --workspace'):
                p.setup(SimpleNamespace(workspace=root, hatena_user='other-reader'))
            self.assertEqual(env.read_text(), 'HATENA_USER=original-reader\n')
            with patch.dict(p.os.environ, {'HATENA_USER': 'other-reader'}):
                with self.assertRaisesRegex(ValueError, 'different --workspace'):
                    p.collect(SimpleNamespace(workspace=root))
            request.assert_not_called()
            self.assertFalse((root / '.picker/state.sqlite3').exists())

    def test_workspace_defaults_to_cwd_or_the_bundle_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p.Path, 'cwd', return_value=Path(tmp)):
            self.assertEqual(p.resolve_workspace(SimpleNamespace(workspace=None)), str(Path(tmp).resolve()))
            bundle = Path(tmp) / '.picker/runs/run/bundle.json'
            args = SimpleNamespace(workspace=None, bundle=str(bundle))
            self.assertEqual(p.resolve_workspace(args), str(Path(tmp).resolve()))
            args.workspace = str(Path(tmp) / 'explicit')
            self.assertEqual(p.resolve_workspace(args), str(Path(args.workspace).resolve()))

    def test_rss_retains_original_links_from_description_and_full_content(self):
        data = b'''<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
          <title>A community post</title><link>https://www.metafilter.com/123/story</link>
          <description><![CDATA[Read <a href="https://example.org/original?utm_source=rss">the original</a>]]></description>
          <content:encoded><![CDATA[Also <a href="https://example.net/other">another source</a>
          <a href="javascript:alert(1)">ignore</a>]]></content:encoded>
        </item></channel></rss>'''
        row = p.feed(data, 'https://feeds.feedburner.com/Metafilter')[0]
        self.assertEqual(row['url'], 'https://www.metafilter.com/123/story')
        self.assertEqual(row['snippet'], 'Read the original')
        self.assertEqual(row['links'], [
            {'url': 'https://example.org/original', 'text': 'the original'},
            {'url': 'https://example.net/other', 'text': 'another source'}])
        bundle = {'candidates': {}}
        row['feed_entry'] = {'feed_url': 'https://feeds.feedburner.com/Metafilter',
                             'post_url': row['url'], 'links': row['links']}
        candidate = p.add(bundle, row, 'metafilter')
        p.add(bundle, {'url': row['url'], 'snippet': 'A later observation'}, 'another-source')
        self.assertEqual(candidate['feed_entries'][0]['links'], row['links'])
        self.assertIsNone(candidate['body'])  # Introductory RSS text is not a verified article body.

    def test_atom_xhtml_links_respect_xml_base(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://kottke.org/">
          <entry xml:base="26/11/"><title>Post</title><link href="post"/>
          <content type="xhtml" xml:base="https://example.org/articles/">
            <div xmlns="http://www.w3.org/1999/xhtml">Read <a href="original">the original</a></div>
          </content></entry></feed>'''
        row = p.feed(data, 'https://feeds.kottke.org/main')[0]
        self.assertEqual(row['url'], 'https://kottke.org/26/11/post')
        self.assertEqual(row['links'], [{'url': 'https://example.org/articles/original', 'text': 'the original'}])

    def test_feed_snapshot_preserves_window_and_parse_failure_evidence(self):
        data = b'<rss><channel><item><link>https://example.org/a</link></item><item><link>https://example.org/b</link></item></channel></rss>'
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'request', return_value=(
                data, 'https://example.org/rss', 'utf-8')):
            rows, record = p.fetch_feed('https://example.org/feed', Path(tmp), 1)
            self.assertEqual(record['status'], 'ok')
            self.assertEqual(record['available_entries'], 2)
            self.assertEqual(record['collected_entries'], 1)
            self.assertTrue(record['limited'])
            self.assertEqual(Path(record['raw_path']).read_bytes(), data)
            self.assertEqual(p.load_bundle(record['entries_path']), rows)
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'request', return_value=(
                b'<html>Not a feed</html>', 'https://example.org/rss', 'utf-8')):
            rows, record = p.fetch_feed('https://example.org/feed', Path(tmp), 1)
            self.assertEqual(rows, [])
            self.assertEqual(record['status'], 'failed')
            self.assertTrue(Path(record['raw_path']).exists())
            self.assertNotIn('collected_entries', record)
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'request', side_effect=urllib.error.HTTPError(
                'https://example.org/blocked-feed', 403, 'Forbidden', {}, None)):
            rows, record = p.fetch_feed('https://example.org/feed', Path(tmp), 1)
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['http_status'], 403)
            self.assertEqual(record['failed_url'], 'https://example.org/blocked-feed')
            self.assertNotIn('available_entries', record)

    def test_local_page_fetch_preserves_article_and_discovery_links(self):
        markup = (Path(__file__).parent / 'fixtures/article-en.html').read_bytes()
        markup += b'<a href="/next">Next page</a><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'request', side_effect=[
                (b'User-agent: *\nAllow: /', '', 'utf-8'),
                (markup, 'https://example.org/story', 'utf-8', {'Content-Type': 'text/html'})]):
            result = p.fetch_page('https://example.org/story', Path(tmp))
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(Path(result['raw_path']).read_bytes(), markup)
            self.assertIn('CONCLUSIONSENTINEL', Path(result['text_path']).read_text())
            self.assertIn({'url': 'https://example.org/next', 'text': 'Next page'}, result['links'])
            self.assertTrue(any(r['url'] == 'https://example.org/feed.xml' for r in result['links']))

    def test_local_page_failure_records_exact_url_without_touching_history(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path = root / 'bundle.json'
            p.dump(path, {'candidates': {}})
            url = 'https://www.metafilter.com/'
            with patch.object(p, 'request', side_effect=urllib.error.HTTPError(
                    url + 'robots.txt', 403, 'Forbidden', {}, None)) as request:
                p.fetch(SimpleNamespace(bundle=path, urls=[url]))
            record = p.load_bundle(path)['page_fetches'][0]
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['http_status'], 403)
            self.assertEqual(record['failed_url'], url + 'robots.txt')
            self.assertNotIn('text_path', record)
            self.assertFalse((root / '.picker/state.sqlite3').exists())
            request.assert_called_once_with(url + 'robots.txt')

    def test_local_discovery_stops_on_robots_denial(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'request', return_value=(
                b'User-agent: *\nDisallow: /', '', 'utf-8')) as request:
            result = p.fetch_page('https://example.org/', Path(tmp))
            self.assertEqual(result['status'], 'failed')
            self.assertIn('robots.txt disallows', result['error'])
            request.assert_called_once()

    def test_local_request_log_distinguishes_http_failure_and_success(self):
        headers = Message()
        headers['Content-Type'] = 'text/plain; charset=utf-8'
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers = 200, headers
        response.geturl.return_value = 'https://example.org/resolved'
        response.read.return_value = b'hello'
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'public_url'), \
             patch.object(p.urllib.request, 'build_opener') as opener, \
             patch.object(p, 'REQUEST_LOG', Path(tmp) / 'requests.jsonl'):
            opener.return_value.open.side_effect = [response, urllib.error.HTTPError(
                'https://example.org/blocked', 403, 'Forbidden', {}, None)]
            self.assertEqual(p.request('https://example.org/ok')[0], b'hello')
            with self.assertRaises(urllib.error.HTTPError):
                p.request('https://example.org/blocked')
            records = [json.loads(line) for line in p.REQUEST_LOG.read_text().splitlines()]
            self.assertEqual([r['status'] for r in records], [200, 403])
            self.assertEqual(records[0]['resolved_url'], 'https://example.org/resolved')
            self.assertEqual(records[1]['url'], 'https://example.org/blocked')
            self.assertEqual(records[1]['method'], 'GET')
            self.assertEqual(records[0]['transport'], 'local-python-urllib')
            self.assertEqual(records[0]['user_agent'], p.USER_AGENT)

    def test_digest_archive_does_not_seed_delivery_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "research/digests/old.json"
            p.dump(archive, {"articles": [{"url": "https://example.org/archive-only"}]})
            with p.db(root) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
                con.execute("INSERT INTO deliveries VALUES (?, ?)", ("https://example.org/delivered", "old-run"))
            archive.write_text("invalid JSON")
            with p.db(root) as con:
                self.assertEqual(con.execute("SELECT url, run FROM deliveries").fetchall(),
                                 [("https://example.org/delivered", "old-run")])

    def test_pickups_count_sites_not_posts_and_preserve_unknowns(self):
        row = {"url": "https://example.org/story", "metrics": [
            {"platform": "HN", "url": "https://news.ycombinator.com/item?id=1", "retrieved_at": "2026-10-30T12:00:00Z"},
            {"platform": "HN", "url": "https://news.ycombinator.com/item?id=2", "retrieved_at": "2026-10-30T12:00:00Z"}],
            "coverage": [
                {"site": "HN", "status": "found", "post_url": "https://news.ycombinator.com/item?id=1",
                 "article_url": "https://example.org/story?utm_source=hn", "retrieved_at": "2026-10-30T12:00:00Z"},
                {"site": "Kottke", "status": "found", "post_url": "https://kottke.org/26/10/story",
                 "article_url": "https://example.org/story", "retrieved_at": "2026-10-30T12:05:00Z"},
                {"site": "MetaFilter", "status": "failed", "note": "403", "retrieved_at": "2026-10-30T12:00:00Z"},
                {"site": "Hatena", "status": "not_found", "note": "URL検索の範囲", "retrieved_at": "2026-10-30T12:00:00Z"}]}
        text = p.pickup_summary(row)
        self.assertIn("確認できたのは2サイト", text)
        self.assertEqual(text.count("https://news.ycombinator.com/item?id=1"), 1)
        self.assertIn("MetaFilter：取得失敗", text)
        self.assertIn("はてなブックマーク：確認範囲で未発見", text)
        self.assertIn("Lobsters：未確認", text)
        self.assertIn("2026/10/30 21:05 JST", text)

    def test_pickups_reject_different_article_and_unrelated_evidence_site(self):
        row = {"url": "https://example.org/story", "resolved_url": "https://example.org/story/"}
        check = {"site": "Kottke", "status": "found", "post_url": "https://kottke.org/26/10/story",
                 "article_url": "https://example.org/story/", "retrieved_at": "2026-10-30T12:00:00Z"}
        p.validate_coverage(row, [check])
        for changes in ({"article_url": "https://example.org/other"},
                        {"post_url": "https://example.org/story"},
                        {"retrieved_at": "2026-10-30T12:00:00"},
                        {"status": "failed"}):
            with self.assertRaises(ValueError):
                p.validate_coverage(row, [{**check, **changes}])

    def test_pickup_import_persists_and_replaces_checks_without_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://example.org/story"
            bundle = {"candidates": {}, "profile": {"critical_cvss": 9}, "bookmarks": []}
            p.add(bundle, {"url": url}, "web-search")
            path, extra = root / 'bundle.json', root / 'extra.json'
            p.dump(path, bundle)
            check = {"site": "MetaFilter", "status": "failed", "note": "403", "retrieved_at": "2026-10-30T12:00:00Z"}
            p.dump(extra, [{"url": url, "source": "web-coverage-check", "coverage": [check]}])
            args = SimpleNamespace(workspace=root, bundle=path, file=extra)
            p.import_candidates(args)
            self.assertEqual(p.load_bundle(path)['candidates'][p.ident(url)]['coverage'], [check])
            p.dump(extra, [{"url": url, "source": "web-coverage-check", "coverage": []}])
            p.import_candidates(args)
            row = p.load_bundle(path)['candidates'][p.ident(url)]
            self.assertEqual(p.pickup_summary(row), "複数サイトでの掲載：未確認")
            with p.db(root) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)

    def interest_run(self, root, run, rows, ok=True):
        bundle = {"run": run, "bookmarks": rows, "profile": {"explicit_preferences": ["本人の希望"]},
                  "source_status": [{"source": s, "status": "ok" if ok else "failed"}
                                    for s in ("hatena", "hatena-reading")], "candidates": {}}
        p.prepare_interests(root, bundle)
        path = root / '.picker/runs' / run / 'bundle.json'
        p.dump(path, bundle)
        return path, bundle

    def interest_input(self, path, bundle, deferred=False, hypotheses=True):
        text = 'We observed how the everyday phenomenon changes and tested our explanation.'
        reviews = []
        for delta in bundle['interest_context']['delta']:
            row = bundle['candidates'][delta['id']]
            body = path.parent / (row['id'] + '.txt')
            body.write_text(text)
            row['body'] = {'path': str(body), 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'scope': 'unverified-extraction'}
            reviews.append({'id': row['id'], 'status': 'deferred', 'reason': '本文取得不可'} if deferred else
                           {'id': row['id'], 'status': 'reviewed', 'body_reviewed': True, 'scope': 'partial',
                            'observation': '日常の現象を実験で調べている', 'inference': '身近な実験への関心の仮説', 'evidence': [text]})
        analysis = {'bookmarks': reviews, 'hypotheses': []}
        if reviews and not deferred and hypotheses:
            analysis['hypotheses'] = [{'id': 'everyday-experiments', 'status': 'active', 'confidence': 'tentative',
                                       'text': '身近な現象の実験に関心がありそう', 'rationale': '保存記事の実験過程から推測',
                                       'bookmark_ids': [r['id'] for r in reviews]}]
        p.dump(path, bundle)
        file = path.parent / 'analysis-input.json'
        p.dump(file, analysis)
        return SimpleNamespace(workspace=path.parents[3], bundle=path, analysis=file, commit=True)

    def test_interests_bootstrap_uses_previous_snapshot_and_detects_metadata_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = [{'url': 'https://example.org/old', 'title': 'Old', 'tags': ['昔'], 'published': '2026-10-29'},
                   {'url': 'https://example.org/edit', 'title': 'Edit', 'tags': [], 'published': '2026-10-29'}]
            p.dump(root / '.picker/runs/20261029/bundle.json',
                   {'run': '20261029', 'bookmarks': old, 'source_status': [{'source': 'hatena', 'status': 'ok'}]})
            rows = [old[0], {**old[0], 'url': old[0]['url'] + '?utm_source=feed'},
                    {**old[1], 'tags': ['新しい関心']}, {'url': 'https://example.org/new', 'title': 'New'}]
            _, bundle = self.interest_run(root, '20261030', rows)
            delta = {d['url']: d['change'] for d in bundle['interest_context']['delta']}
            self.assertEqual(delta, {'https://example.org/edit': 'changed', 'https://example.org/new': 'new'})
            self.assertEqual(bundle['interest_context']['previous_run'], '20261029')
            self.assertEqual(p.interest_state(root)['revision'], 0)

    def test_interest_preview_commit_and_next_run_preserve_provenance_without_delivery(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            row = {'url': 'https://example.org/new', 'title': 'New', 'snippet': '自分のコメント', 'tags': ['実験'], 'published': '2026-10-30'}
            path, bundle = self.interest_run(root, '20261030a', [row])
            args = self.interest_input(path, bundle)
            args.commit = False
            p.update_interests(args)
            self.assertEqual(p.interest_state(root)['revision'], 0)
            args.commit = True
            p.update_interests(args)
            p.update_interests(args)  # Idempotent retry.
            state = p.interest_state(root)
            self.assertEqual(state['revision'], 1)
            hypothesis = state['hypotheses']['everyday-experiments']
            self.assertEqual(hypothesis['origin'], 'bookmark-inference')
            self.assertEqual(hypothesis['evidence'][0]['url'], row['url'])
            self.assertEqual(p.load_bundle(path)['profile']['explicit_preferences'], ['本人の希望'])
            _, next_bundle = self.interest_run(root, '20261030b', [{'url': row['url'], 'title': 'New', 'tags': ['実験']}])
            self.assertEqual(next_bundle['interest_context']['delta'], [])
            self.assertEqual(next_bundle['profile']['inferred_interests'], [hypothesis])
            with p.db(root) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 0)

    def test_deferred_bookmark_retries_even_after_leaving_feed(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path, bundle = self.interest_run(root, '20261030a', [{'url': 'https://example.org/unread', 'title': 'Unread'}])
            p.update_interests(self.interest_input(path, bundle, deferred=True))
            path2, next_bundle = self.interest_run(root, '20261030b', [{'url': 'https://example.org/new', 'title': 'New'}])
            delta = next_bundle['interest_context']['delta']
            self.assertEqual(len(delta), 2)
            self.assertTrue(next(d for d in delta if d['url'].endswith('/unread'))['retry'])
            p.update_interests(self.interest_input(path2, next_bundle))
            self.assertTrue(all(b['status'] == 'reviewed' for b in p.interest_state(root)['bookmarks'].values()))

    def test_interest_evidence_and_incomplete_analysis_cannot_advance_state(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path, bundle = self.interest_run(root, '20261030', [{'url': 'https://example.org/new'}])
            args = self.interest_input(path, bundle)
            analysis = p.load_bundle(args.analysis)
            p.dump(args.analysis, {'bookmarks': [], 'hypotheses': []})
            with self.assertRaisesRegex(ValueError, 'exactly once'):
                p.update_interests(args)
            analysis['bookmarks'][0]['evidence'] = ['Invented evidence that does not occur in the body']
            p.dump(args.analysis, analysis)
            with self.assertRaisesRegex(ValueError, 'absent'):
                p.update_interests(args)
            self.assertEqual(p.interest_state(root)['revision'], 0)

    def test_failed_hatena_collection_keeps_checkpoint_and_hypotheses(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path, bundle = self.interest_run(root, '20261030a', [{'url': 'https://example.org/old'}])
            p.update_interests(self.interest_input(path, bundle))
            before = p.interest_state(root)
            path2, failed = self.interest_run(root, '20261030b', [{'url': 'https://example.org/new'}], ok=False)
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                p.update_interests(self.interest_input(path2, failed))
            self.assertEqual(p.interest_state(root), before)

    def test_stale_interest_update_and_changed_retry_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path, bundle = self.interest_run(root, '20261030a', [{'url': 'https://example.org/first'}])
            path2, other = self.interest_run(root, '20261030b', [{'url': 'https://example.org/second'}])
            first_args = self.interest_input(path, bundle)
            p.update_interests(first_args)
            with self.assertRaisesRegex(ValueError, 'advanced'):
                p.update_interests(self.interest_input(path2, other))
            data = p.load_bundle(first_args.analysis)
            data['hypotheses'][0]['text'] = '異なる分析'
            p.dump(first_args.analysis, data)
            with self.assertRaisesRegex(ValueError, 'different committed'):
                p.update_interests(first_args)

    def test_new_delta_can_revise_or_retire_prior_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path, bundle = self.interest_run(root, '20261030a', [{'url': 'https://example.org/first'}])
            p.update_interests(self.interest_input(path, bundle))
            path2, next_bundle = self.interest_run(root, '20261030b', [{'url': 'https://example.org/second'}])
            args = self.interest_input(path2, next_bundle)
            data = p.load_bundle(args.analysis)
            data['hypotheses'][0].update(status='retired', rationale='新しい記事本文を読むと以前の解釈は狭すぎた')
            p.dump(args.analysis, data)
            p.update_interests(args)
            state = p.interest_state(root)
            self.assertEqual(len(state['hypotheses']['everyday-experiments']['evidence']), 2)
            self.assertEqual(p.load_bundle(path2)['profile']['inferred_interests'], [])

    def test_finalization_requires_interest_analysis_for_successful_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, sel, _, _ = self.setup_run(root)
            bundle = p.load_bundle(path)
            bundle['interest_context'] = {'collection_ok': True}
            p.dump(path, bundle)
            with self.assertRaisesRegex(ValueError, 'interest analysis'):
                p.finalize(SimpleNamespace(workspace=root, bundle=path, selection=sel, commit=False))

    def test_uncommitted_collection_does_not_hide_unanalyzed_bookmarks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [{'url': 'https://example.org/pending'}]
            self.interest_run(root, '20261030a', rows)
            _, again = self.interest_run(root, '20261030b', rows)
            self.assertEqual(len(again['interest_context']['delta']), 1)
            self.assertIsNone(again['interest_context']['previous_run'])

    def test_single_article_cannot_be_promoted_to_supported_interest(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            path, bundle = self.interest_run(root, '20261030', [{'url': 'https://example.org/one'}])
            args = self.interest_input(path, bundle)
            analysis = p.load_bundle(args.analysis)
            analysis['hypotheses'][0]['confidence'] = 'supported'
            p.dump(args.analysis, analysis)
            with self.assertRaisesRegex(ValueError, 'two distinct'):
                p.update_interests(args)

    def test_collect_loads_interests_marks_saved_urls_and_detects_only_new_bookmarks(self):
        def rss(url):
            return f'<rss><channel><item><title>Saved</title><link>{url}</link><pubDate>2026-10-30</pubDate></item></channel></rss>'.encode()
        reading = b'<li data-target-url="https://example.org/reading"><h3 class="centerarticle-entry-title">Reading</h3></li>'
        current = ['https://example.org/first']
        def request(url):
            if url == 'https://feeds.feedburner.com/Metafilter':
                return (b'<rss><channel><item><title>Post</title><link>https://www.metafilter.com/123/story</link>'
                        b'<description>&lt;a href="https://example.org/original"&gt;Original&lt;/a&gt;</description>'
                        b'</item><item><link>https://www.metafilter.com/456/other</link></item></channel></rss>', url, 'utf-8')
            if url.endswith('/bookmark.rss'):
                self.assertEqual(url, 'https://b.hatena.ne.jp/sample-reader/bookmark.rss')
                return rss(current[0]), url, 'utf-8'
            if 'b.hatena.ne.jp' in url:
                return reading, url, 'utf-8'
            return b'<rss><channel/></rss>', url, 'utf-8'
        def get_json(url):
            if url.endswith('topstories.json'):
                return [1]
            if url.endswith('/item/1.json'):
                return {'type': 'story', 'url': 'https://example.org/first', 'title': 'First', 'time': 1, 'score': 1}
            if 'known_exploited' in url:
                return {'catalogVersion': 'test', 'vulnerabilities': []}
            return []
        def page(url, folder):
            return {'status': 'failed', 'retrieved_at': p.now(), 'url': url, 'http_status': 403,
                    'failed_url': url, 'error': 'HTTPError: HTTP Error 403: Forbidden'} if 'metafilter.com' in url else {
                        'status': 'ok', 'retrieved_at': p.now(), 'url': url, 'links': []}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
             patch.dict(p.os.environ, {'HATENA_USER': 'sample-reader'}), \
             patch.object(p, 'request', side_effect=request), patch.object(p, 'get_json', side_effect=get_json), \
             patch.object(p, 'fetch_page', side_effect=page) as fetch_page, \
             patch.object(p, 'advisory_rows', return_value=[]):
            root = Path(tmp)
            args = SimpleNamespace(workspace=root, hn_limit=1, feed_limit=1, days=1, advisory_pages=1)
            p.collect(args)
            path = sorted((root / '.picker/runs').glob('*/bundle.json'))[-1]
            first = p.load_bundle(path)
            self.assertEqual(first['profile']['hatena_user'], 'sample-reader')
            self.assertEqual(p.workspace_account(root), 'sample-reader')
            self.assertTrue(first['page_fetches'])
            self.assertTrue((path.parent / 'page-fetches.json').exists())
            self.assertTrue((path.parent / 'feed-fetches.json').exists())
            statuses = {r['source']: r for r in first['source_status']}
            self.assertEqual(statuses['metafilter']['status'], 'ok')
            self.assertEqual(statuses['metafilter']['collected_entries'], 1)
            self.assertTrue(statuses['metafilter']['limited'])
            self.assertNotIn('metafilter-page', statuses)
            self.assertFalse(any(call.args[0] == 'https://www.metafilter.com/'
                                 for call in fetch_page.call_args_list))
            post = first['candidates'][p.ident('https://www.metafilter.com/123/story')]
            self.assertEqual(post['feed_entries'][0]['links'][0]['url'], 'https://example.org/original')
            self.assertEqual(post['feed_entries'][0]['post_url'], post['url'])
            self.assertEqual(len(first['interest_context']['delta']), 2)
            p.update_interests(self.interest_input(path, first))
            current[0] = 'https://example.org/second'
            p.collect(args)
            path = sorted((root / '.picker/runs').glob('*/bundle.json'))[-1]
            second = p.load_bundle(path)
            self.assertEqual([d['url'] for d in second['interest_context']['delta']], [current[0]])
            self.assertTrue(second['candidates'][p.ident('https://example.org/first')]['bookmarked'])
            self.assertTrue(second['profile']['inferred_interests'])
            p.update_interests(self.interest_input(path, second))
            selection = path.parent / 'selection.json'
            p.dump(selection, {'recommendations': [], 'security': []})
            p.finalize(SimpleNamespace(workspace=root, bundle=path, selection=selection, commit=False))
            output = (path.parent / 'digest.md').read_text()
            self.assertIn('今回のブックマークから見えた関心（仮説）', output)
            self.assertIn('https://example.org/second', output)

    def test_article_extraction_preserves_evidence_and_excludes_boilerplate(self):
        fixture = Path(__file__).parent / "fixtures/article-en.html"
        text, extractor = p.article_text(fixture.read_text(), "https://example.org/story")
        for expected in ["CONCLUSIONSENTINEL", "What failed", "return stories[tag]", "250 ms", "80 ms"]:
            self.assertIn(expected, text)
        for noise in ["NAVIGATIONSENTINEL", "COMMENTSENTINEL", "FOOTERSENTINEL"]:
            self.assertNotIn(noise, text)
        self.assertEqual(extractor['name'], 'trafilatura')

    def test_article_extraction_preserves_japanese_ending(self):
        markup = (Path(__file__).parent / "fixtures/article-ja.html").read_text()
        text, _ = p.article_text(markup, "https://example.org/story")
        self.assertIn("結末の目印", text)
        self.assertIn("一緒に面白がってくれる", text)
        self.assertNotIn("FOOTERSENTINEL", text)

    def test_empty_article_and_missing_dependency_are_explicit_errors(self):
        with self.assertRaisesRegex(ValueError, 'Too little article text'):
            p.article_text('<html><body></body></html>', 'https://example.org/')
        with patch.dict(sys.modules, {'trafilatura': None}):
            with self.assertRaisesRegex(RuntimeError, 'pip install -r'):
                p.article_text('<p>Text</p>', 'https://example.org/')

    def test_body_uses_existing_fetch_and_saves_extractor_provenance(self):
        markup = (Path(__file__).parent / "fixtures/article-en.html").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _, row, _ = self.setup_run(root)
            args = SimpleNamespace(workspace=root, bundle=path, ids=[row['id']], text_file=None)
            responses = [(b'User-agent: *\nAllow: /', '', 'utf-8'),
                         (markup, row['url'], 'utf-8')]
            with patch.object(p, 'request', side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
                p.body(args)
            record = p.load_bundle(path)['candidates'][row['id']]['body']
            self.assertEqual(record['extractor']['name'], 'trafilatura')
            self.assertTrue(record['extractor']['version'])
            self.assertFalse(record['truncated'])
            text = Path(record['path']).read_text()
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), record['sha256'])
            self.assertNotIn('COMMENTSENTINEL', text)
            with p.db(root) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)

    def test_robots_denial_prevents_article_fetch_and_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _, row, _ = self.setup_run(root)
            args = SimpleNamespace(workspace=root, bundle=path, ids=[row['id']], text_file=None)
            with patch.object(p, 'request', return_value=(b'User-agent: *\nDisallow: /', '', 'utf-8')) as request, \
                 patch.object(p, 'article_text') as extract, contextlib.redirect_stdout(io.StringIO()):
                p.body(args)
            request.assert_called_once()
            extract.assert_not_called()
            self.assertIn('robots.txt disallows', p.load_bundle(path)['candidates'][row['id']]['body_error'])

    def test_failed_extraction_keeps_old_snapshot_and_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _, row, _ = self.setup_run(root)
            args = SimpleNamespace(workspace=root, bundle=path, ids=[row['id']], text_file=None)
            responses = [(b'User-agent: *\nAllow: /', '', 'utf-8'),
                         (b'<html><body></body></html>', row['url'], 'utf-8')]
            with patch.object(p, 'request', side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
                p.body(args)
            saved = p.load_bundle(path)['candidates'][row['id']]
            self.assertIn('Too little article text', saved['body_error'])
            self.assertEqual(saved['body'], row['body'])

    def test_verified_text_import_does_not_require_extractor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _, row, _ = self.setup_run(root)
            source = root / 'verified.txt'
            text = 'A verified paragraph from the original article, including the ending. ' * 3
            source.write_text(text)
            args = SimpleNamespace(workspace=root, bundle=path, ids=[row['id']], text_file=source,
                                   evidence_url=row['url'], scope='partial')
            with patch.dict(sys.modules, {'trafilatura': None}), patch.object(p, 'public_url'), \
                 patch.object(p, 'request') as request, contextlib.redirect_stdout(io.StringIO()):
                p.body(args)
            request.assert_not_called()
            record = p.load_bundle(path)['candidates'][row['id']]['body']
            self.assertEqual(record['extractor']['name'], 'text-file')
            self.assertEqual(record['scope'], 'partial')
            self.assertEqual(Path(record['path']).read_text(), text)

    def test_discussion_preserves_replies_and_skips_deleted_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _, row, _ = self.setup_run(Path(tmp))
            bundle = p.load_bundle(path)
            bundle['candidates'][row['id']]['metrics'] = [{'platform':'HN', 'url':'https://news.ycombinator.com/item?id=1'}]
            p.dump(path, bundle)
            items = {1:{'type':'story','url':row['url'],'kids':[2,3],'descendants':3},
                     2:{'id':2,'parent':1,'text':'A useful observation','kids':[4]},
                     3:{'id':3,'parent':1,'deleted':True,'text':'Deleted content'},
                     4:{'id':4,'parent':2,'text':'A reply with a correction'}}
            with patch.object(p, 'get_json', side_effect=lambda url:items[int(url.split('/')[-1].split('.')[0])]), contextlib.redirect_stdout(io.StringIO()):
                p.discussion(SimpleNamespace(bundle=path, ids=[row['id']], limit=3))
            cached = p.load_bundle(path)['candidates'][row['id']]
            snap = json.loads(Path(cached['discussions'][0]['path']).read_text())
            self.assertEqual([c['id'] for c in snap['comments']], [2,4])
            self.assertEqual(snap['comments'][1]['parent'], 2)
            self.assertEqual(snap['status'], 'complete')
            self.assertIn('コメント 4', '\n'.join(p.discussion_notes(cached, {'discussion':[{'summary':'補足','comment_ids':[4]}]})))
            with self.assertRaises(ValueError):
                p.discussion_notes(cached, {'discussion':[{'summary':'捏造','comment_ids':[999]}]})
            Path(cached['discussions'][0]['path']).write_text('{}')
            with self.assertRaises(ValueError):
                p.discussion_notes(cached, {})

    def test_discussion_cap_and_failure_are_not_no_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _, row, _ = self.setup_run(Path(tmp))
            bundle = p.load_bundle(path)
            bundle['candidates'][row['id']]['metrics'] = [{'platform':'HN', 'url':'https://news.ycombinator.com/item?id=1'}]
            p.dump(path, bundle)
            items = {1:{'type':'story','url':row['url'],'kids':[2,3]},2:{'id':2,'parent':1,'text':'First comment'}}
            args = SimpleNamespace(bundle=path, ids=[row['id']], limit=1)
            with patch.object(p, 'get_json', side_effect=lambda url:items[int(url.split('/')[-1].split('.')[0])]), contextlib.redirect_stdout(io.StringIO()):
                p.discussion(args)
            cached = p.load_bundle(path)['candidates'][row['id']]
            self.assertEqual(json.loads(Path(cached['discussions'][0]['path']).read_text())['status'], 'partial')
            with patch.object(p, 'get_json', side_effect=OSError('offline')), contextlib.redirect_stdout(io.StringIO()):
                p.discussion(args)
            cached = p.load_bundle(path)['candidates'][row['id']]
            self.assertIn('failed', '\n'.join(p.discussion_notes(cached, {})))

    def metric(self, platform="HN", score=120, hours=3):
        observed = p.datetime(2026, 10, 25, 12, tzinfo=p.timezone.utc)
        return {"platform": platform, "score": score, "comments": 35,
                "submitted_at": (observed - p.timedelta(hours=hours)).timestamp(),
                "retrieved_at": observed.isoformat(), "url": "https://example.org/discussion"}

    def test_age_changes_label_without_changing_points(self):
        self.assertEqual(p.attention_signal(self.metric())["label"], "今かなり話題")
        self.assertEqual(p.attention_signal(self.metric(hours=6))["label"], "今かなり話題")
        self.assertNotIn("今", p.attention_signal(self.metric(hours=6.01))["label"])
        self.assertIn("累積", p.attention_signal(self.metric(score=1000, hours=24.01))["label"])
        self.assertEqual(p.attention_signal(self.metric(score=500, hours=24))["label"], "今大きな話題")

    def test_platform_scales_and_iso_timezones_are_independent(self):
        lobsters = self.metric(platform="Lobsters", score=30)
        lobsters["submitted_at"] = "2026-10-25T04:00:00-05:00"
        self.assertEqual(p.attention_signal(lobsters)["hours"], 3)
        self.assertEqual(p.attention_signal(lobsters)["label"], "今かなり話題")
        self.assertNotIn("今", p.attention_signal(self.metric(score=30))["label"])
        text = p.attention({"metrics": [self.metric(score=30), lobsters]})
        self.assertIn("HN 30pt", text)
        self.assertIn("Lobsters 30pt", text)
        self.assertIn("投稿後3.0時間", text)

    def test_missing_or_invalid_metrics_do_not_create_buzz(self):
        for patch_data in [{"submitted_at": None}, {"submitted_at": "2026-10-25T09:00:00"},
                           {"submitted_at": "invalid"}, {"score": None},
                           {"retrieved_at": None}, {"platform": "Other"}]:
            metric = self.metric(score=1000)
            metric.update(patch_data)
            self.assertIn("未確認", p.attention_signal(metric)["label"])
            self.assertIn("未確認", p.attention({"metrics": [metric]}))
        self.assertIsNone(p.attention_signal(self.metric(hours=-1))["hours"])

    def test_age_uses_observation_not_render_or_article_publish_time(self):
        metric = self.metric()
        with patch.object(p, "now", return_value="2030-01-01T00:00:00+00:00"):
            rendered = p.attention({"published": "2000-01-01", "metrics": [metric]})
        self.assertIn("投稿後3.0時間", rendered)
        self.assertIn("2026/10/25 21:00 JST", rendered)
        self.assertNotIn("2030", rendered)

    def test_normalize_drops_tracking_but_preserves_article_identity(self):
        self.assertEqual(p.normalize("https://example.org/x?id=3&utm_source=hn#part"), "https://example.org/x?id=3")
        self.assertNotEqual(p.ident("https://example.org/x?id=3"), p.ident("https://example.org/x?id=4"))
        for url in ["file:///etc/passwd", "https://user:pass@example.org"]:
            with self.assertRaises(ValueError):
                p.normalize(url)

    def test_rdf_bookmark_tags_and_date(self):
        data = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>Title</title><link>https://example.org/</link><dc:date>2026-10-25</dc:date><dc:subject>reading</dc:subject><description>Comment</description></item></rdf:RDF>'''
        row = p.feed(data)[0]
        self.assertEqual(row["tags"], ["reading"])
        self.assertEqual(row["published"], "2026-10-25")
        self.assertEqual(row["snippet"], "Comment")

    def test_atom_alternate_link(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link rel="self" href="https://example.org/api"/><link href="https://example.org/story"/><updated>2026-10-25</updated></entry></feed>'''
        self.assertEqual(p.feed(data)[0]["url"], "https://example.org/story")

    def test_threshold_uses_any_assessment_not_products_or_popularity(self):
        raw = {"html_url": "https://example.org/advisory", "cvss": {"score": 8.8, "vector_string": "CVSS:3.1/AV:N"},
               "cvss_severities": {"cvss_v4": {"score": 9.2, "vector_string": "CVSS:4.0/AV:N"}}}
        self.assertEqual(p.security_flags({"cvss": p.scores(raw)}, 9), ["cvss>=9"])
        self.assertEqual(p.security_flags({"kev": True, "cvss": []}, 9), ["known-exploited"])
        self.assertEqual(p.security_flags({"cvss": [{"score": 8.9}]}, 9), [])

    def test_same_article_keeps_separate_platform_metrics(self):
        bundle = {"candidates": {}}
        p.add(bundle, {"url": "https://example.org/x?utm_source=hn"}, "hn", metrics={"platform": "HN", "score": 10})
        p.add(bundle, {"url": "https://example.org/x"}, "lobsters", metrics={"platform": "Lobsters", "score": 5})
        self.assertEqual(len(bundle["candidates"]), 1)
        self.assertEqual(len(next(iter(bundle["candidates"].values()))["metrics"]), 2)

    def setup_run(self, root):
        folder = root / ".picker/runs/test"
        folder.mkdir(parents=True)
        text = "We built a small experiment and measured what actually happened."
        body = folder / "body.txt"
        body.write_text(text)
        bundle = {"run": "test", "source_status": [], "candidates": {}}
        row = p.add(bundle, {"url": "https://example.org/story", "title": "Original title"}, "test")
        row.update({"body": {"path": str(body), "sha256": hashlib.sha256(text.encode()).hexdigest(), "scope": "partial"},
                    "already_recommended": False, "bookmarked": False})
        alert = p.add(bundle, {"url": "https://example.org/advisory", "title": "Unrelated product critical issue", "cvss": [{"score": 9.0, "version": "3.1", "source": "https://example.org/advisory"}]}, "vendor", "security")
        alert.update({"mandatory_reasons": ["cvss>=9"], "security_changed": True, "security_fingerprint": "v1"})
        path = folder / "bundle.json"
        p.dump(path, bundle)
        selection = {"recommendations": [{"id": row["id"], "category": "experiment", "body_reviewed": True,
                                         "summary": "実験の紹介", "reason": "作って検証する好みに近い", "evidence": [text]}], "security": []}
        sel = folder / "selection.json"
        p.dump(sel, selection)
        return path, sel, row, alert

    def test_render_preserves_mandatory_alert_and_dry_run_does_not_deliver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, sel, row, alert = self.setup_run(root)
            args = SimpleNamespace(workspace=root, bundle=path, selection=sel, commit=False)
            with contextlib.redirect_stdout(io.StringIO()):
                p.finalize(args)
            self.assertIn("Unrelated product critical issue", (path.parent / "digest.md").read_text())
            with p.db(root) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
            args.commit = True
            with contextlib.redirect_stdout(io.StringIO()):
                p.finalize(args)
            with p.db(root) as con:
                self.assertEqual(con.execute("SELECT url FROM deliveries").fetchone()[0], row["url"])
                self.assertEqual(con.execute("SELECT fingerprint FROM alerts").fetchone()[0], "v1")
            archive = root / "research/digests/test-manual.json"
            self.assertEqual(json.loads(archive.read_text())["articles"][0]["url"], row["url"])
            self.assertEqual(archive.with_suffix(".md").read_text(), (path.parent / "digest.md").read_text())

    def test_invented_evidence_rejected_before_output_or_history_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, sel, row, alert = self.setup_run(root)
            selection = json.loads(sel.read_text())
            selection["recommendations"][0]["evidence"] = ["An invented result absent from the article"]
            p.dump(sel, selection)
            with self.assertRaises(ValueError):
                p.finalize(SimpleNamespace(workspace=root, bundle=path, selection=sel, commit=True))
            self.assertFalse((path.parent / "digest.md").exists())
            with p.db(root) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_stale_bundle_cannot_repeat_another_runs_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, sel, row, alert = self.setup_run(root)
            with p.db(root) as con:
                con.execute("INSERT INTO deliveries VALUES (?, ?)", (row["url"], "other-run"))
            with self.assertRaises(ValueError):
                p.finalize(SimpleNamespace(workspace=root, bundle=path, selection=sel, commit=True))

    def test_modified_body_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, sel, row, alert = self.setup_run(root)
            Path(row["body"]["path"]).write_text("Changed after collection")
            with self.assertRaises(ValueError):
                p.checked_text(row, json.loads(sel.read_text())["recommendations"][0])

    def test_advisories_follow_link_cursor_not_numeric_page(self):
        next_url = "https://api.github.com/advisories?after=cursor42"
        responses = [(b'[{"ghsa_id":"first"}]', "", "utf-8", {"Link": f'<{next_url}>; rel="next"'}),
                     (b'[{"ghsa_id":"second"}]', "", "utf-8", {})]
        with patch.object(p, "request", side_effect=responses) as request:
            self.assertEqual([r['ghsa_id'] for r in p.advisory_rows("2026-10-18", 3)], ["first", "second"])
            self.assertEqual(request.call_args_list[1].args[0], next_url)

    def test_advisory_cap_is_not_silent_success(self):
        response = (b'[]', "", "utf-8", {"Link": '<https://api.github.com/advisories?after=next>; rel="next"'})
        with patch.object(p, "request", return_value=response):
            with self.assertRaisesRegex(ValueError, "cap reached"):
                list(p.advisory_rows("2026-10-18", 1))


if __name__ == "__main__":
    unittest.main()
