"""
Voice integration helper.
Vytvoří VoiceListener a napojí ho na display_controller_picam.py
tak, aby get_visible_person() vracel aktuálně rozpoznanou osobu z kamery.

Použití v display_controller_picam.py — přidej na konec __init__:

    from scripts.voice_integration import setup_voice
    self._voice = setup_voice(config, openwebui_chat, self)

A na konec start_loop() před picam2.stop():

    if self._voice:
        self._voice.stop()
"""

from __future__ import annotations
import logging
log = logging.getLogger("voice")


def setup_voice(config: dict, chat_handler, display_controller) -> object | None:
    """
    Inicializuje VoiceListener a propojí ho s display_controllerem.
    Vrací VoiceListener instanci nebo None pokud voice disabled.
    """
    enabled = config.get("voice", {}).get("enabled", False)
    print(f"[Voice] setup_voice called — enabled={enabled}")
    if not enabled:
        print("[Voice] Disabled in config — skipping")
        return None

    try:
        from scripts.voice_listener import VoiceListener
    except ImportError as e:
        log.error("[Voice] Import failed: %s", e)
        return None

    listener = VoiceListener(config, chat_handler)

    # Napoj get_visible_person na aktuální identities z display_controlleru
    # display_controller má atributy: identities (list of (name, conf))
    def _get_visible() -> str | None:
        try:
            ids = getattr(display_controller, '_voice_identities', [])
            # Vrať první rozpoznanou (ne Unknown/?) osobu
            for name, conf in ids:
                if name not in ("Unknown", "...", "?", "", "Person"):
                    return name
        except Exception:
            pass
        return None

    listener.get_visible_person = _get_visible
    tts = getattr(chat_handler, 'tts_speaker', None)
    if tts:
        listener._tts = tts
        print('[Voice] TTS mute bridge connected')
    listener.start()
    print('[Voice] VoiceListener started OK')
    return listener