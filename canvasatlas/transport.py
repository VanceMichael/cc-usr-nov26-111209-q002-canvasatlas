import urllib.error
import urllib.parse
import urllib.request


class OriginRestricted(Exception):
    """目标来源不在配置的允许清单内。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def origin_of(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('仅支持 HTTP/HTTPS 来源')
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == 'https' else 80
    return f'{parsed.scheme}://{parsed.hostname.lower()}:{port}'


def _validate_url(url, allowed_origins):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('仅支持 HTTP/HTTPS 请求')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('URL 不允许凭据或片段')
    origin = origin_of(url)
    if origin not in set(allowed_origins):
        raise OriginRestricted(f'来源 {origin} 不在允许清单内')
    return origin


class _AllowedRedirect(urllib.request.HTTPRedirectHandler):
    """只跟随落在允许 origin 内的重定向，并记录最终响应地址。"""

    def __init__(self, allowed_origins, max_hops=5):
        self.allowed_origins = set(allowed_origins)
        self.max_hops = max_hops
        self.hops = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        self.hops += 1
        if self.hops > self.max_hops:
            raise ValueError('重定向次数超过限制')
        if origin_of(new_url) not in self.allowed_origins:
            raise OriginRestricted(f'重定向目标 {origin_of(new_url)} 不在允许清单内')
        return super().redirect_request(request, response, code, message, headers, new_url)


def fetch(url, limit=4 * 1024 * 1024):
    """保留原有本机实验请求行为：不跟随重定向，仅允许本机。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('默认实验请求只允许本机 HTTP')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('URL 不允许凭据或片段')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(url, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('单次响应超过接收限制')
        return response.status, response.headers.get_content_type(), body


def retrieve(url, allowed_origins, limit=4 * 1024 * 1024, timeout=10):
    """按配置来源清单抓取一份文档，返回状态、类型、字节与最终响应地址。

    相对标识的解析由调用方按 final_url 完成；错误 HTTP 状态同样返回，
    交由账本登记，不在这里当作异常吞掉。
    """
    _validate_url(url, allowed_origins)
    redirect = _AllowedRedirect(allowed_origins)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect)
    try:
        response = opener.open(url, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('单次响应超过接收限制')
        return {
            'status': response.status,
            'content_type': response.headers.get_content_type(),
            'body': body,
            'final_url': response.geturl(),
        }
