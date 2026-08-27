# Troubleshooting

Frame TVs vary considerably by model year and firmware. Start with the symptom,
avoid deleting tokens unless the TV explicitly rejects one, and change only one
controller at a time when isolating a connection problem.

## First checks

1. Confirm the TV still has its expected IP address and responds from the
   Docker host.
2. Confirm both `/artwork` and `/tokens` are mapped to persistent, writable
   folders.
3. Check whether the TV is in Art Mode, normal viewing, or true standby.
4. Note whether another application—especially Home Assistant—is also connected
   to the TV's Art channel.
5. Use `LOG_LEVEL=DEBUG` only while collecting a focused trace; debug output is
   noisy and may contain detailed TV responses.

Close an unexpected TV approval dialog rather than selecting **Deny**. Samsung
TVs may remember a denial and silently reject future legitimate attempts. If
that happens, remove only the denied `FrameTVArtworkSync` entry from the TV's
external-device or device-connection list.

## Understanding the two permissions

FrameTVSync can store two independent credentials per TV:

| Permission | Used for | File |
| --- | --- | --- |
| Art/WebSocket | Artwork, Art Mode, slideshow, brightness | `tv_<address_with_underscores>.txt` |
| IP Control | Hardware power-on and power-off | `tv_<address_with_underscores>_ip_control.json` |

The Art token can be approved in Art Mode or normal viewing. IP Control pairing
requires normal TV/HDMI viewing and is requested only when auto-on or auto-off is
configured.

The files have separate lifecycles. An explicit Art authorization rejection
removes only the Art token; an IP Control authentication rejection removes only
the IP token. If both disappear together, investigate the `/tokens` volume,
container replacement, or an external cleanup process rather than TV protocol
handling.

## Repeated or unexpected approval prompts

The Art client does not initiate pairing merely because it was constructed.
Pairing occurs explicitly on the remote-control channel when no Art token exists
or during one guarded stale-token recovery.

Samsung's `ms.channel.timeOut` is ambiguous: it can mean an invalid token, a
slow handshake, a busy TV service, or a transient channel failure. FrameTVSync
therefore preserves the existing token. If the TV is positively awake, it may
make one guarded refresh request without the old token:

- Approving it replaces the old token only after the TV issues a new one.
- Ignoring or closing it leaves the old token untouched.
- A very fast TV-side close gets one delayed final request, allowing time for a
  human approval without producing a stream of prompts.
- After an unsuccessful guarded refresh, no more refresh prompts are made until
  the service restarts or a later Art connection succeeds.

If prompts mention another controller, stop one controller and test the other
in isolation. Repeatedly approving alternating clients can rotate authorization
and create a confusing prompt loop.

## Pairing was approved but no token was saved

Check the container logs and the `/tokens` mount:

- The token directory must be writable by the container.
- A read-only bind mount, NAS permission mapping, or SELinux label can allow the
  TV approval while preventing persistence.
- The Art token is issued on the remote-control channel. An approval associated
  only with an Art-channel connection has not been observed issuing a token.

FrameTVSync checks token-directory writability at startup. If existing tokens
are readable but new ones cannot be saved, it continues with the existing files
and logs that re-pairing will fail.

## IP Control does not pair

Put the TV in normal viewing—TV, HDMI, or an app—not Art Mode. Tested newer
Frames may accept the connection in Art Mode yet never return an access token.
The normal sync loop defers IP Control pairing until it observes normal viewing.

To pair one configured TV deliberately:

```bash
docker compose run --rm frame-tv-sync \
  python sync_artwork.py --pair-ip-control 192.168.1.100
```

A `-32700 Parse error` from an otherwise valid IP Control request has been
observed when newer Frames reject a stale access token. FrameTVSync treats it as
an authentication failure only when a token was actually sent. It removes only
the IP Control token and requests authorization during a later normal-viewing
sync.

## Art-channel handshake errors

The following events do not all mean the same thing:

- `ms.channel.unauthorized` is an explicit token rejection.
- `ms.channel.timeOut` is ambiguous and does not by itself justify deleting a
  saved token.
- `ms.channel.clientDisconnect`, `art_app_request`, and
  `d2d_service_message` can be broadcasts associated with another client.

Some TVs broadcast another client's event before the new socket's own
`ms.channel.connect`. Newer Frames may also omit the later `ms.channel.ready`
event. FrameTVSync tolerates those broadcasts and accepts `ms.channel.connect`
as the completed handshake.

Each TV has a bounded retry budget. One failing or pairing TV is processed
independently and cannot prevent later TVs from syncing or reaching scheduled
auto-off.

## Art connects but requests do not answer

The TV's Art service can become saturated or wedged even though:

- the display is visibly in Art Mode;
- the TV answers ping;
- port 8002 accepts a WebSocket; and
- the saved token completes the initial handshake.

Multiple persistent `com.samsung.art-app` clients increase this risk. Home
Assistant integrations and other Frame tools may keep their own Art connection
open, and some TVs route request responses unpredictably between clients.

To recover:

1. Stop FrameTVSync and temporarily disable the other Art controller for that
   TV.
2. Force-reboot the TV by holding the physical remote's power button until the
   startup logo appears. Normal standby or an ordinary off/on transition may
   not restart the Art service.
3. Wait for the network services. The display can return to Art Mode while
   ports 8001, 8002, and 1516 are still starting.
4. Test one controller in isolation before re-enabling another.

If the problem returns only when both controllers are enabled, avoid running
two persistent Art clients for that TV. Sharing an IP Control token does not
share an Art WebSocket connection or prevent Art-channel contention.

## TV is skipped as not in Art Mode

`Skipping TV <address> - not in art mode (may be in use)` means the TV answered
the status request and reported Art Mode off. `PowerState` alone cannot
distinguish Art Mode from HDMI:

| TV state | REST `PowerState` | `get_artmode_status` |
| --- | --- | --- |
| Art Mode | often `on`, sometimes `standby` | `on` |
| HDMI or normal viewing | `on` | `off` |
| True standby | often `standby` | unavailable or `off` |

A common cause is HDMI-CEC. An attached device asserting itself as the active
source pulls the TV out of Art Mode. On many TVs this appears under **Settings →
Support → About This TV → Event Log** as a CEC source event. Compare its time
with the sync log; container logs may use UTC unless `TZ` is configured.

If the TV visibly shows artwork but repeatedly reports Art Mode off, close any
approval prompt, isolate other Art clients, and retry. A wedged Art service may
require the force-reboot procedure above.

## Scheduled power does not work

### Auto-off opens HDMI instead of powering off

This is how some Frames interpret a held `KEY_POWER` command from Art Mode.
FrameTVSync prefers explicit IP Control and uses the legacy three-second key
hold only after a genuine network transport failure. It does not use the
fallback for missing/rejected authorization or protocol errors.

Confirm that the per-TV IP Control JSON file exists and that the TV accepted
that permission while in normal viewing.

### Auto-on does not run

Check:

- `AUTO_ON_TIME` and `LOCATION_TIMEZONE`;
- the IP Control token;
- WOL broadcast routing if deep standby closes port 1516; and
- whether the service already made today's attempt.

Auto-on is intentionally attempted once during the scheduled window. Its
once-per-day record is in memory, so a service restart during that window can
permit one additional attempt.

## TV appears off but port 8002 responds

Frame TVs can leave REST and WebSocket services reachable in standby. Conversely,
those services can take time to return after a visible reboot. Reachability is
therefore not a reliable power-state test by itself. Scheduled power uses IP
Control's semantic power state where available.

## Collecting diagnostics

Set `LOG_LEVEL=DEBUG`, reproduce one cycle, then return to `INFO`. Redact tokens,
public IP addresses, device identifiers, and personal paths before sharing a
log publicly.

The scripts in [`diagnostics/`](../diagnostics/README.md) provide focused tests:

- `artmode_watch.py` observes Art Mode decisions over one reused connection.
- `token_prompt_test.py` intentionally exercises invalid-token behavior and can
  display TV approval prompts.

These scripts contact real TVs and are not part of the automated test suite.
