# Homebridge control

FrameTVSync can expose a small local HTTP API so Homebridge (or any other local
controller) can power a TV on and off. It reuses the same guard as the scheduled
power control:

- **On** wakes a fully-off TV and places it into Art Mode. A TV that is already
  on, in Art Mode or showing an input, is left untouched.
- **Off** only fires when the TV positively reports Art Mode. A TV being watched
  is refused.

The API runs inside the sync process and is serialized with the sync loop, so it
never opens a second Art connection that could knock the sync service off the TV.

## Endpoints

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | `{"status":"ok"}` |
| `GET` | `/tv/<ip>/status` | `{"ip":"<ip>","state":"art"\|"on"\|"off"\|"unknown"}` |
| `GET` | `/tv/<ip>/art` | `1` when in Art Mode, otherwise `0` (plain text) |
| `POST` | `/tv/<ip>/on` | Wake to Art Mode, or no-op if already on |
| `POST` | `/tv/<ip>/off` | Power off if in Art Mode |

Status codes: `200` success, `404` unknown TV, `409` refused because the TV is
showing content, `502` TV unreachable or the action failed, `504` timed out.

`state` is `art` when the TV is powered on in Art Mode, `on` when powered on
with an input active, `off` in standby, and `unknown` if it cannot be
determined. Art status is refreshed on every connect and keepalive (default
every 60s); the power state is read live from IP Control on each request.

## 1. Enable the API

Set these in the sync service's environment (the supplied `docker-compose.yml`
enables them by default):

```yaml
CONTROL_API_ENABLED: "true"
CONTROL_API_PORT: "8080"
```

The supplied `docker-compose.yml` also publishes the API on the host loopback
(`127.0.0.1:8080`), which is what a host-networked Homebridge uses; see below.
Start the service:

```bash
docker compose up -d --build
```

## 2. Let Homebridge reach the API

Pick the option that matches how your Homebridge container is networked.

### Homebridge with `network_mode: host`

A host-networked container shares the host's network, so it cannot join a
Docker network. Use the loopback publish, which the supplied compose already
configures:

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

Homebridge then reaches the API at `http://127.0.0.1:8080`. Binding to
`127.0.0.1` keeps it off the LAN.

### Homebridge on a bridge network

Join the shared `frame-control` network from your Homebridge Compose project:

```yaml
services:
  homebridge:
    # ... existing config ...
    networks:
      - frame-control

networks:
  frame-control:
    external: true
```

Create the network once (starting the sync service also creates it):

```bash
docker network create frame-control
```

Then reach the service by name: `http://frame-tv-sync:8080`.

## 3. Configure Homebridge

Install the two community plugins:

```bash
npm install -g homebridge-http-switch homebridge-http-contact-sensor
```

### Power switch

```json
{
  "accessory": "HTTP-SWITCH",
  "name": "Frame TV",
  "switchType": "stateful",
  "pullInterval": 15000,
  "statusPattern": "\"state\":\\s*\"(art|on)\"",
  "onUrl": {
    "url": "http://127.0.0.1:8080/tv/192.168.1.100/on",
    "method": "POST",
    "requestTimeout": 60000
  },
  "offUrl": {
    "url": "http://127.0.0.1:8080/tv/192.168.1.100/off",
    "method": "POST",
    "requestTimeout": 60000
  },
  "statusUrl": {
    "url": "http://127.0.0.1:8080/tv/192.168.1.100/status",
    "method": "GET"
  }
}
```

`requestTimeout` is raised above the plugin's 20s default because a cold
power-on plus the Art Mode transition can take longer.

### Art Mode indicator

```json
{
  "accessory": "ContactSensor",
  "name": "Frame TV Art Mode",
  "pollInterval": 15000,
  "statusUrl": "http://127.0.0.1:8080/tv/192.168.1.100/art"
}
```

The sensor is closed (`1`) while the TV is in Art Mode and open (`0`)
otherwise. Together with the switch this distinguishes all three states: off,
Art Mode, and content.

Add one switch and one sensor per TV, replacing `192.168.1.100` with each TV's
address as configured in `TV_IPS`. The examples use the host-loopback address
for a host-networked Homebridge; a bridge-networked Homebridge should swap
`127.0.0.1` for `frame-tv-sync`.

## Testing

From the Docker host (the same address a host-networked Homebridge uses):

```bash
curl -s http://127.0.0.1:8080/tv/192.168.1.100/status
```

From a bridge-network container:

```bash
docker run --rm --network frame-control curlimages/curl -s \
  http://frame-tv-sync:8080/tv/192.168.1.100/status
```

Or from the sync container itself:

```bash
docker exec frame-tv-sync python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

## Behaviour notes

- Commands are queued and executed by the sync loop. They normally run within
  milliseconds, but a request that arrives while a sync cycle is in flight waits
  for that cycle to reach its wait phase (typically seconds).
- `POST .../off` returns `409` and leaves the TV on when it is showing content.
  The HomeKit switch returns to On on the next poll.
- Waking a TV from standby into Art Mode can take several seconds while the TV's
  Art channel comes up.
