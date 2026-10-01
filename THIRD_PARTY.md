# Third-party software

ftbfs is licensed under the GNU GPL version 3 or later (see `LICENSE`).
This file lists the software it ships or links to, and their licenses.
All of them are compatible with GPL-3.0-or-later.

Checked on 2026-09-30 against `uv.lock`.

## Vendored in this repository

Both files are unmodified copies of the upstream releases (checked by
SHA-256 against the files on unpkg.com).

| File | Project | Version | License |
|---|---|---|---|
| `ftbfs/web/static/htmx.min.js` | [htmx](https://github.com/bigskysoftware/htmx) | 2.0.4 | 0BSD |
| `ftbfs/web/static/htmx-ext-sse.js` | [htmx-ext-sse](https://github.com/bigskysoftware/htmx-extensions) | 2.2.2 | 0BSD |

htmx license notice:

```
Zero-Clause BSD
=============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

htmx-ext-sse license notice:

```
BSD Zero Clause License

Copyright (c) 2023, Alexander Petros

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## Python runtime dependencies

Not shipped here: `uv sync` installs them from PyPI. The license is
the one in each package's metadata (`License-Expression`, `License` or
its classifiers).

| License | Packages |
|---|---|
| MIT | annotated-doc, annotated-types, anyio, fastapi, h11, httplib2, markdown-it-py, mdurl, pydantic, pydantic-core, pyparsing, typing-inspection |
| BSD-3-Clause | click, idna, jinja2, lxml, markupsafe, oauthlib, starlette, uvicorn |
| Apache-2.0 | distro, python-multipart, tzdata (Windows only) |
| PSF-2.0 | typing-extensions |
| LGPL-3.0 | launchpadlib, lazr-restfulclient, lazr-uri, wadllib, psycopg, psycopg-binary |

The psycopg-binary wheel bundles libpq (PostgreSQL License), OpenSSL 3
(Apache-2.0), and system libraries under permissive or
LGPL-2.1-or-later licenses (Kerberos, OpenLDAP, Cyrus SASL, PCRE,
libxcrypt, keyutils, libselinux).

Development dependencies (pytest, ruff, httpx2) are used for tests and
lint only and are not part of what the project distributes.

## External programs

The pipeline runs these as separate programs and does not link to them,
so their licenses do not bear on this project's: sbuild, mmdebstrap,
devscripts, quilt, dpkg-dev, LXD (`lxc`), the `claude` CLI and opencode.
