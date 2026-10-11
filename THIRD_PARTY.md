# Third-party software

ftbfs is licensed under the GNU GPL version 3 or later (see `LICENSE`).
This file lists the software it ships or links to, and their licenses.
All of them are compatible with GPL-3.0-or-later; the Ubuntu fonts are
separate files the browser loads, under their own licence.

Checked on 2026-09-30 against `uv.lock`.

## Vendored in this repository

The htmx files are unmodified copies of the upstream releases (checked by
SHA-256 against the files on unpkg.com).

| File | Project | Version | License |
|---|---|---|---|
| `ftbfs/web/static/htmx.min.js` | [htmx](https://github.com/bigskysoftware/htmx) | 2.0.4 | 0BSD |
| `ftbfs/web/static/htmx-ext-sse.js` | [htmx-ext-sse](https://github.com/bigskysoftware/htmx-extensions) | 2.2.2 | 0BSD |

`ftbfs/web/static/vanilla/` is built from upstream sources by
`make vanilla` (versions pinned in the `Makefile`), not edited by hand:

| File | Project | Version | License |
|---|---|---|---|
| `vanilla/vanilla.css` | [Vanilla Framework](https://github.com/canonical/vanilla-framework), compiled from `ftbfs/web/vanilla.scss` | 4.59.0 | LGPL-3.0 (text in `vanilla/LICENSE`) |
| `vanilla/fonts/*.woff2` | [Ubuntu font family](https://design.ubuntu.com/font), latin subsets, as Vanilla references them on assets.ubuntu.com | 0.896a, Mono 0.869 | Ubuntu Font Licence 1.0 |

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

Ubuntu Font Licence (Copyright 2010-2022 Canonical Ltd):

```
-------------------------------
UBUNTU FONT LICENCE Version 1.0
-------------------------------

PREAMBLE
This licence allows the licensed fonts to be used, studied, modified and
redistributed freely. The fonts, including any derivative works, can be
bundled, embedded, and redistributed provided the terms of this licence
are met. The fonts and derivatives, however, cannot be released under
any other licence. The requirement for fonts to remain under this
licence does not require any document created using the fonts or their
derivatives to be published under this licence, as long as the primary
purpose of the document is not to be a vehicle for the distribution of
the fonts.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this licence and clearly marked as such. This may
include source files, build scripts and documentation.

"Original Version" refers to the collection of Font Software components
as received under this licence.

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to
a new environment.

"Copyright Holder(s)" refers to all individuals and companies who have a
copyright ownership of the Font Software.

"Substantially Changed" refers to Modified Versions which can be easily
identified as dissimilar to the Font Software by users of the Font
Software comparing the Original Version with the Modified Version.

To "Propagate" a work means to do anything with it that, without
permission, would make you directly or secondarily liable for
infringement under applicable copyright law, except executing it on a
computer or modifying a private copy. Propagation includes copying,
distribution (with or without modification and with or without charging
a redistribution fee), making available to the public, and in some
countries other activities as well.

PERMISSION & CONDITIONS
This licence does not grant any rights under trademark law and all such
rights are reserved.

Permission is hereby granted, free of charge, to any person obtaining a
copy of the Font Software, to propagate the Font Software, subject to
the below conditions:

1) Each copy of the Font Software must contain the above copyright
notice and this licence. These can be included either as stand-alone
text files, human-readable headers or in the appropriate machine-
readable metadata fields within text or binary files as long as those
fields can be easily viewed by the user.

2) The font name complies with the following:
(a) The Original Version must retain its name, unmodified.
(b) Modified Versions which are Substantially Changed must be renamed to
avoid use of the name of the Original Version or similar names entirely.
(c) Modified Versions which are not Substantially Changed must be
renamed to both (i) retain the name of the Original Version and (ii) add
additional naming elements to distinguish the Modified Version from the
Original Version. The name of such Modified Versions must be the name of
the Original Version, with "derivative X" where X represents the name of
the new work, appended to that name.

3) The name(s) of the Copyright Holder(s) and any contributor to the
Font Software shall not be used to promote, endorse or advertise any
Modified Version, except (i) as required by this licence, (ii) to
acknowledge the contribution(s) of the Copyright Holder(s) or (iii) with
their explicit written permission.

4) The Font Software, modified or unmodified, in part or in whole, must
be distributed entirely under this licence, and must not be distributed
under any other licence. The requirement for fonts to remain under this
licence does not affect any document created using the Font Software,
except any version of the Font Software extracted from a document
created using the Font Software may only be distributed under this
licence.

TERMINATION
This licence becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER
DEALINGS IN THE FONT SOFTWARE.
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
