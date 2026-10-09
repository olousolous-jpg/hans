"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `ModelMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import logging
import requests
import time


class ModelMixin:
    """Část třídy `OpenWebUIDirectHandler` — viz hlavička modulu."""

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    def _build_messages(self, system: str, user: str, name: str | None,
                        grounding: str = "") -> list:  # G4B_GROUNDING_POSITION_V1
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        if name:
            # HANS_CHAT_CHANNEL_AWARE_V1 — LLM vidí historii tohoto kanálu +
            # staré netaggované zprávy (kontinuita). Zprávy z JINÝCH kanálů
            # se filtrují (Bug 3: Rimmer z Telegramu ovlivnil web chat).
            # get_history(channel=X) = ch IN (None, X) — měkký filtr.
            _ch = _h.get_current_channel()
            history = self.conv_store.get_history(name, channel=_ch)
            history = [m for m in history if isinstance(m, dict)]
            # Limit history — kompletní historie přeplňuje context window.
            # 8K context + RAG retrieval + system prompt nechá málo místa,
            # model recituje vzorce ze starých dialogů místo aktuálních dat.
            _hist_limit = int(self.config.get("openwebui_chat", {})
                              .get("history_max_messages", 10))
            if _hist_limit > 0 and len(history) > _hist_limit:
                history = history[-_hist_limit:]
            msgs.extend(history)
        # G4B_GROUNDING_POSITION_V1 — grounding/anti-konfab ZA historii,
        # těsně PŘED user → má poslední slovo, přebije recitaci ze starých
        # dialogů (model váží nejvíc to nejblíž otázce).
        if grounding and grounding.strip():
            msgs.append({"role": "system", "content": grounding.strip()})
        msgs.append({"role": "user", "content": user})
        # region agent log
        try:
            hist_n = (len(msgs) - (1 if system else 0) - 1)
            _h._dbg(
                location="openwebui_direct_handler.py:_build_messages",
                message="Built message list",
                data={
                    "name_present": bool(name),
                    "n_total": int(len(msgs)),
                    "n_history": int(hist_n),
                    "system_chars": int(len(system or "")),
                    "user_chars": int(len(user or "")),
                    # HANS_PROMPT_SIZE_PROBE_V1 — měřeno 19.8.: grounding se
                    # do zprávy vůbec nedostával (n_total == n_history+2).
                    "grounding_chars": int(len((grounding or "").strip())),
                },
            )
        except Exception:
            pass
        # endregion
        return msgs

    def _maybe_deepen_response(self, name: str, message: str):
        """HANS_STUDY_DEEPEN_V2 — reakce na návrh prohloubení ČISTÝM TEXTEM.
        Gated na čekající návrh → klasifikuje (schvaluje/zamítá/kritizuje/nic) a
        rovnou aplikuje. Vrací odpověď nebo None (není reakce → normální chat)."""
        from scripts.hans_study import StudyStore
        dbp = (self.config.get("diary_db")
               or (self.config.get("hans_idle", {}) or {}).get("diary_db")
               or "data/hans_diary.db")
        st = StudyStore(self.config, dbp)
        pend = st.get_pending_deepen()
        if not pend:
            return None
        # HANS_CONFIRM_PRECEDENCE_V1 (7.8.) — ODPOVĚĎ PATŘÍ NEJČERSTVĚJŠÍMU NÁVRHU.
        # Doloženo 7.8. 11:22–11:23: Hans nabídl film na Kodi, uživatel řekl „ne"
        # — a Hans odpověděl „„hrady a historická architektura" nechám tak, jak
        # je." Zamítnutí spolkl návrh prohloubení studia z **03:26 ráno** (8 h
        # starý), protože tahle větev běží PŘED `agent.check_confirmation`
        # (ř. 2719 × 2738) a návrhy prohloubení NEMAJÍ expiraci — kdežto agentní
        # návrh vyprší po 3 min. Starý a nesmrtelný tak vždy přebil čerstvý.
        # Když agent čeká na potvrzení, ustup — odpověď je jeho.
        # (Vedlejší zisk: ušetří se LLM klasifikace na každé zprávě, dokud
        # nějaký návrh prohloubení leží ve frontě.)
        try:
            _ag = self._agent_router()
            _ap = getattr(_ag, "_pending", None) if _ag is not None else None
            _p = _ap.get(name) if _ap else None
            if _p is not None and (time.time() - _p.ts) <= 180:
                logging.getLogger(__name__).info(
                    'HANS_CONFIRM_PRECEDENCE_V1: čeká agentní návrh %s '
                    '→ prohloubení ustupuje', getattr(_p.action, "id", "?"))
                return None
        except Exception as _cpe:
            logging.getLogger(__name__).debug('confirm precedence: %s', _cpe)
        # HANS_DEEPEN_FEEDBACK_GATE_V1 (22.8.) — KE KLASIFIKÁTORU JEN ZPĚTNÁ
        # VAZBA. Doloženo 22.8.: „rekni vice o zameckem parku u hradu Kost"
        # (běžná prosba, jen bez otazníku) prošla dosavadní pojistkou, LLM
        # klasifikátor z ní udělal KRITIZUJE a Hans odpověděl „Beru tvou
        # kritiku, pane — prohloubím studium Český ráj". Návrh přitom vznikl
        # v 00:30 v tichém okně a uživateli nikdy nedorazil.
        # Rozhodnutí je otočené (viz `hans_study.je_reakce_na_navrh`): ne
        # „není to otázka → klasifikuj", ale „nenese to souhlas/nesouhlas/
        # kritiku → nech to být".
        try:
            from scripts.hans_study import je_reakce_na_navrh as _je_fb
            if not _je_fb(message):
                logging.getLogger(__name__).info(
                    'HANS_DEEPEN_FEEDBACK_GATE_V1: %.40s není zpětná vazba '
                    '→ návrhu se nedotýkám', (message or "").strip())
                return None
        except Exception as _fge:
            logging.getLogger(__name__).debug('deepen feedback gate: %s', _fge)
        # HANS_DEEPEN_QUESTION_GUARD_V1 (19.8.) — PŘEDCHŮDCE, dnes podmnožina
        # brány výš (tázací věta bez schvalovacího slova jí neprojde). Ponechán
        # jako pojistka, kdyby brána spadla na výjimku.
        # PŮVODNÍ POPIS: OTÁZKA NENÍ ZPĚTNÁ VAZBA.
        # Dokud leží návrh na prohloubení, posílá se KAŽDÁ další zpráva LLM
        # klasifikátoru (SCHVALUJE/ZAMITA/KRITIZUJE/NIC). Doloženo 19.8.:
        # nevinný dotaz „a Babičku jsi četl ty sám?" vyhodnotil jako KRITIZUJE
        # → `apply_deepen_proposal` reaktivoval DOKONČENÝ program „Český ráj"
        # (completed → active) a přidal 4 pod-témata. Uživatel o nic nežádal,
        # jen se ptal — a přišel tím o pořadí ve studijní frontě
        # (`get_active_program` bere nejstarší aktivní).
        # Deterministicky PŘED klasifikátorem: tázací věta bez schvalovacího
        # nebo odmítacího slova = NIC. Prompt se neladí, dotaz se prostě nepustí.
        try:
            import re as _qre
            _m = (message or "").strip()
            _is_q = _m.endswith("?") or bool(_qre.match(
                r"^\s*(kdo|co|kde|kdy|jak|pro[čc]|kolik|kter|[čc][íi]|zn[áa]|"
                r"vid[íi]|um[íi][šs]|m[áa][šs]|je\s|jsi\s|byl\s)", _m, _qre.I))
            _fb = bool(_qre.search(
                r"\b(ano|jo|souhlas\w*|schval\w*|dob[řr]e|prohlub|prohloub|"
                r"ne\b|nechci|nesouhlas\w*|zru[šs]|nech\s+to|špatn\w*|"
                r"slab\w*|m[ěe]l\s+bys|douč|dodělej)\b", _m, _qre.I))
            if _is_q and not _fb:
                logging.getLogger(__name__).info(
                    'HANS_DEEPEN_QUESTION_GUARD_V1: %.40s je otázka, ne zpětná '
                    'vazba → návrh se nedotýkám', _m)
                return None
        except Exception as _qge:
            logging.getLogger(__name__).debug('deepen question guard: %s', _qge)
        p0 = pend[0]
        from scripts.ollama_client import ollama_generate
        model = (self.config.get("dialog", {}) or {}).get("model") or "hans-czech:latest"
        sysp = ("Byl vytvořen web/dílo o „%s“ a Hans navrhl prohloubit studium "
                "(kritika díla: %s). Rozhodni, jak uživatel na TENTO návrh reaguje. "
                "Odpověz JEDNÍM slovem: SCHVALUJE (souhlasí, ať se prohloubí) / "
                "ZAMITA (nechce) / KRITIZUJE (dává vlastní kritiku díla nebo říká, "
                "co doučit) / NIC (zpráva s návrhem vůbec nesouvisí)."
                % (p0["topic"], (p0.get("critique") or "")[:120]))
        raw = ollama_generate(model, "Zpráva uživatele: %s" % message[:300],
                              system=sysp, config=self.config, timeout=25,
                              keep_alive=-1,
                              options={"temperature": 0, "num_predict": 6})
        if not raw:
            return None
        verdict = raw.strip().upper()
        if "NIC" in verdict:
            return None
        if "ZAMIT" in verdict or "ZAMÍT" in verdict:
            st.reject_deepen_proposal(p0["id"])
            return "Dobře, pane. „%s“ nechám tak, jak je." % p0["topic"]
        user_crit = message.strip() if "KRITIZ" in verdict else ""
        import threading as _th

        def _apply():
            try:
                st.apply_deepen_proposal(self.config, p0["id"],
                                         user_critique=user_crit)
            except Exception:
                pass
        _th.Thread(target=_apply, daemon=True).start()
        if user_crit:
            return ("Beru tvou kritiku, pane — podle ní prohloubím studium „%s“. "
                    "Nová pod-témata pak uvidíš v /studium." % p0["topic"])
        return ("Schváleno, pane. Prohloubím studium „%s“ a příště z něj vytvořím "
                "lepší dílo." % p0["topic"])

    def ping_model(self):
        """Keepalive — udrzi model v VRAM pres Ollama /api/generate."""
        try:
            from scripts.ollama_client import game_mode_on, warmup_paused
            if game_mode_on():   # OLLAMA_GAME_MODE_V1 — nepřipínej, VRAM volná pro hru
                return
            # HANS_STUDY_VRAM_HANDOFF_V1 — během base-model dávky (studium/
            # analytika/immune volá pause_warmup) NEpřipínej hans-czech, jinak
            # 4min ping re-pinuje 8GB model a evictuje base OpenEuroLLM uprostřed
            # dlouhého generování (8+8 > 16GB) → 300s timeout. Stejný princip
            # jako herní mód. hans-czech se dotáhne on-demand při reálném chatu.
            if warmup_paused():
                return
        except Exception:
            pass
        try:
            # Ollama /api/generate s keep_alive=10m — model zustane v VRAM
            base = self.config.get('openwebui_chat', {}).get(
                'base_url', 'http://localhost:11434')
            _t0 = time.time()          # HANS_LLM_TRACE_V1
            _r = requests.post(
                f'{base}/api/generate',
                json={'model': self.model_name,
                      'prompt': '',
                      'keep_alive': '20m'},
                timeout=10,
            )
            # keepalive JE ten krok, co vraci hans-czech do VRAM — v mereni
            # nocniho soubehu je to nejdulezitejsi radek vubec.
            self._trace(_t0, "keepalive", getattr(_r, "status_code", 0),
                        url=f'{base}/api/generate')
        except Exception:
            pass

    def get_available_models(self) -> list:
        try:
            r = requests.get(f"{self.base_url}/api/v1/models",
                             headers=self._headers(), timeout=10)
            if r.status_code == 200:
                return [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            pass
        return []

    def _test_connection(self):
        try:
            headers = self._headers()
            r = requests.get(f"{self.base_url}/api/v1/models",
                             headers=headers, timeout=5)
            if r.status_code == 200:
                models = [m.get("id", "") for m in r.json().get("data", [])]
                if any(self.model_name in m for m in models):
                    print(f"[Chat] Connected — model '{self.model_name}' ready")
                else:
                    print(f"[Chat] WARNING: model '{self.model_name}' not found. "
                          f"Available: {models[:3]}")
                # region agent log
                try:
                    _h._dbg(
                        location="openwebui_direct_handler.py:_test_connection",
                        message="Model list fetched",
                        data={
                            "http": int(r.status_code),
                            "model_name": str(self.model_name),
                            "models_count": int(len(models)),
                            "models_sample": models[:5],
                        },
                    )
                except Exception:
                    pass
                # endregion
            else:
                print(f"[Chat] Connection issue: HTTP {r.status_code}")
        except Exception as e:
            print(f"[Chat] Cannot connect to OpenWebUI at {self.base_url}: {e}")

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import openwebui_direct_handler as _h  # noqa: E402
