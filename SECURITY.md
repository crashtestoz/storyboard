# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability. Use GitHub's
private vulnerability reporting form:

https://github.com/crashtestoz/storyboard/security/advisories/new

If private reporting is not enabled, contact the repository owner through the
`crashtestoz` GitHub account and include only the minimum information needed
to reproduce the issue. Do not include secrets in the report.

## Supported versions

Only the latest `main` branch is currently maintained. This project is under
active development and does not yet promise long-term security support for
released versions.

## Secret handling

Service credentials belong in environment variables referenced by
`apiKeyEnv`; they must not be committed to `llm-services.json`,
`tts-services.json`, source files, logs, or issue reports.
