import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame

import main
from server import GameServer
from story_systems import GuardianBoss, StoryState
from worldgen import compute_tile_pixels, find_spawn, make_world


class StoryStateTests(unittest.TestCase):
    def test_unique_villages_unlock_dash_then_exactly_three_keys(self):
        state = StoryState()
        self.assertEqual(state.repair_village(11), 1)
        self.assertTrue(state.dash_unlocked)
        self.assertEqual(state.ancient_keys, 0)
        self.assertEqual(state.repair_village(11), 0)
        self.assertEqual([state.repair_village(value) for value in (12, 13, 14)], [2, 3, 4])
        self.assertEqual(state.ancient_keys, 3)
        self.assertTrue(state.unlock_portal())
        self.assertFalse(state.unlock_portal())
        self.assertTrue(state.keys_bound)

    def test_guardian_is_deterministic_and_enters_phase_two(self):
        boss = GuardianBoss(10, 10, 42)
        boss.awaken()
        hit, defeated = boss.apply_damage(61)
        self.assertTrue(hit)
        self.assertFalse(defeated)
        self.assertEqual(boss.phase, 2)
        snapshot = boss.snapshot()
        restored = GuardianBoss(10, 10, 42)
        restored.restore(snapshot)
        self.assertEqual(restored.health, boss.health)
        self.assertEqual(restored.phase, 2)


class BossQuestIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        pygame.display.set_mode((1200, 780))
        cls.world = make_world(42)
        cls.tiles = pygame.image.frombuffer(compute_tile_pixels(cls.world),
                                            (main.COLS, main.ROWS), "RGB").convert()

    def session(self, save_path=None):
        world = copy.deepcopy(self.world)
        return main.GameSession(world, world.ports, find_spawn(world), self.tiles,
                                None, world.seed, {}, save_path=save_path)

    def test_world_has_one_hidden_deterministic_boss_island(self):
        other = make_world(42)
        self.assertEqual(self.world.boss_island_index, other.boss_island_index)
        self.assertEqual(self.world.portal_position, other.portal_position)
        self.assertEqual(self.world.islands[self.world.boss_island_index].name, "Kızılkök Adası")
        session = self.session()
        self.assertIn(self.world.boss_island_index, session.map_ui.hidden_islands)
        self.assertFalse(session.start_dash((1, 0)))

    def test_repair_progress_and_save_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            save_path = Path(folder) / "story.json"
            session = self.session(save_path)
            village_ids = [index for index, island in enumerate(session.world.islands)
                           if island.village][:4]
            for index in village_ids:
                session.handle_village_repaired(index, show_dialogue=False)
            self.assertEqual(session.story.ancient_keys, 3)
            self.assertTrue(session.story.dash_unlocked)
            self.assertNotIn(session.world.boss_island_index, session.map_ui.hidden_islands)
            session.save_story()
            saved = json.loads(save_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["story"]["ancient_keys"], 3)
            restored = self.session(save_path)
            self.assertEqual(restored.story.repaired_villages, village_ids)
            self.assertTrue(restored.story.dash_unlocked)

    def test_server_repair_state_and_boss_are_authoritative(self):
        server = GameServer(copy.deepcopy(self.world))
        village_ids = [index for index, island in enumerate(server.world.islands)
                       if island.village][:4]
        for index in village_ids:
            server.story.repair_village(index)
        self.assertEqual(server.story.ancient_keys, 3)
        self.assertEqual(server.guardian.health, GuardianBoss.MAX_HEALTH)
        px, py = server.world.portal_position
        server.players[7] = {
            "x": px, "y": py, "facing": "down", "sailing": False,
            "spawn_confirmed": True, "health": 8, "max_health": 8,
            "combat_revision": 0, "combat_xp": 0, "last_boss_attack_at": -100.0,
        }
        self.assertTrue(server.apply_story_action(7, {"action": "unlock_portal"}))
        self.assertTrue(server.story.portal_open)
        server.players[7].update(x=server.guardian.x, y=server.guardian.y)
        while server.guardian.alive:
            server.players[7]["last_boss_attack_at"] = -100.0
            self.assertTrue(server.apply_story_action(
                7, {"action": "boss_attack", "kind": "bow", "weapon_level": 4}))
        self.assertTrue(server.story.boss_defeated)
        self.assertEqual(list(server.world_loot), ["boss-loot:guardian-core"])
        self.assertFalse(server.apply_story_action(
            7, {"action": "boss_attack", "kind": "bow", "weapon_level": 4}))


if __name__ == "__main__":
    unittest.main()
