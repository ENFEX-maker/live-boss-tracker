#!/usr/bin/env python3
"""
EFT Live Goon Tracker v4.3
Kalibriert auf EFT v1.0.2.5.43579 — nur Player.log.

ERKENNUNGSLOGIK:
  BotBossSpawn:TrySpawn  = 1 Treffer pro Boss-Entity (Leader + Wachen)
  SpawnBossSupports      = Marker nur für Wachen-Entities (bestätigt Wachen-Boss)

  Smuggler-Gruppe (laut tarkov.dev API):
    Leader + 1 Escort (50%) = 2 TrySpawn
    Leader + 2 Escorts (50%) = 3 TrySpawn

  Shoreline hat 2x AF-Boss (Armed Forces, 100%) an den Tor-Türmen → immer +2 TrySpawn!

  Formel: TrySpawn − SpawnBossSupports = Anzahl Boss-Leader
          SpawnBossSupports               = Anzahl Guard-Entities

  Bestätigte Beispiele aus echten Raids:
    2 TrySpawn, 0 Sup → Smuggler (kleines Grüppchen, 1+1)
    3 TrySpawn, 0 Sup → Goons(3) ODER Smuggler(3) — kein Wachen-Boss
    3 TrySpawn, 1 Sup → Partizan(1) + Smuggler(2er) ← Customs bestätigt
    5 TrySpawn, 4 Sup → Reshala (1 Leader + 4 Guards) ← Customs bestätigt
    5 TrySpawn, 0 Sup → Smuggler(2) + Goons(3) — auf Goon-Maps
    5 TrySpawn, 2 Sup → Shoreline: Smuggler(3) + 2x AF ← Shoreline bestätigt
    6 TrySpawn, 0 Sup → Goons(3) + Smuggler(3) ← Shoreline bestätigt
    7 TrySpawn, 4 Sup → Goons(3) + Reshala+4G ← zu erwarten auf Customs

Verwendung:
    GoonTracker.exe                   # normaler Start
    GoonTracker.exe --debug           # zeigt erkannte Signale in Echtzeit
    GoonTracker.exe --full            # liest Player.log von Anfang
    GoonTracker.exe --log PATH        # alternativer Player.log-Pfad
"""

import os
import re
import sys
import time
import json
import argparse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, List

try:
    import winsound
    HAS_SOUND = True
except ImportError:
    HAS_SOUND = False

VERSION       = "4.3"
GITHUB_REPO   = "ENFEX-maker/live-boss-tracker"
GITHUB_API    = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return (0,)


def check_for_update() -> Optional[tuple]:
    """Gibt (tag, download_url, notes) zurück wenn ein Update verfügbar ist, sonst None."""
    try:
        req = urllib.request.Request(GITHUB_API,
                                     headers={"User-Agent": "GoonTracker"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        tag   = data.get("tag_name", "")
        notes = (data.get("body") or "").strip()
        if _ver_tuple(tag) > _ver_tuple(VERSION):
            for asset in data.get("assets", []):
                if asset["name"].lower().endswith(".exe"):
                    return tag, asset["browser_download_url"], notes
    except Exception:
        pass
    return None


def do_update(tag: str, download_url: str):
    """Lädt neue EXE herunter und startet einen Updater-Batch."""
    current_exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    if not current_exe.suffix.lower() == ".exe":
        print("[UPDATE] Nur im EXE-Modus unterstützt.")
        return

    new_exe = current_exe.parent / "GoonTracker_update.exe"
    print(f"[UPDATE] Lade {tag} herunter ...")
    try:
        urllib.request.urlretrieve(download_url, new_exe)
    except Exception as e:
        print(f"[UPDATE] Download fehlgeschlagen: {e}")
        return

    bat = current_exe.parent / "_gt_updater.bat"
    bat.write_text(
        f"@echo off\n"
        f"timeout /t 2 /nobreak >nul\n"
        f"move /y \"{new_exe}\" \"{current_exe}\"\n"
        f"start \"\" \"{current_exe}\"\n"
        f"del \"%~f0\"\n",
        encoding="utf-8",
    )
    print(f"[UPDATE] Update wird installiert – Tool startet neu ...")
    import subprocess
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
    )
    sys.exit(0)


# ══════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

PLAYER_LOG_PATH = (
    Path(os.environ.get("APPDATA", ""))
    .parent / "LocalLow" / "Battlestate Games" / "EscapeFromTarkov" / "Player.log"
)

POLL_INTERVAL    = 0.5    # Sekunden
FINALIZE_AFTER   = 8.0    # Sekunden Stille nach letztem TrySpawn → Ergebnis
NO_BOSS_TIMEOUT  = 90.0   # Sekunden ohne TrySpawn nach Raid-Start → kein Boss

# ── Wahrscheinlichkeitstabelle ────────────────────────────────────────────────
# Quellen: tarkov.dev API + empirische Kalibrierung aus echten Raids
# Format: (map, adj_n, supports) → [(pct_str, beschreibung, is_goon_alarm)]
# "~" = Schätzung  |  kein "~" = aus Logs bestätigt
_PROB: dict = {
    # ── ALLE GOON-MAPS (allgemein) ──────────────────────────────────────────
    # Smuggler 100% (2er 50% / 3er 50%), Goons ~25%, Event alle 100%
    (None, 2, 0): [
        ("~100%", "Smuggler (2er-Gruppe, Leader+1 Escort)", False),
    ],
    (None, 3, 0): [
        ("~70%",  "Smuggler (3er-Gruppe)", False),
        ("~30%",  "GOONS (3) + Smuggler (2er)",            True),
    ],
    (None, 5, 0): [
        ("~70%",  "GOONS (3) + Smuggler (2er-Gruppe)",     True),
        ("~30%",  "Andere 5er-Kombi ohne Guard-Struktur",  False),
    ],
    (None, 6, 0): [
        ("~85%",  "GOONS (3) + Smuggler (3er-Gruppe)",     True),
        ("~15%",  "2x Smuggler-Gruppen (beide Spawn-Zonen)", False),
    ],

    # ── CUSTOMS ─────────────────────────────────────────────────────────────
    # Reshala(1+4G=5T/4S), Partizan(solo=1T/0S), Goons(3T/0S), Smuggler(2-3T/0S)
    ("customs", 2, 0): [
        ("~100%", "Smuggler (2er-Gruppe)", False),
    ],
    ("customs", 3, 0): [
        ("~100%", "Smuggler (3er-Gruppe)  — kein Goon-Alarm", False),
        # Begruendung: Goons+Smuggler(2) = n=5, nicht n=3
    ],
    ("customs", 3, 1): [
        ("100%",  "Partizan + Smuggler (2er)  ← aus Logs bestaetigt", False),
    ],
    ("customs", 4, 0): [
        ("~65%",  "Smuggler (3er) + Partizan (4 indep. Entities)", False),
        ("~30%",  "GOONS (3) + Partizan (4 indep. Entities)",      True),
        ("~5%",   "Andere 4er-Kombi ohne Guard-Struktur",          False),
    ],
    ("customs", 4, 1): [
        ("~75%",  "Smuggler (3er) + Partizan (1 Guard-Marker)",    False),
        ("~25%",  "Andere Kombi mit 1 Guard",                      False),
    ],
    ("customs", 4, 3): [
        ("~80%",  "Boss + 3 Guards  (kein Reshala — der hat 4!)",  False),
        ("~20%",  "Andere 1-Leader-3-Guard Kombi",                 False),
    ],
    ("customs", 5, 0): [
        ("~75%",  "GOONS (3) + Smuggler (2er)",                    True),
        ("~25%",  "Andere 5er-Kombi ohne Guards",                  False),
    ],
    ("customs", 5, 4): [
        ("100%",  "RESHALA (1 Boss + 4 Guards)  ← aus Logs bestaetigt", False),
    ],
    ("customs", 6, 0): [
        ("~65%",  "GOONS (3) + Smuggler (3er)",                    True),
        ("~35%",  "2x Smuggler-Gruppen (Construction + Warehouse)", False),
    ],
    ("customs", 7, 4): [
        ("~95%",  "GOONS (3) + RESHALA (1+4G)",                    True),
    ],
    ("customs", 8, 4): [
        ("~90%",  "GOONS (3) + RESHALA (1+4G) + Smuggler",        True),
    ],

    # ── SHORELINE ───────────────────────────────────────────────────────────
    # bg=2 (2x AF immer), Smuggler 100%, Sanitar(1+2G=3T/2S) ~30-100%, Goons ~25%
    ("shoreline", 2, 0): [
        ("~80%",  "Smuggler (2er-Gruppe)", False),
        ("~20%",  "Unbekannte 2er-Kombi", False),
    ],
    ("shoreline", 3, 0): [
        ("~70%",  "Smuggler (3er-Gruppe)", False),
        ("~30%",  "GOONS (3) + Smuggler (2er)",                    True),
    ],
    ("shoreline", 3, 2): [
        ("~90%",  "Sanitar (1 Boss + 2 Guards = 3 Entities)",     False),
        ("~10%",  "Andere 1-Leader-2-Guard Kombi",                 False),
    ],
    ("shoreline", 5, 0): [
        ("~65%",  "GOONS (3) + Smuggler (2er)",                    True),
        ("~35%",  "Andere 5er-Kombi",                              False),
    ],
    ("shoreline", 5, 2): [
        ("~75%",  "Sanitar (3) + Smuggler (2er)",                  False),
        ("~25%",  "Andere 1-Leader-2-Guard + 2er Kombi",           False),
    ],
    ("shoreline", 6, 0): [
        ("100%",  "GOONS (3) + Smuggler (3er)  ← aus Logs bestaetigt", True),
    ],
    ("shoreline", 6, 2): [
        ("~85%",  "Sanitar (3) + Smuggler (3er)",                  False),
        ("~15%",  "Andere Guard-Kombi",                            False),
    ],
    ("shoreline", 7, 0): [
        ("~80%",  "GOONS (3) + Smuggler (3er) + 1 weiterer",       True),
        ("~20%",  "Andere 7er-Kombi ohne Guards",                  False),
    ],
    ("shoreline", 8, 2): [
        ("~85%",  "GOONS (3) + Sanitar (3) + Smuggler (2er)",      True),
        ("~15%",  "Andere Kombi",                                  False),
    ],
    ("shoreline", 9, 2): [
        ("~90%",  "GOONS (3) + Sanitar (3) + Smuggler (3er)",      True),
    ],

    # ── WOODS ───────────────────────────────────────────────────────────────
    # Shturman(1+2G=3T/2S, Svetloozerskiy-Brüder — verifiziert), Goons ~15%, Smuggler 100%
    ("woods", 2, 0): [
        ("~100%", "Smuggler (2er-Gruppe)", False),
    ],
    ("woods", 3, 0): [
        ("~70%",  "Smuggler (3er-Gruppe)", False),
        ("~30%",  "GOONS (3) + Smuggler (2er)",                    True),
    ],
    ("woods", 3, 2): [
        ("~90%",  "Shturman (1 Boss + 2 Guards = Svetloozerskiy-Brüder)",  False),
        ("~10%",  "Andere 1-Leader-2-Guard Kombi",                 False),
    ],
    ("woods", 5, 0): [
        ("~70%",  "GOONS (3) + Smuggler (2er)",                    True),
        ("~30%",  "Andere 5er-Kombi ohne Guards",                  False),
    ],
    ("woods", 5, 2): [
        ("~80%",  "Shturman (3) + Smuggler (2er)",                 False),
        ("~20%",  "Andere Kombi",                                  False),
    ],
    ("woods", 6, 0): [
        ("~80%",  "GOONS (3) + Smuggler (3er)",                    True),
        ("~20%",  "Andere 6er-Kombi",                              False),
    ],
    ("woods", 6, 2): [
        ("~80%",  "Shturman (3) + Smuggler (3er)",                 False),
        ("~20%",  "Andere Kombi",                                  False),
    ],
    ("woods", 8, 2): [
        ("~85%",  "GOONS (3) + Shturman (3) + Smuggler (2er)",     True),
    ],
    ("woods", 9, 2): [
        ("~90%",  "GOONS (3) + Shturman (3) + Smuggler (3er)",     True),
    ],

    # ── LIGHTHOUSE ──────────────────────────────────────────────────────────
    # Zryachiy(1+2G=3T/2S, IMMER 100%), Goons ~25%, Smuggler 100%
    ("lighthouse", 2, 0): [
        # Zryachiy immer -> n=2 mit bg=0 bedeutet Zryachiy(3T/2S) fehlt → ungewoehnlich
        ("~100%", "Smuggler (2er-Gruppe)  — Zryachiy fehlt (Spawn-Bug?)", False),
    ],
    ("lighthouse", 3, 0): [
        ("~60%",  "Smuggler (3er-Gruppe)  — Zryachiy fehlt (Spawn-Bug?)", False),
        ("~40%",  "GOONS (3) + Smuggler (2er)",                    True),
    ],
    ("lighthouse", 3, 2): [
        ("~85%",  "Zryachiy (1 Boss + 2 Guards = 3 Entities)",     False),
        ("~15%",  "Andere 1-Leader-2-Guard Kombi",                 False),
    ],
    ("lighthouse", 5, 0): [
        ("~70%",  "GOONS (3) + Smuggler (2er)",                    True),
        ("~30%",  "Andere 5er-Kombi",                              False),
    ],
    ("lighthouse", 5, 2): [
        ("~75%",  "Zryachiy (3) + Smuggler (2er)",                 False),
        ("~25%",  "GOONS (3) + Zryachiy ohne Smuggler",            True),
    ],
    ("lighthouse", 6, 0): [
        ("~80%",  "GOONS (3) + Smuggler (3er)",                    True),
        ("~20%",  "Andere 6er-Kombi",                              False),
    ],
    ("lighthouse", 6, 2): [
        ("~80%",  "Zryachiy (3) + Smuggler (3er)",                 False),
        ("~20%",  "Andere Kombi",                                  False),
    ],
    ("lighthouse", 8, 2): [
        ("~85%",  "GOONS (3) + Zryachiy (3) + Smuggler (2er)",     True),
    ],
    ("lighthouse", 9, 2): [
        ("~90%",  "GOONS (3) + Zryachiy (3) + Smuggler (3er)",     True),
    ],

    # ── INTERCHANGE ─────────────────────────────────────────────────────────
    # Killa(solo=1T/0S), Goons ~25%, Smuggler 100%
    ("interchange", 1, 0): [
        ("~70%",  "Killa (solo)", False),
        ("~30%",  "Smuggler (1 Escort) allein — sehr selten", False),
    ],
    ("interchange", 2, 0): [
        ("~60%",  "Smuggler (2er-Gruppe)", False),
        ("~25%",  "Killa (solo) + 1 Smuggler-Escort", False),
        ("~15%",  "Andere 2er-Kombi", False),
    ],
    ("interchange", 3, 0): [
        ("~55%",  "Smuggler (3er-Gruppe)", False),
        ("~25%",  "Killa (1) + Smuggler (2er)",                    False),
        ("~20%",  "GOONS (3) + Smuggler (2er)  — ohne Killa",      True),
    ],
    ("interchange", 4, 0): [
        ("~50%",  "Killa (1) + Smuggler (3er)",                    False),
        ("~35%",  "GOONS (3) + Killa (1)",                         True),
        ("~15%",  "Andere 4er-Kombi",                              False),
    ],
    ("interchange", 6, 0): [
        ("~70%",  "GOONS (3) + Smuggler (3er)",                    True),
        ("~20%",  "GOONS (3) + Killa (1) + Smuggler (2er)",        True),
        ("~10%",  "Andere Kombi",                                  False),
    ],

    # ── RESERVE ─────────────────────────────────────────────────────────────
    # Glukhar(1+6G=7T/6S), Event: ALLE Bosse 100% -> viele Spawns
    ("reserve", 7, 6): [
        ("~95%",  "Glukhar (1 Boss + 6 Guards)",                   False),
    ],
    ("reserve", 5, 4): [
        ("~90%",  "Reshala (1 Boss + 4 Guards)  — Event-Spawn",    False),
    ],

    # ── STREETS ─────────────────────────────────────────────────────────────
    # Kaban(Basmach+Gus+4 Followers+2-3 Snipers), Kollontay(1+4G=5T/4S), Goons ~15%, Smuggler 100%
    ("streets", 2, 0): [
        ("~100%", "Smuggler (2er-Gruppe)", False),
    ],
    ("streets", 3, 0): [
        ("~65%",  "Smuggler (3er-Gruppe)", False),
        ("~35%",  "GOONS (3) + Smuggler (2er)",                    True),
    ],
    ("streets", 5, 4): [
        ("~90%",  "Kollontay (1 Boss + 4 Guards = MVD Officers)",  False),
        ("~10%",  "Andere 1-Leader-4-Guard Kombi",                 False),
    ],
    ("streets", 6, 0): [
        ("~80%",  "GOONS (3) + Smuggler (3er)",                    True),
        ("~20%",  "Andere Kombi",                                  False),
    ],

    # ── GROUND ZERO ─────────────────────────────────────────────────────────
    # Kollontay(1+4G=5T/4S), Goons ~15%, Smuggler 100%
    ("groundzero", 2, 0): [
        ("~100%", "Smuggler (2er-Gruppe)", False),
    ],
    ("groundzero", 3, 0): [
        ("~65%",  "Smuggler (3er-Gruppe)", False),
        ("~35%",  "GOONS (3) + Smuggler (2er)",                    True),
    ],
    ("groundzero", 5, 4): [
        ("~90%",  "Kollontay (1 Boss + 4 Guards = MVD Officers)",  False),
        ("~10%",  "Andere 1-Leader-4-Guard Kombi",                 False),
    ],
    ("groundzero", 6, 0): [
        ("~80%",  "GOONS (3) + Smuggler (3er)",                    True),
        ("~20%",  "Andere Kombi",                                  False),
    ],

    # ── FACTORY ─────────────────────────────────────────────────────────────
    # Tagilla(solo=1T/0S), kein Smuggler, keine Goons
    ("factory", 1, 0): [
        ("~100%", "Tagilla (solo)", False),
    ],
}


def get_prob_lines(map_name: str, n: int, s: int) -> List[tuple]:
    """Gibt [(pct_str, beschreibung, is_goon)] zurück, map-spezifisch vor allgemein."""
    specific = _PROB.get((map_name, n, s))
    if specific:
        return specific
    generic = _PROB.get((None, n, s))
    if generic:
        return generic
    return []

EFT_LOGS_CANDIDATES = [
    Path("C:/Program Files (x86)/Steam/steamapps/common/Escape from Tarkov/build/Logs"),
    Path("C:/Battlestate Games/EFT (live)/build/Logs"),
    Path("C:/Battlestate Games/EFT/build/Logs"),
    Path("C:/Program Files/Steam/steamapps/common/Escape from Tarkov/build/Logs"),
]


def find_eft_session() -> Optional[str]:
    """Gibt den Session-Namen zurück, z.B. '2026.03.16_09-45-37_1.0.2.5.43579'."""
    for base in EFT_LOGS_CANDIDATES:
        if not base.exists():
            continue
        try:
            folders = sorted(
                [d for d in base.iterdir() if d.is_dir() and d.name.startswith("log_")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if folders:
                return folders[0].name[4:]  # "log_" abschneiden
        except OSError:
            pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  REGEX-MUSTER
# ══════════════════════════════════════════════════════════════════════════════

RE_LOAD_START    = re.compile(r'UnloadHideout', re.IGNORECASE)
RE_RAID_ACTIVE   = re.compile(r'Fixed DT = 0\.\d+')
RE_MAP_FROM_GEO  = re.compile(
    r'\[Geometry\] Loading .+[\\\/]Acoustics[\\\/]([A-Za-z][A-Za-z0-9_]+)[\\\/]',
    re.IGNORECASE,
)
RE_BOSS_TRYSPAWN = re.compile(r'BotBossSpawn:TrySpawn')
RE_BOSS_SUPPORTS = re.compile(r'SpawnBossSupports')
RE_RAID_END      = re.compile(
    r'StartLoadHideoutBundles|application quit|Unloading \d+ Unused',
    re.IGNORECASE,
)

MAP_NORM = {
    "shoreline":     "shoreline",
    "bigmap":        "customs",
    "custom":        "customs",   # Acoustics\custom_* → base = "custom"
    "customs":       "customs",
    "woods":         "woods",
    "lighthouse":    "lighthouse",
    "sandbox":       "groundzero",
    "groundzero":    "groundzero",
    "factory":       "factory",
    "laboratory":    "labs",
    "rezervbase":    "reserve",
    "reserve":       "reserve",
    "tarkovstreets": "streets",
    "streets":       "streets",
    "interchange":   "interchange",
}

GOON_MAPS = {
    "shoreline", "woods", "lighthouse", "customs",
    "interchange", "streets", "groundzero",
}

# Karten mit bekannten "Hintergrund"-Boss-Spawns die TrySpawn auslösen
# (z.B. Shoreline: 2x AF an Tor-Türmen = immer +2)
MAP_BACKGROUND_SPAWNS = {
    "shoreline": 2,   # 2x AF (Armed Forces) an GateTowerRight + GateTowerLeft
}

# Boss-Daten: name -> (guards, tryspawn_total, supports, maps, spawn_base_pct)
# tryspawn_total = 1 (leader) + guards (Mindestwert bei variabler Guard-Zahl)
# spawn_base_pct = serverseitiger Basiswert laut Wiki/tarkov.dev (~20-35%)
# Quelle: Spieler-Kalibrierung + tarkov.dev + Wiki (verifiziert März 2026)
BOSS_DATA = {
    # name:       guards  ts  sup  maps                            base%
    "reshala":   (4,  5,  4, ["customs"],                          28),
    # Shturman: 2 Guards (Svetloozerskiy-Brüder) — verifiziert
    "shturman":  (2,  3,  2, ["woods"],                            28),
    "tagilla":   (0,  1,  0, ["factory"],                          28),
    "killa":     (0,  1,  0, ["interchange"],                      28),
    "sanitar":   (2,  3,  2, ["shoreline"],                        28),
    "glukhar":   (6,  7,  6, ["reserve"],                          28),
    "zryachiy":  (2,  3,  2, ["lighthouse"],                      100),  # IMMER 100% (Insel)
    # Kaban: 4-6 aktive Guards (Basmach, Gus u.a.) + 2 stationäre Scharfschützen auf Dächern
    "kaban":     (-1,-1, -1, ["streets"],                          22),
    # Kollontay: 4 Guards (MVD Officers) — verifiziert
    "kollontay": (4,  5,  4, ["streets", "groundzero"],            22),
    "goons":     (0,  3,  0, ["shoreline","woods","lighthouse",
                               "customs","interchange","streets",
                               "groundzero"],                       17),  # rotierend ~15-20%
    "partizan":  (0,  1,  0, ["customs","woods","shoreline"],      -1),  # karma-abhängig
    "cultists":  (-1,-1, -1, ["woods","shoreline","reserve"],      10),  # nur nachts ~10%
    "smuggler":  (0,  3,  0, ["customs","woods","lighthouse",
                               "shoreline","interchange","streets",
                               "groundzero"],                      100),  # IMMER (2-3 Entities)
}


# ══════════════════════════════════════════════════════════════════════════════
#  GOON TRACKER  –  Zustandsmaschine
# ══════════════════════════════════════════════════════════════════════════════

class GoonTracker:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.reset()

    def reset(self):
        self.state              = "IDLE"
        self.map_name           = None
        self.tryspawn_count     = 0
        self.supports_count     = 0
        self.last_spawn_ts: Optional[float] = None
        self.raid_start_ts: Optional[float] = None
        self.result_shown       = False
        self._countdown_logged  = False
        self._noboss_warned     = False

    def feed(self, line: str):
        line = line.rstrip("\n")
        if not line.strip():
            return

        if self.debug and self.state not in ("IDLE", "RESULT"):
            if any(kw in line for kw in [
                "BotBoss", "TrySpawn", "SpawnBossSupports",
                "UnloadHideout", "StartLoad", "Fixed DT", "[Geometry]",
            ]):
                self._log(f"  [DBG] {line[:160]}")

        # Raid-Ende: in LOADING/SPAWNING/RESULT auf Hideout-Rückkehr prüfen
        if self.state in ("LOADING", "SPAWNING", "RESULT"):
            if RE_RAID_END.search(line):
                if "StartLoadHideoutBundles" in line or "application quit" in line:
                    if self.state in ("LOADING", "SPAWNING"):
                        self._log("◄ Raid abgebrochen – zurück ins Hauptmenü. Tool wartet ...")
                    else:
                        self._log("◄ Raid beendet – Tool wartet auf nächsten Start ...")
                    self.reset()
                    return

        # UnloadHideout = neuer Raid startet → in ALLEN States reset + neu starten
        # (deckt ab: IDLE, RESULT, aber auch LOADING/SPAWNING nach ALT+F4 ohne Log-Neuerstell.)
        # Bereits in LOADING → ignorieren (UnloadHideout erscheint mehrfach im Log)
        if RE_LOAD_START.search(line) and self.state != "LOADING":
            self.reset()
            self.state = "LOADING"
            self._log("► Raid lädt – Karte und Boss-Spawns werden erkannt ...")
            return

        # LOADING / SPAWNING
        if self.state in ("LOADING", "SPAWNING"):
            if self.map_name is None:
                m = RE_MAP_FROM_GEO.search(line)
                if m:
                    raw  = m.group(1).lower()
                    base = raw.split("_")[0]
                    self.map_name = MAP_NORM.get(base, base)
                    self._log(f"  ✓ Karte erkannt: {self.map_name.upper()}")

            if self.state == "LOADING" and RE_RAID_ACTIVE.search(line):
                self.state         = "SPAWNING"
                self.raid_start_ts = time.monotonic()
                self._log(f"  ✓ Raid aktiv – überwache Boss-Spawns (max. {NO_BOSS_TIMEOUT:.0f}s) ...")

            if self.state == "SPAWNING":
                if RE_BOSS_TRYSPAWN.search(line):
                    self.tryspawn_count += 1
                    self.last_spawn_ts   = time.monotonic()
                    self._log(f"  Boss-Spawn #{self.tryspawn_count} erkannt!")
                elif RE_BOSS_SUPPORTS.search(line):
                    self.supports_count += 1

    def tick(self):
        if self.state != "SPAWNING":
            return
        now = time.monotonic()

        if self.last_spawn_ts:
            elapsed = now - self.last_spawn_ts
            if elapsed > FINALIZE_AFTER:
                self._finalize()
                return
            # Countdown-Meldung einmalig wenn ~60% der Stille verstrichen
            if not self._countdown_logged and elapsed > FINALIZE_AFTER * 0.6:
                self._countdown_logged = True
                remaining = FINALIZE_AFTER - elapsed
                self._log(f"  ⏳ Letzte Spawn vor {elapsed:.0f}s – Ergebnis in ~{remaining:.0f}s ...")

        if self.raid_start_ts:
            in_raid = now - self.raid_start_ts
            if self.tryspawn_count == 0:
                # Warnung bei 60% der Timeout-Zeit
                if not self._noboss_warned and in_raid > NO_BOSS_TIMEOUT * 0.6:
                    self._noboss_warned = True
                    remaining = NO_BOSS_TIMEOUT - in_raid
                    self._log(f"  ℹ  {in_raid:.0f}s seit Raid-Start, noch kein Boss-Spawn – noch ~{remaining:.0f}s ...")
                if in_raid > NO_BOSS_TIMEOUT:
                    self._finalize()

    # ── Ergebnis ──────────────────────────────────────────────────────────

    def _finalize(self):
        if self.result_shown:
            return
        self.result_shown = True
        self.state = "RESULT"
        self._print_result()

    def _print_result(self):
        raw_n    = self.tryspawn_count
        s        = self.supports_count
        mapname  = (self.map_name or "unbekannt").upper()
        goon_map = (self.map_name or "") in GOON_MAPS

        # Hintergrund-Spawns abziehen (z.B. Shoreline 2x AF)
        bg = MAP_BACKGROUND_SPAWNS.get(self.map_name or "", 0)
        n  = raw_n - bg   # "echte" Boss-Spawns ohne Map-fixe Hintergrund-Bosse

        W   = 66
        sep = "═" * W
        def row(text=""):
            return f"║  {text:<{W-2}}║"

        if s > 0:
            sup_line = f"Wachen-Marker: {s}x SpawnBossSupports (Wachen-Boss bestätigt)"
        else:
            sup_line = f"Kein Wachen-Marker (SpawnBossSupports=0)"

        if bg > 0:
            bg_note = f"({raw_n} gesamt − {bg} Karten-Fixspawns = {n} Boss-Spawns)"
        else:
            bg_note = ""

        prob_lines = get_prob_lines(self.map_name or "", n, s)

        print()
        print(f"╔{sep}╗")
        print(row(f"KARTE:  {mapname}"))
        print(f"╠{sep}╣")

        if n <= 0 and raw_n == bg:
            # Nur Hintergrund-Spawns, kein echter Boss
            print(row("✓  KEIN BOSS-SPAWN erkannt"))
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            print(row("  Nur normale Scavs erwartet – kein Boss auf der Map"))
            print(row("  → Raid spielen oder ALT+F4 + Neustart"))
            self._play_other()

        elif n == 0 and raw_n > 0:
            print(row(f"✓  KEIN zusätzlicher Boss"))
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            print(row("  Nur Karten-Fixspawns (z.B. AF-Wachen) – kein echter Boss"))
            print(row("  → Raid spielen oder ALT+F4 + Neustart"))
            self._play_other()

        elif n == 1:
            print(row("ℹ   1 BOSS-SPAWN"))
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            solo = {
                "interchange": "Killa (solo, keine Wachen)",
                "factory":     "Tagilla (solo, keine Wachen)",
                "labs":        "Resident / Glukhar (solo möglich)",
            }.get(self.map_name or "", "Solo-Boss oder Partial-Spawn")
            print(row(f"  {solo}"))
            print(row(f"  {sup_line}"))
            self._play_other()

        elif n == 2:
            print(row("⚠   2 BOSS-SPAWNS  –  SMUGGLER (kleines Grüppchen)"))
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            print(row("  Smuggler Leader + 1 Escort  (50% Chance)"))
            print(row(f"  {sup_line}"))
            self._play_three()

        elif n == 3:
            leaders = n - s
            if s == 0:
                print(row("⚠ ⚠   3 BOSS-SPAWNS  –  GOONS oder SMUGGLER"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                print(row("  Kein Wachen-Marker — reine 3er-Gruppe"))
                if goon_map:
                    print(row("  Auf dieser Map: eher GOONS!"))
                else:
                    print(row("  Auf dieser Map: eher SMUGGLER-Event"))
            elif leaders == 2 and s == 1:
                # 2 Leader + 1 Guard: Partizan(solo) + Smuggler(2er) auf Customs bestätigt
                hint = {
                    "customs": "Partizan + Smuggler (2er-Gruppe)  — kein Goons-Alarm",
                }.get(self.map_name or "", f"2 unabh. Bosse, 1 Guard  ({sup_line})")
                print(row(f"ℹ   3 BOSS-SPAWNS  –  {hint}"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                print(row(f"  {sup_line}"))
                self._play_other()
                return
            elif s == 2:
                hint = {
                    "shoreline":  "Sanitar (1 Boss + 2 Guards)",
                    "woods":      "Shturman (1 Boss + 2 Guards = Svetloozerskiy-Brüder)",
                    "lighthouse": "Zryachiy (1 Boss + 2 Guards)",
                }.get(self.map_name or "", f"1 Leader + 2 Guards")
                print(row(f"ℹ   3 BOSS-SPAWNS  –  {hint}"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                print(row(f"  {sup_line}"))
                self._play_other()
                return
            else:
                print(row(f"⚠   3 BOSS-SPAWNS  –  {leaders} Leader + {s} Guard(s)"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                print(row(f"  {sup_line}"))
            self._play_three()

        elif n == 4:
            leaders = n - s
            if s == 0:
                # 4 unabhaengige Entities — kein Guard-Boss
                hint = {
                    "customs":   "Smuggler (3er) + Partizan  oder  GOONS + Partizan",
                    "shoreline": "Smuggler (3er) + 1 weiterer  oder  Smuggler(2) + 2",
                    "woods":     "Smuggler (3er) + 1 weiterer",
                }.get(self.map_name or "", "4 unabhaengige Boss-Entities (kein Guard-Boss)")
                print(row(f"ℹ   4 BOSS-SPAWNS  –  {hint}"))
                print(row(f"  Formel: {n} TrySpawn − {s} Supports = {leaders} Leader, 0 Guards"))
            else:
                # Guard-Struktur erkannt
                hint = {
                    "shoreline":  f"Sanitar ({leaders} Boss + {s} Guards)",
                    "customs":    f"Boss mit Guards ({leaders} Leader + {s} Guards)",
                    "woods":      f"Shturman ({leaders} Boss + {s} Guards)",
                    "reserve":    f"Glukhar ({leaders} Boss + {s} Guards)",
                    "streets":    f"Kollontay oder anderer Boss ({leaders} Leader + {s} Guards)",
                    "groundzero": f"Kollontay oder anderer Boss ({leaders} Leader + {s} Guards)",
                }.get(self.map_name or "", f"{leaders} Leader + {s} Guards")
                print(row(f"ℹ   4 BOSS-SPAWNS  –  {hint}"))
                print(row(f"  Formel: {n} TrySpawn − {s} Supports = {leaders} Leader, {s} Guards"))
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            print(row(f"  {sup_line}"))
            self._play_other()

        elif n == 5:
            leaders = n - s
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            if s == 0 and goon_map:
                print(row("ℹ   5 BOSS-SPAWNS  –  Smuggler(2) + Goons(3)"))
                print(f"╠{sep}╣")
                print(row("  Kein Wachen-Marker — zwei unabhaengige Gruppen"))
                self._play_three()
            elif s == 4:
                # Bestaetigt: 1 Leader + 4 Guards
                boss_hint = {
                    "customs":     "RESHALA (1 Boss + 4 Guards = Zawodskoy-Brüder)  — bestaetigt",
                    "reserve":     "GLUKHAR (1 Boss + 4 Guards)",
                    "streets":     "KOLLONTAY (1 Boss + 4 Guards = MVD Officers)",
                    "groundzero":  "KOLLONTAY (1 Boss + 4 Guards = MVD Officers)",
                }.get(self.map_name or "", f"1 Boss + 4 Guards ({leaders} Leader berechnet)")
                print(row(f"ℹ   5 BOSS-SPAWNS  –  {boss_hint}"))
                print(row(f"  {sup_line}"))
                self._play_other()
            else:
                combo = {
                    "shoreline":  "Smuggler(3) + Boss+Wachen?  ODER  Sanitar(4)+1",
                    "lighthouse": "Smuggler(2)+Goons(3)  ODER  Goons(3)+Zryachiy(2)",
                    "customs":    f"Reshala+4G  ODER  Smuggler(2)+Goons(3)  [{leaders} Leader, {s} Guards]",
                    "woods":      f"Smuggler(2)+Goons(3)  ODER  Shturman(3)+Smuggler(2)  [{leaders} Leader, {s} Guards]",
                }.get(self.map_name or "", f"Multi-Boss: {leaders} Leader, {s} Guards")
                print(row("ℹ   5 BOSS-SPAWNS"))
                print(row(f"  Wahrscheinlich: {combo}"))
                print(row(f"  {sup_line}"))
                self._play_other()

        elif n == 6:
            if s == 0:
                print(row("⚠ ⚠   6 BOSS-SPAWNS  –  GOONS + SMUGGLER!"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                print(row("  Kein Wachen-Marker → zwei unabhängige 3er-Gruppen"))
                print(row("  Goons (3) + Smuggler (3)  ← aus Logs bestätigt"))
                self._play_three()
            else:
                print(row("⚠   6 BOSS-SPAWNS + WACHEN-BOSS co-spawn!"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                combo = {
                    "customs":   "Goons/Smuggler (3) + Reshala/Partizan (3)?",
                    "shoreline": "Goons/Smuggler (3) + Sanitar (3)?",
                    "woods":     "Goons/Smuggler (3) + Shturman (3)?",
                }.get(self.map_name or "", "Multi-Boss mit Wachen")
                print(row(f"  {combo}"))
                print(row(f"  {sup_line}"))
                self._play_three()

        elif n == 7:
            leaders = n - s
            if s == 6:
                print(row("ℹ   7 BOSS-SPAWNS  –  GLUKHAR (1 Boss + 6 Guards)"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                print(row(f"  {sup_line}"))
                self._play_other()
            else:
                print(row("⚠ ⚠   7 BOSS-SPAWNS  –  GOONS + BOSS co-spawn!"))
                print(f"╠{sep}╣")
                if bg_note:
                    print(row(f"  {bg_note}"))
                combo = {
                    "shoreline":  "GOONS (3) + SANITAR (1+2G) + Smuggler (1)?",
                    "customs":    "GOONS (3) + RESHALA (1+4G)  ← Formel: 3 Leader + 4 Guards",
                    "woods":      "GOONS (3) + SHTURMAN (1+2G) + Smuggler (1)?",
                    "lighthouse": "GOONS (3) + ZRYACHIY (1+2G) + Smuggler (1)?",
                }.get(self.map_name or "", f"{leaders} Leader + {s} Guards")
                print(row(f"  {combo}"))
                print(row(f"  {sup_line}"))
                self._play_three()

        elif n >= 8:
            print(row(f"⚠   {n} BOSS-SPAWNS  –  VIELE BOSSE CO-SPAWN!"))
            print(f"╠{sep}╣")
            if bg_note:
                print(row(f"  {bg_note}"))
            print(row("  Mehrere Bosse + Wachen gleichzeitig gespawnt"))
            print(row(f"  {sup_line}"))
            self._play_three()

        else:
            print(row(f"?   {n} BOSS-SPAWNS"))
            if bg_note:
                print(row(f"  {bg_note}"))
            print(row(f"  {sup_line}"))

        # Wahrscheinlichkeiten
        if prob_lines:
            print(f"╠{sep}╣")
            print(row("  Wahrscheinlichkeiten:"))
            for pct, desc, is_goon in prob_lines:
                alarm = " ◄ GOON-ALARM" if is_goon else ""
                print(row(f"    {pct:<6}  {desc}{alarm}"))

        print(f"╚{sep}╝")
        print()
        print("[INFO] Warte auf nächsten Raid-Start...")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")

    def _play_three(self):
        if not HAS_SOUND:
            return
        try:
            for _ in range(3):
                winsound.Beep(1000, 180)
                time.sleep(0.08)
        except Exception:
            pass

    def _play_other(self):
        if not HAS_SOUND:
            return
        try:
            winsound.Beep(700, 350)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  FILE WATCHER
# ══════════════════════════════════════════════════════════════════════════════

class FileWatcher:
    _RECREATED = "__FILE_RECREATED__"
    _WAITING   = "__FILE_WAITING__"

    def __init__(self, path: Path):
        self.path   = path
        self._fh    = None
        self._ctime = None

    def open(self, from_start: bool = False) -> bool:
        for attempt in range(4):
            try:
                fh   = open(self.path, "r", encoding="utf-8", errors="replace")
                stat = os.stat(self.path)
                if not from_start:
                    fh.seek(0, 2)
                self._fh    = fh
                self._ctime = getattr(stat, "st_birthtime", stat.st_mtime)
                return True
            except (PermissionError, OSError):
                time.sleep(0.25 * (attempt + 1))
            except FileNotFoundError:
                return False
        return False

    def poll(self) -> List[str]:
        # Datei verschwunden (z.B. Cache-Clear) → warte auf Wiederkehr
        if self._fh is None:
            if self.path.exists():
                if self.open():
                    return [self._RECREATED]
            return [self._WAITING]

        try:
            stat = os.stat(self.path)
            # Neu erstellt (st_ctime geändert) ODER trunciert (Größe < Leseposition)
            recreated = getattr(stat, "st_birthtime", stat.st_mtime) != self._ctime
            if not recreated:
                try:
                    recreated = stat.st_size < self._fh.tell()
                except OSError:
                    pass
            if recreated:
                self._fh.close()
                self.open()
                return [self._RECREATED]
        except (FileNotFoundError, OSError):
            if self._fh:
                self._fh.close()
            self._fh = None
            return [self._WAITING]

        lines: List[str] = []
        try:
            while True:
                line = self._fh.readline()
                if not line:
                    break
                lines.append(line)
        except (PermissionError, OSError):
            pass
        return lines

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    @property
    def RECREATED(self) -> str:
        return self._RECREATED

    @property
    def WAITING(self) -> str:
        return self._WAITING


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def print_header(player_log: Path):
    W   = 66
    sep = "═" * W
    def row(text=""):
        return f"║  {text:<{W-2}}║"
    def shorten(p):
        s = str(p)
        return ("..." + s[-(W - 11):]) if len(s) > W - 8 else s

    print(f"╔{sep}╗")
    print(row("EFT  LIVE  GOON  TRACKER   v4.3"))
    print(row("Knight · Big Pipe · Birdeye  –  PvE Edition"))
    print(f"╠{sep}╣")
    print(row(f"Log:  {shorten(player_log)}"))
    print(row(f"Poll: {POLL_INTERVAL * 1000:.0f} ms"))
    print(f"╠{sep}╣")
    print(row("Signal:  BotBossSpawn:TrySpawn  +  SpawnBossSupports"))
    print(row("  3+0sup = Goons/Smuggler   |  4+Nsup = Boss+Wachen"))
    print(row("  6+0sup = Goons+Smuggler   |  7+Nsup = Goons+Boss"))
    print(f"╚{sep}╝")
    print()


def main():
    parser = argparse.ArgumentParser(description="EFT Live Goon Tracker v4.3")
    parser.add_argument("--debug", action="store_true",
                        help="Zeigt erkannte Signale in Echtzeit")
    parser.add_argument("--full",  action="store_true",
                        help="Liest Player.log komplett von Anfang")
    parser.add_argument("--log",   type=str,
                        help="Alternativer Pfad zur Player.log")
    args = parser.parse_args()

    log_path = Path(args.log) if args.log else PLAYER_LOG_PATH

    print_header(log_path)

    # ── Update-Check ──────────────────────────────────────────────────────────
    print("[~] Prüfe auf Updates ...", end="", flush=True)
    update = check_for_update()
    if update:
        tag, url, notes = update
        print(f"\r[!] Update verfügbar: v{VERSION} → {tag}          ")
        if notes:
            print("─" * 60)
            for line in notes.splitlines()[:12]:   # max 12 Zeilen Changelog
                print(f"  {line}")
            print("─" * 60)
        print(f"    Link: https://github.com/{GITHUB_REPO}/releases/latest")
        ans = input("    Jetzt aktualisieren? [J/n]: ").strip().lower()
        if ans in ("", "j", "ja", "y", "yes"):
            do_update(tag, url)
            return
    else:
        print(f"\r[✓] Tool ist aktuell  (v{VERSION})                 ")

    tracker      = GoonTracker(debug=args.debug)
    watcher      = FileWatcher(log_path)
    _log_missing = False

    session = find_eft_session()
    if session:
        print(f"[✓] EFT-Session aktiv:  {session}")
    else:
        print("[~] EFT-Session: nicht gefunden (Logs-Ordner unbekannt)")

    if watcher.open(from_start=args.full):
        print(f"[✓] Player.log gefunden – Tool bereit\n")
        if args.full:
            print("[INFO] Lese Player.log komplett (--full Modus)...")
        else:
            print("[INFO] Warte auf Raid-Start ...  (Strg+C zum Beenden)")
        print("[INFO] Starte das Tool VOR dem Klick auf 'Raid starten'!\n")
    else:
        _log_missing = True
        print("[~] Player.log nicht gefunden – warte auf EFT-Spielstart ...")
        print("[INFO] Starte EFT jederzeit – Tool verbindet sich automatisch.\n")

    try:
        while True:
            for line in watcher.poll():
                if line == watcher.WAITING:
                    if not _log_missing:
                        _log_missing = True
                        tracker.reset()
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"\n[{ts}] ⚠  Player.log verschwunden (Cache-Clear erkannt).")
                        print("[INFO] Warte auf EFT-Neustart – starte das Spiel neu ...")
                elif line == watcher.RECREATED:
                    _log_missing = False
                    tracker.reset()
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{ts}] ♻  Neue Spielsitzung – Player.log neugeladen.")
                    session = find_eft_session()
                    if session:
                        print(f"[✓] EFT-Session:  {session}")
                    print("[INFO] Spiel bereit – warte auf Raid-Start ...\n")
                else:
                    tracker.feed(line)

            tracker.tick()
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[INFO] Tracker beendet (Strg+C).")
    finally:
        watcher.close()


if __name__ == "__main__":
    main()
