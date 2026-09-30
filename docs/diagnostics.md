# Diagnostics

[← README](../README.md)

The device page's ⋮ → **Download diagnostics** dumps the config entry, whether
the last update succeeded, and the whole bus — every address the integration has
heard from, in hex, with everything that address published under it. That is the
evidence for what is on a given vehicle's bus, and the form addresses are read
and quoted in.

Each device also carries its own `param_meta`: what it says each parameter *is*,
as opposed to what it currently reads — its range, whether it can be written,
and for an enum the panel's own name for every value, with the ones this vehicle
cannot produce marked. Truma documents none of the protocol, but the panel
describes it in every frame, so a download answers "what does this value mean"
without anyone watching their heater and writing it down. The same descriptions
are logged, once each, at debug level.

`contested_topics` names the topics more than one device publishes on that
vehicle — usually none, which is why a single flat view of the bus looked
correct for so long. `unattributed` holds anything that arrived without a usable
source address; nothing reads it, and it should be empty.

A download can be read back with the
[dump tool](development.md#dumping-the-bus-without-home-assistant), which prints
the same bus device by device without Home Assistant in the way. The downloads
this project has learned from are kept in [`dumps/`](../dumps/README.md).

The BLE address, the panel's name, the discovery keys and the persisted app
identity (`muid` / `uuid`) are redacted: the address is a private address that
still pins the panel to a location, and the identity is what the panel bonds
against.

Redaction runs twice, because the two halves of a download hide an address
differently. By **key name**, which covers the config entry, where the keys are
Home Assistant's own. And by **value**, which covers everything: any string
anywhere that is shaped like a BLE address, or like the panel's advertised
`iNetX-xxxxxx` name, whose last three bytes are its identity address.

The value pass is what reaches the bus, where key names are the panel's
vocabulary rather than ours and say nothing about what they hold. The
gas-bottle sensors publish their own addresses as ordinary parameters —
`BluetoothDevice.BleAddress`, described as a string like any other, and with
`BleAddressType: 0` saying they are *public* addresses rather than rotating
ones. Any download taken before
[#35](https://github.com/rpodgorny/hass-truma-inetx/issues/35) was fixed
carries those in clear text.

It is also what reaches the discovery keys, where the address hides from a
redactor that works on key names — Home Assistant serialises the key as
`"repr": "DiscoveryKey(domain='bluetooth', key='…', version=1)"`. Any download
taken before *that* was fixed carries the panel's address in that string,
including the three attached to
[#22](https://github.com/rpodgorny/hass-truma-inetx/issues/22).

So: scrub any download taken before these fixes before re-posting it.
`tools/import_dump.py` hunts the same shapes and refuses to file a dump where
one survives.
