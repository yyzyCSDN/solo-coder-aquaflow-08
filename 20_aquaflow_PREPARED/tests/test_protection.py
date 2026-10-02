import unittest
from datetime import datetime,timedelta
from aquaflow.reservoir.protection import (
    ProtectionBook,Reservoir,ReservationError,
    REASON_FLOOR,REASON_RESERVED,REASON_HORIZON,REASON_EXPIRED,
    PROPOSED,CONFIRMED,CONSUMED,RELEASED,EXPIRED)

class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.t0=datetime(2026,10,2,8,0,0)

    # ---- 零流入：只出不进，到期前会跌破底线，确认被阻止；到期释放后恢复 ----
    def test_zero_inflow(self):
        # 1000 m3，死库容 400，无入流，出流 100 m3/h
        book=ProtectionBook(Reservoir(1000,0,100,400),self.t0)
        book.propose("f1",500,self.t0+timedelta(hours=2))
        # 当前可用 600，但 2 小时后只剩 400，无法在整个有效期内保住 500
        with self.assertRaises(ReservationError) as cm:book.confirm("f1")
        self.assertEqual(cm.exception.reason,REASON_HORIZON)
        # 缩到 300，全程可行（到期时水位 500，可用 100…… 300>100，仍不行）
        book.release("f1")
        book.propose("f2",400,self.t0+timedelta(hours=2))
        book.confirm("f2")  # 到期时水位 800，800-400=400，恰好盖住预留
        # 当前净运行容积 600-400=200；但全程（到期前）净空收窄到 0，默认看全程 -> 阻止
        blocked=book.plan_dispatch(1)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason,REASON_RESERVED)
        self.assertAlmostEqual(blocked.deliverable,0)
        # 只看当前时刻则不受未来收紧影响
        self.assertTrue(book.plan_dispatch(200,hours=0).allowed)
        self.assertFalse(book.plan_dispatch(201,hours=0).allowed)
        # 推进到过期后，预留失效，仅受死库容约束（此时水位 700，净空 300）
        book.tick(self.t0+timedelta(hours=3))
        self.assertEqual(book.get("f2").state,EXPIRED)
        self.assertTrue(book.plan_dispatch(50).allowed)
        decision=book.plan_dispatch(301)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason,REASON_FLOOR)

    def test_zero_inflow_still_dispatchable_now(self):
        # 零流入但当前有水：短期内普通调度可正常周转
        book=ProtectionBook(Reservoir(1000,0,100,400),self.t0)
        ok=book.plan_dispatch(200,hours=0)
        self.assertTrue(ok.allowed)
        self.assertAlmostEqual(ok.outlook.available_operating,600)
        # 6 小时后水位 400，贴底：任何正流量被阻止
        d=book.plan_dispatch(1,hours=6)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason,REASON_FLOOR)

    # ---- 两个预留互相竞争 ----
    def test_two_reservations_compete(self):
        book=ProtectionBook(Reservoir(1000,50,50,400),self.t0)  # 水位恒定
        exp=self.t0+timedelta(hours=4)
        book.propose("a",400,exp,purpose="fire");book.confirm("a")
        book.propose("b",300,exp,purpose="emergency supply")
        # 仅剩 200 运行容积，300 抢不到
        with self.assertRaises(ReservationError) as cm:book.confirm("b")
        self.assertEqual(cm.exception.reason,REASON_RESERVED)
        self.assertEqual(book.get("b").state,PROPOSED)  # 仍保持拟定
        # 缩减后可确认
        r=book.get("b");r.amount=200
        book.confirm("b")
        self.assertAlmostEqual(book.outlook().available_operating,0)
        # 普通调度只能取到 0
        d=book.plan_dispatch(1)
        self.assertFalse(d.allowed);self.assertEqual(d.reason,REASON_RESERVED)
        self.assertAlmostEqual(d.deliverable,0)
        # 释放 a 后 b 独占，普通调度可取回 a 的份额
        book.release("a")
        self.assertEqual(book.get("a").state,RELEASED)
        d=book.plan_dispatch(400)
        self.assertTrue(d.allowed);self.assertAlmostEqual(d.deliverable,400)
        with self.assertRaises(ReservationError) as cm:book.dispatch(401)
        self.assertEqual(cm.exception.reason,REASON_RESERVED)

    # ---- 消耗后刚好落在保护边界 ----
    def test_consume_exactly_on_protection_boundary(self):
        book=ProtectionBook(Reservoir(900,0,0,400),self.t0)  # 净运行容积 500
        book.propose("fire",500,self.t0+timedelta(hours=1));book.confirm("fire")
        row=book.consume("fire")  # 全部消耗 900-500=400，恰好贴死库容
        self.assertEqual(row.state,CONSUMED);self.assertAlmostEqual(row.amount,0)
        self.assertAlmostEqual(book.reservoir.volume,400)
        view=book.outlook()
        self.assertAlmostEqual(view.available_operating,0)
        # 普通调度触底被阻止
        d=book.plan_dispatch(0.0001)
        self.assertFalse(d.allowed);self.assertEqual(d.reason,REASON_FLOOR)
        # 零量调度始终允许
        self.assertTrue(book.plan_dispatch(0).allowed)

    def test_partial_consume_then_boundary(self):
        book=ProtectionBook(Reservoir(900,0,0,400),self.t0)
        book.propose("em",500,self.t0+timedelta(hours=2));book.confirm("em")
        book.consume("em",300)
        self.assertAlmostEqual(book.reservoir.volume,600)
        self.assertEqual(book.get("em").state,CONFIRMED)
        # 剩余 200 全部取走 -> 400 贴边
        book.consume("em",200)
        self.assertAlmostEqual(book.reservoir.volume,400)
        # 再想动用其它紧急预留已不可能：会击穿死库容
        book.propose("em2",10,self.t0+timedelta(hours=1))
        with self.assertRaises(ReservationError) as cm:book.confirm("em2")
        self.assertEqual(cm.exception.reason,REASON_FLOOR)

    # ---- 生命周期与水龄 ----
    def test_lifecycle_and_age(self):
        book=ProtectionBook(Reservoir(1000,100,100,400,age_hours=12),self.t0)
        book.propose("x",100,self.t0+timedelta(hours=6));book.confirm("x")
        book.tick(self.t0+timedelta(hours=2))
        # 恒定水位、有换水：水龄应下降
        self.assertLess(book.outlook().age_hours,12)
        book.tick(self.t0+timedelta(hours=6))
        self.assertEqual(book.get("x").state,EXPIRED)
        with self.assertRaises(ReservationError) as cm:book.consume("x")
        self.assertEqual(cm.exception.reason,REASON_EXPIRED)
        # 过期预留不再占用运行容积
        self.assertAlmostEqual(book.outlook().available_operating,600)

    def test_consume_cannot_starve_later_reservation(self):
        # 水位恒定：两个各 300 的预留恰好占满净运行容积 600，取走短预留后长预留仍有保障
        book=ProtectionBook(Reservoir(1000,100,100,400),self.t0)
        book.propose("fire",300,self.t0+timedelta(hours=1));book.confirm("fire")
        book.propose("supply",300,self.t0+timedelta(hours=10));book.confirm("supply")
        book.consume("fire",300)
        self.assertAlmostEqual(book.reservoir.volume,700)
        self.assertEqual(book.get("fire").state,CONSUMED)
        # 无入流 100/h：短预留 100（1h）+ 长预留 300（2h），确认时恰好贴 2h 边界
        book2=ProtectionBook(Reservoir(1000,0,100,400),self.t0)
        book2.propose("fire",100,self.t0+timedelta(hours=1));book2.confirm("fire")
        book2.propose("supply",300,self.t0+timedelta(hours=2));book2.confirm("supply")
        # 1h 点仅剩 100 余量：叠加 101 当前够（200）但未来会饿穿 -> HORIZON
        book2.propose("x",101,self.t0+timedelta(hours=1))
        with self.assertRaises(ReservationError) as cm:book2.confirm("x")
        self.assertEqual(cm.exception.reason,REASON_HORIZON)
        # 100 恰好为边界，可确认
        book2.get("x").amount=100
        book2.confirm("x")
        # 释放长预留后，fire/x 均可消耗
        book2.release("supply")
        book2.consume("fire",100);book2.consume("x",100)
        self.assertAlmostEqual(book2.reservoir.volume,800)
        self.assertAlmostEqual(book2.outlook().available_operating,400)

    def test_invalid_transitions(self):
        book=ProtectionBook(Reservoir(1000,0,0,400),self.t0)
        book.propose("x",100,self.t0+timedelta(hours=1))
        with self.assertRaises(ReservationError):book.consume("x")      # 未确认
        book.confirm("x");book.consume("x")
        with self.assertRaises(ReservationError):book.release("x")     # 已消耗终结

    def test_bad_reservoir(self):
        with self.assertRaises(ValueError):Reservoir(100,0,0,400)      # 死库容大于当前水量
        with self.assertRaises(ValueError):Reservoir(100,-1,0,0)

if __name__=="__main__":unittest.main()
