import unittest
from datetime import datetime, timedelta
from aquaflow.reservoir.reserve import (
    Reservoir, DispatchRequest, propose, confirm, release, consume, outlook,
    active_protection, remaining,
)

T0 = datetime(2026, 10, 1, 8, 0)
H = timedelta(hours=1)


class ReserveLifecycleTest(unittest.TestCase):
    def test_propose_confirm_consume_release(self):
        r = Reservoir(100, 20, 10, 6.0, 24.0)
        r = propose(r, "fire", 30, T0 + H, T0)
        self.assertEqual(r.reservations[0].state, "proposed")
        r = confirm(r, "fire", T0)
        self.assertEqual(active_protection(r, T0), 30)
        r, op, res = consume(r, "fire", 100, T0)
        self.assertEqual((op, res), (70.0, 30.0))
        self.assertEqual(r.volume, 0)
        self.assertEqual(r.reservations[0].state, "consumed")
        r2 = Reservoir(100, 20, 10, 6.0, 24.0)
        r2 = propose(r2, "em", 10, T0 + H, T0)
        r2 = release(r2, "em", T0)
        self.assertEqual(r2.reservations[0].state, "released")
        self.assertEqual(active_protection(r2, T0), 0)

    def test_expiry_drops_protection_and_is_reported(self):
        r = Reservoir(100, 0, 0, 6.0, 24.0)
        r = confirm(propose(r, "fire", 40, T0 + H, T0), "fire", T0)
        o = outlook(r, T0 + 2 * H)
        self.assertEqual(o.protected_volume, 0)
        self.assertEqual(o.expired_reservation_ids, ["fire"])
        with self.assertRaises(ValueError):
            consume(r, "fire", 1, T0 + 2 * H)


class ZeroInflowTest(unittest.TestCase):
    def test_zero_inflow_normal_dispatch_blocked_from_protected_volume(self):
        # 100 stored, no inflow, 40 routine outflow, 50 reserved fire protection
        r = Reservoir(100, 0, 40, 12.0, 24.0)
        r = confirm(propose(r, "fire", 50, T0 + H, T0), "fire", T0)
        o = outlook(r, T0)
        self.assertEqual(o.projected_volume, 60)
        self.assertEqual(o.available_operational_volume, 10)
        self.assertEqual(o.protected_volume, 50)
        # zero inflow: age only grows, no renewal
        self.assertAlmostEqual(o.projected_age_hours, 36.0)

        ok = outlook(r, T0, DispatchRequest(10))
        self.assertFalse(ok.blocked)  # drawing exactly down to the floor
        blocked = outlook(r, T0, DispatchRequest(10.001))
        self.assertTrue(blocked.blocked)
        self.assertIn("protected", blocked.reasons[0])
        # emergency draw against the reservation is allowed past operational
        em = outlook(r, T0, DispatchRequest(40, True, "fire"))
        self.assertFalse(em.blocked)


class CompetingReservationsTest(unittest.TestCase):
    def test_two_reservations_compete_for_scarce_volume(self):
        r = Reservoir(100, 0, 20, 6.0, 24.0)
        r = confirm(propose(r, "fire", 70, T0 + H, T0), "fire", T0)
        # after 70 protected against 100 stored, only 30 uncommitted
        r = propose(r, "emergency-supply", 50, T0 + H, T0)
        with self.assertRaises(ValueError) as ctx:
            confirm(r, "emergency-supply", T0)
        self.assertIn("competes", str(ctx.exception))

        # a smaller second reservation that fits beside the first confirms
        r = propose(r, "backup", 30, T0 + H, T0)
        r = confirm(r, "backup", T0)
        self.assertEqual(active_protection(r, T0), 100)
        # zero operational room: every normal dispatch is blocked
        o = outlook(r, T0, DispatchRequest(1))
        self.assertTrue(o.blocked)
        self.assertEqual(o.available_operational_volume, 0)

    def test_releasing_first_reservation_frees_competitor(self):
        r = Reservoir(100, 0, 0, 6.0, 24.0)
        r = confirm(propose(r, "fire", 70, T0 + H, T0), "fire", T0)
        r = propose(r, "backup", 50, T0 + H, T0)
        r = release(r, "fire", T0)
        r = confirm(r, "backup", T0)
        self.assertEqual(active_protection(r, T0), 50)


class ProtectionBoundaryTest(unittest.TestCase):
    def test_consumption_lands_exactly_on_protection_boundary(self):
        # 100 stored, zero inflow/outflow, 60 protected across two reservations
        r = Reservoir(100, 0, 0, 6.0, 24.0)
        r = confirm(propose(r, "fire", 40, T0 + H, T0), "fire", T0)
        r = confirm(propose(r, "backup", 20, T0 + H, T0), "backup", T0)
        self.assertEqual(active_protection(r, T0), 60)

        # emergency takes the 40 operational slack first -> exactly on the floor
        r, used_op, used_res = consume(r, "fire", 40, T0)
        self.assertEqual((used_op, used_res), (40.0, 0.0))
        self.assertEqual(r.volume, 60)
        self.assertEqual(active_protection(r, T0), 60)
        self.assertEqual(r.reservations[0].state, "confirmed")
        # sitting exactly on the boundary: boundary is inclusive, not consumed
        self.assertEqual(outlook(r, T0, DispatchRequest(0)).blocked, False)
        # one more operational litre is blocked (no slack left)
        self.assertTrue(outlook(r, T0, DispatchRequest(1)).blocked)
        # but the emergency reservation can now be drawn into
        em = outlook(r, T0, DispatchRequest(1, True, "fire"))
        self.assertFalse(em.blocked)

    def test_full_reservation_consumed_at_floor(self):
        # reservoir already at the protection floor with zero inflow
        r = Reservoir(60, 0, 0, 6.0, 24.0)
        r = confirm(propose(r, "fire", 60, T0 + H, T0), "fire", T0)
        r, used_op, used_res = consume(r, "fire", 60, T0)
        self.assertEqual((used_op, used_res), (0.0, 60.0))
        self.assertEqual(r.volume, 0)
        rec = next(x for x in r.reservations if x.id == "fire")
        self.assertEqual(rec.state, "consumed")
        self.assertEqual(remaining(rec), 0)
        # consuming more than remaining protection is refused
        with self.assertRaises(ValueError):
            consume(r, "fire", 1, T0)

    def test_partial_consumption_then_boundary(self):
        r = Reservoir(100, 0, 0, 6.0, 24.0)
        r = confirm(propose(r, "fire", 60, T0 + H, T0), "fire", T0)
        # 50 emergency: 40 slack + 10 of the reservation
        r, used_op, used_res = consume(r, "fire", 50, T0)
        self.assertEqual((used_op, used_res), (40.0, 10.0))
        self.assertEqual(r.volume, 50)
        self.assertEqual(active_protection(r, T0), 50)  # floor moved with it
        self.assertEqual(r.reservations[0].state, "confirmed")
        # remaining 10 operational slack drawn -> back exactly on the boundary
        r, _, _ = consume(r, "fire", 10, T0)
        self.assertEqual((r.volume, active_protection(r, T0)), (40, 40))


if __name__ == "__main__":
    unittest.main()
