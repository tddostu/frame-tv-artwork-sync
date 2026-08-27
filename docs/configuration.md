# Configuration

FrameTVSync is configured entirely through environment variables. For Docker
Compose, set them under `services.frame-tv-sync.environment` in
[`docker-compose.yml`](../docker-compose.yml). For direct Python use, start from
[`.env.example`](../.env.example).

Most installations need only `TV_IPS` and `SYNC_INTERVAL_MINUTES`. The remaining
settings enable specific features or tune recovery for unusual TVs.

## Essential settings

| Variable | Description | Default |
| --- | --- | --- |
| `TV_IPS` | Comma-separated TV IP addresses | required |
| `SYNC_INTERVAL_MINUTES` | Minutes between sync cycles | `5` |
| `ARTWORK_DIR` | Folder containing local artwork | `/artwork` |
| `TOKEN_DIR` | Persistent tokens and per-TV tracking data | `/tokens` |
| `CLIENT_NAME` | Name shown in the TV's connected-device list | `FrameTVArtworkSync` |
| `LOG_LEVEL` | Python logging level, such as `INFO` or `DEBUG` | `INFO` |

The artwork and token paths normally remain unchanged in Docker. Persist
`TOKEN_DIR`; otherwise every container replacement requires pairing again and
loses the record of which images FrameTVSync manages.

## Pairing and tokens

Artwork and scheduled power control use different Samsung interfaces:

- **Art/WebSocket authorization** permits artwork operations. It can be
  approved in Art Mode or normal viewing and is stored as
  `tv_<address_with_underscores>.txt` (for example,
  `tv_192_168_1_100.txt`).
- **IP Control authorization** powers the hardware on and off. It is requested
  only when `AUTO_ON_TIME` or `AUTO_OFF_TIME` is configured and is stored as
  `tv_<address>_ip_control.json`.

IP Control pairing must happen while the TV is in normal TV/HDMI viewing. Some
newer Frames accept the connection in Art Mode but never return a token, so the
normal loop defers the request until it observes normal viewing.

If Art Mode status cannot be determined, FrameTVSync may make one IP Control
request because some TVs do not answer the Art status check while showing an
input. A failed request does not block artwork sync or the other TVs.

The Art client never starts pairing implicitly. FrameTVSync uses the remote
control channel for explicit Art-token acquisition, then opens the separate Art
channel with the saved token.

## Slideshow

| Variable | Description | Default |
| --- | --- | --- |
| `SLIDESHOW_ENABLED` | Enables the slideshow override when explicitly set | unset |
| `SLIDESHOW_INTERVAL` | Interval in minutes supported by the TV model | `15` |
| `SLIDESHOW_TYPE` | `shuffle` or `sequential` | `shuffle` |

With no slideshow variables set, FrameTVSync preserves the TV's current
slideshow settings. If any of the three variables is set, the others use their
defaults and the complete override is applied.

Slideshow settings are touched only when artwork is uploaded or removed. Common
intervals include 3, 15, 60, 720, and 1440 minutes, but the valid values vary by
model year.

## Brightness

### Fixed brightness

Set `BRIGHTNESS` to a value supported by the TV. Depending on the model, the
range is commonly 0–10 or 0–50. The value is applied on every sync.

### Solar brightness

| Variable | Description | Default |
| --- | --- | --- |
| `SOLAR_BRIGHTNESS_ENABLED` | Enable sunlight-based brightness | unset |
| `LOCATION_LATITUDE` | Latitude in decimal degrees | unset |
| `LOCATION_LONGITUDE` | Longitude in decimal degrees | unset |
| `LOCATION_TIMEZONE` | IANA time zone name | `UTC` |
| `BRIGHTNESS_MIN` | Brightness below the horizon | `2` |
| `BRIGHTNESS_MAX` | Brightness at maximum modeled sunlight | `10` |

Solar brightness takes precedence over `BRIGHTNESS`. It uses sun elevation and
an atmospheric air-mass model, producing gradual changes rather than a simple
day/night switch.

```yaml
SOLAR_BRIGHTNESS_ENABLED: "true"
LOCATION_LATITUDE: "42.3601"
LOCATION_LONGITUDE: "-71.0589"
LOCATION_TIMEZONE: "America/New_York"
BRIGHTNESS_MIN: "2"
BRIGHTNESS_MAX: "10"
```

Preview the curve before enabling it:

```bash
docker compose run --rm frame-tv-sync python sync_artwork.py --test-solar
```

## Image cleanup

`REMOVE_UNKNOWN_IMAGES=false` is the safe default. FrameTVSync removes only
images it previously uploaded and that are no longer present locally. Images
uploaded manually or before tracking began remain on the TV.

Set `REMOVE_UNKNOWN_IMAGES=true` to make the TV's uploaded-art collection match
the local folder exactly. Unknown TV content is then eligible for deletion.

## Scheduled power

Power scheduling requires `LOCATION_TIMEZONE` and the additional IP Control
authorization described above.

| Variable | Description | Default |
| --- | --- | --- |
| `AUTO_ON_TIME` | Daily power-on time in `HH:MM` | unset |
| `AUTO_OFF_TIME` | Daily power-off time in `HH:MM` | unset |
| `AUTO_OFF_GRACE_HOURS` | Time after auto-off to keep checking | `2` |
| `POWER_ON_VERIFY_TIMEOUT` | Seconds to wait for TV services after power-on | `30` |
| `WOL_BROADCAST_ADDRESS` | Broadcast address for the WOL fallback | `255.255.255.255` |
| `WOL_PORT` | Wake-on-LAN UDP port | `9` |
| `WOL_REPEAT` | Number of WOL packets | `3` |

```yaml
AUTO_ON_TIME: "07:00"
AUTO_OFF_TIME: "22:00"
AUTO_OFF_GRACE_HOURS: "2"
LOCATION_TIMEZONE: "America/New_York"
```

### Auto-on behavior

At the scheduled time, FrameTVSync makes one attempt for that calendar day. A
TV that is already on is not changed. A TV started by the schedule is returned
to Art Mode even if it wakes to its last HDMI input.

The once-per-day record is in memory. Restarting the service during the short
auto-on attempt window permits another attempt that day.

IP Control is attempted first. Wake-on-LAN is used only when deep standby makes
IP Control temporarily unreachable; after WOL, FrameTVSync retries the explicit
IP power command.

### Auto-off behavior

Auto-off acts only when Art Mode is positively confirmed. A TV showing HDMI or
normal content is left alone, and each TV proceeds independently so one pairing
or connection problem cannot delay another TV's power-off.

Explicit IP Control is preferred. The legacy three-second `KEY_POWER` hold is
used only after a genuine IP Control transport failure. It is not used when the
permission is missing or rejected, or when the TV returns a protocol error,
because some Frames interpret the key hold as leaving Art Mode for HDMI rather
than powering off.

## Connection tuning

Defaults are appropriate for most installations.

| Variable | Description | Default |
| --- | --- | --- |
| `CONNECTION_TIMEOUT` | Overall Art handshake/status deadline with a saved token | `10.0` |
| `AUTH_TIMEOUT` | Approval window for a pairing request | `30.0` |
| `KEEPALIVE_INTERVAL` | Seconds between Art-channel keepalives | `60` |
| `CONNECT_MAX_ATTEMPTS` | Normal connection attempts per cycle | `3` |
| `CHANNEL_DROP_RETRY_DELAY` | Delay between connection attempts | `3.0` |
| `PAIRING_MAX_RETRIES` | First-time approval attempts | `5` |
| `PAIRING_RETRY_DELAY` | Delay between approval attempts | `5.0` |
| `API_TIMEOUT` | General Art request timeout | `20` |
| `CONTENT_LIST_TIMEOUT` | Uploaded-content-list timeout | `45` |

Large TV libraries can make `get_content_list` unusually slow. A collection of
roughly 500 images may take around 10 seconds and return a response approaching
750 KB. If logs report `Failed to get uploaded images from TV`, increase
`CONTENT_LIST_TIMEOUT`; FrameTVSync skips that cycle rather than treating a
timeout as an empty TV and re-uploading everything.

Keepalive reduces reconnects between syncs. When another application also keeps
an Art connection open, however, some TVs may misroute responses or wedge their
Art service. See [Troubleshooting](troubleshooting.md#art-connects-but-requests-do-not-answer).

## Image requirements

- JPEG, JPG, or PNG
- 16:9 aspect ratio recommended
- 3840×2160 recommended for 43-inch and larger TVs
- 1920×1080 recommended for 32-inch TVs
- sRGB color space
- Under 20 MB per image recommended

## Matte styles

Set `MATTE_STYLE` to `none` (the default) for no border, or combine a style and
color as `{style}_{color}`.

**Styles:** `modernthin`, `modern`, `modernwide`, `flexible`, `shadowbox`,
`panoramic`, `triptych`, `mix`, `squares`

**Colors:** `black`, `neutral`, `antique`, `warm`, `polar`, `sand`, `seafoam`,
`sage`, `burgandy`, `navy`, `apricot`, `byzantine`, `lavender`, `redorange`,
`skyblue`, `turquoise`

Examples: `shadowbox_polar`, `modern_apricot`, `flexible_antique`.

## Running without Docker

Python 3.10 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p artwork tokens
```

Set `ARTWORK_DIR=./artwork` and `TOKEN_DIR=./tokens`, load the environment, and
run:

```bash
set -a
source .env
set +a
python sync_artwork.py
```

Additional modes:

```bash
python sync_artwork.py --dry-run
python sync_artwork.py --test-solar
python sync_artwork.py --pair-ip-control 192.168.1.100
```

Dry run prevents artwork, setting, and power changes, but connection
authorization remains active and can save missing tokens.
