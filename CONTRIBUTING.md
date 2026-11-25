# Contributing

Thanks for wanting to make runout better.

## Getting set up

```bash
git clone https://github.com/cfer50/runout
cd runout
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

You don't need a Pi or a turntable to work on most of it. The session logic, targets and
queue are all tested with fakes, and `runout identify --file song.wav` tests
recognition against a file.

## Before you open a PR

```bash
ruff format .
ruff check .
pytest
```

CI runs the same three on Python 3.11 to 3.13.

## Adding things

- **A trigger:** subclass `Trigger` in `triggers.py`, implement `async poll() -> bool | None`
  (None means "couldn't tell"), add it to `build_trigger` and to the allowed types in
  `config.py`.
- **A recogniser:** subclass `Recogniser` in `recognisers.py`, implement
  `async recognise(wav: bytes) -> Track | None`. Keep the JSON parsing in a separate
  `parse_*` function so it can be tested without a network.
- **A target:** subclass `Target` in `targets.py`, implement `now_playing` and `scrobble`.
  Raise `TargetError(msg, retryable=True)` for failures worth retrying (network, 5xx,
  rate limits) and `retryable=False` for ones that won't fix themselves (bad credentials).

Changes to `session.py` change what gets scrobbled for everyone, so they need tests that
show the before and after behaviour.

## Reporting a bad match or missed song

Run with `-v` and include the log lines around it: they show each sample's level and what
was matched, which is usually enough to see what went wrong.
