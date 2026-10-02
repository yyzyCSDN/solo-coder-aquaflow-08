from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from .turnover import State,project_age

EPS=1e-9
PROPOSED,CONFIRMED,CONSUMED,RELEASED,EXPIRED="proposed","confirmed","consumed","released","expired"
REASON_FLOOR="protection_floor"
REASON_RESERVED="reserved_volume"
REASON_EXPIRED="reservation_expired"
REASON_HORIZON="projected_shortfall"
REASON_STATE="invalid_state"
ACTIVE=(PROPOSED,CONFIRMED)

class ReservationError(ValueError):
    def __init__(self,reason:str,message:str|None=None):
        super().__init__(message or reason);self.reason=reason

@dataclass(frozen=True)
class Reservoir:
    volume:float;inflow:float;outflow:float;protection:float;age_hours:float=0.0
    def __post_init__(self):
        if min(self.volume,self.inflow,self.outflow,self.protection,self.age_hours)<0:raise ValueError("negative")
        if self.protection>self.volume+EPS:raise ValueError("protection exceeds volume")

@dataclass
class Reservation:
    id:str;amount:float;expires_at:datetime;purpose:str;created_at:datetime
    state:str=PROPOSED
    def __post_init__(self):
        if self.amount<=0:raise ValueError("amount")
        if self.expires_at<=self.created_at:raise ValueError(REASON_EXPIRED)
    @property
    def remaining(self)->float:
        return self.amount if self.state==CONFIRMED else 0.0

@dataclass(frozen=True)
class Outlook:
    at:datetime;hours:float;volume:float;age_hours:float
    protection:float;reserved:float;available_operating:float

@dataclass(frozen=True)
class DispatchDecision:
    allowed:bool;requested:float;deliverable:float;reason:str|None;outlook:Outlook

def projected_volume(r:Reservoir,hours:float)->float:
    if hours<0:raise ValueError("negative")
    return max(0.0,r.volume+(r.inflow-r.outflow)*hours)

def projected_age_hours(r:Reservoir,hours:float)->float:
    return project_age(State(r.volume,r.inflow,r.outflow,r.age_hours),hours)

class ProtectionBook:
    """保护死库容预留台账：拟定/确认/消耗/释放，普通调度不得动用保护容积。"""
    def __init__(self,reservoir:Reservoir,now:datetime):
        self.reservoir=reservoir;self.now=now;self._rows:dict[str,Reservation]={}

    # ---- 时间推进 ----
    def tick(self,now:datetime):
        if now<self.now:raise ValueError("time reversal")
        hours=(now-self.now).total_seconds()/3600.0
        if hours>0:
            self.reservoir=replace(self.reservoir,
                volume=projected_volume(self.reservoir,hours),
                age_hours=projected_age_hours(self.reservoir,hours))
        self.now=now
        self._sweep()
        return self

    def _sweep(self):
        for row in self._rows.values():
            if row.state in ACTIVE and row.expires_at<=self.now:row.state=EXPIRED

    # ---- 预留生命周期 ----
    def propose(self,rid:str,amount:float,expires_at:datetime,purpose:str="emergency")->Reservation:
        self._sweep()
        if rid in self._rows:raise ReservationError(REASON_STATE,"duplicate reservation")
        row=Reservation(rid,amount,expires_at,purpose,self.now)
        self._rows[rid]=row
        return row

    def _min_headroom(self,hours:float,lock_row:Reservation|None=None,
                      include_row:Reservation|None=None)->float:
        """时段 [now, now+hours] 内 水位-保护-已确认预留（可临时纳入候选行）的最小值。

        净流量恒定时，水位线性变化、已预留量只在到期时刻阶跃下降，拐点必在 0/到期点/末端。
        """
        points={0.0,hours}
        rows=[r for r in self._rows.values() if r.state==CONFIRMED and r is not lock_row]
        if include_row is not None:rows.append(include_row)
        for row in rows:
            h=(row.expires_at-self.now).total_seconds()/3600.0
            if 0<h<hours:points.add(h)
        min_free=float("inf")
        include_hours=(include_row.expires_at-self.now).total_seconds()/3600.0 if include_row else None
        for h in points:
            at=self.now+timedelta(hours=h)
            reserved=self._reserved(at=at,lock_row=lock_row)
            if include_hours is not None and h<=include_hours+EPS:reserved+=include_row.amount
            min_free=min(min_free,projected_volume(self.reservoir,h)-self.reservoir.protection-reserved)
        return min_free

    def confirm(self,rid:str)->Reservation:
        self._sweep()
        row=self._rows[rid]
        if row.state==EXPIRED or row.expires_at<=self.now:
            row.state=EXPIRED;raise ReservationError(REASON_EXPIRED)
        if row.state!=PROPOSED:raise ReservationError(REASON_STATE)
        hours=(row.expires_at-self.now).total_seconds()/3600.0
        with_row=self._min_headroom(hours,include_row=row)
        if with_row< -EPS:
            current_free=self.reservoir.volume-self.reservoir.protection-self._reserved()
            if row.amount>current_free+EPS:
                # 当前就放不下：其他确认预留挤占 -> reserved，否则连死库容都保不住 -> floor
                raise ReservationError(REASON_RESERVED if self._reserved()>EPS else REASON_FLOOR)
            raise ReservationError(REASON_HORIZON)  # 当前够、预计流量导致未来缺口
        row.state=CONFIRMED
        return row

    def consume(self,rid:str,amount:float|None=None)->Reservation:
        """消防/紧急连续供水动用预留（紧急调度，可取用至保护边界）。"""
        self._sweep()
        row=self._rows[rid]
        if row.state==EXPIRED or row.expires_at<=self.now:
            row.state=EXPIRED;raise ReservationError(REASON_EXPIRED)
        if row.state!=CONFIRMED:raise ReservationError(REASON_STATE)
        take=row.amount if amount is None else amount
        if take<0 or take>row.amount+EPS:raise ReservationError(REASON_STATE)
        others=self._reserved(lock_row=row)
        if take>self.reservoir.volume-self.reservoir.protection-others+EPS:
            raise ReservationError(REASON_RESERVED if others>EPS and
                                   take<=self.reservoir.volume-self.reservoir.protection+EPS
                                   else REASON_FLOOR)
        # 动用后，其余确认预留在其各自到期前仍须被保护边界托住
        remaining_amount=row.amount-take
        latest=0.0
        for other in self._rows.values():
            if other.state==CONFIRMED and other is not row:
                latest=max(latest,(other.expires_at-self.now).total_seconds()/3600.0)
        if latest>0:
            post=self.reservoir.volume-take
            need=others+max(0.0,remaining_amount)
            worst=post+(self.reservoir.inflow-self.reservoir.outflow)*latest
            if worst<self.reservoir.protection+need-EPS:
                raise ReservationError(REASON_HORIZON)
        self.reservoir=replace(self.reservoir,volume=self.reservoir.volume-take)
        row.amount=remaining_amount
        if row.amount<=EPS:row.state=CONSUMED;row.amount=0.0
        return row

    def release(self,rid:str)->Reservation:
        self._sweep()
        row=self._rows[rid]
        if row.state not in ACTIVE:raise ReservationError(REASON_STATE)
        row.state=RELEASED;row.amount=0.0
        return row

    def get(self,rid:str)->Reservation:
        self._sweep();return self._rows[rid]

    # ---- 查询与普通调度 ----
    def _reserved(self,at:datetime|None=None,lock_row:Reservation|None=None)->float:
        at=self.now if at is None else at
        total=0.0
        for row in self._rows.values():
            if row.state==CONFIRMED and row is not lock_row and row.expires_at>=at:total+=row.amount
        return total

    def _commitment_horizon(self,hours:float|None)->float:
        """普通调度必须在所有已确认预留的剩余有效期内都不挤占，故默认看到最晚到期点。"""
        if hours is not None:return hours
        latest=0.0
        for row in self._rows.values():
            if row.state==CONFIRMED:
                latest=max(latest,(row.expires_at-self.now).total_seconds()/3600.0)
        return max(0.0,latest)

    def outlook(self,hours:float=0.0)->Outlook:
        at=self.now+timedelta(hours=hours)
        volume=projected_volume(self.reservoir,hours)
        reserved=self._reserved(at=at)
        return Outlook(at,hours,volume,projected_age_hours(self.reservoir,hours),
                       self.reservoir.protection,reserved,
                       max(0.0,volume-self.reservoir.protection-reserved))

    def plan_dispatch(self,amount:float,hours:float|None=None)->DispatchDecision:
        """普通周转调度预检：全程不得消耗保护容积或已确认预留。hours 默认覆盖全部预留有效期。"""
        if amount<0:raise ValueError("negative")
        horizon=self._commitment_horizon(hours)
        view=self.outlook(horizon)
        min_free=self._min_headroom(horizon)
        bare=min(projected_volume(self.reservoir,h)-self.reservoir.protection
                 for h in (0.0,horizon))
        deliverable=max(0.0,min_free)
        if amount<=deliverable+EPS:
            return DispatchDecision(True,amount,min(amount,deliverable),None,view)
        reason=REASON_RESERVED if amount<=bare+EPS else REASON_FLOOR
        return DispatchDecision(False,amount,deliverable,reason,view)

    def dispatch(self,amount:float)->DispatchDecision:
        decision=self.plan_dispatch(amount)
        if not decision.allowed:raise ReservationError(decision.reason)
        self.reservoir=replace(self.reservoir,volume=self.reservoir.volume-decision.deliverable)
        return decision
