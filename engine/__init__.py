"""Localhost shim in front of api.meta.ai for claude-muse.

The Meta Model API serves an Anthropic-compatible *subset*. Claude Code sends five
request shapes it rejects, which took out WebSearch and every subagent launch. This
rewrites those five on the way through and passes everything else byte for byte.

It never reads or stores the credential: x-api-key is forwarded as received, so the
key stays in the keychain behind apiKeyHelper.
"""
