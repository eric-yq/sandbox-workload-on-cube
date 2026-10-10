"""B7 worker - text-game simulator (ALFWorld / TextWorld-style).

Stateless HTTP handler. POST /episode {seed, steps, batch} runs `batch`
episodes of `steps` actions each and returns aggregate stats. The
simulator is hand-rolled to keep the workload portable across amd64
/ arm64 with no native deps - the signal it captures is identical to
what the original B7 design called out: "scheduling + GIL + string
handling in tight Python loops".

We batch episodes per request because each individual episode is
sub-millisecond (~40 steps × ~5us); HTTP / asyncio dispatch noise
would otherwise dwarf the CPU signal we want to measure.

World model (matches ALFWorld's room+receptacle abstraction):
  - 8 rooms in a 4x2 grid, connected via doors
  - 30 items distributed across rooms / receptacles (fridge, table, ...)
  - Each episode picks one of 6 goal templates: fetch / cook / clean /
    place / examine / sequence
  - Agent runs a small heuristic: BFS to nearest goal-related room,
    open receptacle, take/put item. Heuristic is intentionally tiny;
    most CPU goes to state updates + observation rendering (string
    formatting), which is what we want to measure.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()

# ---------------------------------------------------------------
# World model
# ---------------------------------------------------------------

ROOMS = [
    "kitchen", "living_room", "bedroom", "bathroom",
    "garage", "garden", "office", "pantry",
]
# Adjacency (4x2 grid, row-major):
#   kitchen     - living_room - bedroom   - bathroom
#   garage      - garden      - office    - pantry
ADJACENCY = {
    "kitchen":     ["living_room", "garage"],
    "living_room": ["kitchen", "bedroom", "garden"],
    "bedroom":     ["living_room", "bathroom", "office"],
    "bathroom":    ["bedroom", "pantry"],
    "garage":      ["kitchen", "garden"],
    "garden":      ["garage", "living_room", "office"],
    "office":      ["garden", "bedroom", "pantry"],
    "pantry":      ["office", "bathroom"],
}

RECEPTACLES = [
    "fridge", "table", "shelf", "drawer", "cabinet",
    "sink", "oven", "couch", "desk", "wardrobe",
]

ITEMS = [
    "apple", "bread", "knife", "cup", "plate",
    "book", "lamp", "key", "phone", "wallet",
    "towel", "soap", "shampoo", "scissors", "pen",
    "remote", "blanket", "pillow", "shirt", "pants",
    "shoes", "hammer", "screwdriver", "rope", "bucket",
    "watering_can", "seeds", "potion", "candle", "mug",
]

GOAL_TEMPLATES = [
    "fetch:{item}",
    "place:{item}:{receptacle}",
    "examine:{item}",
    "clean:{item}",
    "cook:{item}",
    "sequence:{item}:{receptacle}",
]


@dataclass
class World:
    rooms: dict[str, dict] = field(default_factory=dict)
    item_loc: dict[str, tuple[str, str]] = field(default_factory=dict)
    agent_room: str = "kitchen"
    agent_inventory: list[str] = field(default_factory=list)
    visited: set[str] = field(default_factory=set)
    # Track once-only reward events so a heuristic that re-issues the
    # same action (e.g. repeated examine) doesn't inflate total_reward.
    rewarded: set[str] = field(default_factory=set)


def build_world(seed: int) -> World:
    rng = random.Random(seed)
    world = World()
    for r in ROOMS:
        world.rooms[r] = {
            "receptacles": rng.sample(RECEPTACLES, k=3),
            "items": [],
        }
    # Distribute items: each item goes to a (room, receptacle).
    for item in ITEMS:
        room = rng.choice(ROOMS)
        recep = rng.choice(world.rooms[room]["receptacles"])
        world.rooms[room]["items"].append(item)
        world.item_loc[item] = (room, recep)
    world.agent_room = rng.choice(ROOMS)
    world.visited.add(world.agent_room)
    return world


def make_goal(seed: int) -> str:
    rng = random.Random(seed * 7 + 1)
    tmpl = rng.choice(GOAL_TEMPLATES)
    item = rng.choice(ITEMS)
    recep = rng.choice(RECEPTACLES)
    return tmpl.format(item=item, receptacle=recep)


# ---------------------------------------------------------------
# Action handling
# ---------------------------------------------------------------

def render_observation(world: World) -> str:
    room = world.agent_room
    desc = world.rooms[room]
    lines = [
        f"You are in the {room}.",
        f"You see receptacles: {', '.join(desc['receptacles'])}.",
        f"You see items: {', '.join(desc['items']) if desc['items'] else '(none)'}.",
        f"Exits: {', '.join(ADJACENCY[room])}.",
        f"Inventory: {', '.join(world.agent_inventory) if world.agent_inventory else '(empty)'}.",
    ]
    return "\n".join(lines)


def step(world: World, action: str, goal: str) -> tuple[str, float]:
    """Apply `action`, return (observation, reward).

    Goal-completion rewards are recorded once via `world.rewarded`, so
    repeated identical actions don't keep stacking score.
    """
    parts = action.split()
    if not parts:
        return render_observation(world), 0.0

    def claim(tag: str, value: float) -> float:
        if tag in world.rewarded:
            return 0.0
        world.rewarded.add(tag)
        return value

    verb = parts[0]
    reward = 0.0
    if verb == "go" and len(parts) >= 2:
        target = parts[1]
        if target in ADJACENCY[world.agent_room]:
            new_room = target not in world.visited
            world.agent_room = target
            world.visited.add(target)
            if new_room:
                reward = 0.1
    elif verb == "take" and len(parts) >= 2:
        item = parts[1]
        room = world.rooms[world.agent_room]
        if item in room["items"]:
            room["items"].remove(item)
            world.agent_inventory.append(item)
            if goal == f"fetch:{item}":
                reward = claim(f"fetch:{item}", 1.0)
            elif goal == f"examine:{item}":
                reward = claim(f"take:{item}", 0.25)
    elif verb == "put" and len(parts) >= 4 and parts[2] == "on":
        item, recep = parts[1], parts[3]
        if item in world.agent_inventory and recep in world.rooms[world.agent_room]["receptacles"]:
            world.agent_inventory.remove(item)
            world.rooms[world.agent_room]["items"].append(item)
            if goal == f"place:{item}:{recep}" or goal == f"sequence:{item}:{recep}":
                reward = claim(f"place:{item}:{recep}", 1.0)
    elif verb == "examine" and len(parts) >= 2:
        # Cheap inspection - exercises string formatting but is
        # bounded by `claim` so a stuck heuristic can't farm reward.
        _ = render_observation(world)
        if goal == f"examine:{parts[1]}":
            reward = claim(f"examine:{parts[1]}", 0.5)
    elif verb == "clean" and len(parts) >= 2:
        if goal == f"clean:{parts[1]}" and parts[1] in world.agent_inventory:
            reward = claim(f"clean:{parts[1]}", 1.0)
    elif verb == "cook" and len(parts) >= 2:
        if goal == f"cook:{parts[1]}" and parts[1] in world.agent_inventory:
            reward = claim(f"cook:{parts[1]}", 1.0)
    elif verb == "look":
        _ = render_observation(world)

    return render_observation(world), reward


def heuristic_action(world: World, goal: str, rng: random.Random) -> str:
    """Pick the next action using a small handcrafted policy.

    Mostly straightforward: pathfind via BFS, take/place items. We
    don't run a real planner - just enough decision logic to keep
    each step's wall time non-trivial while remaining deterministic
    once `rng` is fixed.
    """
    parts = goal.split(":")
    target_item = parts[1] if len(parts) >= 2 else None
    target_recep = parts[2] if len(parts) >= 3 else None

    def first_hop_to(target_room: str) -> str | None:
        """BFS one hop toward target_room, or None if unreachable."""
        if target_room == world.agent_room:
            return None
        seen = {world.agent_room}
        q: deque = deque([(world.agent_room, [])])
        while q:
            r, path = q.popleft()
            if r == target_room:
                return path[0] if path else None
            for n in ADJACENCY[r]:
                if n not in seen:
                    seen.add(n)
                    q.append((n, path + [n]))
        return None

    # Already holding target?  Decide what to do with it.
    if target_item and target_item in world.agent_inventory:
        if target_recep:
            # We need a room that actually contains target_recep.
            # If current room has it, put now; otherwise navigate.
            if target_recep in world.rooms[world.agent_room]["receptacles"]:
                return f"put {target_item} on {target_recep}"
            # Find the nearest room that has target_recep.
            candidate_rooms = [
                r for r, info in world.rooms.items()
                if target_recep in info["receptacles"]
            ]
            if candidate_rooms:
                # Pick a deterministic candidate (closest by BFS hop count
                # is overkill here; first match keeps latency stable).
                hop = first_hop_to(candidate_rooms[0])
                if hop is not None:
                    return f"go {hop}"
                # already in candidate room (shouldn't happen given
                # the explicit check above); fall through.
            # No room contains this receptacle - drop the item somewhere
            # to avoid spinning forever (won't earn the reward but the
            # episode still does varied work).
            here_recep = world.rooms[world.agent_room]["receptacles"][0]
            return f"put {target_item} on {here_recep}"
        if goal.startswith("clean:"):
            return f"clean {target_item}"
        if goal.startswith("cook:"):
            return f"cook {target_item}"
        if goal.startswith("examine:"):
            return f"examine {target_item}"
        # Generic fetch achieved - just look around.
        return "look"

    # Target visible in current room?  Take it.
    if target_item and target_item in world.rooms[world.agent_room]["items"]:
        return f"take {target_item}"

    # Otherwise wander toward the room that holds the target.
    if target_item and target_item in world.item_loc:
        target_room = world.item_loc[target_item][0]
        if target_room == world.agent_room:
            return f"take {target_item}"
        hop = first_hop_to(target_room)
        if hop is not None:
            return f"go {hop}"

    # Fallback: random walk.
    return f"go {rng.choice(ADJACENCY[world.agent_room])}"


# ---------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------

class EpisodeRequest(BaseModel):
    seed: int = Field(..., ge=0, le=2**31 - 1)
    steps: int = Field(40, ge=1, le=200)
    # Number of episodes to run sequentially in one HTTP call.
    # We default to 10 because individual episodes are sub-millisecond
    # (~40 steps of pure-Python state updates), and at high concurrency
    # the HTTP / asyncio dispatch noise would dwarf the CPU signal we
    # actually want to measure. Batching makes wall_ms a few ms - in the
    # range the design doc calls "极低 per-step latency, 主要看调度+GIL".
    batch: int = Field(10, ge=1, le=200)


class EpisodeResponse(BaseModel):
    seed: int
    batch: int
    steps_done: int           # batch * steps
    total_reward: float
    rooms_visited: int        # max across the batch (per-episode max)
    wall_ms: float
    final_inventory: int      # last episode's



def _run_episode_blocking(seed: int, steps: int, batch: int = 1) -> dict:
    """Run `batch` episodes in sequence with deterministic seeds derived
    from the request seed. Returns aggregate stats.
    """
    total_reward = 0.0
    total_steps = 0
    max_rooms_visited = 0
    last_inventory = 0

    t0 = time.perf_counter()
    for k in range(batch):
        ep_seed = seed * 1_000_003 + k  # stable per-episode seed
        rng = random.Random(ep_seed)
        world = build_world(ep_seed)
        goal = make_goal(ep_seed)
        for _ in range(steps):
            action = heuristic_action(world, goal, rng)
            _obs, reward = step(world, action, goal)
            total_reward += reward
        total_steps += steps
        if len(world.visited) > max_rooms_visited:
            max_rooms_visited = len(world.visited)
        last_inventory = len(world.agent_inventory)
    wall_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "seed": seed,
        "batch": batch,
        "steps_done": total_steps,
        "total_reward": round(total_reward, 4),
        "rooms_visited": max_rooms_visited,
        "wall_ms": wall_ms,
        "final_inventory": last_inventory,
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "rooms": len(ROOMS), "items": len(ITEMS),
            "goal_templates": len(GOAL_TEMPLATES)}


@app.post("/episode", response_model=EpisodeResponse)
async def episode(req: EpisodeRequest) -> EpisodeResponse:
    # CPU-bound work; offload to a worker thread so the asyncio event
    # loop keeps accepting new requests. With many uvicorn workers
    # (UVICORN_WORKERS env), we get true parallelism across processes
    # which is what gives c8g its multi-core advantage on B7.
    data = await asyncio.to_thread(
        _run_episode_blocking, req.seed, req.steps, req.batch
    )
    return EpisodeResponse(**data)
