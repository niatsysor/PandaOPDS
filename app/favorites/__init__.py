"""Favorites operations proxy + periodic favorites sync.

- ``router``   : ``/api/favorites/*`` (write ops, categories, sync control)
- ``sync``     : ``FavoritesSyncer`` background task (incremental scan +
                 optional auto-archive of newly discovered items)
- ``state``    : JSON snapshot persistence for the sync scanner
"""
