"""WebUI: JSON API + single-page HTML frontend for configuration & status.

API-first design: all data assembly (grouping, credential masking, source
flags) lives in ``router.py``; the HTML page (``page.html``) is a thin
consumer that fetches the JSON endpoints. Future features — offline gallery
management, automation workflows — extend the API layer without changing the
page contract or the OPDS/stream routers.
"""
