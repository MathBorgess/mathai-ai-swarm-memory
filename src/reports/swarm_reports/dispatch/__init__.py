"""Dispatch core: quota pacing, provider routing, durable claims, merge policy, decision digest.

Standalone module. No network calls, no provider CLI invocations except through
an injected transport/runner, so tests never touch a live provider.
"""
