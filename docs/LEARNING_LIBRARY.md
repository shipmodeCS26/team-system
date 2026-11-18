# Internal learning library

This optional read-only feature adds separate reusable learning, brand applications, tasks and course coverage to the existing Knowledgebase. The application repository contains only generic rendering/loading code and synthetic test fixtures. Keep private content outside it.

## Runtime configuration

Set `LEARNING_LIBRARY_ENABLED=true` only in an authorized private environment. The feature requires `WORKSPACE_USER`, `WORKSPACE_PASSWORD_HASH` and `SECRET_KEY` regardless of whether operational integrations are enabled. Use existing approved workspace access; no new credentials or permissions are provisioned by this code.

Set `LEARNING_LIBRARY_COMMIT` to one reviewed, full 40-character source commit. Choose one content mode:

- **Private artifact:** `LEARNING_LIBRARY_DIR` points to a private server directory containing four data files and a matching manifest. Create it with `package_learning_library.py --source <private-data-directory> --output <new-private-runtime-directory> --commit <reviewed-source-commit>`. Keep the output outside the public checkout, static files and build artifacts. Deploy it through the host's approved private mechanism.
- **Server-side GitHub:** set `LEARNING_LIBRARY_REPO` and `LEARNING_LIBRARY_TOKEN` privately. The token must have read-only contents access to the intended source repository. The reader fetches fixed data filenames from that single pinned commit, disallows redirects and bounds response sizes. Creating/granting access requires separate authorization. Do not paste a token into chat, commit it, or expose it to browser code.

The four files are `knowledge.json`, `sources.json`, `tasks.json`, and `skool-lessons.json` under the source's `knowledge-base/data` directory. The artifact manifest stores the pinned commit and SHA-256 checksums for each file. Package only a reviewed snapshot; the manifest verifies artifact consistency, not the truth of its content.

The feature defaults off. Disabled requests return 404 without loading data. Missing configuration, an invalid source or an outage shows an unavailable state without returning content or detailed errors. Successful results show the revision and record date. There is no fallback to demo learning or an unmarked stale snapshot.

## Data and interface

Records link learning to its decision, scope, source IDs, application/status and next task IDs. Tasks contain proposed owners, dependencies, completion evidence and status. Sources include their class, review date and coverage. Lessons distinguish `indexed_only` from `reviewed_summary`; an index entry must not invent a summary.

`/knowledgebase` is the general guide overview. `/knowledgebase/learning` is the authenticated internal section. Search and filters use GET forms and server-rendered escaped text. No separate unauthenticated JSON/static content endpoint is provided. External source links use HTTPS; local evidence references remain text. Task writes, account setup and conversational AI are outside this feature.

## Verification and release

Run `python -B -m unittest -v test_learning_library test_knowledgebase`, followed by the existing application suite. Use synthetic fixtures for public test evidence. In private staging, verify actual sign-in, data revision, source outage behavior, filters/citations, responsive layout, and that source content/tokens are absent from public assets, caches and logs. Runtime credentials, real upstream access and deployment configuration are not established by unit tests.

Preserve the existing Shipmode brand and operational behavior. Follow the approved plan, independent review and staging acceptance workflow. Do not merge to production or deploy under implementation approval alone. Rollback: disable `LEARNING_LIBRARY_ENABLED` and revert the identified app release if needed; the library makes no source-data or operational writes.
