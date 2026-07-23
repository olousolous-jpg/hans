"""HANS_NOTIFIER_V1 — fan-out přes zapnuté notifikační mosty (Telegram, Matrix).

Proč: přechod z Telegramu na Matrix (E2E). Volající kód (`hans_idle`,
`display_controller_picam`) sahá na `openwebui_chat.telegram` a používá jen
`enabled`/`send`/`send_proactive`/`send_photo`/`send_video`/`_pending_brain_notify`.
Notifier KVÁKÁ jako bridge a rozešle výstup všem zapnutým mostům → volající se
nemění a backend je swappable configem.

Cílový stav (rozhodnuto 23.7.): až Matrix poběží ověřeně, `telegram.enabled=false`
→ Notifier drží jen Matrix; pak lze Telegram kód smazat úplně bez zásahu do
volajících (drží se seamu, ne konkrétního mostu).

Příchozí zprávy NEjdou přes Notifier — každý most má vlastní smyčku a volá týž
`chat_handler.send_chat_message(person, text, channel=...)` a odpovídá po SVÉM
kanále. Notifier je jen OUTBOUND fan-out.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("hans.notifier")


class Notifier:
    def __init__(self, bridges):
        # zachovej pořadí; None a nezaložené vynech
        self._bridges = [b for b in (bridges or []) if b is not None]

    @property
    def enabled(self) -> bool:
        return any(getattr(b, "enabled", False) for b in self._bridges)

    def _fan(self, method: str, *a, **k) -> bool:
        """Zavolej method na všech ZAPNUTÝCH mostech; True když aspoň jeden uspěl.
        Nikdy nevyhodí — selhání jednoho mostu nesmí shodit ostatní ani volajícího."""
        ok = False
        for b in self._bridges:
            if not getattr(b, "enabled", False):
                continue
            fn = getattr(b, method, None)
            if fn is None:
                continue
            try:
                if fn(*a, **k):
                    ok = True
            except Exception as e:
                _log.warning("notifier %s na %s selhal: %s", method,
                             type(b).__name__, e)
        return ok

    def send(self, *a, **k) -> bool:
        return self._fan("send", *a, **k)

    def send_proactive(self, *a, **k) -> bool:
        return self._fan("send_proactive", *a, **k)

    def send_photo(self, *a, **k) -> bool:
        return self._fan("send_photo", *a, **k)

    def send_video(self, *a, **k) -> bool:
        return self._fan("send_video", *a, **k)

    def start(self):
        for b in self._bridges:
            try:
                b.start()
            except Exception as e:
                _log.warning("notifier start %s: %s", type(b).__name__, e)

    def stop(self):
        for b in self._bridges:
            try:
                b.stop()
            except Exception:
                pass

    # ── brain-notify flag (HANS_TELEGRAM_BRAIN_NOTIFY_V1) ────────────────────
    # hans_idle na naběhnutí mozku: když aspoň jeden most čeká (uživatel psal
    # při spícím mozku), pošli „jsem online" a vyčisti flag na VŠECH.
    @property
    def _pending_brain_notify(self) -> bool:
        return any(getattr(b, "_pending_brain_notify", False)
                   for b in self._bridges)

    @_pending_brain_notify.setter
    def _pending_brain_notify(self, val):
        for b in self._bridges:
            try:
                setattr(b, "_pending_brain_notify", val)
            except Exception:
                pass
