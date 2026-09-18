from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from lib import env, getxapi, http, pipeline


def tweet(id='123', handle='alice'):
    return {'id': id, 'text': 'AI agents are useful', 'author': {'userName': handle},
            'createdAt': 'Fri Sep 18 10:00:00 +0000 2026', 'likeCount': 7}


def test_paging_dedupe_and_normalization():
    pages = [{'tweets': [tweet()], 'has_more': True, 'next_cursor': 'next'},
             {'tweets': [tweet(), tweet('456')], 'has_more': False}]
    with patch.object(http, 'get', side_effect=pages) as call:
        result = getxapi.search_x('AI agents', '2026-08-19', '2026-09-19', depth='quick', token='dummy')
    assert len(result['items']) == 2
    assert result['items'][0]['author_handle'] == 'alice'
    assert result['items'][0]['engagement']['likes'] == 7
    assert result['items'][0]['date'] == '2026-09-18'
    assert call.call_args_list[0].kwargs['headers'] == {'Authorization': 'Bearer dummy'}
    params = parse_qs(urlparse(call.call_args_list[1].args[0]).query)
    assert params['cursor'] == ['next']
    assert 'since:2026-08-19 until:2026-09-20' in params['q'][0]


def test_partial_results_survive_rate_limit():
    with patch.object(http, 'get', side_effect=[{'tweets': [tweet()], 'has_more': True, 'next_cursor': 'next'}, http.HTTPError('secret response', status_code=429)]):
        result = getxapi.search_x('agents', '2026-08-19', '2026-09-19', depth='quick', token='dummy')
    assert len(result['items']) == 1
    assert '429' in result['error']
    assert 'secret' not in str(result)


def test_repeated_cursor_stops():
    page = {'tweets': [tweet()], 'has_more': True, 'next_cursor': 'same'}
    with patch.object(http, 'get', return_value=page) as call:
        result = getxapi.search_x('agents', '2026-08-19', '2026-09-19', depth='quick', token='dummy')
    assert call.call_count == 2
    assert result['error']


def test_empty_and_missing_key():
    assert getxapi.search_x('a', '2026-08-19', '2026-09-19')['error']
    with patch.object(http, 'get', return_value={'tweets': [], 'has_more': False}):
        assert getxapi.search_x('a', '2026-08-19', '2026-09-19', token='dummy') == {'items': []}


def test_person_lanes():
    with patch.object(http, 'get', return_value={'tweets': [tweet(), tweet('456', 'bob')], 'has_more': False}) as call:
        items = getxapi.search_mentions(['alice'], '2026-08-19', '2026-09-19', token='dummy')
        assert [i['author_handle'] for i in items] == ['bob']
        assert '@alice' in parse_qs(urlparse(call.call_args.args[0]).query)['q'][0]
        getxapi.search_handles(['alice'], 'unrelated topic', '2026-08-19', '2026-09-19', token='dummy')
        assert parse_qs(urlparse(call.call_args.args[0]).query)['q'][0].startswith('from:alice since:')


def test_config_and_pipeline(monkeypatch):
    config = {'GETXAPI_KEY': 'dummy', 'LAST30DAYS_X_BACKEND': 'getxapi'}
    assert env.x_backend_chain(config) == ['getxapi']
    assert env.x_backend_chain({'LAST30DAYS_X_BACKEND': 'getxapi'}) == []
    assert 'getxapi' in env.x_backend_chain({'GETXAPI_KEY': 'dummy'}, local_only=True)
    with patch.object(getxapi, 'search_x', return_value={'items': [tweet()]}) as search:
        items, error = pipeline._fetch_x_backend('getxapi', 'agents', '2026-08-19', '2026-09-19', 'quick', config)
        assert items and not error
        assert search.call_args.kwargs['token'] == 'dummy'


def test_auth_and_bad_schema():
    for status in (401, 402, 403, 500):
        with patch.object(http, 'get', side_effect=http.HTTPError('do not expose', status_code=status)) as call:
            result = getxapi.search_x('agents', '2026-08-19', '2026-09-19', token='dummy')
        assert result['items'] == [] and str(status) in result['error']
        assert call.call_count == 1
    with patch.object(http, 'get', return_value={'error': 'private detail'}):
        assert 'schema' in getxapi.search_x('a', '2026-08-19', '2026-09-19', token='dummy')['error']


def test_window_and_invalid_identity():
    stale = dict(tweet('789'), createdAt='2025-01-01T00:00:00Z')
    with patch.object(http, 'get', return_value={'tweets': [stale, tweet('bad/id'), tweet('456', 'bad/handle'), tweet()], 'has_more': False}):
        result = getxapi.search_x('agents', '2026-08-19', '2026-09-19', token='dummy')
    assert [i['post_id'] for i in result['items']] == ['123']


def test_page_budget():
    pages = [{'tweets': [], 'has_more': True, 'next_cursor': str(i)} for i in range(5)]
    with patch.object(http, 'get', side_effect=pages) as call:
        result = getxapi.search_x('agents', '2026-08-19', '2026-09-19', token='dummy')
    assert call.call_count == 5 and 'page limit' in result['error']


def test_key_is_loaded(monkeypatch):
    monkeypatch.setenv('GETXAPI_KEY', 'dummy-getxapi')
    assert env.get_config()['GETXAPI_KEY'] == 'dummy-getxapi'


def test_engine_end_date_includes_today():
    with patch.object(http, 'get', return_value={'tweets': [tweet()], 'has_more': False}) as call:
        result = getxapi.search_x('agents', '2026-08-19', '2026-09-18', token='dummy')
    assert len(result['items']) == 1
    assert 'until:2026-09-19' in parse_qs(urlparse(call.call_args.args[0]).query)['q'][0]
