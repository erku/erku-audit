"""Isolated test sandbox runner.

Everything in this package runs INSIDE a network-less, secret-less
container (see the `sandbox-runner` service in compose.yaml). It never
imports f916/worker code and never sees API_KEY or any other secret: it
only reads job files dropped on a shared volume and shells out to pytest.
"""
