# Contributing

Thanks for helping improve Storyboard.

## Before opening a change

Please search existing issues first. For a bug, include the operating system,
Python version, model/backend involved, reproduction steps, and the relevant
Backend output or saved log. Do not attach API keys, access tokens, private
voice recordings, or generated media containing personal or confidential
material.

For a feature or behavior change, open an issue first when the change could
affect the project format, render pipeline, service configuration, or other
users' workflows.

## Pull requests

Pull requests should target `main` and explain what changed, why it changed,
and how it was tested. Maintainer approval is required before merging. Keep
commits focused and avoid committing local project data, generated outputs,
logs, credentials, or machine-specific configuration.

The repository is maintained on Gitea and published as a GitHub mirror. The
canonical development branch is the Gitea `main` branch; GitHub is used for
public visibility and review. A GitHub pull request may be transferred or
recreated on Gitea before it is merged.

## Testing

Run the checks that apply to your change:

```sh
node --check js/app.js
PYTHONPATH=. python tests/scene_layers.py
python tests/render_all.py
```

Rendering tests do not require model downloads. Tests that exercise a live
backend should document the service and model used.

## Licensing contributions

By submitting a contribution, you confirm that you have the right to submit
it and that you agree it may be distributed as part of this project under the
project's license. Please do not submit third-party code or assets without
checking their license and recording the attribution.
