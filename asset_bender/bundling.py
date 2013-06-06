
import hashlib
import logging
import os
import re
import socket
import traceback
from itertools import izip_longest

try:
    import simplejson as json
except ImportError:
    import json

try:
    from hubspot.hsutils import get_setting, get_setting_default
except ImportError:
    from hscacheutils.setting_wrappers import get_setting, get_setting_default

from hscacheutils.raw_cache import MAX_MEMCACHE_TIMEOUT
from hscacheutils.generational_cache import CustomUseGenCache, DummyGenCache

from asset_bender.http import fetch_ab_url_with_retries


logger = logging.getLogger(__name__)

CSS_EXTENSIONS = ('css', 'sass', 'scss')
JS_EXTENSIONS  = ('js', 'coffee')
NON_PRECOMPILED_EXTENSIONS = ('js', 'css')
PRECOMPILED_EXTENSIONS = tuple([ext for ext in CSS_EXTENSIONS + JS_EXTENSIONS if ext not in NON_PRECOMPILED_EXTENSIONS])

TAG_FUNCTION_NAME = 'bender_asset_url_callback'
SCAFFOLD_CONTEXT_NAME = 'bender_scaffold'
BENDER_ASSETS_CONTEXT_NAME = 'bender_assets_instance'
STATIC_DOMAIN_CONTEXT_NAME = 'bender_domain'

FORCE_BUILD_PARAM_PREFIX = "forceBuildFor-"
HOST_NAME = socket.gethostname()

LOG_CACHE_MISSES = get_setting_default('BENDER_LOG_CACHE_MISSES', True)
LOG_S3_FETCHES = get_setting_default('BENDER_LOG_S3_FETCHES', False)

def build_scaffold(request, included_bundles):
    return BenderAssets(included_bundles, request.GET).generate_scaffold()

def get_static_url(full_asset_path, template_context=None, bender_assets=None):
    '''
    Gets an absolute url with the correct build version for an asset of the given project name.

    Re-uses the BenderAssets instance on the template context so that we only have to hit memcached
    for the build versions once per request (or you can manually pass in an instance).
    '''

    if template_context == None and bender_assets == None:
        logger.warning("No template_context or bender_assets instance passed, that will probably cause lots of excess memcache requests")
    elif bender_assets == None:
        bender_assets = template_context.get(BENDER_ASSETS_CONTEXT_NAME)

    if not bender_assets:
        bender_assets = BenderAssets()

    if template_context and not template_context.get(BENDER_ASSETS_CONTEXT_NAME):
        template_context.get(BENDER_ASSETS_CONTEXT_NAME, bender_assets)

    return bender_assets.get_bender_asset_url(full_asset_path)

def _is_only_on_qa():
    return get_setting('ENV') == 'qa'

project_version_cache = CustomUseGenCache([
    'static_build_name_for:project',
    'static_deps_for_project:host_project'
    ],
    timeout=MAX_MEMCACHE_TIMEOUT)

scaffold_cache = CustomUseGenCache([
    'bender_all_scaffolds',
    'bender_scaffold_for_project:scaffold_key'
    ],
    timeout=MAX_MEMCACHE_TIMEOUT)

if get_setting_default('BENDER_NO_CACHE', False):
    project_version_cache = DummyGenCache()
    scaffold_cache = DummyGenCache()

def invalidate_cache_for_deploy(project_name):
    '''
    Invalidates the Asset Bender versions for this project. Do this as a part of your build
    and/or deploy scripts to force a live running app to resolve its versions anew (and run against
    the latest front-end code).
    '''
    project_version_cache.invalidate('static_build_name_for:project', project=project_name)
    project_version_cache.invalidate('static_deps_for_project:host_project', host_project=project_name)
    scaffold_cache.invalidate('bender_all_scaffolds')


class BenderAssets(object):
    def __init__(self, bundle_paths=(), http_get_params=None, exclude_default_bundles=False):
        '''
        @bundle_paths - a list containing the paths of the bundles to include
        @http_get_params - the request.GET query dictionary
        '''
        http_get_params = http_get_params if http_get_params else {}
        self.is_debug = self._check_is_debug_mode(http_get_params)
        self.use_local_daemon = self._check_use_local_daemon(http_get_params)
        self.skip_scaffold_cache = False

        self.included_bundle_paths = []

        # Used for the cases when you don't want to include the default bundles (style_guide)
        if not exclude_default_bundles:
            self.included_bundle_paths.extend(get_setting_default('DEFAULT_ASSET_BENDER_BUNDLES', []))

        self.included_bundle_paths.extend(bundle_paths)

        # strip first slashes for consistency
        self.included_bundle_paths = [path.lstrip('/') for path in self.included_bundle_paths]

        self.host_project_name = get_setting_default('PROJ_NAME', None)

        forced_build_version_by_project = self._extract_forced_versions_from_params(http_get_params)
        self.s3_fetcher = S3BundleFetcher(self.host_project_name, self.is_debug, forced_build_version_by_project)
        is_local_debug = self.is_debug

        if get_setting_default('BENDER_LOCAL_PROJECT_MODE', False):
            is_local_debug = True

        self.local_daemon_fetcher = LocalDaemonBundleFetcher(self.host_project_name, is_local_debug, forced_build_version_by_project)

    def generate_context_dict(self):
        '''
        Helper to get the variables you need to exist in your request context for Asset Bender
        '''
        return {
            SCAFFOLD_CONTEXT_NAME: self.generate_scaffold(),
            STATIC_DOMAIN_CONTEXT_NAME: self.get_domain(),
            BENDER_ASSETS_CONTEXT_NAME: self,
        }

    def generate_scaffold(self):
        '''
        The primary public method that will be called from the project's context_processor

        Either gets the Scaffold object from the cache or dispatches to actually building
        the scaffold from the the included bundles
        '''
        cache_key = self._get_scaffold_cache_key()
        scaffold = None

        # We don't cache the scaffold during local development or when ?forceBuildFor-<project> params are used
        if not self.use_local_daemon and not self.skip_scaffold_cache:
            scaffold = scaffold_cache.get(scaffold_key=cache_key)

            if not scaffold and LOG_CACHE_MISSES:
                logger.debug("Asset Bender scaffold cache miss: %s" % cache_key)

        if not scaffold:
            scaffold = self._generate_scaffold_without_cache()

        if not self.use_local_daemon and not self.skip_scaffold_cache:
            scaffold_cache.set(scaffold, scaffold_key=cache_key)

        return scaffold

    def _get_scaffold_cache_key(self):
        '''
        The key is a hash of all the data the scaffold needs to be uniqued by
        '''
        # we include the host name since when we deploy a project to one node, it might have a version
        # of the static bundles that is ahead of nodes that have not recieved a deploy yet
        # we include the __file__ name so that every deploy will clear the cache (since it will have a new virtuvalenv path)
        args = self.included_bundle_paths + [self.host_project_name] + [str(self.is_debug)] + [str(self.use_local_daemon)] \
               + [HOST_NAME] + [__file__]

        long_key = '-'.join(args)
        key = hashlib.md5(long_key).hexdigest()
        return key

    def _generate_scaffold_without_cache(self):
        self._validate_configuration()
        scaffold = Scaffold()

        for bundle_path in self.included_bundle_paths:
            self._add_bundle_to_scaffold(bundle_path, scaffold)

        return scaffold

    def _add_bundle_to_scaffold(self, bundle_path, scaffold, wrapper_template=None):
            html = ''
            contains_hardcoded_version = '/static-' in bundle_path

            if not contains_hardcoded_version and (self.use_local_daemon or self._check_use_local_daemon_for_project(bundle_path)):
                html = self.local_daemon_fetcher.fetch_include_html(bundle_path)

                if not html:
                    logger.error("Couldn't find bundle in local daemon: %s" % bundle_path)

            # If not using daemon, or if the html was not found in the daemon, then we check S3
            if not html:
                html = self.s3_fetcher.fetch_include_html(bundle_path)

            if wrapper_template:
                html = wrapper_template % html

            if html:
                scaffold.add_html_by_file_name(bundle_path, html)
            else:
                logger.error("Unknown bundle couldn't be added to scaffold: " % bundle_path)

    def invalidate_scaffold_cache(self):
        cache_key = self._get_scaffold_cache_key()
        scaffold_cache.invalidate('bender_scaffold_for_project:scaffold_key', scaffold_key=cache_key)

    def get_bender_asset_url(self, full_asset_path):
        '''
        Builds a URL to the particulr CSS, JS, IMG, or other file that is
        part of project_name.  The URL includes the proper domain and build information.

        '''
        project_name = _extract_project_name_from_path(full_asset_path)
        if not project_name:
            raise Exception('Your path must be of the form: "<project_name>/static/js/whatever.js"')

        # Break the full path down to just the asset path (everything under static/)
        asset_path = full_asset_path.replace("%s/static/" % project_name, '')

        # Make sure the path doesn't refer to a precompiled extension (since that won't actually exist on s3)
        extension = _find_extension(full_asset_path)

        if extension in PRECOMPILED_EXTENSIONS:
            message = "You cannot use the '%s' extension in this static path: %s.\n You must use 'js' or 'css' (It will work locally, but it won't work on QA/prod)." % (extension, full_asset_path)

            if get_setting('ENV') == 'prod':
                logger.error(message)
            else:
                raise Exception(message)

        # Dispatch to the correct fetcher
        if self.use_local_daemon:
            return self.local_daemon_fetcher.get_asset_url(project_name, asset_path)
        else:
            return self.s3_fetcher.get_asset_url(project_name, asset_path)

    def get_dependency_version_snapshot(self):
        '''
        Called by a special endpoint on QA by the deploy script.  Generates a snapshot
        of the current version of each depency so that when we deploy to prod we are guaranteed
        to use the same version we were using on QA
        '''
        return self.s3_fetcher.get_dependency_version_snapshot()

    def _validate_configuration(self):
        if not self.host_project_name:
            raise Exception("You must hav PROJ_NAME set to your project name in settings.py (eg: PROJ_NAME = \"example_app_whatever...\") !")

        for bundle_path in self.included_bundle_paths:
            extension = _find_extension(bundle_path)

            if extension in PRECOMPILED_EXTENSIONS:
                raise Exception("You cannot use the '%s' extension in a bundle path, you must use 'js' or 'css' (It can work locally, but it won't work on QA/prod)." % extension)

    def _check_use_local_daemon_for_project(self, bundle_path):
        if not get_setting_default('BENDER_LOCAL_PROJECT_MODE', False):
            return False
        if bundle_path.startswith(self.host_project_name + '/'):
            return True
        return False

    def _check_use_local_daemon(self, request):
        use_local = get_setting_default('BENDER_LOCAL_MODE', None)
        if use_local != None:
            return use_local
        if get_setting('ENV') in ('local',):
            return True
        return False

    def _check_is_debug_mode(self, http_get_params):
        '''
        Debug mode will include the expanded, unbundled, non-minifised assets
        returns a Boolean
        '''
        hs_debug = http_get_params.get('hsDebug')
        if hs_debug:
            return hs_debug != 'false'

        local_mode = get_setting_default('BENDER_LOCAL_MODE', None)
        if local_mode != None:
            return local_mode

        debug_mode = get_setting_default('BENDER_DEBUG_MODE', None)
        if debug_mode != None:
            return debug_mode

        return  get_setting('ENV') in ('local',)

    def _extract_forced_versions_from_params(self, http_get_params):
        """
        Pulls out all url params that look like "&forceBuildFor-<project_name>=<build_name>" and
        puts them into a dict such as:

        {
            "<project_name>": <build_name>,
            ...
        }

        This enables us to dynamically force a specific static build for any project at runtime. So with this
        you can test future major versions of static assets on QA concurrently with the current major version.
        (Eg. test major version 2 while 1 is still the one running by default on QA/prod).

        For <build_name> you can specify generic values (like "current", "latest", or "3") as well as specific
        build names (like "static-4.59").
        """
        forced_build_version_by_project = dict()

        for param, value in http_get_params.items():
            if param.startswith(FORCE_BUILD_PARAM_PREFIX):
                project_name = param[len(FORCE_BUILD_PARAM_PREFIX):]
                forced_build_version_by_project[project_name] = value

                # Always skip the scaffold cache if we are forcing a version
                self.skip_scaffold_cache = True

        if len(forced_build_version_by_project):
            return forced_build_version_by_project

    def get_domain(self):
        if self.use_local_daemon:
            return self.local_daemon_fetcher.get_domain()
        else:
            return self.s3_fetcher.get_domain()

class BundleFetcherBase(object):
    '''
    Base class from getting included assets from either the local Asset Bender server or S3
    '''
    src_or_href_regex = re.compile(r'((?:src|href)=([\'"]))([^\'"]+\2)')

    def __init__(self, host_project_name='', is_debug=False, forced_build_version_by_project=None):
        '''
        @host_project_name - the project name of the application that we are runing from
        '''
        self.host_project_name = host_project_name
        self.is_debug = is_debug
        self.project_directory = get_setting('PROJ_DIR')

        # We store the build versions locally in this object so we don't have to
        # hit memcached dozens of times per request every time we call get_asset_url.
        self.per_request_project_build_version_cache = {}
        self._add_forced_versions_to_per_request_cache(forced_build_version_by_project)

    def fetch_include_html(self, bundle_path):
        raise NotImplementedError("Implement me in a subclass")

    def get_asset_url(self, project_name, asset_path):
        raise NotImplementedError("Implement me in a subclass")

    def get_domain(self):
        raise NotImplementedError("Implement me in a subclass")

    def _append_static_domain_to_links(self, html):
        # Use the CDN domain instead of directly pointing to the s3 domain.
        # Only doing the switch at this point because the rest of the build fetching code
        # needs to use the s3 domain (to not get borked by caching).
        result = self.src_or_href_regex.sub(r'\1//%s\3' % self.get_domain(), html)
        return result

    def _add_forced_versions_to_per_request_cache(self, forced_build_version_by_project):
        """
        Initialize the per request cache with any specific builds overrides on this
        request (via FORCE_BUILD_PARAM_PREFIX)
        """
        if forced_build_version_by_project:
            for dep_name, dep_value in forced_build_version_by_project.items():
                if self._is_specific_build_name(dep_value):
                    self.per_request_project_build_version_cache[dep_name] = dep_value
                else:
                    self.per_request_project_build_version_cache[dep_name] = self._fetch_version_from_version_pointer(dep_value, dep_name)

    def _is_specific_build_name(self, build_name):
       return isinstance(build_name, basestring) and build_name.startswith('static-')


class LocalDaemonBundleFetcher(BundleFetcherBase):
    def fetch_include_html(self, bundle_path):
        url = "http://%s/bundle%s/%s.html?from=%s" % \
            (self.get_domain(),
             '-expanded' if self.is_debug else '',
             bundle_path,
             self.host_project_name
             )

        result = fetch_ab_url_with_retries(url, timeouts=[1, 5, 25])
        html = self._append_static_domain_to_links(result.text)

        return html

    def get_asset_url(self, project_name, asset_path):
        try:
            build_version = self._fetch_build_version(project_name)
            url = 'http://%s/%s/%s/%s' % (self.get_domain(),  project_name, build_version, asset_path)
        except Exception:
            traceback.print_exc()
            # HACK until we have a fix for projects with '.' in the project name
            url = 'http://%s/%s/static/%s' % (self.get_domain(),  project_name, asset_path)
        return url

    def get_domain(self):
        return get_setting_default('BENDER_DAEMON_DOMAIN', 'localhost:3333')

    def _fetch_build_version(self, project_name):
        """
        Fetching the build for a specific project from the daemon (if that hasn't already been cached this request)
        """
        build_version = self.per_request_project_build_version_cache.get(project_name)

        if build_version:
            return build_version

        build_version = self._fetch_build_version_from_daemon(project_name)
        self.per_request_project_build_version_cache[project_name] = build_version

        return build_version

    def _fetch_build_version_from_daemon(self, project_name):
        url = "http://%s/builds/%s?from=%s" % \
            (self.get_domain(),
             project_name,
             self.host_project_name)

        result = fetch_ab_url_with_retries(url, timeouts=[1, 2, 5])
        return result.text

class S3BundleFetcher(BundleFetcherBase):
    def fetch_include_html(self, bundle_path):
        project_name, hardcoded_version, bundle_postfix_path = self._split_bundle_path(bundle_path)

        if hardcoded_version:
            build_version = hardcoded_version
        else:
            build_version = self._fetch_build_version(project_name)

        url = 'http://%s/%s/%s/%s.bundle%s.html' % (
            self.get_domain(),
            project_name,
            build_version,
            bundle_postfix_path,
            '-expanded' if self.is_debug else '')

        if LOG_S3_FETCHES:
            logger.info("Fetching the bundle html (static versions) for %(bundle_path)s" % locals())

        result = fetch_ab_url_with_retries(url, timeouts=[1,2,5])
        return self._append_static_domain_to_links(result.text)


    def get_asset_url(self, project_name, asset_path):
        build_version = self._fetch_build_version(project_name)
        url = 'https://%s/%s/%s/%s' % (self.get_domain(),  project_name, build_version, asset_path)
        return url

    def get_dependency_version_snapshot(self):
        '''
        For every project in the lcoal static_conf.json,
        get the current version of that dependency.

        returns a dictionary in the form project_name=>build version
        '''
        project_name_to_version = {}
        project_names = self._get_static_conf_data().get('deps', {}).keys() + [self.host_project_name]
        for project_name in project_names:
            version = self._fetch_build_version(project_name)
            project_name_to_version[project_name] = version
        return project_name_to_version

    def make_url_to_pointer(self, pointer, project_name):
        # if the version is just an integer, that represents a major version, and so we create
        # the major version pointer
        if str(pointer).isdigit():
            pointer = 'latest-version-%s' % str(pointer)

        url = 'http://%s/%s/%s' % (self._get_non_cdn_domain(), project_name, pointer)
        if get_setting('ENV') not in ('prod',):
            url += '-qa'
        return url

    project_name_re = re.compile(r'^/?([^/]+)/(static(?:-\d+.\d+)?)/(.*)')

    def _split_bundle_path(self, bundle_path):
        match = self.project_name_re.match(bundle_path)

        if match.group(2).startswith('static-'):
            hardcoded_version = match.group(2)
        else:
            hardcoded_version = None

        return match.group(1), hardcoded_version, match.group(3)

    def _fetch_build_version(self, project_name):
        # Are there any fixed local versions?
        build_version = self._fetch_local_project_build_version(project_name)

        if build_version:
            return build_version

        # Next, try the per-request mini-cache
        build_version = self.per_request_project_build_version_cache.get(project_name)

        if build_version:
            return build_version

        # Try memcache
        build_version = project_version_cache.get(
            project=project_name,
            host_project=self.host_project_name)

        if not build_version:
            if LOG_CACHE_MISSES:
                logger.debug("Asset Bender build version cache miss: %s from %s" % (project_name, self.host_project_name))

            # Next try fetching directly from s3
            build_version = self._fetch_build_version_without_cache(project_name)

        if not build_version:
            raise BundleException("Could not find a build version for %s" % project_name)

        project_version_cache.set(
            build_version,
            project=project_name,
            host_project=self.host_project_name)

        self.per_request_project_build_version_cache[project_name] = build_version
        return build_version

    def _fetch_local_project_build_version(self, project_name):
        '''
        If this bundle_path is being included from the project we are currently running in,
        then we always use the version in prebuilt_recursive_static_conf.json, if it exists,
        that way the .html and .py code is always linked to the exact javascript version
        and we don't get issues where javascript can be deployed before a node is updated
        '''
        build_version = ''
        if project_name == self.host_project_name:
            build_version = self._get_prebuilt_version(project_name)
            if not build_version:
                build_version = os.environ.get('HS_BENDER_FORCED_BUILD_VERSION_%s' % project_name.upper())
        return build_version

    def _fetch_build_version_without_cache(self, project_name):
        static_conf_version = self._get_version_from_static_conf(project_name)
        if self._is_specific_build_name(static_conf_version):
            return static_conf_version
        pointer_build_version = self._fetch_version_from_version_pointer(static_conf_version, project_name)
        prebuilt_build_version = self._get_prebuilt_version(project_name)
        frozen_by_deploy_version = self._get_frozen_at_deploy_version(project_name)
        build_version = self._maximum_version_of(
            pointer_build_version,
            prebuilt_build_version,
            frozen_by_deploy_version)

        if LOG_S3_FETCHES:
            logger.info("Fetched static version for %(project_name)s: %(build_version)s (max of %(pointer_build_version)s, %(prebuilt_build_version)s, and %(frozen_by_deploy_version)s)" % locals())

        return build_version

    def _get_version_from_static_conf(self, project_name):
        deps = self._get_static_conf_data().get('deps', {})

        if not deps.get(project_name):
            logger.error("Tried to find a dependency (%s) in static_conf.json, but it didn't exist. Your static_conf.json must include all the static dependencies that your project may reference." % project_name)

        return deps.get(project_name, 'current')

    def _get_static_conf_data(self):
        path = os.path.join(self.project_directory, 'static/static_conf.json')
        if not os.path.isfile(path):
            return {}
        return _load_json_file_with_cache(path, throw_exception_if=_is_only_on_qa)

    def _fetch_version_from_version_pointer(self, pointer, project_name):
        '''
        Pointer is either 'current' or 'edge'.  This method downloads the pointer
        from S3 and gets the actual build version from it (ex. 1.4.123 )
        '''
        url = self.make_url_to_pointer(pointer, project_name)
        result = fetch_ab_url_with_retries(url, timeouts=[1, 2, 5])

        if not result.text:
            self._check_for_fetch_html_errors_and_raise_exception(result, url)
            raise AssetBenderException("Invalid version file (empty) from: %s" % url)

        return result.text.strip()

    def _get_prebuilt_version(self, project_name):
        '''
        When a project is built in Jenkins to QA, we store the version of the bundle that existed
        when it was built
        '''
        path = os.path.join(self.project_directory, 'static/prebuilt_recursive_static_conf.json')
        data = _load_json_file_with_cache(path, throw_exception_if=_is_only_on_qa)

        # If this is the host project, get the build from the "build" key instead of the deps dict
        if project_name == self.host_project_name:
            if data.get('build'):
                return "static-%s" % data['build']
            else:
                return ''
        else:
            return data.get('deps', {}).get(project_name, '')

    def _get_frozen_at_deploy_version(self, project_name):
        '''
        At the time we deploy to prod, we get a snapshot from QA of the exact version number
        of all dependencies.  When we deploy to prod, we write out a file that includes
        this version number of snapshot.  Prod will then always use 'at least' the version number
        of the snapshot.  So there is never any danger of having working code on QA, then deploying
        to prod only to find you are importing an old, buggy version of a dependency
        '''
        path = os.path.join(self.project_directory, 'static/frozen_at_deploy_version_snapshot.json')
        return _load_json_file_with_cache(path).get(project_name, '')

    def _maximum_version_of(self, *args):
        args = [a for a in args if a]
        return sorted(args, cmp=self._compare_build_names).pop()

    def _compare_build_names(self, x_build, y_build):
        """
        Implementation of cmp() for build names. So:

        >>> compare_build_names('static-1.0', 'static-1.1')
        -1
        >>> compare_build_names('static-2.0', 'static-1.1')
        1
        >>> compare_build_names('static-3.4', 'static-3.4')
        0

        """
        # Convert each build name to a two element tuple
        x, y = [tuple(map(int, build.replace('static-', '').split('.'))) for build in (x_build, y_build)]
        major_cmp = cmp(x[0], y[0])
        if major_cmp != 0:
          return major_cmp
        else:
          return cmp(x[1], y[1])

    def get_domain(self):
        return get_setting_default('BENDER_CDN_DOMAIN', 'static2cdn.hubspot.com')

    def _get_non_cdn_domain(self):
        '''
        When downloading the version pointer, we need to skip the CDN and go direct to avoid problems with caching
        '''
        return get_setting_default('BENDER_S3_DOMAIN', 'hubspot-static2cdn.s3.amazonaws.com')


class Scaffold(object):
    """
    An object for holding a set of js and css paths
    """

    # Lovely IE, http://john.albin.net/css/ie-stylesheets-not-loading ...
    # Six minus the real max (31) so that one style tag can used
    # to "@import" the rest, and we leave enough of a buffer for any
    # js files that append link/style blocks later.
    MAX_IE_CSS_INCLUDES = 20

    # Also, if that isn't fun enough, there is a max number of @imports in a
    # single <style> element: http://blogs.msdn.com/b/ieinternals/archive/2011/05/14/internet-explorer-stylesheet-rule-selector-import-sheet-limit-maximum.aspx
    MAX_IMPORTS_PER_STYLE_ELEMENT = 25

    head_template = "asset_bender/scaffold/head.html"
    end_of_body_template = "asset_bender/scaffold/end_of_body.html"

    def __init__(self, force_normal_include=False):
        '''
        @head_js - a collection of javascript src files
        @head_css - a collection of css files for the head
        @footer_js - a collection of js files for the footer
        '''
        self.head_js = []
        self.head_css = []
        self.footer_js = []

        self.force_normal_include = force_normal_include

    def total_css_files(self):
        return len(self.head_css)

    def add_head_css_html(self, html):
        self.head_css += html.split('\n')

    def add_head_js_html(self, html):
        self.head_js += html.split('\n')

    def add_footer_js_html(self, html):
        self.footer_js += html.split('\n')

    def add_html_by_file_name(self, file_name, html):
        '''
        Adds the html to the proper section based on the name and extension of 'file_name'
        '''
        if _find_extension(file_name) in CSS_EXTENSIONS:
            self.add_head_css_html(html)
        else:
            if '_head.js' in file_name or '-head.js' in file_name:
                self.add_head_js_html(html)
            else:
                self.add_footer_js_html(html)


    # Methods used by the layout templates to output scaffold files
    def header_js_html(self):
        html = "\n".join(self.head_js)

        # JS only for IE
        html += """
        """

        return html

    def footer_js_html(self):
        return "\n".join(self.footer_js)

    def header_css_html(self):
        if self.force_normal_include:
            return "\n".join(self.head_css)
        else:
            return "\n".join(self.head_css[:self.MAX_IE_CSS_INCLUDES])

    def has_excess_stylesheets_for_IE(self):
        return self.total_css_files() > self.MAX_IE_CSS_INCLUDES and not self.force_normal_include

    def header_forced_import_css_html_for_IE(self):
        """
        Get all of the excess css links that wouldn't work in IE and breaks them
        into @imports in a separate <style> element.

        Also chunks things so there are less than 30 @imports per <style> element
        (oh IE...)
        """

        if self.has_excess_stylesheets_for_IE():

            # Convert all the excess <link> elements into @imports
            import_lines = map(self._convert_link_to_import, self.head_css[self.MAX_IE_CSS_INCLUDES:])

            # Chunk those @imports by MAX_IMPORTS_PER_STYLE_ELEMENT, and then
            # turn each chunk into a single string separated by newlines
            chunked_import_lines = chunk(self.MAX_IMPORTS_PER_STYLE_ELEMENT, import_lines)
            chunked_import_lines = ['\n'.join(filter(None, lines)) for lines in chunked_import_lines]

            # Join together into <style> tags
            result = '\n\n'.join(["<style>\n%s\n</style>" % c for c in chunked_import_lines])

            return result

        else:
            return ""

    def _convert_link_to_import(self, link_html):
        """
        Converts:
            <link href="/style_guide/static/sass/style_guide_plus_layout.css?body=1" media="screen" rel="stylesheet" type="text/css" />
        To:
            @import "/style_guide/static/sass/style_guide_plus_layout.css?body=1";
        """

        try:
            # Get the URL via indexes
            left_index = link_html.index('href=') + 6
            quote_char = link_html[left_index - 1]
            right_index = link_html.index(quote_char, left_index)
            url = link_html[left_index:right_index]
        except:
            print "Warning, trying to add a non css file (link element) to the scaffold: %s" % link_html
            return ""

        return "@import \"%s\";" % url

_file_json_cache = {}
def _load_json_file_with_cache(path, throw_exception_if=None):
    '''
    Loads json data from the local file system and caches the result in local memory
    '''
    if path in _file_json_cache:
        return _file_json_cache[path]

    if not os.path.isfile(path):
        if hasattr(throw_exception_if, '__call__') and throw_exception_if() and not get_setting_default('BENDER_QA_EMULATION', False):
            raise IOError("""
Couldn't find the prebuilt static dependencies file at: %s
You should double check that your static and jenkins config are correct. And that you have these lines in your Manifest.in:

        global-include static_conf.json
        global-include prebuilt_recursive_static_conf.json

Note: this error only appears on QA (and is a warning so things don't unknowningly break on prod).

If you have any questions, you can bug tfinley@hubspot.com.
                """ % path)
        else:
            _file_json_cache[path] = {}
            return {}

    with open(path, 'r') as f:
        data = json.load(f)
        f.close()
        _file_json_cache[path] = data
        return data


path_extension_regex = re.compile(r'/(css|sass|scss|coffee|js)/')

def _find_extension(filename):
    extension = os.path.splitext(filename)[1]
    # If there was an extension at the end of the file, grab it
    # and strip off the leading period
    if extension:
        extension = extension[1:]
    # Otherwise look for /<ext>/ in the path
    else:
        match = path_extension_regex.search(filename)
        if match:
            extension = match.group(1)
    if extension:
        extension = extension.lower()
    return extension


project_name_static_path_regex = re.compile(r"(?:\/|^)([^\/]+)\/static\/")

def _extract_project_name_from_path(path_or_url):
    """
    Extracts the project_name out of a path or URL that looks like any of these:
    project_name/static/...
    .../project_name/static/...
    """
    match = project_name_static_path_regex.search(path_or_url)
    if match:
        return match.group(1)
    else:
        return None


# Via http://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks-in-python
def chunk(n, iterable, padvalue=None):
    "chunk(3, 'abcdefg', 'x') --> ('a','b','c'), ('d','e','f'), ('g','x','x')"
    return izip_longest(*[iter(iterable)]*n, fillvalue=padvalue)
