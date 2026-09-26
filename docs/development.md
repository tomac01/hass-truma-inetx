# Development

[← README](../README.md)

## Dumping the bus without Home Assistant

`custom_components/truma_inetx/bus.py` is the protocol's own model of the
panel's bus and imports no Home Assistant, so it can be run on its own — the
only way to debug the protocol on the bench. `tools/dump_bus.py` is the
launcher; it prints every device on the bus, everything each one publishes, and
what each device says those parameters are.

```bash
./tools/dump_bus.py diagnostics.json        # read somebody's download back
./tools/dump_bus.py --live --identity ~/homeassistant/.storage/truma_inetx_<entry id>
```

Reading a download needs nothing but the standard library. `--live` needs
`bleak`, and the app identity the panel is bonded to — a panel only talks to one
it has been paired with, and Home Assistant stores that under
`.storage/truma_inetx_<config entry id>`. Add `--name 'Truma iNetX-XXXXXX'` if
more than one panel is in range, `--json` for machine-readable output, `--debug`
to log every frame.

```
0x0201  class 0x02 instance 1  Combi 6 E  serial 12345678  (31 parameters)
    AirCirculation.FanLevel                      4   [0..10; perm 1]
    AirHeating.Temp                              228
    ...

Topics with more than one publisher -- no flat reading of
these can mean anything:
    AirCirculation           0x0201, 0x0406
```

Real downloads are kept in [`dumps/`](../dumps/README.md) — the evidence for
vehicles nobody here can plug into, read by the tool above and by the tests.
File a new one with `tools/import_dump.py`, which scrubs it and refuses to
write anything with an address-shaped string left in it.

## Tests

The checks in `tests/` are self-contained: they stub Home Assistant, bleak and
dbus, so they need neither an HA install nor hardware, and each file is a script
— run one directly, or run the lot:

```bash
python3 tests/test_entry_teardown.py              # unload leaves nothing running
for t in tests/test_*.py; do python3 "$t" || break; done
```

Most need nothing installed. Seven reach code that imports a library and the
loop above stops on them unless it is there — `voluptuous` for the config flow's
schema, which two of them drive for real (`test_panel2_discovery.py` and
`test_passive_scan_discovery.py`), `cbor2` for the five that reach the protocol
module, whether to build real frames and parse them back or by way of the
coordinator that imports it:

```bash
pip install voluptuous cbor2==5.6.5
```

`tests/stubs.py` holds the shared Home Assistant stand-ins; it is not a test and
runs nothing on its own.

CI runs every file too, in two stages: everything that needs no library on a
bare interpreter first, so a test double that quietly grows an `import cbor2`
fails there instead of passing because a later step had already installed it.
Adding a test file is enough to have it run — there is no list of every test to
keep in step, which is how five of them went uncovered for the whole 0.9.0 beta
series. What does have to be kept in step is the short skip list of the seven
library tests, and a file wrongly on it falls out of the bare stage's guarantee
rather than failing loudly: `test_panel_declared_options.py` sat there from
f6d6476 until 2026-09-26 without ever importing either library. The week before,
1b8158e had placed that file correctly, above the first `pip install`; f6d6476
moved it into the skip list against its own message, which says only the seven
tests it names may need a third-party library — and one of those seven imports
neither.

## How good are those tests?

`tools/mutate.py` answers that by measuring instead of guessing: it changes the
source in one place and checks whether a test goes red. Nothing notices, and
either that line is uncovered or the change means nothing — which of the two is
a question only reading the code answers, so the tool gives the list, not the
verdict.

```bash
tools/mutate.py --python /path/to/venv/bin/python3        # full run, ~10 min
tools/mutate.py --only bus.py --python …                   # one module
tools/mutate.py --limit 40 --python …                      # even sample
```

Point `--python` at an interpreter that has `cbor2` and `voluptuous`. Without
them the tests that need one fail on import and every mutation in them counts as
caught, which flatters the score — the tool says so when it sees that.

**74.3 % as of 2026-09-26** (733 of 987 mutations caught). The tool holds that
number in `REFERENCE` and compares every run against it, so a regression shows
up instead of passing unnoticed. The weakest files are the Bluetooth side:
`session.py` at 53 %, `bt.py` 54 %, `ble.py` 57 %, `pairing.py` 58 %. The
strongest are `config_flow.py` at 97 % and `truma/const.py` at 96 %.

Two things make the measurement trustworthy, and both were once broken. Each
worker gets its own copy of the tree, so the working directory is never touched.
And `tests/stubs.py` must compile from source rather than from a cached `.pyc`
— Python treats a cache entry as valid while mtime (whole seconds) and file size
are unchanged, which is exactly the kind of edit a mutation makes. Before that
was fixed, real mutations in `select.py` read as survivors. If a run suddenly
reports far more survivors, check that guard first:
`python3 tests/test_fresh_compile.py`.

The history is written up in `REV-005` of the proxy project, together with what
the first measurement found.
