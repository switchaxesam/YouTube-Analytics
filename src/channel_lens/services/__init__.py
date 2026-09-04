"""Analysis and ingestion logic.

Each module here owns one question the app answers, depends on the layers below
it (``youtube``, ``models``, ``db``), and never imports a sibling's internals.
"""
