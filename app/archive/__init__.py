"""Archive: persistent E-Hentai archive store (purchased with GP).

- ``store``: on-disk data directory (zip master + meta.json), index rebuild
  by scanning, atomic meta writes, zip page reads.
- ``manager``: lifecycle (quote/submit/download/zip-convert/validate), state
  machine, per-gallery single-flight, bounded concurrency.
- ``router``: WebUI JSON API (quote/start/status/list/delete/refresh).
"""
