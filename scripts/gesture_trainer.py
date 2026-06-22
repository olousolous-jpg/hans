#!/usr/bin/env python3
"""
Gesture Trainer — sběr dat a trénink klasifikátoru gest.

Použití:
  1. Spusť: python3 gesture_trainer.py collect
     - Ukazuj gesto a stiskni klávesu pro label:
       O = open_hand, T = thumbs_up, N = none/fist
       Q = ukonči sběr
  2. Spusť: python3 gesture_trainer.py train
     - Natrénuje MLP a uloží model do data/gesture_model.pkl

Server pak použije model místo geometrických pravidel.
"""

import sys
import json
import socket
import struct
import time
import numpy as np
from pathlib import Path

SOCK_PATH  = "/tmp/gesture.sock"
DATA_FILE  = Path("data/gesture_landmarks.jsonl")
MODEL_FILE = Path("data/gesture_model.pkl")

LABELS = {
    'o': 'open_hand',
    't': 'thumbs_up',
    'n': 'none',
    'f': 'fist',
}

# ── Normalizace landmarks ─────────────────────────────────────────────────────

def normalize_landmarks(lm_flat):
    """
    Normalizuj 63 floats (21×xyz) aby byly invariantní vůči pozici a velikosti.
    1. Přesuň zápěstí do originu
    2. Normalizuj škálu podle vzdálenosti zápěstí-střed_prstu
    """
    lm = np.array(lm_flat, dtype=np.float32).reshape(21, 3)

    # 1. Odečti zápěstí (bod 0)
    lm -= lm[0]

    # 2. Normalizuj škálu
    scale = np.linalg.norm(lm[9])  # střed dlaně (bod 9)
    if scale > 1e-6:
        lm /= scale

    return lm.flatten()


# ── Sběr dat ──────────────────────────────────────────────────────────────────

def collect():
    """Sbírej landmarks ze gesture socketu a ukládej s labely."""
    import termios, tty, os

    DATA_FILE.parent.mkdir(exist_ok=True)

    print("=== Sběr gesture dat ===")
    print("Připojuji se na gesture socket...")

    # Připoj se na gesture socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCK_PATH)
    except Exception as e:
        print(f"✗ Nelze se připojit: {e}")
        print("  Spusť nejdřív: bash run.sh")
        return

    sock.settimeout(2.0)
    print("✓ Připojeno")
    print()
    print("Klávesy:")
    for k, v in LABELS.items():
        print(f"  {k.upper()} = {v}")
    print("  Q = konec")
    print()
    print("Ukazuj gesto a stiskni klávesu. Každý stisk zaznamená ~30 vzorků.")

    # Non-blocking terminal input
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    counts = {v: 0 for v in LABELS.values()}
    collected = 0

    try:
        tty.setraw(fd)

        while True:
            # Čti klávesu
            ch = sys.stdin.read(1).lower()
            if ch == 'q':
                break
            if ch not in LABELS:
                continue

            label = LABELS[ch]
            print(f"\r[{label}] Záznamenávám 30 vzorků...", end='', flush=True)

            # Zaznamenej 30 vzorků
            saved = 0
            t_start = time.time()
            while saved < 30 and time.time() - t_start < 5.0:
                try:
                    # Přečti gesture_id (1 byte)
                    resp = sock.recv(1)
                    if not resp:
                        break
                    gesture_id = struct.unpack('B', resp)[0]

                    # Přečti bbox (16 bytes)
                    bbox_raw = b''
                    while len(bbox_raw) < 16:
                        chunk = sock.recv(16 - len(bbox_raw))
                        if not chunk: break
                        bbox_raw += chunk

                    # Přečti landmarks (252 bytes)
                    lm_raw = b''
                    while len(lm_raw) < 252:
                        chunk = sock.recv(252 - len(lm_raw))
                        if not chunk: break
                        lm_raw += chunk

                    if len(lm_raw) < 252:
                        continue

                    lm = struct.unpack('>63f', lm_raw)
                    if all(v == 0.0 for v in lm):
                        continue  # prázdné landmarks

                    # Normalizuj a ulož
                    lm_norm = normalize_landmarks(lm)
                    entry = {
                        'label': label,
                        'landmarks': lm_norm.tolist(),
                        'raw_gesture': gesture_id,
                    }
                    with open(DATA_FILE, 'a') as f:
                        f.write(json.dumps(entry) + '\n')

                    saved += 1
                    counts[label] += 1
                    collected += 1
                    print(f"\r[{label}] {saved}/30  (celkem: {counts})", end='', flush=True)

                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"\nChyba: {e}")
                    break

            print(f"\r[{label}] ✓ Uloženo {saved} vzorků               ")

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sock.close()

    print(f"\n=== Hotovo ===")
    print(f"Celkem: {collected} vzorků")
    for label, count in counts.items():
        print(f"  {label}: {count}")
    print(f"Uloženo do: {DATA_FILE}")
    print(f"\nSpusť: python3 gesture_trainer.py train")


# ── Trénink ───────────────────────────────────────────────────────────────────

def train():
    """Natrénuj MLP klasifikátor na sebraných datech."""
    try:
        from sklearn.neural_network import MLPClassifier
        from sklearn.preprocessing import LabelEncoder
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import classification_report
        import pickle
    except ImportError:
        print("✗ Chybí scikit-learn: pip install scikit-learn")
        return

    if not DATA_FILE.exists():
        print(f"✗ Data soubor nenalezen: {DATA_FILE}")
        print("  Spusť nejdřív: python3 gesture_trainer.py collect")
        return

    # Načti data
    X, y = [], []
    with open(DATA_FILE) as f:
        for line in f:
            entry = json.loads(line.strip())
            X.append(entry['landmarks'])
            y.append(entry['label'])

    X = np.array(X, dtype=np.float32)
    print(f"Načteno {len(X)} vzorků")
    for label in set(y):
        print(f"  {label}: {y.count(label)}")

    if len(set(y)) < 2:
        print("✗ Potřebuješ alespoň 2 různé labely")
        return

    # Enkoduj labely
    le = LabelEncoder()
    y_enc = le.fit_transform(y)

    # Split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y_enc, test_size=0.2, random_state=42, stratify=y_enc)

    # Trénink
    print("\nTrénuji MLP klasifikátor...")
    clf = MLPClassifier(
        hidden_layer_sizes=(128, 64),
        max_iter=500,
        random_state=42,
        early_stopping=True,
        validation_fraction=0.1,
        verbose=True,
    )
    clf.fit(X_train, y_train)

    # Evaluace
    y_pred = clf.predict(X_test)
    print("\n=== Výsledky ===")
    print(classification_report(y_test, y_pred, target_names=le.classes_))

    # Ulož model
    MODEL_FILE.parent.mkdir(exist_ok=True)
    with open(MODEL_FILE, 'wb') as f:
        pickle.dump({'clf': clf, 'le': le}, f)
    print(f"✓ Model uložen: {MODEL_FILE}")
    print(f"\nLabely: {list(le.classes_)}")
    print("\nNyní restartuj run.sh — server automaticky načte model.")


# ── Hlavní ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'
    if cmd == 'collect':
        collect()
    elif cmd == 'train':
        train()
    else:
        print("Použití:")
        print("  python3 gesture_trainer.py collect  — sběr dat")
        print("  python3 gesture_trainer.py train    — trénink modelu")