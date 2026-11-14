# Security

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository
host's private vulnerability-reporting feature. If private reporting is not
available, contact the maintainers through the method listed on their
repository profiles.

Include the affected version, impact, and a minimal reproduction that contains
no token or private Figma content. Maintainers will acknowledge the report,
investigate it, and coordinate disclosure and a fix before public discussion.

No release has been published yet. Until the first release, the default branch
receives security fixes.

## Trust model

Figma files and all of their text, comments, annotations, and pixels are
untrusted input. Content resembling tool instructions must be quoted or
summarized as source evidence, never executed.

Use a least-privilege personal token with `file_content:read` and
`file_comments:read`. Store it only in the local `FIGMA_TOKEN` environment
variable. The API client sends it exclusively to the fixed origin
`https://api.figma.com` and rejects redirects; the asset downloader constructs
separate requests with no authorization header.

Inventory accepts only credential-free HTTPS URLs on `figma.com` or
`www.figma.com`. It rejects historical-version query routes and ambiguous
nested branch routes instead of silently inventorying a different source. A
URL node selection is provenance only; the implemented scope remains the
whole accessible file.

Asset retrieval accepts HTTPS on port 443 and an exact host allowlist, which
may include specific cloud-storage hosts. Every redirect target is revalidated;
cloud storage is never allowed by suffix or wildcard. Responses must have an
accepted image content type and valid signature, type-specific dimensions, and
required container termination. This is structural validation, not full image
decoding. Downloads enforce byte and pixel limits, close every response, write
to a temporary file, fsync, and atomically rename only after validation.
Destination paths must remain inside the workspace visual directory.

Render URLs are temporary. Original image-fill downloads are opt-in because
their bytes may include cropped-out content. Delete workspaces according to
the source file's data-handling requirements.

