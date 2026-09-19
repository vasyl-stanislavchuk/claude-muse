# Auth

## How auth works

`muse login` runs an OIDC device-code flow against `auth.meta.com`, and the server provisions a **Muse Code subscription API key** in return. The muse CLI stores that key in the login keychain, wrapped in a JSON envelope:

```
service  ai.meta.dev.credentials
account  meta
secret   {"secret_schema_version":1,"api_key":"LLM|…","access_token":"dca:…"}
```

`api-key.sh` unwraps `.api_key` from it. Claude Code calls that script through `apiKeyHelper` and uses stdout as the credential, re-reading it hourly (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS=3600000`).

So the key exists in exactly one place - the keychain. Nothing is copied into a config file, and `muse logout && muse login` rotates it with nothing here to edit.

**Subscription vs pay-as-you-go matters.** The keys you can mint in the console at `https://dev.meta.ai` are pay-as-you-go and return `402 billing_error` until a payment method is attached. Only the `Muse Code` key - the one `muse login` provisions, which the console shows as created by you - bills against the subscription. Don't swap the helper out for a console key.

## Notes

- The muse CLI's own config is separate: `~/.config/muse/{auth.json,settings.json}`. `auth.json` holds no secret - it records `"storage": "keychain"` and points at the item above.
- The device flow's constants are readable in the launcher at `~/.local/bin/muse`: `auth.meta.com`, public client id `1031625952748946`, no client secret.
- `~/.config/claude-muse/env` held the original pay-as-you-go key and is no longer read by anything. Safe to delete.

