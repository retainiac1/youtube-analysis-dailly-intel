// Pure colour-slot allocator for the trajectory chart. No echarts/DOM refs so it
// is unit-testable on its own (node --input-type=module). The trajectory must keep
// each tracked video's colour stable across re-renders: colour is keyed to the
// video's identity (its allocated slot), never to its position in the current list.
// Otherwise tracking/untracking another video re-indexes and recolours the rest.

// Assign a stable palette slot to each id in `currentIds`, reusing the slots a
// previous render handed out and freeing slots whose ids are gone.
//
//   currentIds: string[]  - ids shown this render
//   prevSlots:  { [id]: number } - slot assigned to each id last render (or {})
//
// Returns a fresh { [id]: number } where:
//   - an id present last render keeps its slot,
//   - a new id gets the lowest slot index not held by a surviving id,
//   - ids absent from currentIds are dropped (their slots become free to reuse).
//
// Slots are distinct across the returned ids, so as long as the palette is at
// least as long as currentIds, no two shown videos ever share a colour.
export function allocateSlots(currentIds, prevSlots) {
  const prev = prevSlots || {};
  const result = {};
  const taken = new Set();

  // Keep survivors on their existing slot first, so their colour does not move.
  for (const id of currentIds) {
    if (Object.prototype.hasOwnProperty.call(prev, id)) {
      result[id] = prev[id];
      taken.add(prev[id]);
    }
  }
  // Give each new id the lowest free slot (deterministic by currentIds order).
  let next = 0;
  for (const id of currentIds) {
    if (id in result) continue;
    while (taken.has(next)) next += 1;
    result[id] = next;
    taken.add(next);
  }
  return result;
}
