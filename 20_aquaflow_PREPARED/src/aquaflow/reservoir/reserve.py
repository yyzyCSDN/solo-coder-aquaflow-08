"""Protective (dead-storage) volume reservations.

Operators reserve volume for firefighting and emergency continuous supply while
the reservoir keeps turning over. Lifecycle: proposed -> confirmed ->
consumed/released. Normal dispatch may never draw protected volume; emergency
dispatch consumes a specific reservation (first the running operational slack,
then the reservation itself, so drawing only slack lands exactly on the
protection boundary).
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from .turnover import State, project_age

TOL = 1e-9

STATES = ("proposed", "confirmed", "consumed", "released")


@dataclass(frozen=True)
class Reservation:
    id: str
    volume: float
    expires_at: datetime
    state: str = "proposed"
    consumed: float = 0.0
    confirmed_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Reservoir:
    volume: float
    inflow: float            # projected inflow over the planning horizon
    outflow: float           # projected routine outflow over the horizon
    age_hours: float
    horizon_hours: float
    reservations: tuple[Reservation, ...] = ()


@dataclass(frozen=True)
class DispatchRequest:
    volume: float = 0.0
    emergency: bool = False
    reservation_id: str | None = None


@dataclass(frozen=True)
class Outlook:
    projected_volume: float
    projected_age_hours: float
    protected_volume: float          # confirmed reservations not expired
    available_operational_volume: float
    blocked: bool
    reasons: list[str]
    expired_reservation_ids: list[str]


def _valid(volume: float):
    if volume < -TOL:
        raise ValueError("negative volume")


def _get(reservoir: Reservoir, rid: str) -> Reservation:
    for r in reservoir.reservations:
        if r.id == rid:
            return r
    raise ValueError(f"unknown reservation: {rid}")


def is_active(r: Reservation, at: datetime) -> bool:
    return r.state == "confirmed" and at < r.expires_at


def is_expired(r: Reservation, at: datetime) -> bool:
    return r.state in ("proposed", "confirmed") and at >= r.expires_at


def remaining(r: Reservation) -> float:
    return max(0.0, r.volume - r.consumed)


def active_protection(reservoir: Reservoir, at: datetime) -> float:
    return sum(remaining(r) for r in reservoir.reservations if is_active(r, at))


def _put(reservoir: Reservoir, updated: Reservation) -> Reservoir:
    rows = tuple(updated if r.id == updated.id else r for r in reservoir.reservations)
    return replace(reservoir, reservations=rows)


def propose(reservoir: Reservoir, rid: str, volume: float, expires_at: datetime,
            at: datetime) -> Reservoir:
    _valid(volume)
    if at > expires_at:
        raise ValueError("reservation already expired at creation")
    if any(r.id == rid for r in reservoir.reservations):
        raise ValueError(f"duplicate reservation: {rid}")
    r = Reservation(rid, volume, expires_at, "proposed", 0.0, None, at)
    return replace(reservoir, reservations=reservoir.reservations + (r,))


def confirm(reservoir: Reservoir, rid: str, at: datetime) -> Reservoir:
    r = _get(reservoir, rid)
    if r.state != "proposed":
        raise ValueError(f"cannot confirm reservation in state {r.state}")
    if r.updated_at is not None and at < r.updated_at:
        raise ValueError("time reversal")
    if at >= r.expires_at:
        raise ValueError("reservation expired before confirmation")
    held = active_protection(reservoir, at)
    headroom = reservoir.volume - held
    if remaining(r) - headroom > TOL:
        raise ValueError(
            f"reservation {rid} competes with confirmed reservations: "
            f"needs {remaining(r):g}, only {headroom:g} uncommitted"
        )
    return _put(reservoir, replace(r, state="confirmed", confirmed_at=at, updated_at=at))


def release(reservoir: Reservoir, rid: str, at: datetime) -> Reservoir:
    r = _get(reservoir, rid)
    if r.state not in ("proposed", "confirmed"):
        raise ValueError(f"cannot release reservation in state {r.state}")
    if r.updated_at is not None and at < r.updated_at:
        raise ValueError("time reversal")
    return _put(reservoir, replace(r, state="released", updated_at=at))


def consume(reservoir: Reservoir, rid: str, amount: float, at: datetime
            ) -> tuple[Reservoir, float, float]:
    """Emergency withdrawal against a reservation.

    Takes operational slack (volume above the protection floor) first, then
    the reservation. Returns (new reservoir, used_operational, used_reserved).
    """
    _valid(amount)
    r = _get(reservoir, rid)
    if r.state != "confirmed":
        raise ValueError(f"cannot consume reservation in state {r.state}")
    if at >= r.expires_at:
        raise ValueError("reservation expired")
    protected = active_protection(reservoir, at)
    slack = max(0.0, reservoir.volume - protected)
    if amount - reservoir.volume > TOL:
        raise ValueError("consumption exceeds reservoir volume")
    from_res = max(0.0, amount - slack)
    if from_res - remaining(r) > TOL:
        raise ValueError("consumption exceeds reserved protection")
    from_op = amount - from_res
    used = r.consumed + from_res
    nr = replace(r, consumed=min(r.volume, used), updated_at=at)
    nr = replace(nr, state="consumed" if remaining(nr) <= TOL else nr.state)
    new = replace(reservoir, volume=reservoir.volume - amount)
    return _put(new, nr), from_op, from_res


def outlook(reservoir: Reservoir, at: datetime,
            request: DispatchRequest | None = None) -> Outlook:
    req = request or DispatchRequest()
    for v in (reservoir.volume, reservoir.inflow, reservoir.outflow,
              reservoir.age_hours, reservoir.horizon_hours, req.volume):
        _valid(v)
    projected = reservoir.volume + reservoir.inflow - reservoir.outflow
    if projected < -TOL:
        raise ValueError("projected outflow exceeds inflow and stored volume")
    projected = max(0.0, projected)
    protected = active_protection(reservoir, at)
    available = max(0.0, projected - protected)
    age = project_age(State(projected, reservoir.inflow, reservoir.outflow,
                            reservoir.age_hours), reservoir.horizon_hours)
    expired = sorted(r.id for r in reservoir.reservations if is_expired(r, at))
    reasons: list[str] = []
    if not req.emergency:
        if req.volume - available > TOL:
            reasons.append(
                f"normal dispatch of {req.volume:g} would encroach on protected "
                f"volume: only {available:g} operational available above "
                f"{protected:g} protected"
            )
    elif req.reservation_id is None:
        reasons.append("emergency dispatch must name a confirmed reservation")
    else:
        r = next((x for x in reservoir.reservations if x.id == req.reservation_id), None)
        if r is None:
            reasons.append(f"unknown reservation: {req.reservation_id}")
        elif r.state != "confirmed":
            reasons.append(f"reservation {req.reservation_id} not confirmed ({r.state})")
        elif at >= r.expires_at:
            reasons.append(f"reservation {req.reservation_id} expired")
        elif req.volume - projected > TOL:
            reasons.append(
                f"emergency draw {req.volume:g} exceeds projected volume {projected:g}"
            )
        else:
            slack = max(0.0, projected - protected)
            cover = slack + remaining(r)
            if req.volume - cover > TOL:
                reasons.append(
                    f"emergency draw {req.volume:g} exceeds slack {slack:g} plus "
                    f"reservation {req.reservation_id} remaining {remaining(r):g}"
                )
    return Outlook(projected, age, protected, available, bool(reasons),
                   reasons, expired)
