"""HTTP client for dashboard and regional dashboard REST APIs."""
import requests


class RestClient:
    """Thin wrapper around requests for dashboard REST APIs."""

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _handle_error(self, e):
        if isinstance(e, requests.ConnectionError):
            return {'error': f'Cannot connect to {self.base_url}'}
        if isinstance(e, requests.Timeout):
            return {'error': f'Timeout connecting to {self.base_url}'}
        if isinstance(e, requests.HTTPError):
            try:
                body = e.response.json()
                detail = body.get('detail', e.response.text[:200])
            except Exception:
                detail = e.response.text[:200]
            return {'error': f'HTTP {e.response.status_code}: {detail}'}
        return {'error': str(e)}

    def get(self, path: str, headers: dict = None, **params) -> dict:
        """GET a JSON endpoint. Returns parsed dict or error dict."""
        url = f'{self.base_url}{path}'
        try:
            r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return self._handle_error(e)

    def post(self, path: str, json_data: dict = None, headers: dict = None) -> dict:
        """POST JSON. Returns parsed dict or error dict."""
        url = f'{self.base_url}{path}'
        try:
            r = requests.post(url, json=json_data, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return self._handle_error(e)

    def put(self, path: str, json_data: dict = None, headers: dict = None) -> dict:
        """PUT JSON. Returns parsed dict or error dict."""
        url = f'{self.base_url}{path}'
        try:
            r = requests.put(url, json=json_data, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return self._handle_error(e)

    def delete(self, path: str, headers: dict = None) -> dict:
        """DELETE. Returns parsed dict or error dict."""
        url = f'{self.base_url}{path}'
        try:
            r = requests.delete(url, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return self._handle_error(e)
