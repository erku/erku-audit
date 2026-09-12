"""Least-privilege GitHub broker service.

This package owns the single GitHub credential used by the autonomous
agent and exposes a narrow, authenticated HTTP API to the worker. The
worker (and the LLM behind it) never sees the GitHub token: it only ever
holds a separate `BROKER_TOKEN` bearer credential scoped to this API.

The broker can:
  - create a bounded number of public, prefix-enforced repositories
  - publish validated project manifests/files to a repo it created

The broker cannot:
  - delete any repository
  - change a repository's visibility
  - touch a repository it did not create
  - run arbitrary git commands
"""
