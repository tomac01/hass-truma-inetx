# Umstieg von 0.7.3-live.x auf 0.9.0b23.post4

[← README](../README.md)

**Alle Entity-IDs ändern sich. Es gibt keine Migration.**

Der Upstream hat die Unique-ID zweimal umgestellt — einmal auf die Busadresse
(0.9.0), einmal ohne den Translation-Key (0.9.0b17). Von 0.7.3 aus fallen beide
Umbenennungen in einem Schritt an. Was das im Einzelnen kostet, steht im
Upstream-Hinweis: [Upgrading from 0.8.x](upgrading.md). Hier steht nur, was für
diesen Fork dazukommt.

## Vorher

1. **Diagnose-Download ziehen und aufheben.** Er listet die Busadressen mit
   Seriennummern und ist danach die einzige Zuordnungshilfe.
2. **Automationen, Skripte und Dashboards inventarisieren**, die eine
   Truma-Entität nennen. Sie alle brauchen die neuen IDs.
3. **Geräteliste am Panel prüfen.** Das Panel hält nur etwa vier Kopplungen und
   lehnt danach stumm ab. Alte Einträge löschen.

## Der Umstieg

Der saubere Weg ist, den Config-Entry zu **löschen und neu einzurichten** —
einmal neu koppeln, Panel im Modus „Gerät hinzufügen“. Ein In-Place-Update
funktioniert auch, hinterlässt aber tote Registry-Einträge und `_2`-Suffixe.

Die neuen IDs sind **vorab nicht berechenbar**. Die Unique-ID trägt jetzt die
Busadresse, und der Entity-Slug entsteht aus dem Namen des Busgeräts plus dem
Entitätsnamen — beides steht erst fest, wenn das Panel den Bus gemeldet hat.
Nachsehen unter Einstellungen → Geräte & Dienste → Truma iNet X (BLE) →
Entitäten. Beim Neu-Koppeln kann sich die Busadresse erneut ändern.

## Was sich fachlich ändert

| Vorher (0.7.3-live) | Jetzt |
|---|---|
| ein Gerät mit flachem Zustand | ein HA-Gerät je Busadresse unter dem Panel |
| **Flamme** als Ja/Nein | **Heizung** plus dreiwertiger **Heizstatus** |
| kein Fehlercode | **Störung**, **Störungscode** und **Störung zurücksetzen** |
| keine Timer | sechs **Timer**-Schalter samt **Zeitplan Timer** je Slot |
| — | **Uhrzeit im Bedienteil**, **Freie Plätze**, **Landstrom**, **Displayhelligkeit** |

Die Zustände des Heizstatus — `off` / `running` / `idle` — sind dieselben wie
vorher, eine Automation, die darauf prüft, passt also weiter. Nur der Name der
Entität ist ein anderer.

Erhalten bleiben alle Erweiterungen dieses Forks: bestätigte Schreibvorgänge,
Live-Modus, die beiden getrennten Verbindungssensoren, die kombinierte
Energiequellen-Auswahl und die Vorgangs-Rückmeldung. Der einzelne
**Dieselbrenner**-Schalter des Upstreams steht weiterhin daneben; er schaltet
nur den Brenner und kann, anders als die Auswahl, auch alle Quellen abschalten.

## Das Lovelace-Beispiel

`examples/lovelace/truma-controls.yaml` nennt Entity-IDs. Das sind und waren
Platzhalter; nach dem Umstieg passt keine davon mehr auf eine Installation,
auch nicht nach dem bisherigen Ersetzen des Präfixes. Wie man sie ersetzt,
steht in `examples/lovelace/README.md`.
