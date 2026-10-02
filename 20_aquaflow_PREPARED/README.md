# AquaFlow
Dependency-free municipal drinking-water treatment and distribution operations platform.

## Reservoir protection volume (`aquaflow.reservoir.protection`)
Reserves protected/dead-storage volume for firefighting and emergency continuous
supply while normal turnover continues. Reservations support
`propose → confirm → consume / release` (plus automatic expiry), projected from
current volume, forecast inflow/outflow, protected volume and the reservation
expiry. Normal dispatch can never consume protected or confirmed-reserved volume;
blocked dispatches return a machine-readable reason (`protection_floor`,
`reserved_volume`, `projected_shortfall`, `reservation_expired`). Queries return
projected water age and available operating volume (`Outlook` / `DispatchDecision`).

