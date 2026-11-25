# runout

**Scrobble your records.**

runout is a small daemon for a Raspberry Pi (or any Linux box) that sits next to your
turntable. When the record player is on, it listens through a microphone, works out what's
playing, and scrobbles it to your self-hosted [Maloja](https://github.com/krateng/maloja) or
[Koito](https://github.com/gabehf/koito), to [ListenBrainz](https://listenbrainz.org), to
last.fm, or to all of them at once.

Every other scrobbler assumes the music comes from software that reports what it's playing.
Records, tapes, CDs and the radio don't, so they never get counted. runout counts them.

```
 turntable ─▶ smart plug ─▶ Home Assistant ──"it's spinning"──┐
     │                                                        ▼
     └─▶ speakers ~~~~ 🎤 mic ─▶ Raspberry Pi: sample ─▶ identify ─▶ confirm ─▶ scrobble
                                                                          │
                                         Maloja / Koito / ListenBrainz / last.fm ◀─┘
```

## Why it's built this way

- **It doesn't listen all day.** A *trigger* decides when to switch the mic on. The neat
  version is a power-monitoring smart plug on the turntable: the motor draws a few extra
  watts when the platter spins, Home Assistant sees that, and runout only samples
  while it's spinning. You can also listen all the time, or use any shell command
  (a GPIO pin, an MQTT check) as the trigger.
- **It doesn't scrobble rubbish.** A song has to be matched twice in a row before it counts,
  a single odd match mid-song won't interrupt it, and it follows last.fm's rules: scrobbled
  after 4 minutes, or at the end if you heard at least 30 seconds.
- **The timestamps are right.** The recogniser reports how far into the song the sample
  was, so the scrobble time is when the song actually started, not when we noticed it.
- **It speaks the open protocol.** One `listenbrainz` target covers ListenBrainz, Maloja
  and Koito. last.fm is supported too.
- **It doesn't double up.** Restarting mid-song won't scrobble it twice, and optional
  last.fm checking skips songs your streaming app already logged.
- **It doesn't lose scrobbles.** If your server or the Wi-Fi is down, scrobbles wait in a
  small SQLite queue and get delivered later.

## What you need

- A Raspberry Pi (a Pi 3 is plenty) running Raspberry Pi OS Lite, or any Linux machine.
- A microphone: a cheap USB mic works fine; so does line-in from your amp's tape-out via a
  USB sound card, which gives perfect recognition with no room noise.
- Optional: a smart plug with power monitoring that Home Assistant can read.

## Install

On the Pi:

```bash
git clone https://github.com/cfer50/runout
cd runout
sudo ./deploy/install.sh
```

That installs runout into `/opt/runout`, creates a `runout` system user in the
`audio` group, puts a config in `/etc/runout/` and sets up a systemd service (not
started yet).

Or just `pip install` it anywhere (Python 3.11+):

```bash
pip install git+https://github.com/cfer50/runout
cp config.example.toml ~/.config/runout/config.toml
```

## Set up

Everything lives in one file, [`config.example.toml`](config.example.toml), which is commented
throughout. The short version:

**1. Find your mic.** Usually nothing to do: `device = "auto"` uses the first microphone plugged in, and picks up a new one if you swap it. If you have more than one, `arecord -l` lists them; use the card *name* rather than its number (numbers can change between reboots): a line like `card 2: camera [...]` becomes `plughw:CARD=camera,DEV=0`. Put it in
`[audio] device`.

**2. Choose a trigger.** For a Home Assistant power plug, watch the sensor with the platter
stopped and then spinning, and set `on_above` to a value between the two. Secrets go in
`/etc/runout/env` and are referenced as `"env:HA_TOKEN"`.

**3. Add targets.**

| Where            | `type`         | `url`                                         | `token`                 |
|------------------|----------------|-----------------------------------------------|-------------------------|
| Maloja           | `listenbrainz` | `http://your-maloja:42010/apis/listenbrainz`  | a Maloja API key        |
| Koito            | `listenbrainz` | `http://your-koito:4110/apis/listenbrainz`    | a Koito API key         |
| ListenBrainz     | `listenbrainz` | `https://api.listenbrainz.org`                | your user token         |
| last.fm          | `lastfm`       | run `runout lastfm-auth`                  | API key/secret/session  |
| Just print it    | `log`          |                                               |                         |

**4. Check it.**

```bash
sudo -u runout runout -c /etc/runout/config.toml check
```

This validates the config, reads the trigger and records a sample so you can see the mic
level:

```
config ok: /etc/runout/config.toml
  targets: maloja (listenbrainz), log (log)
trigger (homeassistant): ON
recording 10s from 'plughw:1,0'...
mic level: [######------------------------] rms=0.0213 (silence below 0.004) -> sound detected
```

Put a record on and try a match:

```bash
runout -c /etc/runout/config.toml identify
```

**5. Start it.**

```bash
sudo systemctl start runout
journalctl -u runout -f
```

```
21:04:12 INFO    record player on; listening
21:04:52 INFO    now playing: Saint Etienne - Only Love Can Break Your Heart
21:10:13 INFO    scrobbled: Saint Etienne - Only Love Can Break Your Heart
21:10:33 INFO    now playing: Saint Etienne - Girl VII
```

## Commands

| Command                       | What it does                                           |
|-------------------------------|--------------------------------------------------------|
| `runout run`              | Run the daemon (what the service does)                 |
| `runout check`            | Validate config, read the trigger, measure mic level   |
| `runout identify`         | Record one sample and show the match (`--file x.wav` to test a file) |
| `runout lastfm-auth`      | Walk through last.fm authorisation, print a session key |
| `runout queue [--flush]`  | Show scrobbles waiting to be delivered, or retry them now |

Add `-v` to any command for debug logging, which shows every sample, its level and its match.

## Tuning

- **Missed quiet songs?** Lower `silence_rms`, move the mic closer to a speaker, or use
  line-in.
- **Fake scrobbles?** Raise `min_confirmations` to 3, or set `max_skew = 0.001` for stricter
  Shazam matching.
- **Wrong album version?** Shazam often matches a compilation or remaster rather than your
  pressing. runout treats "Song" and "Song (2012 Remaster)" as the same track, but the
  album name comes from whatever Shazam matched. Maloja and Koito both let you fix album
  names afterwards.
- **Streaming while the turntable's on?** runout can't tell your record from Deezer
  playing through the same speakers, so you'd get doubles. Turn on `[dedupe]` with your
  last.fm username: runout waits until each song ends plus a couple of minutes, checks your
  last.fm history, and skips anything another app already scrobbled.
- **Short songs getting missed?** Lower `interval_seconds` (e.g. 15) so you get more samples.

## Recognition, honestly

The default recogniser uses Shazam through [shazamio](https://github.com/shazamio/ShazamIO),
a reverse-engineered client. It's free, needs no account and handles mic recordings of vinyl
remarkably well, but it isn't an official API: it could break, and heavy use may not be
something Shazam would approve of. runout only samples while a record is playing,
about three requests a minute, which is gentle. [AudD](https://audd.io) is the paid,
official alternative (`type = "audd"`). A fully offline recogniser would be a great
contribution; see below.

## Contributing

This is meant to be tweaked. The code is small and split by job:

```
runout/
  triggers.py     when to listen        → add MQTT, a GPIO pin, a tonearm sensor...
  audio.py        capture from a mic
  recognisers.py  audio → song          → add ACRCloud, a local fingerprint DB...
  session.py      matches → scrobbles   (pure logic, heavily tested)
  targets.py      where scrobbles go    → add Libre.fm, a webhook, MQTT...
  queue.py        offline retry
  daemon.py       the loop tying it together
```

Each extension point is one small class. Tests run with `pip install -e ".[dev]" && pytest`;
CI runs `ruff` and `pytest` on every PR. See [CONTRIBUTING.md](CONTRIBUTING.md).

Ideas that would make it better:

- an offline recogniser (Chromaprint against your own digitised collection)
- side detection: notice the run-out groove and log "Side A finished"
- a tiny web page on the Pi showing what's playing now
- Discogs lookups so the album name matches the pressing you own
- a Home Assistant sensor exposing the current track

## Licence

MIT. Do what you like with it.
