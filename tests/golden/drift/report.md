# DriftGuard report

- stages: 3
- edges: 2
- missing refs: 1
- drifts: 2 (0 breaking)

## Unresolved refs
- `stg_orders` references missing stage `raw_orders`

## Drifts
### stg_orders -> fct_orders [warning]
- added (non-breaking): `status`
### stg_orders -> fct_orders_renamed [warning]
- added (non-breaking): `status`
