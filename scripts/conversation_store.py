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
import unicodedata as _ud_g4d

# Kandidát na oslovení: "s dovolením" nebo slovo končící na -o/-e před čárkou.
# ⚠️ Tenhle výraz sám o sobě NESTAČÍ — je schválně široký a o tom, co je
# doopravdy oslovení, rozhoduje až `_vokativy_g4d` (viz G4D_ADDRESS_KNOWN_VOCATIVE_V1).
_ADDRESS_RE_G4D = _re_g4d.compile(
    r",?\s*(?:s dovolením|[A-ZŠČŘŽÝÁÍÉÚŮ][a-zěščřžýáíéúůďťň]+[oe])\s*,",
    _re_g4d.IGNORECASE,
)


def _fold_g4d(s: str) -> str:
    s = _ud_g4d.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not _ud_g4d.combining(c)).strip()


def _vokativy_g4d(name: str = None, config: dict = None) -> set:
    """G4D_ADDRESS_KNOWN_VOCATIVE_V1 (22.8.) — koho Hans SKUTEČNĚ oslovuje.

    Bez jména a configu zbude jen „pane"/„paní"/„s dovolením" — vokativ
    jmen se v tom případě NEDEDUPUJE (radši nechat oslovení dvakrát než
    ukousnout slovo z věty).
    """
    povol = {"pane", "pani", "s dovolenim"}
    jmena = list((config or {}).get("known_persons", {}) or {})
    if name:
        jmena.append(str(name))
    try:
        from scripts.cz_names import address as _addr
    except Exception:
        return povol
    for j in jmena:
        try:
            povol.add(_fold_g4d(_addr(j, config)))
        except Exception:
            pass
    return povol


def dedup_address_g4d(text: str, name: str = None, config: dict = None) -> str:
    """Nech první oslovení/'s dovolením', další opakování zahoď.
    'Stando, s dovolením, Stando, mé povinnosti...' → 'Stando, mé povinnosti...'
    Mechanické, nedestruktivní k obsahu — maže jen opakované vokativy.

    G4D_ADDRESS_KNOWN_VOCATIVE_V1 (22.8.) — OSLOVENÍ SE NEHÁDÁ Z TVARU SLOVA.
    Regex má IGNORECASE, takže třída `[A-ZŠČŘŽ…]`, která měla znamenat „velké
    písmeno = jméno", matchovala i malá písmena → za oslovení se považovalo
    JAKÉKOLI slovo končící na -o/-e před čárkou a druhý výskyt se smazal.
    Doloženo: „Udělám to v noci a kdyby to nesedělo, ráno se ozvu" přišlo
    o „nesedělo"; v uložených hovorech je „Podle, co jsem teď našel" (sežráno
    „toho") a „ticho,". Změřeno na 386 skutečných replikách: dnešní pravidlo
    zasáhlo 6 zpráv (2 z toho škoda), pravidlo „vokativ známé osoby" 4 — a nic
    jiného než opravdový vokativ domácího. (Koriguje závěr z 20.8. „post-processing nic
    nemaže": tehdejší testovací věty měly jen JEDEN match, a při jednom se
    nemaže nic.)
    """
    if not text:
        return text
    _povol = _vokativy_g4d(name, config)
    matches = [m for m in _ADDRESS_RE_G4D.finditer(text)
               if _fold_g4d(m.group(0).strip(" ,")) in _povol]
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

    @staticmethod
    def _sbal_monolog(msgs: list) -> list:
        """HANS_CONV_GREETING_ECHO_V1 (4.9.) — z nepreruseneho behu Hansovych
        replik nech jen POSLEDNI.

        PROC: pozdrav pri rozpoznani tvare se uklada sem jako `assistant`
        zprava BEZ uzivatelske repliky. Kdyz clovek neodpovi (obvykle
        neodpovi), hromadi se monolog — a `get_history` ho cely podava
        chatovemu modelu jako few-shot. Ten pak jejich TVAR kopiruje do
        odpovedi na uplne jine otazky.
        **Doloženo živě 4.9.**: ve 3 z 5 tahu zacala odpoved „Henko, dobry
        den." a mela vlepenou doslovnou predpoved „Zitra (05.09.): slaby
        dest 16-23°C." — vcetne odpovedi na dotaz o Star Treku. Predpoved
        do POZDRAVU patri (uzivatelsky opt-in `greeting.special_greetings`),
        do odpovedi o Star Treku ne.
        Zmereno: v poslednich 40 zpravach jedne osoby bylo 30 nezodpovezenych
        Hansovych replik.

        Posledni z behu se NECHAVA schvalne: pokud clovek odpovi, odpovida
        prave na ni (na tom stoji HANS_QUESTION_CONTINUITY_V1).
        Starsi uz nikoho nezajimaly — nejsou to repliky dialogu, je to log.

        ⚠️ Meni JEN pohled do promptu, v ulozisti zustava vse (zobrazeni
        i `/rozhovory` ctou odjinud).
        Zmereno na vsech ulozenych konverzacich: kde probehl skutecny dialog
        (4 z 9 souboru) je zmena **0 %**; ubyva jen tam, kde se hromadil
        monolog (-65 az -99 %).
        """
        out = []
        for m in msgs or []:
            if (m.get("role") == "assistant" and out
                    and out[-1].get("role") == "assistant"):
                out[-1] = m
                continue
            out.append(m)
        return out

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
        msgs = self._sbal_monolog(msgs)   # HANS_CONV_GREETING_ECHO_V1
        return [{"role": m["role"],
                 "content": (dedup_address_g4d(m["content"], name, self.config)
                             if m["role"] == "assistant" else m["content"])}
                for m in msgs]

    def get_history_scoped(self, name: str, channel: str) -> list:
        """PŘÍSNÝ režim: vrátí JEN zprávy s daným kanálem (netaggované zprávy
        NEZAHRNUJE). Pro paint destilaci — kde cross-channel leak = bug."""
        data = self._load(name)
        return [{"role": m["role"],
                 "content": (dedup_address_g4d(m["content"], name, self.config)
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
        assistant_msg = dedup_address_g4d(assistant_msg, name, self.config)  # G4D_DEDUP_ADDRESS_V1
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
