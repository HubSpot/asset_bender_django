from nose.tools import eq_, ok_
from nose.tools import assert_raises

from asset_bender import http
from asset_bender.http import fetch_ab_url_with_retries, FauxException

from requests import Response

def build_fetch_tester(times_to_fail=0, expected_timeouts=[1]):
    num_attempts = [0]

    def wrapper(url, timeout=None):
        attempt = num_attempts[0]

        eq_(timeout, expected_timeouts[attempt], msg="Unexpected timeout")

        if attempt < times_to_fail:
            num_attempts[0] += 1
            raise FauxException("Random exception at %i" % (attempt + 1))
        else:
            result = Response()
            result._content = "Success at %i" % (attempt + 1)
            result.status_code = 200

        num_attempts[0] += 1
        return result

    return wrapper

def setup():
    global download_url_orig
    download_url_orig = http._download_url

def teardown():
    http._download_url = download_url_orig

def test_no_failures():
    http._download_url = build_fetch_tester(times_to_fail=0, expected_timeouts=[1,2,5])
    result = fetch_ab_url_with_retries('faux_url', timeouts=[1])
    eq_(result.text, "Success at 1")

    http._download_url = build_fetch_tester(times_to_fail=0, expected_timeouts=[1,2,5])
    result = fetch_ab_url_with_retries('faux_url', timeouts=[1,2])
    eq_(result.text, "Success at 1")

    http._download_url = build_fetch_tester(times_to_fail=0, expected_timeouts=[1,2,5])
    result = fetch_ab_url_with_retries('faux_url', timeouts=[1,2,5])
    eq_(result.text, "Success at 1")

def test_one_failure():
    http._download_url = build_fetch_tester(times_to_fail=1, expected_timeouts=[1,2,3])

    try:
        result = fetch_ab_url_with_retries('faux_url', timeouts=[1])
        eq_("Error wasn't thrown", False)
    except Exception as e:
        eq_(e.message, "Random exception at 1")

    http._download_url = build_fetch_tester(times_to_fail=1, expected_timeouts=[1,2,3])
    result = fetch_ab_url_with_retries('faux_url', timeouts=[1, 2])
    eq_(result.text, "Success at 2")

    http._download_url = build_fetch_tester(times_to_fail=1, expected_timeouts=[1,2,3])
    result = fetch_ab_url_with_retries('faux_url', timeouts=[1, 2, 3])
    eq_(result.text, "Success at 2")

def test_two_failures():
    http._download_url = build_fetch_tester(times_to_fail=2, expected_timeouts=[1,2,3])

    try:
        result = fetch_ab_url_with_retries('faux_url', timeouts=[1])
        eq_("Error wasn't thrown", False)
    except Exception as e:
        eq_(e.message, "Random exception at 1")

    http._download_url = build_fetch_tester(times_to_fail=2, expected_timeouts=[1,2,3])

    try:
        result = fetch_ab_url_with_retries('faux_url', timeouts=[1, 2])
        eq_("Error wasn't thrown", False)
    except Exception as e:
        eq_(e.message, "Random exception at 2")

    http._download_url = build_fetch_tester(times_to_fail=2, expected_timeouts=[1,2,3])
    result = fetch_ab_url_with_retries('faux_url', timeouts=[1, 2, 3])
    eq_(result.text, "Success at 3")

def test_three_failures():
    http._download_url = build_fetch_tester(times_to_fail=3, expected_timeouts=[1,2,3])
    try:
        result = fetch_ab_url_with_retries('faux_url', timeouts=[1])
        eq_("Error wasn't thrown", False)
    except Exception as e:
        eq_(e.message, "Random exception at 1")

    http._download_url = build_fetch_tester(times_to_fail=3, expected_timeouts=[1,2,3])
    try:
        result = fetch_ab_url_with_retries('faux_url', timeouts=[1, 2])
        eq_("Error wasn't thrown", False)
    except Exception as e:
        eq_(e.message, "Random exception at 2")

    http._download_url = build_fetch_tester(times_to_fail=3, expected_timeouts=[1,2,3])
    try:
        result = fetch_ab_url_with_retries('faux_url', timeouts=[1, 2, 3])
        eq_("Error wasn't thrown", False)
    except Exception as e:
        eq_(e.message, "Random exception at 3")
