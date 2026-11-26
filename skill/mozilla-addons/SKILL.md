---
name: mozilla-addons
description: Use the Mozilla Add-ons (AMO) API to look up Firefox extensions, upload ZIP or XPI packages, submit a new listing or version, and check validation, review, or signing status.
---

# Mozilla Add-ons

Use `uv run --script /path/to/skill/mozilla-addons/scripts/amo.py` as `amo` in the examples below. For a default OMP install, the script is in `~/.omp/agent/skills/mozilla-addons/scripts/amo.py`. It calls the production AMO v5 API. It does not follow redirects or retry submissions.

## Credentials

Sign in to the [AMO Developer Hub API credentials page](https://addons.mozilla.org/en-US/developers/addon/api/key/) with the account that will submit the add-on. For a version update, that account must already be an author of the add-on. Accept the developer agreement if AMO prompts for it.

Store both credentials in [pass](https://www.passwordstore.org/). If this is a new store, create a GPG key (`gpg --full-generate-key`) and initialize it with `pass init <GPG-key-fingerprint>`. Prefer a passphrase for interactive use. An unattended key without a passphrase lets any process running as your user decrypt the entries; use it only on a trusted account, with private GPG and password-store directories (`0700`) and a home directory other users cannot write to. Skip initialization if you already use pass. These commands prompt for the values without echoing them:

```bash
pass insert aiconfig/mozilla-addons/jwt-issuer
pass insert aiconfig/mozilla-addons/jwt-secret
```

The issuer is the API key; the secret is the API secret. Don't paste them in chat or place them in Git, shell arguments, or logs. The client reads just these entries and signs each request with a short-lived JWT. If pinentry can't find your terminal, run `export GPG_TTY=$(tty)` and `gpg-connect-agent updatestartuptty /bye` in an interactive terminal, then unlock the vault there. Verify credentials without uploading anything with `amo GET 'addons/addon/?size=1'`. Public lookup needs no credentials: `amo GET --public 'addons/search/?q=ublock&page_size=1'`.

## Submit a package

1. Confirm the user wants a new add-on or an update, and whether it should be `listed` (public on AMO) or `unlisted` (signed for self-distribution). Don't choose a channel or submit without authorization. Read the ZIP/XPI manifest without running the package; verify its version and Firefox ID (`browser_specific_settings.gecko.id`) against the intended target, and review permissions. For an update, check that the add-on is listed under the authenticated account and the version isn't already submitted. For a new listed add-on, gather listing name, summary, categories for each supported app, and version license; don't invent them. Follow Mozilla's [source submission rules](https://extensionworkshop.com/documentation/publish/source-code-submission/) for generated or minified builds.
2. Upload the original package to AMO, using the confirmed channel:
   ```bash
   amo POST addons/upload/ --field channel=listed --file upload=/path/to/addon.xpi
   ```
   Save the response `uuid`. Check `amo GET addons/upload/<uuid>/` until `processed` is true; proceed only if `valid` is true and `submitted` is false. Stop on validation failure and report the errors. If a request times out, check the upload/version state before repeating any POST.
3. Submit the validated upload. Replace `<addon>` with the URL-encoded AMO ID, slug, or Firefox ID. Put request bodies in local JSON files:
   - **New add-on:** `amo POST addons/addon/ --json new-addon.json`. For an unlisted add-on the body can be `{"version":{"upload":"<uuid>"}}`; for a listed add-on include `name`, `summary`, and `categories` at the top level and `license` inside `version` as documented by AMO.
   - **Existing add-on:** `amo POST addons/addon/<addon>/versions/ --json new-version.json`, with `{"upload":"<uuid>"}` plus any needed version metadata. An existing license may be inherited. To attach source during version creation, use multipart `--field upload=<uuid> --file source=/path/to/source.zip` (and `--field license=<slug>` if needed). For complex version metadata or a new add-on, use the documented version PATCH to attach the source archive after creation.
4. Check `amo GET addons/addon/<addon>/versions/<version-id>/`. For a version number without dots, use the `v` prefix to distinguish it from a numeric version ID. Report the add-on ID, version, channel, and observed review/signing status. Upload and validation are not publication; don't report a public listing or a signed artifact before AMO confirms it. Use the returned `file.url` for the signed package once available.

References: [AMO JWT authentication](https://mozilla.github.io/addons-server/topics/api/auth.html), [AMO add-on endpoints and payloads](https://mozilla.github.io/addons-server/topics/api/addons.html), [Firefox submission requirements](https://extensionworkshop.com/documentation/publish/submitting-an-add-on/).

## Delete an add-on

Deletion removes every version and permanently retires the Firefox ID. Only proceed with explicit authorization for the exact add-on. Fetch `addons/addon/<id>/delete_confirm/`, URL-encode its `delete_confirm` value, and within 60 seconds run `amo DELETE 'addons/addon/<id>/?delete_confirm=<encoded-token>'`. Verify that the old ID is absent from the authenticated add-on list.
