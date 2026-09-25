# Security and privacy

Do not report leaked credentials in a public issue. Revoke the affected WebVPN/CAS session first, then contact the repository owner privately.

The following files are secrets or personal data and must never be attached to an issue or committed: browser profiles, Playwright storage state, `.env`, diagnostic keys, SQLite databases, logs, exported ranklists, generated images, and statistics CSV files.

This project does not bypass authentication. Deploy it only where you are authorized to access the source system and process the resulting student data. Keep VNC and noVNC bound to localhost and expose them only through an authenticated SSH tunnel.


Before publication, run `python3 -B scripts/check_public_release.py --history`.
The check inspects working candidates, staged blobs and locally reachable history;
it does not fetch remote refs, publish anything or certify that all personal data
has been identified. This distribution includes no tutorial media.
See [the release checklist](docs/PUBLIC_RELEASE.md) for asset hash approvals and
manual review of identities, fixtures, school-specific mappings and history.
