"""
Conversation Store
Ukládá historii konverzací per-person do data/conversations/.
Přesunuto z openwebui_chat_handler.py pro sdílení mezi handlery.
"""

import json
import time
from pathlib import Path
from datetime import datetime



# ── G4D_DEDUP_ADDRESS_V1 — dedup opakovaného oslovení ──
import re as _re_g4d

# oslovení mezi čárkami: "s dovolením" nebo vokativ (Slovo končící -o/-e)
_ADDRESS_RE_G4D = _re_g4d.compile(
    r",?\s*(?:s dovolením|[A-ZŠČŘŽÝÁÍÉÚŮ][a-zěščřžýáíéúůďťň]+[oe])\s*,",
    _re_g4d.IGNORECASE,
)


def dedup_address_g4d(text: str) -> str:
    """Nech první oslovení/'s dovolením', další opakování zahoď.
    'Stando, s dovolením, Stando, mé povinnosti...' → 'Stando, mé povinnosti...'
    Mechanické, nedestruktivní k obsahu — maže jen opakované vokativy."""
    if not text:
        return text
    matches = list(_ADDRESS_RE_G4D.finditer(text))
    if len(matches) <= 1:
        return text  # 0 nebo 1 oslovení = OK
    out = text
    for m in reversed(matches[1:]):  # od konce, ať nerozhodím indexy
        out = out[:m.start()] + "," + out[m.end():]
    # úklid vícenásobných čárek/mezer
    out = _re_g4d.sub(r"(,\s*){2,}", ", ", out)
    out = _re_g4d.sub(r"\s{2,}", " ", out).strip()
    # ── G4D_PUNCT_FIX_V1 — úklid interpunkce na švech po smazání oslovení ──
    # ", ." → "."  (oslovení bylo na konci věty)
    out = _re_g4d.sub(r",\s*\.", ".", out)
    # ".," → "." + následující slovo velkým ("případů., zaznamenal" → "případů. Zaznamenal")
    def _cap_after_dot(m):
        return ". " + m.group(1).upper()
    out = _re_g4d.sub(r"\.\s*,\s*([a-zěščřžýáíéúůďťň])", _cap_after_dot, out)
    # osamocená čárka po tečce bez písmene: ". ," → ". "
    out = _re_g4d.sub(r"\.\s*,\s*", ". ", out)
    # mezera před interpunkcí
    out = _re_g4d.sub(r"\s+([,.!?])", r"\1", out)
    # znovu vícenásobné čárky (úklid mohl nějaké vytvořit)
    out = _re_g4d.sub(r"(,\s*){2,}", ", ", out)
    out = _re_g4d.sub(r"\s{2,}", " ", out).strip()
    return out  # G4D_PUNCT_FIX_V1


class ConversationStore:

    def __init__(self, config: dict):
        self.config     = config
        conv_cfg        = config.get("conversations", {})
        self._dir       = Path(conv_cfg.get("dir", "data/conversations"))
        self._max_turns = int(conv_cfg.get("max_turns", 20))
        self._dir.mkdir(parents=True, exist_ok=True)

    def get_history(self, name: str, channel: str = None) -> list:
        """HANS_CHAT_CHANNEL_AWARE_V1 — channel=None vrací vše (zpětná
        kompatibilita, default). channel='web'/'telegram'/'voice'/'popup' vrací
        JEN zprávy s tímto kanálem NEBO bez kanálu (starý netaggovaný data).
        Prevence cross-channel leaku: „zkus to znova" ve web chatu nesmí vidět
        historii z Telegramu."""
        data = self._load(name)
        msgs = data.get("messages", [])
        if channel is not None:
            msgs = [m for m in msgs if m.get("ch") in (None, channel)]
        return [{"role": m["role"],
                 "content": (dedup_address_g4d(m["content"])
                             if m["role"] == "assistant" else m["content"])}
                for m in msgs]

    def get_history_scoped(self, name: str, channel: str) -> list:
        """PŘÍSNÝ režim: vrátí JEN zprávy s daným kanálem (netaggované zprávy
        NEZAHRNUJE). Pro paint destilaci — kde cross-channel leak = bug."""
        data = self._load(name)
        return [{"role": m["role"],
                 "content": (dedup_address_g4d(m["content"])
                             if m["role"] == "assistant" else m["content"])}
                for m in data.get("messages", []) if m.get("ch") == channel]

    def add_exchange(self, name: str, user_msg: str, assistant_msg: str,
                     channel: str = None):
        data = self._load(name)
        msgs = data.get("messages", [])
        now  = time.time()
        _u = {"role": "user", "content": user_msg, "ts": now}
        if channel:
            _u["ch"] = channel
        msgs.append(_u)
        assistant_msg = dedup_address_g4d(assistant_msg)  # G4D_DEDUP_ADDRESS_V1
        _a = {"role": "assistant", "content": assistant_msg, "ts": now}
        if channel:
            _a["ch"] = channel
        msgs.append(_a)
        max_msgs = self._max_turns * 2
        if len(msgs) > max_msgs:
            msgs = msgs[-max_msgs:]
        data["messages"] = msgs
        data["updated"]  = datetime.now().isoformat(timespec="seconds")
        self._save(name, data)

    def add_greeting(self, name: str, greeting_text: str, channel: str = None):
        data = self._load(name)
        msgs = data.get("messages", [])
        _g = {"role": "assistant", "content": greeting_text, "ts": time.time()}
        if channel:
            _g["ch"] = channel
        msgs.append(_g)
        max_msgs = self._max_turns * 2
        if len(msgs) > max_msgs:
            msgs = msgs[-max_msgs:]
        data["messages"] = msgs
        data["updated"]  = datetime.now().isoformat(timespec="seconds")
        self._save(name, data)

    def clear(self, name: str):
        p = self._path(name)
        if p.exists():
            p.unlink()
            print(f"[ConvStore] Cleared history for '{name}'")

    def clear_all(self):
        for f in self._dir.glob("*.json"):
            f.unlink()
        print("[ConvStore] All histories cleared")

    def list_persons(self) -> list:
        return [f.stem for f in sorted(self._dir.glob("*.json"))]

    def summary(self) -> str:
        persons = self.list_persons()
        if not persons:
            return "no history"
        parts = []
        for name in persons:
            data = self._load(name)
            n = len(data.get("messages", []))
            parts.append(f"{name}:{n//2}turns")
        return "  ".join(parts)

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        return self._dir / f"{safe}.json"

    def _load(self, name: str) -> dict:
        p = self._path(name)
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[ConvStore] Load error for '{name}': {e}")
        return {"name": name, "messages": []}

    def _save(self, name: str, data: dict):
        try:
            with open(self._path(name), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[ConvStore] Save error for '{name}': {e}")
