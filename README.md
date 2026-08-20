# LastRecommendations

**LastRecommendations** is a small self-hosted Docker application that turns your personalized Last.fm artist recommendations into a Lidarr Custom Import List using MusicBrainz Artist IDs (MBIDs).

It also keeps a permanent, browsable history of every unique artist Last.fm has recommended to you.

> This project is not affiliated with Last.fm, MusicBrainz, or Lidarr.

## What it does

On container startup, and then every configured refresh interval, LastRecommendations:

1. Authenticates to `https://www.last.fm/home/artists` using your existing Last.fm browser session cookie.
2. Scrapes **only the actual recommended artist** from each recommendation card. Artists shown under Last.fm's `Similar to ...` text are intentionally ignored.
3. Resolves each recommended artist to a MusicBrainz Artist ID (MBID).
4. Uses conservative matching: unique exact artist-name/alias matches are accepted; ambiguous names can be disambiguated using the exact Last.fm artist URL relationship; unresolved artists are omitted rather than guessed.
5. Caches successful MBID mappings so the same artist does not need to be repeatedly resolved through MusicBrainz.
6. Publishes the current recommendations as JSON in the format Lidarr expects for a Custom Import List.
7. Adds every recommendation to a persistent **All History** database so artists remain visible even after Last.fm stops recommending them.

The default refresh interval is **25 hours**. A refresh also runs immediately whenever the container starts.

## Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Main dashboard showing the **current** Last.fm recommendations, MBID resolution status, MusicBrainz status, and refresh information. |
| `GET` | `/history` | **All History** page. Shows every unique artist ever recorded as a recommendation, with cover art, first/last seen dates, total count, and adjustable pagination. Default: 20 artists per page. |
| `GET` | `/history.json` | Full all-time history as JSON. Useful for backup, inspection, or other integrations. |
| `GET` | `/lidarr.json` | **Primary Lidarr Custom List URL.** Returns an array of `{ "MusicBrainzId": "..." }` objects for the current recommendations. |
| `GET` | `/mbids.json` | Bare JSON array containing only current MBID UUID strings. |
| `GET` | `/details.json` | Full diagnostic data for every current recommendation, including Last.fm URL, image, MBID, match method, and any match error. |
| `GET` | `/healthz` | Machine-readable application health/status endpoint. |
| `GET` | `/musicbrainz-test?artist=Electric%20Guest` | Runs a live MusicBrainz search for the supplied artist. Useful for troubleshooting MusicBrainz connectivity or matching. |
| `POST` | `/refresh` | Runs an immediate Last.fm scrape and MusicBrainz resolution. The dashboard's **Refresh now** button uses this endpoint. |

The application also stores the most recently fetched Last.fm HTML at `./data/lastfm-debug.html` for troubleshooting if Last.fm changes its page markup.

## Requirements

- Docker Engine on a 64-bit `amd64`/`x86_64` or `arm64` host
- Docker Compose (`docker compose`)
- A Last.fm account with personalized artist recommendations
- A valid logged-in Last.fm browser session cookie
- Network access from the container to `last.fm` and `musicbrainz.org`
- Lidarr, if you want to use the generated Custom Import List

## Docker Compose configuration

Create a directory for LastRecommendations, then create a `docker-compose.yml` inside it using the example below.

```yaml
services:
  lastrecommendations:
    image: ghcr.io/fantasymaster85/lastrecommendations:latest
    container_name: lastrecommendations
    restart: unless-stopped
    init: true

    environment:
      TZ: America/New_York
      PORT: "9654"
      DATA_DIR: /app/data
      REFRESH_HOURS: "25"

      LASTFM_COOKIE: >-
        csrftoken=PASTE_YOUR_COOKIE_HERE; sessionid=PASTE_YOUR_SESSION_COOKIE_HERE

      LASTFM_USER_AGENT: >-
        Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36
        (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36

      MUSICBRAINZ_USER_AGENT: >-
        LastRecommendations/1.0 (https://github.com/FantasyMaster85/LastRecommendations)

    volumes:
      - ./data:/app/data

    ports:
      - "9654:9654"

    healthcheck:
      test:
        - CMD
        - python
        - -c
        - >-
          import os, urllib.request;
          urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '9654') + '/healthz', timeout=5)
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 120s
```

A copy of this configuration is included in the repository as `docker-compose.example.yml`.

### Configuration reference

| Option | Default | What it does / how to set it |
| --- | --- | --- |
| `image` | `ghcr.io/fantasymaster85/lastrecommendations:latest` | Prebuilt LastRecommendations image. |
| `container_name` | `lastrecommendations` | Friendly Docker container name. Normally leave unchanged. |
| `restart` | `unless-stopped` | Restarts the container automatically unless you intentionally stop it. |
| `init` | `true` | Adds a tiny init process for cleaner signal/process handling. |
| `TZ` | `America/New_York` | Container timezone used for logs/display. Use an IANA timezone such as `America/Chicago` or `Europe/London`. |
| `PORT` | `9654` | Port the web app listens on **inside** the container. If changed, also change the container side of the `ports` mapping. |
| `DATA_DIR` | `/app/data` | Internal persistent-data directory. Normally leave unchanged. |
| `REFRESH_HOURS` | `25` | Hours between automatic recommendation refreshes. A refresh still runs immediately at startup. Decimal values are supported. |
| `LASTFM_COOKIE` | none | **Required.** Paste the complete authenticated Last.fm `Cookie` request-header value here. |
| `LASTFM_USER_AGENT` | browser-like Chrome UA | User-Agent sent to Last.fm. Normally leave unchanged. |
| `MUSICBRAINZ_USER_AGENT` | LastRecommendations identifier | Identifies this application to MusicBrainz. Normally leave unchanged. |
| `volumes` | `./data:/app/data` | Persists current recommendations, all-time history, MBID cache, and diagnostic data across container updates/restarts. |
| `ports` | `9654:9654` | Publishes the web UI and JSON endpoints on the Docker host. Left side = host port; right side = container `PORT`. |
| `healthcheck` | included | Uses `/healthz` so Docker can report whether the application has completed a successful refresh. |

### Getting your Last.fm cookie

1. Log into Last.fm in your normal browser.
2. Open `https://www.last.fm/home/artists`.
3. Open Developer Tools (`F12`) and select **Network**.
4. Reload the page.
5. Select the request for `/home/artists` or another authenticated `www.last.fm` request.
6. Under **Request Headers**, locate `Cookie`.
7. Copy the **entire Cookie header value**, not only `sessionid`.
8. Paste it under `LASTFM_COOKIE` in your `docker-compose.yml`.

The YAML `>-` block makes it easy to paste the full cookie without a separate `.env` file.

Treat this cookie like a password: it contains a logged-in session credential.

## How to run it

Navigate to the directory containing your completed `docker-compose.yml` and pull the current image:

```bash
docker compose pull
```

Start the container:

```bash
docker compose up -d
```

Watch the first scrape and MusicBrainz resolution:

```bash
docker compose logs -f
```

Open the dashboard:

```text
http://YOUR-SERVER-IP:9654/
```

Open all-time history:

```text
http://YOUR-SERVER-IP:9654/history
```

### Configure Lidarr

In Lidarr, add a **Custom List** and use:

```text
http://YOUR-SERVER-IP:9654/lidarr.json
```

If Lidarr itself runs in Docker, remember that `localhost` means the **Lidarr container**, not the Docker host. Use an address Lidarr can actually reach, such as the Docker host's LAN IP, or connect both applications to a shared Docker network.

## Updating

From the directory containing `docker-compose.yml`:

```bash
docker compose pull
docker compose up -d
```

The `./data` volume remains in place, so recommendation history and cached MBIDs survive image updates.

## Persistent data and All History

Runtime data is stored in `./data` on the Docker host.

`state.json` contains the current recommendation set, MusicBrainz cache, and the permanent all-time history. Each unique history entry records information such as:

- artist name and Last.fm URL
- artwork URL when available
- MusicBrainz Artist ID when resolved
- first time the artist was recommended
- most recent time the artist was recommended
- number of refreshes in which the artist appeared

The `/history` page is sorted newest-first and defaults to **20 artists per page**. The page-size selector supports 10, 20, 50, or 100 artists per page.

If upgrading from an older version that already has a `state.json`, the currently stored recommendation set is used to seed All History. Older `seen` records are intentionally not bulk-imported because early scraper versions could contain `Similar to` artists that were not true recommendations. From the first run of this version onward, every actual recommendation is retained permanently.

## Manual refresh

Use the **Refresh now** button on the dashboard, or:

```bash
curl -X POST http://localhost:9654/refresh
```

This does not change the normal automatic schedule.

## Troubleshooting

**Last.fm redirects away from `/home/artists`**  
Your Last.fm browser session has probably expired. Copy a fresh Cookie header into `docker-compose.yml`, then recreate the container:

```bash
docker compose up -d --force-recreate
```

**MusicBrainz IDs are not resolving**  
Open:

```text
http://YOUR-SERVER-IP:9654/musicbrainz-test?artist=Electric%20Guest
```

The response shows whether the container can reach MusicBrainz and which candidates were returned. The main dashboard also displays the last MusicBrainz HTTP/error status.

**Too many artists appear**  
The scraper intentionally selects only Last.fm recommendation-title links and ignores artists listed under `Similar to`. If Last.fm changes its HTML structure, inspect `./data/lastfm-debug.html` and the container logs.

**Lidarr cannot reach the list URL**  
Confirm the host port is published, your firewall permits the connection, and a Dockerized Lidarr instance is not trying to use its own `localhost`.

## Screenshots
Homepage:
<img width="1379" height="1060" alt="Homepage - Current Recommendations" src="https://github.com/user-attachments/assets/1e1ffeda-ac25-4612-bb3e-06557f792374" />

Lidarr JSON Output for use with "custom list" in the Lidarr "Import Lists" setup:
<img width="453" height="533" alt="lidarr json output" src="https://github.com/user-attachments/assets/e338eae5-4a5e-441f-b549-8b0305d0594c" />

All History page:
<img width="1333" height="1061" alt="all history page" src="https://github.com/user-attachments/assets/e9503171-1610-4b41-b53b-8f587b79d325" />


## License

MIT. See [LICENSE](LICENSE).
