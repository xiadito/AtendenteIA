"""Accounts: user identity, authentication and tenant provisioning (Module S3a).

A peer package of ``bot/`` and ``integrations/``, not a child of either, because
``provision.py`` has to write tables owned by BOTH: ``owners`` belongs to
``integrations/store.py`` while ``ai_configs``, ``class_types`` and
``scheduling_configs`` belong to ``bot/``. The rule stated in
``integrations/store.py`` — nothing under ``integrations/`` imports ``bot/`` —
rules that package out, and ``webhook/`` is the HTTP layer while the
provisioning CLI must run outside Flask entirely. Same reasoning that put
``jobs/`` where it is.

Only ``auth.py`` knows Flask exists. ``users.py``, ``tenants.py``,
``provision.py`` and ``bootstrap.py`` take an explicit ``tenant_id`` and read
through ``database.db.get_connection()``, never through ``flask.g`` or a
request — the provisioning CLI and the Railway cron both run with no
application context at all.
"""
