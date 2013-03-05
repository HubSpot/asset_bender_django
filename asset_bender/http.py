import logging
import requests
from requests import ConnectionError, HTTPError, Timeout

from asset_bender import AssetBenderException

logger = logging.getLogger(__name__)


# For testing
class FauxException(Exception):
    pass


def _download_url(url, timeout=10, **kwargs):
    result = requests.get(url, timeout=timeout, **kwargs)
    result.raise_for_status()

    return result

def fetch_ab_url_with_retries(url, retries=None, timeouts=None, **kwargs):
    """
    Calls download_url retries number of times unless a valid response is returned earlier. 
    Each retry will have a timeout of timeouts[i - 1] where i is the attempt number.

    If you omit retries, it will be set to len(timeouts).
    """

    attempt = 1
    latest_result = None

    if retries is None and timeouts is None:
        retries = 1
        timeouts = [None]
    elif retries is None:
        retries = len(timeouts)

    while attempt <= retries:
        timeout = timeouts[min(len(timeouts), attempt) - 1] 

        try:
            latest_result = _download_url(url, timeout=timeout, **kwargs)
            return latest_result

        except (ConnectionError, HTTPError, Timeout, FauxException) as e:
            status_code = getattr(latest_result, 'status_code', None)

            # Warn an continue if there are retries
            if attempt < retries:
                logger.warning(e)

            # Otherwise throw a wrapped error
            elif status_code >= 500:
                logger.error(e)
                raise AssetBenderException("Server error from Asset Bender (%s) for: %s" % (status_code, url), e)

            elif status_code in (404, 410):
                logger.error(e)
                raise AssetBenderException("Url doesn't exist in Asset Bender (%s): %s" % (status_code, url), e)

            elif status_code is not None and (status_code >= 400 or status_code < 200):
                logger.error(e)
                raise AssetBenderException("Asset Bender returned error (%s) for: %s" % (status_code, url), e)

            else:
                logger.error(e)
                raise e

        attempt += 1

    return latest_result


def _format_result_error(self, content):
    if not content:
        return '(No error body found)'
    start_code_block = content.find('<pre>')
    end_code_block = content.find('</pre>')
    if start_code_block < 0 or end_code_block < start_code_block:
        return content
    error = content[start_code_block + 5:end_code_block]
    # Unescape entities in the error
    from BeautifulSoup import BeautifulStoneSoup, Tag
    error = BeautifulStoneSoup(error, convertEntities=BeautifulStoneSoup.HTML_ENTITIES).contents[0]
    if isinstance(error, Tag):
        error = ''.join(error.contents)
    return "Error from hs-static:\n\n%s" % error