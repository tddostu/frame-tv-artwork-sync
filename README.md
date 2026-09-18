# Samsung Frame TV Artwork Sync

Keep one or more Samsung Frame TVs synchronized with a local artwork folder.
FrameTVSync runs quietly in Docker, uploads only what changed, preserves each
TV's slideshow settings, and leaves TVs alone while they are being watched.

**Docker image:** [turley/frame-tv-artwork-sync](https://hub.docker.com/r/turley/frame-tv-artwork-sync)

## Highlights

- Syncs JPEG and PNG artwork to multiple Frame TVs independently
- Tracks uploads so unchanged images are not sent again
- Preserves existing slideshow settings or applies an optional override
- Supports fixed or sunlight-based Art Mode brightness
- Can remove only its own missing artwork, or optionally enforce an exact mirror
- Optionally powers TVs on into Art Mode and turns them off only when safe
- Optional local HTTP control API for Homebridge and other controllers
- Keeps tokens in a persistent volume and handles pairing in the normal loop

## Quick start

1. Download [docker-compose.yml](docker-compose.yml).
2. Create the persistent folders:

   ```bash
   mkdir -p artwork tokens
   ```

3. Put your images in `artwork/`.
4. Set `TV_IPS` in `docker-compose.yml`.
5. Start the service:

   ```bash
   docker compose up -d
   ```

Approve `FrameTVArtworkSync` on each TV when prompted. Artwork authorization
works in Art Mode or normal viewing, and its token is saved under `tokens/`.

Scheduled power control uses a second Samsung permission. If auto-on or
auto-off is configured, leave the TV in normal TV/HDMI viewing once and approve
the additional IP Control request when it appears. The service defers that
request while the TV is in Art Mode.

## Common configuration

All settings are environment variables. The supplied compose file and
[`.env.example`](.env.example) contain ready-to-edit examples.

| Variable | Purpose | Default |
| --- | --- | --- |
| `TV_IPS` | Comma-separated TV addresses | required |
| `SYNC_INTERVAL_MINUTES` | Minutes between syncs | `5` |
| `MATTE_STYLE` | Artwork matte, such as `shadowbox_polar` | `none` |
| `SLIDESHOW_ENABLED` | Apply configured slideshow settings | unset |
| `SLIDESHOW_INTERVAL` | Model-supported interval in minutes | `15` |
| `SLIDESHOW_TYPE` | `shuffle` or `sequential` | `shuffle` |
| `BRIGHTNESS` | Fixed Art Mode brightness | unset |
| `REMOVE_UNKNOWN_IMAGES` | Remove TV artwork not in the local folder | `false` |
| `AUTO_ON_TIME` | Daily power-on time (`HH:MM`) | unset |
| `AUTO_OFF_TIME` | Daily Art Mode power-off time (`HH:MM`) | unset |
| `LOCATION_TIMEZONE` | Time zone for schedules and solar brightness | `UTC` |

See [Configuration](docs/configuration.md) for the complete reference,
including solar brightness, cleanup behavior, power scheduling, Wake-on-LAN,
connection tuning, image requirements, and matte styles.

## How syncing behaves

On each cycle, the service processes every TV independently:

1. Connect and confirm the TV is displaying Art Mode.
2. Compare local files with the service's per-TV upload record.
3. Upload new images and remove tracked images deleted locally.
4. Select artwork and restore or apply slideshow settings when content changed.
5. Apply configured brightness.

A TV that is off, unreachable, or showing an input is skipped without delaying
the others. Existing TV uploads that FrameTVSync does not recognize are kept by
default; enable `REMOVE_UNKNOWN_IMAGES` only if the TV should exactly mirror the
local folder.

## Artwork

- Formats: JPEG, JPG, and PNG
- Recommended aspect ratio: 16:9
- Recommended resolution: 3840×2160 for 43-inch and larger TVs; 1920×1080
  for 32-inch TVs
- Recommended color space: sRGB
- Maximum recommended size: 20 MB per file

Matte styles combine a style and color, for example `modern_apricot` or
`shadowbox_polar`. Use `none` for full-screen artwork. The complete style and
color lists are in [Configuration](docs/configuration.md#matte-styles).

## Useful commands

Preview a sync without changing artwork, settings, or power:

```bash
docker compose run --rm frame-tv-sync python sync_artwork.py --dry-run
```

Dry run still connects to the TVs. Missing permissions can therefore display an
approval prompt and save a token. Stop the normal service first if it is already
running, so the dry run does not open a competing Art connection.

Pair IP Control for one configured TV while it is in normal viewing:

```bash
docker compose run --rm frame-tv-sync \
  python sync_artwork.py --pair-ip-control 192.168.1.100
```

Preview a configured solar-brightness curve:

```bash
docker compose run --rm frame-tv-sync python sync_artwork.py --test-solar
```

Instructions for running directly with Python are in
[Configuration](docs/configuration.md#running-without-docker).

## Troubleshooting

Start with [Troubleshooting](docs/troubleshooting.md), which covers pairing,
separate Art and IP Control tokens, repeated prompts, skipped TVs, power-control
failures, multi-controller conflicts, and recovery of a wedged Art service.

| Symptom | First check |
| --- | --- |
| Repeated approval prompts | Close rather than deny the prompt, then check the saved Art token |
| IP Control never pairs | Put the TV in normal viewing rather than Art Mode |
| One TV times out while others work | Check its Art service and any other controller connected to it |
| TV is skipped as not in Art Mode | Check HDMI-CEC and the TV's Event Log for an input switch |
| Art connects but requests never answer | Stop competing Art clients and force-reboot the TV |
| Both token files disappeared | Check the `/tokens` volume mapping and permissions |

For interactive tests against a real TV, see [Diagnostics](diagnostics/README.md).

## Documentation

- [Configuration](docs/configuration.md)
- [Homebridge control](docs/homebridge.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Interactive diagnostics](diagnostics/README.md)

## Requirements

- Samsung Frame TV with Tizen OS
- Docker with Compose, or Python 3.10+
- Network access from the service to each TV

## Credits

Built with [samsung-tv-ws-api](https://github.com/NickWaterton/samsung-tv-ws-api)
by Nick Waterton.

This project was created with assistance from AI tools.

## License

[MIT](LICENSE)
