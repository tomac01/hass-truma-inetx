"""Register der laufenden Vorgänge und ihres zuletzt veröffentlichten Ergebnisses.

Bewusst ohne Home-Assistant-Import: die Vorrangregeln sind die eigentliche
Fachlichkeit und lassen sich so direkt prüfen, statt über einen halben
HA-Stub. Der Coordinator reicht nur einen Callback herein, mit dem er seine
Entitäten benachrichtigt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager

_IDLE = ("idle", None, None, None)


class OperationRegistry:
    """Wer arbeitet gerade, und was ist zuletzt schiefgegangen."""

    def __init__(self, on_change: Callable[[], None]) -> None:
        """Initialisieren; ``on_change`` benachrichtigt die Entitäten."""
        self._on_change = on_change
        # token -> (state, action, target); state ist "syncing" oder "changing"
        self._running: dict[int, tuple[str, str, object]] = {}
        self._serial = 0
        self._result = _IDLE
        self._result_serial = 0
        self._command_serial = 0

    @property
    def state(self) -> str:
        """Der Zustand, den der Sensor meldet."""
        return self._foreground()[0]

    @property
    def attributes(self) -> dict:
        """Stabiler Vertrag fürs Frontend: action, target, error."""
        _, action, target, error = self._foreground()
        return {"action": action, "target": target, "error": error}

    def changing(self, action: str) -> bool:
        """Ob eine Transaktion dieser Art läuft — auch wartend hinter einer anderen."""
        return any(op[1] == action for op in self._running.values())

    def _foreground(self) -> tuple:
        """Befehle und ihre Fehler schlagen Hintergrund-Synchronisation."""
        for op in self._running.values():
            if op[0] == "changing":
                return (*op, None)
        if self._result[0] == "error" and self._result[1] != "sync":
            return self._result
        if self._running:
            return (*next(iter(self._running.values())), None)
        return self._result

    def begin(self, action: str, target: object = None) -> int:
        """Einen Vorgang eröffnen und sein Token zurückgeben."""
        self._serial += 1
        token = self._serial
        self._running[token] = (
            "syncing" if action == "sync" else "changing",
            action,
            target,
        )
        self._on_change()
        return token

    def end(self, token: int, error: str | None = None) -> None:
        """Einen Vorgang abschließen, mit oder ohne Fehler."""
        op = self._running.pop(token, None)
        if op is None:
            return
        # Ein Reconnect kann nach dem Befehl beginnen, der ihn ausgelöst hat.
        # Wird dieser Hintergrund-Lesevorgang fertig, darf er den gescheiterten
        # Nutzerbefehl nicht löschen.
        preserves_command_error = (
            op[1] == "sync" and self._result[0] == "error" and self._result[1] != "sync"
        )
        if op[0] == "changing":
            # Ein Sync kann ein neueres Token tragen als der Befehl, der ihn
            # geweckt hat. Nur ein weiterer Befehl darf ein Befehlsergebnis
            # ablösen.
            publish = token >= self._command_serial
            if publish:
                self._command_serial = token
        else:
            publish = token >= self._result_serial and not preserves_command_error
        if publish:
            self._result_serial = max(token, self._result_serial)
            self._result = ("error", op[1], op[2], error) if error else _IDLE
        self._on_change()

    @contextmanager
    def operation(self, action: str, target: object = None):
        """Genau einen Lebenszyklus besitzen; auch ein Abbruch beendet ihn."""
        token = self.begin(action, target)
        try:
            yield token
        except asyncio.CancelledError:
            self.end(token, "Operation cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - jeder Fehler ist ein Ergebnis
            self.end(token, str(exc) or type(exc).__name__)
            raise
        else:
            self.end(token)
