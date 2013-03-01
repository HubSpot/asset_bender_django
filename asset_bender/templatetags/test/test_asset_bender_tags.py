
from nose.tools import eq_, ok_

from django.template import Context, Template

from hsdjango.testcase import HubSpotTestCase
from asset_bender import bundling
from asset_bender.bundling import _extract_project_name_from_path

template_src = '''\
{% load asset_bender_tags %}{% bender_url "my_project/static/my/path.html"  %}'''
mock_url = 'http://staticdomain.com/my_project/static-1.3/my/path.html'


def get_mock_static3_asset_url(asset_path):
    eq_('my_project', _extract_project_name_from_path(asset_path))
    return 'http://staticdomain.com/my_project/static-1.3/my/path.html'

class StaticTagsCase(HubSpotTestCase):
    def test_static3_url(self):
        self.lax_mock(bundling3a.Static3, 'get_static3_asset_url', get_mock_static3_asset_url)
        t = Template(template_src)
        c = Context({})
        rendered = t.render(c)
        eq_(mock_url, rendered)
