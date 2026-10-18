"""Player relay plus authoritative resource cooldowns, using the shared world seed.

Legacy clients retain the original player-dictionary response. Updated clients
negotiate protocol 2 and receive sparse depleted-resource snapshots and harvest
acknowledgements. The server owns respawn deadlines, including during client pause.
"""
import json
import math
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from worldgen import WORLD_VERSION, SpawnPool, can_stand, make_world, stable_random
from economy import inflation_multiplier
from gameplay_systems import AnimalSystem
from story_systems import GuardianBoss, StoryState

HOST = '127.0.0.1'
PORT = 5555
WORLD_SEED = 42


class ResourceAuthority:
    def __init__(self, world, clock=time.monotonic):
        self.nodes = {node.id: node for node in world.nodes}
        self.depleted = {}
        self.clock = clock
        self.seed = world.seed

    def harvest(self, uid, player):
        node = self.nodes.get(uid)
        now = self.clock()
        if (node is None or self.depleted.get(uid, 0) > now or player.get('sailing', False)
                or math.hypot(player['x'] - node.x, player['y'] - node.y) >= 1.65):
            return False
        self.depleted[uid] = now + 30 + stable_random(int(node.x * 100), int(node.y * 100), 83) * 10
        return True

    def snapshot(self):
        now = self.clock()
        self.depleted = {uid: deadline for uid, deadline in self.depleted.items() if deadline > now}
        return {uid: round(deadline - now, 3) for uid, deadline in self.depleted.items()}


class GameServer:
    def __init__(self, world, state_path=None):
        self.world = world
        self.resources = ResourceAuthority(world)
        self.players = {}
        self.island_owners = {}
        self.capture_count = 0
        self.winner = None
        self.trades = {}  # trade_id -> dict
        self.next_trade_id = 1
        self.start_time = time.monotonic()
        self.lock = threading.Lock()
        self.spawn_islands = {}
        self.spawn_cursor = 0
        self.spawn_pool = SpawnPool(world)
        self.animal_system = AnimalSystem(world)
        self.world_loot = {}
        self.next_world_loot_id = 1
        self.pickup_events = {}
        self.last_animal_update = time.monotonic()
        self.state_path = Path(state_path) if state_path else None
        self.story = StoryState()
        boss_position = world.boss_position or world.portal_position
        self.guardian = GuardianBoss(boss_position[0], boss_position[1], world.seed)
        self.last_boss_update = time.monotonic()
        self._load_state()

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            if int(data.get('seed', self.world.seed)) != self.world.seed:
                return
            self.story = StoryState.from_dict(data.get('story'))
            self.guardian.restore(data.get('boss'))
            self.island_owners = {int(k): v for k, v in data.get('islands', {}).items()}
            self.capture_count = int(data.get('capture_count', len(self.island_owners)))
            self.world_loot = {str(item['id']): dict(item) for item in data.get('world_loot', [])
                               if isinstance(item, dict) and 'id' in item}
            numeric_ids = [int(uid.rsplit(':', 1)[-1]) for uid in self.world_loot
                           if uid.startswith('animal-loot:') and uid.rsplit(':', 1)[-1].isdigit()]
            self.next_world_loot_id = max(numeric_ids, default=0) + 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def save_state(self):
        if not self.state_path:
            return
        payload = {'version': 1, 'seed': self.world.seed,
                   'story': self.story.to_dict(), 'boss': self.guardian.snapshot(),
                   'islands': {str(k): v for k, v in self.island_owners.items()},
                   'capture_count': self.capture_count,
                   'world_loot': self.loot_snapshot()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + '.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.state_path)
        except OSError:
            pass

    def _player_island(self, player):
        x, y = int(player.get('x', -1)), int(player.get('y', -1))
        if 0 <= y < len(self.world.owners) and 0 <= x < len(self.world.owners[0]):
            return self.world.owners[y][x]
        return -1

    def _damage_guardian_target(self, player_id, damage, source_x, source_y):
        player = self.players.get(player_id)
        now = time.monotonic()
        if player is None or now < player.get('invulnerable_until', 0.0):
            return
        player['health'] = max(0, player.get('health', 8) - max(0, int(damage)))
        player['last_hit_by'] = 'guardian'
        player['combat_revision'] = player.get('combat_revision', 0) + 1
        dx, dy = player['x'] - source_x, player['y'] - source_y
        distance = math.hypot(dx, dy)
        if distance > .001:
            nx, ny = player['x'] + dx / distance * 1.05, player['y'] + dy / distance * 1.05
            if can_stand(self.world, nx, player['y'], False):
                player['x'] = nx
            if can_stand(self.world, player['x'], ny, False):
                player['y'] = ny
        if player['health'] <= 0:
            player['deaths'] = player.get('deaths', 0) + 1
            self.respawn_combat_player(player_id, player)
        else:
            player['invulnerable_until'] = now + .65

    def update_guardian(self):
        now = time.monotonic()
        dt = min(.1, max(0.0, now - self.last_boss_update))
        self.last_boss_update = now
        targets = {pid: {'x': player['x'], 'y': player['y'],
                         'active': (player.get('spawn_confirmed', False)
                                    and not player.get('sailing', False)
                                    and self._player_island(player) == self.world.boss_island_index)}
                   for pid, player in self.players.items()}
        self.guardian.update(dt, targets, self._damage_guardian_target,
                             active=self.story.portal_open and not self.story.boss_defeated)

    def apply_story_action(self, player_id, request):
        player = self.players.get(player_id)
        if player is None or not player.get('spawn_confirmed') or player.get('sailing'):
            return False
        action = request.get('action')
        if action == 'unlock_portal':
            px, py = self.world.portal_position
            if math.hypot(player['x'] - px, player['y'] - py) > 2.2:
                return False
            changed = self.story.unlock_portal()
            if changed:
                self.guardian.awaken()
                self.save_state()
            return changed
        if action != 'boss_attack' or not self.story.portal_open or not self.guardian.alive:
            return False
        if self._player_island(player) != self.world.boss_island_index:
            return False
        kind = request.get('kind')
        rules = {'normal': (2.6, 1, .18), 'crouch': (2.9, 2, .34),
                 'dash': (3.7, 3, .42), 'bow': (10.0, 2, .30)}
        if kind not in rules:
            return False
        reach, damage, cooldown = rules[kind]
        try:
            level = max(1, min(4, int(request.get('weapon_level', 1))))
        except (TypeError, ValueError):
            level = 1
        damage += level - 1
        reach *= 1.0 + (.15 if kind == 'bow' else .06) * (level - 1)
        cooldown /= 1.0 + (.12 if kind == 'bow' else .08) * (level - 1)
        distance = math.hypot(player['x'] - self.guardian.x, player['y'] - self.guardian.y)
        now = time.monotonic()
        if distance > reach or now - player.get('last_boss_attack_at', -100.0) < cooldown:
            return False
        player['last_boss_attack_at'] = now
        hit, defeated = self.guardian.apply_damage(damage)
        if not hit:
            return False
        player['combat_xp'] = player.get('combat_xp', 0) + damage
        player['combat_revision'] = player.get('combat_revision', 0) + 1
        if defeated and self.story.complete_boss():
            drop_id = 'boss-loot:guardian-core'
            self.world_loot[drop_id] = dict(id=drop_id, x=round(self.guardian.x, 3),
                                            y=round(self.guardian.y, 3),
                                            item='Muhafız Özü', count=1)
            self.story.boss_loot_awarded = True
        self.save_state()
        return True

    def get_inflation(self):
        # Real elapsed time, shared by every client; slow growth is capped at 35%.
        return inflation_multiplier(time.monotonic() - self.start_time)

    def allocate_spawn(self, player_id):
        occupied = set(self.spawn_islands.values())
        island_idx, spawn = self.spawn_pool.acquire(occupied)
        self.spawn_islands[player_id] = island_idx
        return island_idx, spawn

    def update_animals(self):
        now = time.monotonic()
        dt = min(.1, max(0.0, now - self.last_animal_update))
        self.last_animal_update = now
        self.animal_system.update(dt, SimpleNamespace(x=-9999.0, y=-9999.0),
                                  lambda x, y: can_stand(self.world, x, y, False))

    def animal_snapshot(self):
        return [dict(id=a.id, island_index=a.island_index, kind=a.kind,
                     x=round(a.x, 3), y=round(a.y, 3), health=a.health,
                     max_health=a.max_health, alive=a.alive, facing=a.facing,
                     walk_phase=round(a.walk_phase, 2))
                for a in self.animal_system.animals]

    def loot_snapshot(self):
        return [dict(drop) for drop in self.world_loot.values()]

    def apply_entity_attack(self, player_id, request):
        player = self.players.get(player_id)
        target_id, kind = str(request.get('target_id')), request.get('kind')
        rules = {'normal': (1.85, 1, .18), 'crouch': (2.15, 2, .34),
                 'dash': (3.25, 3, .42), 'bow': (8.5, 2, .32)}
        target = next((a for a in self.animal_system.animals if a.id == target_id), None)
        if (player is None or target is None or not target.alive or kind not in rules
                or player.get('sailing') or not player.get('spawn_confirmed')):
            return False
        reach, damage, cooldown = rules[kind]
        try:
            weapon_level = max(1, min(4, int(request.get('weapon_level', 1))))
        except (TypeError, ValueError):
            weapon_level = 1
        damage += weapon_level - 1
        if kind == 'bow':
            reach *= 1.0 + .15 * (weapon_level - 1)
            cooldown /= 1.0 + .12 * (weapon_level - 1)
        else:
            reach *= 1.0 + .06 * (weapon_level - 1)
            cooldown /= 1.0 + .08 * (weapon_level - 1)
        dx, dy = target.x - player['x'], target.y - player['y']
        distance = math.hypot(dx, dy)
        if distance > reach:
            return False
        if kind != 'bow':
            fx, fy = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0),
                      'right': (1, 0)}.get(player.get('facing'), (0, 1))
            if distance > .001 and (dx / distance) * fx + (dy / distance) * fy < -.12:
                return False
        now = time.monotonic()
        if now - player.get('last_entity_attack_at', -100.0) < cooldown:
            return False
        player['last_entity_attack_at'] = now
        target.health = max(0, target.health - damage)
        player['combat_xp'] = player.get('combat_xp', 0) + 1
        player['combat_revision'] = player.get('combat_revision', 0) + 1
        if distance > .001:
            force = .58 if kind == 'dash' else .38
            nx, ny = target.x + dx / distance * force, target.y + dy / distance * force
            if self.animal_system.suitable_step(
                    target, nx, target.y, lambda x, y: can_stand(self.world, x, y, False)):
                target.x = nx
            if self.animal_system.suitable_step(
                    target, target.x, ny, lambda x, y: can_stand(self.world, x, y, False)):
                target.y = ny
        self.animal_system.flee(target, player['x'], player['y'])
        if target.health <= 0:
            target.alive = False
            target.death_handled = True
            base = 2 if target.kind == 'cow' else 1
            extra_range = 3 if target.kind == 'cow' else 2
            extra = int(stable_random(int(target.x * 100), int(target.y * 100), self.world.seed + 919)
                        * extra_range)
            drop_id = f"animal-loot:{self.next_world_loot_id}"
            self.next_world_loot_id += 1
            self.world_loot[drop_id] = dict(id=drop_id, x=round(target.x, 3), y=round(target.y, 3),
                                            item='Çiğ Et', count=base + extra)
        return True

    def apply_pickup(self, player_id, request):
        player = self.players.get(player_id)
        drop_id = str(request.get('id'))
        drop = self.world_loot.get(drop_id)
        if (player is None or drop is None or not player.get('spawn_confirmed')
                or math.hypot(player['x'] - drop['x'], player['y'] - drop['y']) > .95):
            return False
        self.world_loot.pop(drop_id, None)
        self.pickup_events.setdefault(player_id, []).append({
            'id': drop_id, 'item': drop['item'], 'count': drop['count'],
        })
        self.save_state()
        return True

    def respawn_combat_player(self, player_id, player):
        self.spawn_islands.pop(player_id, None)
        new_island, new_spawn = self.allocate_spawn(player_id)
        player.update(x=new_spawn[0], y=new_spawn[1], sailing=False, sail_raised=True,
                      health=player.get('max_health', 8), spawn_confirmed=False,
                      spawn_revision=player.get('spawn_revision', 0) + 1,
                      combat_revision=player.get('combat_revision', 0) + 1,
                      invulnerable_until=time.monotonic() + 1.0)

    def apply_pvp_attack(self, attacker_id, request):
        attacker = self.players.get(attacker_id)
        try:
            target_id = int(request.get('target_id'))
        except (TypeError, ValueError):
            return False
        target = self.players.get(target_id)
        kind = request.get('kind')
        rules = {
            'normal': (1.85, 1, .18),
            'crouch': (2.15, 2, .34),
            'dash': (3.25, 3, .42),
            'bow': (8.5, 2, .32),
        }
        if (attacker is None or target is None or target_id == attacker_id or kind not in rules
                or not attacker.get('spawn_confirmed') or not target.get('spawn_confirmed')):
            return False
        reach, damage, cooldown = rules[kind]
        try:
            weapon_level = max(1, min(4, int(request.get('weapon_level', 1))))
        except (TypeError, ValueError):
            weapon_level = 1
        damage += weapon_level - 1
        if kind == 'bow':
            reach *= 1.0 + .15 * (weapon_level - 1)
            cooldown /= 1.0 + .12 * (weapon_level - 1)
        else:
            reach *= 1.0 + .06 * (weapon_level - 1)
            cooldown /= 1.0 + .08 * (weapon_level - 1)
        # A sailor cannot perform a grounded sword attack.  The target's last
        # relayed sail flag is not an invulnerability toggle: a boat beside a
        # dock can still be struck, and stale movement packets cannot cancel a hit.
        if kind != 'bow' and attacker.get('sailing'):
            return False
        if kind == 'bow' and bool(attacker.get('sailing')) != bool(target.get('sailing')):
            return False
        if math.hypot(attacker['x'] - target['x'], attacker['y'] - target['y']) > reach:
            return False
        if kind != 'bow':
            facing = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}
            fx, fy = facing.get(attacker.get('facing'), (0, 1))
            dx, dy = target['x'] - attacker['x'], target['y'] - attacker['y']
            distance = max(.001, math.hypot(dx, dy))
            if (dx / distance) * fx + (dy / distance) * fy < -.12:
                return False
        now = time.monotonic()
        if now - attacker.get('last_attack_at', -100.0) < cooldown:
            return False
        if now < target.get('invulnerable_until', 0.0):
            return False
        attacker['last_attack_at'] = now
        target['health'] = max(0, target.get('health', 8) - damage)
        target['last_hit_by'] = attacker_id
        target['combat_revision'] = target.get('combat_revision', 0) + 1
        target['damage_revision'] = target.get('damage_revision', 0) + 1
        attacker['combat_xp'] = attacker.get('combat_xp', 0) + (2 if kind == 'bow' else max(1, damage))
        attacker['combat_revision'] = attacker.get('combat_revision', 0) + 1
        if kind != 'bow':
            dx, dy = target['x'] - attacker['x'], target['y'] - attacker['y']
            distance = max(.001, math.hypot(dx, dy))
            force = .52 if kind == 'dash' else .34
            nx, ny = target['x'] + dx / distance * force, target['y'] + dy / distance * force
            if can_stand(self.world, nx, target['y'], False):
                target['x'] = nx
            if can_stand(self.world, target['x'], ny, False):
                target['y'] = ny
        if target['health'] <= 0:
            attacker['kills'] = attacker.get('kills', 0) + 1
            target['deaths'] = target.get('deaths', 0) + 1
            self.respawn_combat_player(target_id, target)
        else:
            target['invulnerable_until'] = now + .42
        return True

    def handle_client(self, conn, addr, player_id):
        with self.lock:
            spawn_island, spawn = self.allocate_spawn(player_id)
            self.players[player_id] = dict(x=spawn[0], y=spawn[1], facing='down', moving=False,
                                           sailing=False, sail_raised=True, spawn_confirmed=False,
                                           spawn_revision=0, health=8, max_health=8,
                                           combat_revision=0, kills=0, deaths=0,
                                           combat_xp=0,
                                           damage_revision=0,
                                           last_hit_by=None, last_attack_at=-100.0,
                                           invulnerable_until=0.0, attack_kind=None,
                                           attack_timer=0.0, combo_step=0,
                                           last_entity_attack_at=-100.0,
                                           last_boss_attack_at=-100.0)
        last_sequence = 0
        last_combat_sequence = 0
        last_entity_sequence = 0
        last_story_sequence = 0
        last_animal_snapshot_at = 0.0
        try:
            with self.lock:
                hello = dict(id=player_id, seed=self.world.seed, world_version=WORLD_VERSION, resources=True,
                             pvp=True, animal_authority=True,
                             islands={str(k): v for k, v in self.island_owners.items()}, winner=self.winner,
                             inflation=self.get_inflation(), capture_count=self.capture_count,
                             spawn=list(spawn), spawn_island=spawn_island,
                             health=8, max_health=8, combat_revision=0, combat_xp=0,
                             kills=0, deaths=0,
                             animals=self.animal_snapshot(), world_loot=self.loot_snapshot(),
                             story=self.story.to_dict(), boss=self.guardian.snapshot())
            conn.sendall((json.dumps(hello) + '\n').encode())
            buffer = b''
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                buffer += data
                if len(buffer) > 2_000_000:
                    break
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not line:
                        continue
                    update = json.loads(line)
                    modern = update.get('protocol') == 2
                    if modern and update.get('world_version') != WORLD_VERSION:
                        return
                    with self.lock:
                        self.update_animals()
                        self.update_guardian()
                        player = self.players[player_id]
                        was_spawn_confirmed = player.get('spawn_confirmed', False)
                        if update.get('respawn') and player.get('spawn_confirmed'):
                            self.respawn_combat_player(player_id, player)
                        sent_x, sent_y = update.get('x'), update.get('y')
                        spawn_confirmed = (bool(update.get('spawn_confirmed'))
                                           and isinstance(sent_x, (int, float))
                                           and isinstance(sent_y, (int, float))
                                           and math.hypot(sent_x - player['x'], sent_y - player['y']) < 0.5)
                        if spawn_confirmed:
                            player['spawn_confirmed'] = True
                        for key in ('x', 'y', 'boat_angle', 'sail_angle'):
                            value = update.get(key, player.get(key, 0))
                            if isinstance(value, (int, float)) and math.isfinite(value):
                                if key not in ('x', 'y') or player.get('spawn_confirmed'):
                                    player[key] = value
                        facing = update.get('facing')
                        if facing in ('up', 'down', 'left', 'right'):
                            player['facing'] = facing
                        for key in ('moving', 'sailing', 'sail_raised'):
                            if isinstance(update.get(key), bool):
                                player[key] = update[key]
                        attack_kind = update.get('attack_kind')
                        player['attack_kind'] = attack_kind if attack_kind in ('normal', 'crouch', 'dash') else None
                        attack_timer = update.get('attack_timer', 0.0)
                        player['attack_timer'] = (max(0.0, min(1.0, attack_timer))
                                                  if isinstance(attack_timer, (int, float)) else 0.0)
                        combo_step = update.get('combo_step', 0)
                        player['combo_step'] = max(0, min(3, combo_step)) if isinstance(combo_step, int) else 0
                        if modern:
                            for request in update.get('harvests', []):
                                seq = request.get('seq', 0)
                                if isinstance(seq, int) and seq > last_sequence and was_spawn_confirmed:
                                    self.resources.harvest(request.get('id'), player)
                                    last_sequence = seq
                            for request in update.get('captures', []):
                                seq = request.get('seq', 0)
                                island_idx = request.get('island_index')
                                if isinstance(seq, int) and seq > last_sequence and isinstance(island_idx, int):
                                    if 0 <= island_idx < len(self.world.islands):
                                        if island_idx not in self.island_owners:
                                            self.island_owners[island_idx] = player_id
                                            self.capture_count += 1
                                            island = self.world.islands[island_idx]
                                            if island.village:
                                                self.story.repair_village(island_idx)
                                            self.save_state()
                                        owned = sum(1 for o in self.island_owners.values() if o == player_id)
                                        if owned >= 6 and self.winner is None:
                                            self.winner = player_id
                                        last_sequence = seq
                            for request in update.get('pvp_attacks', []):
                                if not isinstance(request, dict):
                                    continue
                                seq = request.get('seq', 0)
                                if isinstance(seq, int) and seq > last_combat_sequence:
                                    self.apply_pvp_attack(player_id, request)
                                    last_combat_sequence = seq
                            entity_actions = [(req.get('seq', 0), 'attack', req)
                                              for req in update.get('entity_attacks', []) if isinstance(req, dict)]
                            entity_actions += [(req.get('seq', 0), 'pickup', req)
                                               for req in update.get('pickups', []) if isinstance(req, dict)]
                            for seq, action, request in sorted(entity_actions, key=lambda entry: entry[0]):
                                if not isinstance(seq, int) or seq <= last_entity_sequence:
                                    continue
                                if action == 'attack':
                                    self.apply_entity_attack(player_id, request)
                                else:
                                    self.apply_pickup(player_id, request)
                                last_entity_sequence = seq
                            for request in update.get('story_actions', []):
                                if not isinstance(request, dict):
                                    continue
                                seq = request.get('seq', 0)
                                if isinstance(seq, int) and seq > last_story_sequence:
                                    self.apply_story_action(player_id, request)
                                    last_story_sequence = seq
                            # Trade requests
                            for treq in update.get('trade_actions', []):
                                action = treq.get('action')
                                if action == 'create':
                                    tid = str(self.next_trade_id)
                                    self.next_trade_id += 1
                                    self.trades[tid] = {
                                        'id': tid,
                                        'from_pid': player_id,
                                        'to_pid': treq.get('to_pid'),
                                        'offer': treq.get('offer', {}),
                                        'request': treq.get('request', {}),
                                        'status': 'pending',
                                        'time': time.time(),
                                    }
                                elif action == 'accept':
                                    tid = str(treq.get('trade_id'))
                                    trade = self.trades.get(tid)
                                    if trade and trade.get('status') == 'pending':
                                        trade['status'] = 'accepted'
                                elif action == 'cancel':
                                    tid = str(treq.get('trade_id'))
                                    trade = self.trades.get(tid)
                                    if trade and trade.get('status') == 'pending':
                                        trade['status'] = 'cancelled'

                        # Prune old trades
                        now_t = time.time()
                        self.trades = {tid: t for tid, t in self.trades.items() if now_t - t.get('time', 0) < 180}

                        others = {pid: dict(p) for pid, p in self.players.items() if pid != player_id}
                        response = (dict(players=others, resources=self.resources.snapshot(), ack=last_sequence,
                                         combat_ack=last_combat_sequence,
                                         entity_ack=last_entity_sequence,
                                         story_ack=last_story_sequence,
                                         story=self.story.to_dict(), boss=self.guardian.snapshot(),
                                         world_loot=self.loot_snapshot(),
                                         pickup_events=self.pickup_events.pop(player_id, []),
                                         islands={str(k): v for k, v in self.island_owners.items()}, winner=self.winner,
                                         inflation=self.get_inflation(), capture_count=self.capture_count,
                                         spawn=list((player['x'], player['y'])) if not player.get('spawn_confirmed') else None,
                                         spawn_island=self.spawn_islands.get(player_id),
                                         spawn_revision=player.get('spawn_revision', 0),
                                         spawn_confirmed=player.get('spawn_confirmed', False),
                                         health=player.get('health', 8), max_health=player.get('max_health', 8),
                                         combat_revision=player.get('combat_revision', 0),
                                         combat_xp=player.get('combat_xp', 0),
                                         authoritative_position=[player.get('x', 0), player.get('y', 0)],
                                         kills=player.get('kills', 0), deaths=player.get('deaths', 0),
                                         last_hit_by=player.get('last_hit_by'),
                                         trades=[t for t in self.trades.values() if t.get('to_pid') == player_id or t.get('from_pid') == player_id or t.get('to_pid') is None])
                                    if modern else others)
                        if modern and time.monotonic() - last_animal_snapshot_at >= .20:
                            response['animals'] = self.animal_snapshot()
                            last_animal_snapshot_at = time.monotonic()
                    conn.sendall((json.dumps(response) + '\n').encode())
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        finally:
            with self.lock:
                self.players.pop(player_id, None)
                self.spawn_islands.pop(player_id, None)
                # Cancel pending trades by this player
                for t in self.trades.values():
                    if t.get('from_pid') == player_id and t.get('status') == 'pending':
                        t['status'] = 'cancelled'
            conn.close()


def serve(host=HOST, port=PORT, seed=WORLD_SEED):
    print(f'[SERVER] Generating shared world {seed}...', flush=True)
    game = GameServer(make_world(seed), Path(__file__).with_name(f'server_save_{seed}.json'))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(8)
        print(f'[SERVER] Ready on {host}:{port}', flush=True)
        player_id = 0
        while True:
            conn, addr = listener.accept()
            threading.Thread(target=game.handle_client, args=(conn, addr, player_id), daemon=True).start()
            player_id += 1


if __name__ == '__main__':
    serve()
