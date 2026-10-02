---
name: create-plugin
description: Create or update a plugin for The Connector with a standalone npm UI, Python backend, plugin manifest, cross-platform launchers, and an installable plugin.zip. Use when a user asks to build or package a Connector plugin; not for installing third-party Codex plugins.
---

# Create a Connector Plugin

Turn the user's requested capability into a self-contained plugin under
`plugin/<plugin_id>/` and deliver its source plus a validated `plugin.zip`.

## Establish the capability

Infer the plugin's name, inputs, outputs, and data access from the request and
repository context. Ask only about missing choices that materially affect the
implementation. Default to Python for managed installation. If the user requests
Java or C++, explain the current installer limitation and establish whether they
want a standalone package or an installer extension before proceeding.

Choose a unique lowercase snake_case plugin ID. The ID also becomes the agent
tool name. Use that ID consistently in module names, routes, and the manifest;
npm distribution names can use hyphens. Preserve existing plugin source when
updating a plugin and avoid overwriting another plugin or its ZIP.

## Read the local contract

Read [references/plugin-contract.md](references/plugin-contract.md) before
authoring the package. In this repository, the root is three directories above
this skill folder. Locate it by `backend/app/services/plugin_manager.py` and
`plugin/count_message/plugin.json`, rather than assuming the current directory.

Check the current sources when applying this skill:

- `backend/app/services/plugin_manager.py`: ZIP validation, dependencies,
  backend invocation, health checks, and agent tool registration.
- `backend/app/api/plugins.py`: hosted UI options and allowed proxy routes.
- `plugin/count_message/`: complete reference package, launcher, UI lifecycle,
  backend, packaging script, tests, and `instruction.md`.

Use the live implementation when it differs from this reference. Adapt only
the relevant parts of `count_message`; its message-counting logic, database
permission, port, and API fields are specific to that example.

## Build the plugin

Create the manifest, backend package, browser-ready UI package, standalone
preview, launchers, packaging script, and usage instructions described in the
contract. Keep plugin business logic independently callable so both HTTP and
agent calls use the same implementation.

Implement the requested feature completely, including input validation, useful
JSON errors, and UI loading, empty, success, and failure states. Use the supplied
`apiBaseUrl` and `sessionId` in managed mode. Clean up requests, timers, listeners,
and DOM on unmount. Declare actual runtime dependencies and only the supported
permissions the capability needs.

Keep plugin source self-contained. Managed installation extracts only the ZIP
and does not install the plugin's Python project or compile its frontend.
Choose source paths and relative imports that work in that extracted layout.
Keep host changes within the user's requested integration scope.

Document managed installation through **Plugins → Install → Enable → Open UI**,
standalone launch commands, runtime requirements, configuration, API inputs and
outputs, the UI lifecycle, tests, and the ZIP rebuild command in `instruction.md`.
Explain any plugin-specific data access or external effects.

## Verify and deliver

Test the requested behavior and relevant invalid-input or unavailable-data paths.
Build the UI when necessary, then create `plugin/<plugin_id>/plugin.zip` with
package files at the archive root. Inspect the final archive and run the host's
`validate_zip` against it using the command in the reference.

Smoke-test the extracted backend using the manifest's exact `module:function`
entry, a free loopback port, `/health`, and a representative JSON POST. Verify
the UI's mount and cleanup in a browser when browser tools are available. Use
temporary fixture data and isolated plugin storage for integration tests rather
than changing an existing Connector installation merely to validate a package.
Report checks that could not run and their specific blockers.

Finish with the source folder, ZIP path, relevant test results, and the shortest
usable installation instructions. Creation alone does not imply publishing the
npm/Python package or enabling it in a running Connector instance.
