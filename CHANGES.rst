=========
Changelog
=========

All notable changes to this project will be documented in this file.

The format is based on `Keep a Changelog <https://keepachangelog.com/en/1.0.0/>`_,
and this project adheres to `Semantic Versioning <https://semver.org/spec/v2.0.0.html>`_.

---

Version 0.1.1
=============

Documentation
-------------

*   **Fixed internal links for PyPI's rendered page**:
    The Usage Guide, Design & Architecture doc, and Roadmap were linked with relative paths, which GitHub resolves against the repo tree but PyPI does not -- it renders the README standalone, so every one of these 404'd there. Switched to absolute GitHub URLs, and reworded the link text to read as titles ("Usage Guide") rather than raw filenames ("docs/USAGE.md").

*   **Added status badges**: PyPI version, CI tests, Coveralls coverage, supported Python versions, and license.

*   **Added PyPI packaging metadata**: classifiers and project URLs (homepage, bug tracker).

*   **Reframed the `X-Forwarded-For` limitation as a deliberate default, not an oversight**:
    ``by_ip`` ignoring ``X-Forwarded-For`` was previously documented as "not yet configurable." Explained why: trusting a client-suppliable header unconditionally would let a caller spoof their rate-limit identity. See the `Roadmap <https://github.com/cehstephen/jetio-ratelimit/blob/main/DESIGN.md#roadmap>`_ for what a real implementation needs.

*   **Rewrote DESIGN.md in a documentation voice**:
    Replaced first-person build narration and strikethrough checklists with the same design rationale, verified Jetio internals, and bugs-found-and-fixed content presented as reference documentation rather than a build log.

---

Version 0.1.0
=============

*   **Initial release**:
    Rate limiting plugin for `Jetio <https://pypi.org/project/jetio/>`_ -- sliding-window algorithm, in-memory store, IP/account/header/user-keyed limiting. Usable as ASGI middleware (for routes registered by other plugins, e.g. jetio-auth's ``/login``) or as a ``Depends()``-composable dependency (for routes you register yourself, e.g. a ``CrudRouter`` policy).

*   **Stacked and reusable policies**:
    ``RateLimiter.protect_many()`` stacks multiple independent limits on one route (e.g. IP + account on ``/login``, as defense against distributed credential stuffing) or reuses one policy list across several routes, in a single call.

*   **Key functions**:
    ``by_ip``, ``by_field(name)`` (request body field), ``by_header(name)`` (e.g. an API key), ``by_user`` (resolved identity, dependency mode only).

*   **Collision-safe by default**:
    Every ``.protect()``/``.dependency()`` call gets an automatically-unique name, prefixed onto its store key, so independently-registered limits can never collide even when their raw keys happen to coincide.

*   **Real end-to-end scenario tests**:
    ``tests_e2e/`` runs actual scenario apps over real HTTP -- a SaaS-style app stacking IP+account limits on jetio-auth's ``/login``, and a public API keyed by an API key header -- alongside the ``tests/`` unit suite.

*   **Requires jetio>=1.2.3**:
    This package relies on two fixes shipped in that jetio release: ``Request.client`` being public (used by ``by_ip`` in dependency mode) and ``HTTPException.headers`` being propagated (so a blocked dependency-mode request gets a real ``Retry-After`` header, the same as middleware mode, rather than only the retry time embedded in the error message).
