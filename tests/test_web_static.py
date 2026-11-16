"""Tests for the cache header of the pages and the files in static/.

Without "Cache-Control: no-cache" the browser may keep an old ui.css for hours after a change.
"""

import os

from agentic_os import web


def test_pages_ask_the_browser_to_check_each_time():
    for page in (web.page_jobs, web.page_costs, web.page_artifacts, web.page_brain):
        assert page().headers["cache-control"] == "no-cache", page.__name__


def test_static_files_ask_the_browser_to_check_each_time():
    static = web.StaticNoCache(directory=web.STATIC_DIR)
    path = web.STATIC_DIR / "ui.css"
    scope = {"type": "http", "method": "GET", "headers": []}
    response = static.file_response(path, os.stat(path), scope)
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]  # so an unchanged file is a short 304 answer
