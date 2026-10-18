"""First-boss storyline state and deterministic Treant encounter logic.

The module deliberately has no pygame dependency so the exact same state machine
can run in the single-player client and on the authoritative multiplayer server.
"""
from dataclasses import dataclass, field
import math
import random


QUEST_NAME = "Sonbaharın Laneti"
BOSS_NAME = "Sonbaharın Lanetli Muhafızı"
BOSS_ISLAND_NAME = "Kızılkök Adası"

WIZARD_STORY = (
    "Yiğit yolcu, bu köyü yalnız kılıcınla değil, merhametinle de kurtardın.",
    "Önündeki yol uzundur. Sana rüzgârın adımını bahşediyorum: yön tuşuna iki kez bas ve gölge gibi atıl.",
    "Bu Dash büyüsü, kadim mühürlere giden yolda sana yardım etsin.",
)

KEY_STORIES = (
    ("Köyün Yaşlısı", (
        "Dinle ey yiğit! Rivayet olunur ki vakti zamanında, denizlerin ötesinde yaprakları hiç solmayan bir ada varmış.",
        "O adanın bağrında, dağlardan ulu, kalelerden sağlam bir mahlûk yaşarmış. Derler ki gözleri cehennem ateşi misali yanar, kökleri toprağın altından uzanıp nice şövalyeyi karanlığa çekermiş.",
        "Üç bilge, bu canavarı kadim bir kapının ardına mühürlemiş. Kapıyı açacak üç anahtarı da üç ayrı köye emanet etmişler.",
        "Al şu anahtarı yiğidim. Lakin sakın unutma; üç mühür birleşmeden o kapı açılmazmış!",
    )),
    ("Köyün Bilgesi", (
        "Demek ilk mührü buldun ha, cesur yolcu! Öyleyse sana eskilerden kalma bir sır anlatayım.",
        "Vaktiyle yedi şövalye, Lanetli Muhafız'ın adasına yelken açmış. İlk gece orman fısıldamış, ikinci gece toprak titremiş. Üçüncü geceyse gökten kızıl yapraklar yağmış.",
        "Sabah olduğunda şövalyelerden eser kalmamış. Yalnızca paslı kılıçları, köklerin arasında bulunmuş.",
        "Al ikinci mührü. Sonuncusunu da bulursan kadim kapıyı açabilirmişsin. Amma velakin açtığın kapıyı kapamak, onu açmaktan müşkülmüş!",
    )),
    ("Son Muhafız", (
        "Ey cesur seyyah! Üçüncü mührün vakti nihayet gelmiş.",
        "Kadim kayıtlar der ki Lanetli Muhafız, Kızılkök Adası'nın derinliklerinde uyurmuş. Her sonbahar yaklaşırken gözlerini açar, köklerini toprağa salar, ormanın tüm canlılarına hükmedermiş.",
        "İşte son anahtar! Artık üç mühür senin elindedir.",
        "Kızılkök Adası'na var. Kadim taş çemberini bul. Üç anahtarı yerlerine koy ve mühürlü kapıyı aç.",
        "Lakin iyi belle yiğidim: O kapının ardında seni bekleyen şey, yalnızca bir canavar değil; asırlardır dinmeyen bir lanetmiş!",
    )),
)


@dataclass
class StoryState:
    repaired_villages: list = field(default_factory=list)
    dash_unlocked: bool = False
    ancient_keys: int = 0
    portal_open: bool = False
    keys_bound: bool = False
    boss_defeated: bool = False
    boss_loot_awarded: bool = False
    revision: int = 0

    def repair_village(self, village_id):
        """Record one stable village ID and return its one-based story ordinal."""
        village_id = int(village_id)
        if village_id in self.repaired_villages:
            return 0
        self.repaired_villages.append(village_id)
        ordinal = len(self.repaired_villages)
        self.dash_unlocked = True
        self.ancient_keys = min(3, max(0, ordinal - 1))
        self.revision += 1
        return ordinal

    def unlock_portal(self):
        if self.portal_open or self.ancient_keys < 3:
            return False
        self.portal_open = self.keys_bound = True
        self.revision += 1
        return True

    def complete_boss(self):
        if self.boss_defeated:
            return False
        self.boss_defeated = True
        self.revision += 1
        return True

    @property
    def quest_text(self):
        if self.boss_defeated:
            return f"{QUEST_NAME} — Tamamlandı"
        if self.portal_open:
            return f"Yeni Görev: {BOSS_NAME}'nı yen."
        if self.ancient_keys >= 3:
            return "Yeni Görev: Kızılkök Adası'ndaki kadim kapıyı bul."
        if not self.dash_unlocked:
            return "Köyü kurtar ve Büyücü ile konuş."
        return f"Kadim Anahtarlar: {self.ancient_keys}/3"

    def to_dict(self):
        return {
            "repaired_villages": list(self.repaired_villages),
            "dash_unlocked": self.dash_unlocked,
            "ancient_keys": self.ancient_keys,
            "portal_open": self.portal_open,
            "keys_bound": self.keys_bound,
            "boss_defeated": self.boss_defeated,
            "boss_loot_awarded": self.boss_loot_awarded,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data):
        data = data if isinstance(data, dict) else {}
        repaired = []
        for value in data.get("repaired_villages", []):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value not in repaired:
                repaired.append(value)
        state = cls(
            repaired_villages=repaired,
            dash_unlocked=bool(data.get("dash_unlocked", bool(repaired))),
            ancient_keys=max(0, min(3, int(data.get("ancient_keys", max(0, len(repaired) - 1))))),
            portal_open=bool(data.get("portal_open", False)),
            keys_bound=bool(data.get("keys_bound", False)),
            boss_defeated=bool(data.get("boss_defeated", False)),
            boss_loot_awarded=bool(data.get("boss_loot_awarded", False)),
            revision=max(0, int(data.get("revision", 0))),
        )
        if state.portal_open:
            state.keys_bound = True
        return state


class GuardianBoss:
    """Stationary two-phase boss with serializable telegraphs and hazards."""
    MAX_HEALTH = 120

    def __init__(self, x, y, seed=0):
        self.x, self.y = float(x), float(y)
        self.max_health = self.MAX_HEALTH
        self.health = self.max_health
        self.defeated = False
        self.state = "sealed"
        self.timer = 0.0
        self.cooldown = 1.2
        self.target_x, self.target_y = self.x, self.y
        self.leaf_zones = []
        self.hit_flash = 0.0
        self.revision = 0
        self._sequence = 0
        self._leaf_hit_targets = set()
        self.rng = random.Random(seed * 8191 + 0xB055)

    @property
    def phase(self):
        return 2 if self.health < self.max_health * .5 else 1

    @property
    def alive(self):
        return not self.defeated and self.health > 0

    def awaken(self):
        if self.state == "sealed" and self.alive:
            self.state, self.cooldown = "idle", .8
            self.revision += 1

    def apply_damage(self, amount):
        if not self.alive or self.state == "sealed":
            return False, False
        amount = max(0, int(amount))
        if not amount:
            return False, False
        self.health = max(0, self.health - amount)
        self.hit_flash = .16
        defeated_now = self.health <= 0
        if defeated_now:
            self.defeated = True
            self.state, self.timer = "defeated", 2.4
            self.leaf_zones = []
        self.revision += 1
        return True, defeated_now

    def _start_attack(self, target):
        pid, data = target
        distance = math.hypot(data["x"] - self.x, data["y"] - self.y)
        self._sequence += 1
        if self.phase == 2 and self._sequence % 3 == 0:
            kind, duration = "leaf_warning", .55
        elif distance <= 2.7 and self._sequence % 2 == 0:
            kind, duration = "sweep_warning", .42 if self.phase == 2 else .56
        else:
            kind, duration = "root_warning", .55 if self.phase == 2 else .78
        self.state, self.timer = kind, duration
        self.target_x, self.target_y = float(data["x"]), float(data["y"])
        self.leaf_zones = []
        self._leaf_hit_targets.clear()
        self.revision += 1

    def _resolve_warning(self, targets, damage_target):
        if self.state == "root_warning":
            for pid, data in targets.items():
                if data.get("active", True) and math.hypot(data["x"] - self.target_x,
                                                            data["y"] - self.target_y) <= 1.15:
                    damage_target(pid, 2, self.target_x, self.target_y)
            self.state, self.timer = "root_impact", .38
        elif self.state == "sweep_warning":
            for pid, data in targets.items():
                if data.get("active", True) and math.hypot(data["x"] - self.x, data["y"] - self.y) <= 2.65:
                    damage_target(pid, 2 if self.phase == 1 else 3, self.x, self.y)
            self.state, self.timer = "sweep_impact", .34
        else:
            zones = []
            for index in range(7):
                angle = index * math.tau / 7 + self.rng.uniform(-.18, .18)
                radius = self.rng.uniform(1.2, 4.2)
                zones.append((self.target_x + math.cos(angle) * radius,
                              self.target_y + math.sin(angle) * radius))
            self.leaf_zones = zones
            self.state, self.timer = "leaf_storm", 2.1
        self.revision += 1

    def update(self, dt, targets, damage_target, active=True):
        dt = max(0.0, min(.1, float(dt)))
        self.hit_flash = max(0.0, self.hit_flash - dt)
        if not active or not self.alive:
            return
        self.awaken()
        live = [(pid, data) for pid, data in targets.items() if data.get("active", True)]
        if self.state == "idle":
            self.cooldown -= dt
            if self.cooldown <= 0 and live:
                self._start_attack(min(live, key=lambda entry: math.hypot(
                    entry[1]["x"] - self.x, entry[1]["y"] - self.y)))
            return
        self.timer = max(0.0, self.timer - dt)
        if self.state == "leaf_storm":
            for pid, data in live:
                if pid not in self._leaf_hit_targets and any(
                        math.hypot(data["x"] - zx, data["y"] - zy) <= .8
                        for zx, zy in self.leaf_zones):
                    self._leaf_hit_targets.add(pid)
                    damage_target(pid, 2, data["x"], data["y"])
        if self.timer > 0:
            return
        if self.state.endswith("_warning"):
            self._resolve_warning(dict(live), damage_target)
        else:
            self.state = "idle"
            self.cooldown = .62 if self.phase == 2 else 1.05
            self.leaf_zones = []
            self.revision += 1

    def snapshot(self):
        return {
            "x": round(self.x, 3), "y": round(self.y, 3),
            "health": self.health, "max_health": self.max_health,
            "defeated": self.defeated, "state": self.state,
            "timer": round(self.timer, 3), "cooldown": round(self.cooldown, 3),
            "target_x": round(self.target_x, 3), "target_y": round(self.target_y, 3),
            "leaf_zones": [[round(x, 3), round(y, 3)] for x, y in self.leaf_zones],
            "hit_flash": round(self.hit_flash, 3), "revision": self.revision,
            "phase": self.phase,
        }

    def restore(self, data):
        if not isinstance(data, dict):
            return
        self.health = max(0, min(self.max_health, int(data.get("health", self.health))))
        self.defeated = bool(data.get("defeated", self.health <= 0))
        self.state = str(data.get("state", self.state))
        self.timer = max(0.0, float(data.get("timer", self.timer)))
        self.cooldown = max(0.0, float(data.get("cooldown", self.cooldown)))
        self.target_x = float(data.get("target_x", self.target_x))
        self.target_y = float(data.get("target_y", self.target_y))
        self.leaf_zones = [tuple(zone[:2]) for zone in data.get("leaf_zones", [])
                           if isinstance(zone, (list, tuple)) and len(zone) >= 2]
        self.hit_flash = max(0.0, float(data.get("hit_flash", self.hit_flash)))
        self.revision = max(self.revision, int(data.get("revision", self.revision)))
