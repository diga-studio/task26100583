import random
import tempfile
import unittest
from pathlib import Path

from absettle import (
    ActionType,
    BannerExperimentStore,
    Event,
    ExperimentStatus,
)
from absettle.core import CHI2_THRESHOLD_95, chi2_2x2


def make_block(seq_start, variant, impressions, consent_flags, actions_first=False):
    """生成一个变体的一段事件：先全部展示再全部动作（或交替）。"""
    events = []
    seq = seq_start
    imp_seqs = []
    for _ in range(impressions):
        events.append(Event.impression(seq, variant))
        imp_seqs.append(seq)
        seq += 1
    for imp_seq, consent in zip(imp_seqs, consent_flags):
        act = ActionType.CONSENT if consent else ActionType.REJECT
        events.append(Event.action_event(seq, imp_seq, act))
        seq += 1
    return events, seq


def flags(total, consents):
    return [True] * consents + [False] * (total - consents)


def flags_consents_last(total, consents):
    return [False] * (total - consents) + [True] * consents


def build_freeze_dataset():
    """control/a/b 三变体：a 在 5000 点达标，b 的动作全部排在 5000 之后。

    布局：control 1..2200，a 2201..4400（同意动作集中在 4258..4400，
    使 4000 点尚不达标），b 4401..6000（动作 5201 起，5000 点无动作）。
    """
    events = []
    seq = 1
    block, seq = make_block(seq, "control", 1100, flags(1100, 110))
    events += block
    block, seq = make_block(seq, "a", 1100, flags_consents_last(1100, 143))
    events += block
    block, seq = make_block(seq, "b", 800, flags(800, 200))
    events += block
    assert seq == 6001
    return events


class Chi2Test(unittest.TestCase):
    def test_known_value(self):
        chi2 = chi2_2x2(130, 870, 100, 900)
        self.assertAlmostEqual(chi2, 4.421518054532056)

    def test_zero_totals_not_significant(self):
        self.assertIsNone(chi2_2x2(0, 1000, 0, 1000))
        self.assertIsNone(chi2_2x2(1000, 0, 1000, 0))


class GapAndDuplicateTest(unittest.TestCase):
    def setUp(self):
        self.store = BannerExperimentStore()
        self.addCleanup(self.store.close)
        self.store.create_experiment("exp", ["control", "a", "b"], "control")

    def test_gap_blocks_then_fills_multiple_points(self):
        events = build_freeze_dataset()
        by_seq = {e.seq: e for e in events}

        # 动作先于展示到达 + 缺口：先投 4401..6000（b 块），再投 1..4400 但扣掉 1000
        first = [by_seq[s] for s in range(4401, 6001)]
        second = [by_seq[s] for s in list(range(1, 1000)) + list(range(1001, 4401))]
        self.assertEqual(self.store.receive_events("exp", first), [])
        self.assertEqual(self.store.receive_events("exp", second), [])
        current = self.store.get_result("exp")
        self.assertIs(current.status, ExperimentStatus.COLLECTING)

        # 重复投递已有事件，不产生任何变化
        self.assertEqual(self.store.receive_events("exp", first[:50]), [])
        self.assertIs(self.store.get_result("exp").status, ExperimentStatus.COLLECTING)

        # 补上缺口 seq=1000：一次性结算 1000~5000 五个观察点
        results = self.store.receive_events("exp", [by_seq[1000]])
        self.assertEqual([r.point for r in results], [1000, 2000, 3000, 4000, 5000])
        self.assertEqual(results[-1].status, ExperimentStatus.CONCLUDED)
        self.assertEqual(results[-1].winner, "a")

    def test_delivery_order_and_batch_size_invariant(self):
        events = build_freeze_dataset()

        def settle_all(delivery):
            store = BannerExperimentStore()
            self.addCleanup(store.close)
            store.create_experiment("exp", ["control", "a", "b"], "control")
            for batch in delivery:
                store.receive_events("exp", batch)
            points = [
                store.get_result("exp", p)
                for p in (1000, 2000, 3000, 4000, 5000)
            ]
            return points, store.get_result("exp")

        baseline_points, baseline_current = settle_all([events])

        # 逆序小批次 + 重复投递 + 动作先于展示
        rng = random.Random(42)
        shuffled = events[:]
        rng.shuffle(shuffled)
        batches = [shuffled[i:i + 37] for i in range(0, len(shuffled), 37)]
        batches.insert(3, events[100:120])  # 重复投递
        batches.insert(7, events[4400:4402])
        variant_points, variant_current = settle_all(batches)

        self.assertEqual(baseline_points, variant_points)
        self.assertEqual(baseline_current, variant_current)
        self.assertEqual(baseline_current.winner, "a")
        self.assertEqual(baseline_current.point, 5000)


class FreezeTest(unittest.TestCase):
    def test_earlier_winner_frozen_despite_later_bigger_lift(self):
        store = BannerExperimentStore()
        self.addCleanup(store.close)
        store.create_experiment("exp", ["control", "a", "b"], "control")
        results = store.receive_events("exp", build_freeze_dataset())
        concluded = results[-1]
        self.assertEqual((concluded.point, concluded.winner), (5000, "a"))

        stats = {s.variant: s for s in concluded.stats}
        self.assertEqual(stats["b"].impressions, 600)  # b 在 5000 点未达标
        self.assertEqual(stats["b"].consents, 0)

        # 冻结后到达的事件（含 b 的大幅提升已在内）不再产生新结算
        extra, _ = make_block(6001, "b", 600, flags(600, 300))
        self.assertEqual(store.receive_events("exp", extra), [])
        with self.assertRaises(KeyError):
            store.get_result("exp", 6000)

        current = store.get_result("exp")
        self.assertEqual((current.point, current.winner), (5000, "a"))
        self.assertIs(current.status, ExperimentStatus.CONCLUDED)
        self.assertEqual(current.stats, concluded.stats)


class RuleTest(unittest.TestCase):
    def test_impressions_must_exceed_1000(self):
        store = BannerExperimentStore()
        self.addCleanup(store.close)
        store.create_experiment("exp", ["control", "a"], "control")
        # 双方各 1000 展示（不超过 1000），即使差异巨大也不判胜
        events, seq = make_block(1, "control", 1000, flags(1000, 100))
        block, seq = make_block(seq, "a", 1000, flags(1000, 200))
        events += block
        results = store.receive_events("exp", events)
        self.assertTrue(results)
        self.assertTrue(all(r.winner is None for r in results))

    def test_strictly_higher_rate_required(self):
        store = BannerExperimentStore()
        self.addCleanup(store.close)
        store.create_experiment("exp", ["control", "a"], "control")
        events, seq = make_block(1, "control", 1100, flags(1100, 143))
        block, seq = make_block(seq, "a", 1100, flags(1100, 143))
        events += block
        results = store.receive_events("exp", events)
        self.assertTrue(all(r.winner is None for r in results))

    def test_unsettled_point_query_raises(self):
        store = BannerExperimentStore()
        self.addCleanup(store.close)
        store.create_experiment("exp", ["control", "a"], "control")
        with self.assertRaises(KeyError):
            store.get_result("exp", 1000)

    def test_threshold_boundary(self):
        # 卡方恰好略低于阈值时不显著
        chi2 = chi2_2x2(143, 957, 110, 990)
        self.assertGreaterEqual(chi2, CHI2_THRESHOLD_95)
        chi2_low = chi2_2x2(135, 965, 110, 990)
        self.assertLess(chi2_low, CHI2_THRESHOLD_95)


class TieBreakTest(unittest.TestCase):
    @staticmethod
    def build_simultaneous_dataset(non_control):
        """control 1100 展示 110 同意；各非对照变体 1100 展示，
        动作交错排列，使所有变体首次在同一观察点同时具备判定条件。
        non_control: [(variant, consents), ...]
        """
        variants = ["control"] + [v for v, _ in non_control]
        events = []
        imp_base = {}
        seq = 1
        for v in variants:
            imp_base[v] = seq
            for k in range(1100):
                events.append(Event.impression(seq + k, v))
            seq += 1100
        # 对照动作：同意在前，落在较早观察窗
        for k in range(1100):
            act = ActionType.CONSENT if k < 110 else ActionType.REJECT
            events.append(Event.action_event(seq, imp_base["control"] + k, act))
            seq += 1
        # 非对照动作交错：同意集中在同一观察窗内
        counts = {v: c for v, c in non_control}
        for k in range(1100):
            for v, _ in non_control:
                act = (
                    ActionType.CONSENT if k < counts[v] else ActionType.REJECT
                )
                events.append(Event.action_event(seq, imp_base[v] + k, act))
                seq += 1
        return events

    def test_same_point_picks_largest_lift_then_creation_order(self):
        store = BannerExperimentStore()
        self.addCleanup(store.close)
        store.create_experiment(
            "exp", ["control", "a", "b", "c"], "control"
        )
        # a、b 提升相同（并列时取创建顺序靠前者 a），c 提升较小
        events = self.build_simultaneous_dataset(
            [("a", 149), ("b", 149), ("c", 132)]
        )
        results = store.receive_events("exp", events)
        concluded = [r for r in results if r.status is ExperimentStatus.CONCLUDED]
        self.assertEqual(len(concluded), 1)
        self.assertEqual(concluded[0].winner, "a")
        stats = {s.variant: s for s in concluded[0].stats}
        self.assertEqual(stats["a"].consents, 149)
        self.assertEqual(stats["b"].consents, 149)

    def test_larger_lift_wins_over_creation_order(self):
        store = BannerExperimentStore()
        self.addCleanup(store.close)
        store.create_experiment("exp", ["control", "a", "b"], "control")
        events = self.build_simultaneous_dataset([("a", 140), ("b", 150)])
        results = store.receive_events("exp", events)
        concluded = [r for r in results if r.status is ExperimentStatus.CONCLUDED]
        self.assertEqual(len(concluded), 1)
        self.assertEqual(concluded[0].winner, "b")


class PersistenceTest(unittest.TestCase):
    def test_events_and_settlements_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ab.db")
            store = BannerExperimentStore(db)
            store.create_experiment("exp", ["control", "a", "b"], "control")
            events = build_freeze_dataset()
            store.receive_events("exp", events[:3000])
            store.close()

            reopened = BannerExperimentStore(db)
            self.addCleanup(reopened.close)
            self.assertEqual(
                reopened.get_result("exp", 1000).point, 1000
            )
            results = reopened.receive_events("exp", events[3000:])
            self.assertEqual(results[-1].winner, "a")
            self.assertEqual(results[-1].point, 5000)


class CreateExperimentTest(unittest.TestCase):
    def setUp(self):
        self.store = BannerExperimentStore()
        self.addCleanup(self.store.close)

    def test_invalid_definitions_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_experiment("e1", ["only"], "only")
        with self.assertRaises(ValueError):
            self.store.create_experiment(
                "e2", ["a", "b", "c", "d", "e"], "a"
            )
        with self.assertRaises(ValueError):
            self.store.create_experiment("e3", ["a", "a"], "a")
        with self.assertRaises(ValueError):
            self.store.create_experiment("e4", ["a", "b"], "c")

    def test_duplicate_experiment_rejected(self):
        self.store.create_experiment("exp", ["control", "a"], "control")
        with self.assertRaises(Exception):
            self.store.create_experiment("exp", ["control", "b"], "control")

    def test_unknown_experiment_rejected(self):
        with self.assertRaises(KeyError):
            self.store.receive_events("nope", [])
        with self.assertRaises(KeyError):
            self.store.get_result("nope")


if __name__ == "__main__":
    unittest.main()
