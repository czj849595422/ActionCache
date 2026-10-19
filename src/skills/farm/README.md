# farm

`farm` provides farming helpers for harvesting mature crops.

## Commands

- `farm.harvest` (continuous): harvest mature crops of a given crop name within `range`.

## Params

- `crop` (required): crop block name, one of `wheat` / `carrots` / `potatoes` / `beetroots` / `nether_wart`.
- `count` (optional): how many crops to harvest; omit to harvest all mature crops in range.
- `range` (optional): search radius in blocks. Defaults to `8`.
- `max_duration_ms` (optional): safety limit. Defaults to `600000`.

## Behavior

- Only mature crops are harvested (wheat/carrots/potatoes at age 7, beetroots/nether_wart at age 3).
- The action moves to each mature crop, digs it, and collects the dropped items.
- It is a continuous action: `start` / `update` (count, range) / `stop` / `status` are supported and it is interruptible.
- Completion reasons: `count_reached`, `all_harvested`, `max_duration_exceeded`.
- Errors: `invalid_params` (missing crop), `unknown_crop`, `no_mature_crop` (no mature crop in range).
