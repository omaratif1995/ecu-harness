# Trace an example and mirror it

Use when: the new element must be known in several modules (a PDU, a signal, a frame, a diagnostic service) and you do not know every place.

The project already contains a working example. Copy how it is wired.

1. Pick an existing sibling that does what the new element should do.
2. `ecu_find` with `users_of` set to the sibling: every container, in any module, that refers to it, and through which reference (`via`). Repeat for those users until nothing new turns up; two rounds are usually enough.
3. `ecu_find` with `targets_of` set to the sibling: everything it refers to itself.
4. One `ecu_edit` call with a goal:
   - `clone` the sibling and each user that must exist once per element, with new names and the values that differ;
   - `set` the references of the clones to the new containers;
   - `append` the new paths to lists that collect such elements.
5. Fix what the result lists under `new`. The vendor tool's findings usually name the place you missed. Then `ecu_change_finish`.

Tell the user which example you mirrored, so they can judge whether it was the right one.
