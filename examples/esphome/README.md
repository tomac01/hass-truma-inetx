# ESPHome-Beispiel: Bluetooth-Proxy mit Bond-Anzeige

[English version](#english-version)

`truma-inetx-proxy.yaml` ist ein ESPHome-Bluetooth-Proxy für das Truma iNet X
Panel. Der Proxy selbst ist Standard-Firmware aus dem ESPHome-Paket
`esp32-generic` — mehr braucht die Integration nicht
([Konnektivität](../../docs/connectivity.md)). Das Beispiel ergänzt nur
Diagnose: Es zeigt in Home Assistant, ob der Proxy seinen Kopplungsschlüssel
(BLE-Bond) für das Panel noch hat.

Stabiler wird die Verbindung dadurch nicht. Der Nutzen liegt in der
Fehlersuche: Wenn nichts mehr verbindet, sagt die Anzeige, auf welcher Seite
man suchen muss.

## Was dazukommt

| Entität | Art | Bedeutung |
|---|---|---|
| **BLE-Bonds** | Sensor (Diagnose) | Zahl der im ESP gespeicherten Kopplungen |
| **BLE-Bond-Liste** | Textsensor (Diagnose) | deren Adressen, oder `keine` |
| **BLE-Bond vorhanden** | Binärsensor (Diagnose) | an, sobald mindestens eine Kopplung gespeichert ist |
| **Neu starten** | Knopf | startet den ESP neu, der Bond bleibt erhalten |
| **BLE-Bonds löschen** | Knopf, standardmäßig deaktiviert | löscht alle Kopplungen des ESP |

Die Werte werden einmal je Minute gelesen. Die Adressen entstehen zur Laufzeit
auf dem Gerät; in den Dateien hier steht keine.

## Einrichten

1. `truma-inetx-proxy.yaml` **und** `truma_bond_status.h` in dasselbe
   ESPHome-Konfigurationsverzeichnis legen. Ohne den Header schlägt die
   Validierung fehl.
2. In `secrets.yaml` einen Eintrag `api_encryption_key` anlegen
   (`openssl rand -base64 32`).
3. `name` und `friendly_name` unter `substitutions` anpassen.
4. Flashen.

Wer schon eine eigene Proxy-Konfiguration hat, übernimmt nur den Eintrag
`includes:` unter `esphome:` und alles ab `button:`. Voraussetzung ist
`framework: type: esp-idf` mit dem Bluedroid-Stack; der Header bricht den Build
mit einer klaren Meldung ab, wenn Bluedroid fehlt.

**Ein bereits gekoppelter Proxy wird per OTA aktualisiert, nicht per USB.** Ein
USB-Flash, der den Flash-Speicher löscht, nimmt den Bond mit, und das Panel
muss neu gekoppelt werden.

## Die Anzeige lesen

| Anzeige | Verbindung | Bedeutung |
|---|---|---|
| Bonds ≥ 1 | läuft | alles in Ordnung |
| Bonds = 0 | kommt nicht zustande | Der Proxy hat keinen Schlüssel (mehr). Am Panel den alten Eintrag löschen, dann neu koppeln ([Kopplung](../../docs/pairing.md)). |
| Bonds ≥ 1 | kommt nicht zustande | Der Schlüssel ist da; der Fehler liegt nicht am Bond. Siehe [bekannte Einschränkungen](../../docs/limitations.md), Abschnitt „Reconnects can wedge“. |

**Neu starten** hilft, wenn der Proxy selbst hängt. Er hilft nicht, wenn das
Panel keine Verbindung mehr annimmt — dann ändert auch ein Neustart des ESP
nichts, und das Panel muss stromlos gemacht werden.

**BLE-Bonds löschen** ist für eine gezielte Neukopplung gedacht. Danach
erreicht der Proxy das Panel erst wieder, wenn neu gekoppelt wurde. Den Knopf
vor der Benutzung in Home Assistant aktivieren und danach wieder deaktivieren.
Anders als „Factory reset“ aus dem ESPHome-Paket lässt er WLAN und übrige
Einstellungen unangetastet.

## Stand

Die YAML ist mit ESPHome 2026.9.1 validiert (`esphome config`) und kompiliert
(`esphome compile`, ESP-IDF 5.5.5). Geflasht wurde sie in genau dieser Form
nicht: Auf dem Proxy dieses Forks läuft seit Ende September 2026 derselbe Code
unter anderen Bezeichnern und Gerätenamen.

---

## English version

`truma-inetx-proxy.yaml` is an ESPHome Bluetooth proxy for the Truma iNet X
panel. The proxy itself is stock firmware from the ESPHome `esp32-generic`
package — the integration needs nothing more
([connectivity](../../docs/connectivity.md)). The example only adds
diagnostics: it shows in Home Assistant whether the proxy still holds its
pairing key (BLE bond) for the panel.

It does not make the link more stable. What it buys is troubleshooting: when
nothing connects any more, it says which side to look at.

## What it adds

The entity names are German, as shipped in the YAML; rename them there if you
like.

| Entity | Kind | Meaning |
|---|---|---|
| **BLE-Bonds** | sensor (diagnostic) | number of pairings stored on the ESP |
| **BLE-Bond-Liste** | text sensor (diagnostic) | their addresses, or `keine` (none) |
| **BLE-Bond vorhanden** | binary sensor (diagnostic) | on once at least one pairing is stored |
| **Neu starten** | button | restarts the ESP; the bond survives |
| **BLE-Bonds löschen** | button, disabled by default | deletes every pairing on the ESP |

The values are read once a minute. The addresses come into being at runtime on
the device; none is written in the files here.

## Setting up

1. Put `truma-inetx-proxy.yaml` **and** `truma_bond_status.h` into the same
   ESPHome configuration directory. Validation fails without the header.
2. Add an `api_encryption_key` entry to `secrets.yaml`
   (`openssl rand -base64 32`).
3. Adjust `name` and `friendly_name` under `substitutions`.
4. Flash.

If you already have a proxy configuration of your own, take only the
`includes:` entry under `esphome:` and everything from `button:` on. It needs
`framework: type: esp-idf` with the Bluedroid stack; the header stops the build
with a clear message if Bluedroid is missing.

**Update a proxy that is already paired over OTA, not over USB.** A USB flash
that erases the flash memory takes the bond with it, and the panel has to be
paired again.

## Reading it

| Shown | Link | Meaning |
|---|---|---|
| bonds ≥ 1 | up | all is well |
| bonds = 0 | will not come up | The proxy has no key (any more). Delete the old entry on the panel, then pair again ([pairing](../../docs/pairing.md)). |
| bonds ≥ 1 | will not come up | The key is there; the bond is not the fault. See [known limitations](../../docs/limitations.md), "Reconnects can wedge". |

**Neu starten** (restart) helps when the proxy itself is stuck. It does not
help when the panel has stopped accepting connections — restarting the ESP
changes nothing then, and the panel has to be power-cycled.

**BLE-Bonds löschen** (delete bonds) is for a deliberate re-pairing. Afterwards
the proxy cannot reach the panel until it has been paired again. Enable the
button in Home Assistant before using it and disable it again afterwards.
Unlike the package's "Factory reset" it leaves Wi-Fi and the other settings
alone.

## Status

The YAML validates with ESPHome 2026.9.1 (`esphome config`) and compiles
(`esphome compile`, ESP-IDF 5.5.5). It has not been flashed in exactly this
form: this fork's proxy has been running the same code under other identifiers
and device names since the end of September 2026.
