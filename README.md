# PushCloud for Home Assistant

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)
[![hassfest](https://github.com/PushCloudApp/home-assistant/actions/workflows/hassfest.yml/badge.svg)](https://github.com/PushCloudApp/home-assistant/actions/workflows/hassfest.yml)
[![HACS validation](https://github.com/PushCloudApp/home-assistant/actions/workflows/hacs.yml/badge.svg)](https://github.com/PushCloudApp/home-assistant/actions/workflows/hacs.yml)
[![Tests](https://github.com/PushCloudApp/home-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/PushCloudApp/home-assistant/actions/workflows/tests.yml)

Send [PushCloud](https://pushcloud.app) notifications from Home Assistant
automations.

## What you get

One `notify` service per PushCloud application. Set up the application called
*Home Assistant* and you get `notify.pushcloud_home_assistant`:

```yaml
action: notify.pushcloud_home_assistant
data:
  title: Washing machine
  message: Cycle finished.
```

Notifications land on every phone signed in to your PushCloud account, or on
just the ones you pick.

## Install

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PushCloudApp&repository=home-assistant&category=integration)

That button opens this repository inside HACS on your own Home Assistant. Press
**Download**, then restart Home Assistant.

<details>
<summary>By hand, if the button does not work</summary>

**HACS**: HACS → three-dot menu → **Custom repositories** → add
`https://github.com/PushCloudApp/home-assistant` with category **Integration** →
install **PushCloud** → restart.

**Without HACS**: copy `custom_components/pushcloud` into your
`config/custom_components/` and restart.

</details>

## Set up

1. In the [PushCloud panel](https://pushcloud.app/app), open the application
   you want Home Assistant to send as (or create one) and copy its
   **application token**, the string beginning `pca_`.
2. In Home Assistant: **Settings → Devices & services → Add integration →
   PushCloud**.
3. Paste the token. That is the whole form: the application's name comes back
   from PushCloud and titles the entry.

To send as a second application, add the integration again with that
application's token. Each entry gets its own notify service.

Two applications named the same thing would want the same service name, so the
one added later takes a short piece of its application id instead:
`notify.pushcloud_home_assistant_zzz999`. The name it ends up with is written to
the log as a warning. Whichever application was added first keeps the plain
name, so automations already written against it carry on working. Renaming
either application in the PushCloud panel and reloading gives both a plain name
again.

### Why a token and not an API key

The integration asks for an application token, which can do exactly three things:
send from its own application, say which application it is, and name your devices
so the picker below has something to show. It cannot read your messages, see a
device's push credentials, or change anything on your account. A PushCloud API
key (`pck_`) can do all of those, which is why this does not ask for one. Home
Assistant stores credentials in `.storage`, and what is stored here is worth very
little to anyone who gets it.

## Choose which devices get notified

By default a notification goes to every device on your PushCloud account. To
narrow it: **Settings → Devices & services → PushCloud → Configure**, then tick
the devices this application should reach.

The list comes from your account, so there is no device name to type and none to
get wrong. Ticking nothing means every device, which is what a new entry does.

Each entry has its own choice, so one application can go to your phone while
another goes to the tablet in the kitchen. An automation that needs to differ
just this once can name its own target with `device` under `data:`, which wins
over the setting:

```yaml
action: notify.pushcloud_home_assistant
data:
  message: The car is unlocked.
  data:
    device: bobbys-iphone
```

What gets stored is the device's **target name** - `bobbys-iphone` above - not the
device itself. Reinstalling the app or restoring a phone from a backup keeps that
name, so the setting survives the things you did not choose to do.

Renaming or deleting the device in PushCloud does change it, and sends then fail
until you reopen **Configure** and save. That is deliberately loud: the error in
the automation trace names the devices you actually have, which beats a
notification quietly going to the wrong phone or to none. Pinning the target name
in the PushCloud app makes a rename safe for good.

## Options

Everything under `data:` is optional.

| Key | What it does |
| --- | --- |
| `device` | Which devices this one notification goes to, by target name, comma-separated. Overrides the **Configure** setting above. |
| `priority` | `-2` to `2`. `-2`/`-1` arrive quietly, `0` is normal, `1` is time-sensitive and can break through a focus mode, `2` is critical and can sound when the phone is silenced. |
| `sound` | The notification sound, by name. |
| `url` | A link to attach to the notification. |
| `url_title` | The text for that link. |

```yaml
action: notify.pushcloud_home_assistant
data:
  message: Water detected under the sink.
  title: Leak
  data:
    priority: 2
    url: http://homeassistant.local:8123/lovelace/leaks
    url_title: Open the dashboard
```

**`priority: 2` is Pro-only.** On a free account the send fails outright with the
server's explanation rather than being quietly downgraded, so the automation
trace tells you what happened.

Unknown `data:` keys are ignored and logged at debug level, so a typo does not
break an automation.

## How it works

HACS is Home Assistant's community store. It reads a GitHub repository's
releases (or its default branch), copies the folder under
`custom_components/` into your Home Assistant configuration, and tells you when
a newer release is published. It installs files; Home Assistant does the rest
after a restart. `hacs.json` at the root is what makes this repository
installable that way.

Inside `custom_components/pushcloud/`:

| File | Role |
| --- | --- |
| `manifest.json` | Declares the domain, version and that the integration is set up through the UI. |
| `config_flow.py` | The add-integration form and the re-authentication prompt. Both validate a token against `GET /v1/applications/me`, which is how a send-only token can name itself without notifying anybody. Adding an application already configured is rejected as a duplicate. Also the **Configure** form, which lists the account's devices from `GET /v1/applications/me/devices` so the target is picked rather than typed. |
| `__init__.py` | Sets an entry up: builds the API client, confirms the token, follows a rename made in the PushCloud panel, and loads that entry's notify service. Unloading takes the service away again without disturbing the other entries. |
| `api.py` | The whole of the PushCloud HTTP surface: one call to identify the application and one to send a message, made over Home Assistant's shared aiohttp session. Server error messages are passed through so they reach the automation trace intact. |
| `notify.py` | The notify service. Turns a service call into a `POST /v1/messages` body. |
| `const.py` | Domain and attribute names. |
| `strings.json`, `translations/` | The UI text for the config flow. |
| `brand/` | The icon Home Assistant shows for the integration, at 256px and 512px. Used from 2026.3 onwards; older versions fall back to the placeholder icon. |

A rejected token during normal running starts a re-authentication flow rather
than failing silently; anything else retryable, such as an outage or a suspended
account, leaves Home Assistant to retry the entry on its own schedule.

## Development

Home Assistant needs Python 3.13.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r tests/requirements_test.txt
.venv/bin/python -m pytest
```

The test suite covers the API client, the config, re-auth and options flows,
entry setup and unload, and the notify service. CI runs it alongside
[hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest/) and
HACS validation on every push.

## Troubleshooting

**"That application token was not accepted"** - the token has been rotated, or
belongs to a deleted application. Copy the current one from the application's
page.

**Notifications succeed but nothing arrives** - no device is signed in. Install
the PushCloud app and sign in with the same account.

**"No device named ..."** - the device chosen under **Configure** was renamed or
deleted in PushCloud. Reopen **Configure** and save; the error message also lists
the devices the account does have.

**"This PushCloud account has no devices yet"** on **Configure** - nothing has
signed in, so there is nothing to choose between. Notifications still work; they
go to every device that signs in later.

**The service disappeared after a restart** - PushCloud was unreachable at
startup. Home Assistant retries on its own; check the entry under Settings →
Devices & services.

To check a token by hand:

```bash
curl https://pushcloud.app/v1/applications/me \
  -H "Authorization: Bearer pca_your_application_token"
```

That answers with the application the token belongs to, or `401` if it is not a
live token.

## Not in this version

Actionable notifications, attachments, scheduled sends, end-to-end encrypted
sends, and a self-hosted base URL.

## Licence

MIT.
