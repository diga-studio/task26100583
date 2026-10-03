"""判胜结算库的自动化测试。"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

import ab_experiment as ab
from ab_experiment.stats import SIGNIFICANCE_CHI2_THRESHOLD, pearson_chi2_2x2


@pytest.fixture()
def conn(tmp_path):
    connection = ab.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def scenario_one_events() -> list[ab.Event]:
    """变体 a 在观察点 3000 达标的场景，最大序号 3120。"""
    events: list[ab.Event] = []
    for i in range(1, 1101):
        events.append(ab.ImpressionEvent(seq=i, variant="ctrl"))
    for i in range(1101, 2201):
        events.append(ab.ImpressionEvent(seq=i, variant="a"))
    for k, i in enumerate(range(2201, 2321)):  # ctrl 120 个同意
        events.append(ab.ActionEvent(seq=i, impression_seq=1 + k, action_type="consent"))
    for k, i in enumerate(range(2321, 2521)):  # a 200 个同意
        events.append(ab.ActionEvent(seq=i, impression_seq=1101 + k, action_type="consent"))
    for k, i in enumerate(range(2521, 2621)):  # ctrl 拒绝
        events.append(ab.ActionEvent(seq=i, impression_seq=121 + k, action_type="reject"))
    for k, i in enumerate(range(2621, 2721)):  # a 关闭
        events.append(ab.ActionEvent(seq=i, impression_seq=1301 + k, action_type="close"))
    for k, i in enumerate(range(2721, 2921)):  # ctrl 关闭
        events.append(ab.ActionEvent(seq=i, impression_seq=221 + k, action_type="close"))
    for k, i in enumerate(range(2921, 3121)):  # a 拒绝
        events.append(ab.ActionEvent(seq=i, impression_seq=1401 + k, action_type="reject"))
    return events


def scenario_two_events() -> list[ab.Event]:
    """a 在 3000 点先达标，b 在 4000 点提升更大的场景，最大序号 4000。"""
    events: list[ab.Event] = []
    for i in range(1, 1051):
        events.append(ab.ImpressionEvent(seq=i, variant="ctrl"))
    for i in range(1051, 2101):
        events.append(ab.ImpressionEvent(seq=i, variant="a"))
    for i in range(2101, 2151):
        events.append(ab.ImpressionEvent(seq=i, variant="b"))
    for k, i in enumerate(range(2151, 2251)):  # ctrl 100 个同意
        events.append(ab.ActionEvent(seq=i, impression_seq=1 + k, action_type="consent"))
    for k, i in enumerate(range(2251, 2401)):  # a 150 个同意
        events.append(ab.ActionEvent(seq=i, impression_seq=1051 + k, action_type="consent"))
    for i in range(2401, 3451):  # b 补齐到 1100 个展示
        events.append(ab.ImpressionEvent(seq=i, variant="b"))
    for k, i in enumerate(range(3451, 3651)):  # b 200 个同意（提升更大）
        events.append(ab.ActionEvent(seq=i, impression_seq=2101 + k, action_type="consent"))
    for k, i in enumerate(range(3651, 4001)):  # ctrl 拒绝（填充到 4000）
        events.append(ab.ActionEvent(seq=i, impression_seq=101 + k, action_type="reject"))
    return events


def stats_map(result: ab.ObservationResult) -> dict[str, ab.VariantStats]:
    return {s.variant: s for s in result.stats}


class TestScenarioOne:
    def test_gap_fill_settles_multiple_points_with_duplicates(self, conn):
        eid = ab.create_experiment(conn, ["ctrl", "a"], "ctrl")
        events = scenario_one_events()
        held_back = next(e for e in events if e.seq == 1500)
        rest = [e for e in events if e.seq != 1500]
        # 动作先于展示到达（倒序投递），并夹带重复投递
        first_batch = list(reversed(rest)) + rest[::101]
        # 缺口 1500 之前连续到 1499：观察点 1000 可结算，2000/3000 不能越过缺口
        first_results = ab.receive_events(conn, eid, first_batch)
        assert [r.point for r in first_results] == [1000]
        status = ab.get_status(conn, eid)
        assert status.settled_points == (1000,)

        # 补齐缺口后连续产生 2000、3000 两个观察点
        results = ab.receive_events(conn, eid, [held_back])
        assert [r.point for r in results] == [2000, 3000]

        point1000 = stats_map(ab.get_observation(conn, eid, 1000))
        assert point1000["ctrl"].impressions == 1000
        assert point1000["a"].impressions == 0
        assert first_results[0].winning_variant is None

        point2000 = stats_map(results[0])
        assert point2000["ctrl"].impressions == 1100
        assert point2000["a"].impressions == 900
        assert results[0].winning_variant is None

        point3000 = stats_map(results[1])
        assert point3000["ctrl"].impressions == 1100
        assert point3000["ctrl"].consents == 120
        assert point3000["a"].impressions == 1100
        assert point3000["a"].consents == 200
        assert results[1].qualified_variants == ("a",)
        assert results[1].winning_variant == "a"

        status = ab.get_status(conn, eid)
        assert status.terminated
        assert status.winning_variant == "a"
        assert status.winning_point == 3000
        assert status.settled_points == (1000, 2000, 3000)

        # 重复投递整批事件不改变任何已发布统计
        assert ab.receive_events(conn, eid, events) == []
        again = ab.get_observation(conn, eid, 3000)
        assert stats_map(again)["a"].consents == 200
        assert again.winning_variant == "a"

    def test_delivery_order_and_batch_size_do_not_change_results(self, conn):
        events = scenario_one_events()

        def run(delivery: list[list[ab.Event]]) -> tuple:
            eid = ab.create_experiment(conn, ["ctrl", "a"], "ctrl")
            for batch in delivery:
                ab.receive_events(conn, eid, batch)
            status = ab.get_status(conn, eid)
            observations = tuple(
                ab.get_observation(conn, eid, point)
                for point in status.settled_points
            )
            return replace(status, experiment_id=0), observations

        sequential = [events[i : i + 500] for i in range(0, len(events), 500)]

        shuffled = events[:]
        random.Random(42).shuffle(shuffled)
        messy = []
        for i in range(0, len(shuffled), 737):
            batch = shuffled[i : i + 737]
            messy.append(batch + batch[:50])  # 每批夹带重复

        status_a, observations_a = run(sequential)
        status_b, observations_b = run(messy)
        assert status_a == status_b
        assert observations_a == observations_b


class TestScenarioTwo:
    def test_earlier_winner_is_frozen(self, conn):
        eid = ab.create_experiment(conn, ["ctrl", "a", "b"], "ctrl")
        events = scenario_two_events()
        first = [e for e in events if e.seq <= 3000]
        rest = [e for e in events if e.seq > 3000]

        results = ab.receive_events(conn, eid, first)
        assert [r.point for r in results] == [1000, 2000, 3000]
        final = results[-1]
        assert final.winning_variant == "a"
        final_stats = stats_map(final)
        assert final_stats["a"].consents == 150
        assert final_stats["b"].impressions == 650  # 序号 <=3000 的 b 展示

        #  sanity check：b 在 4000 点的提升确实更大且显著
        chi2_b = pearson_chi2_2x2(200, 900, 100, 950)
        chi2_a = pearson_chi2_2x2(150, 900, 100, 950)
        assert chi2_b > chi2_a >= SIGNIFICANCE_CHI2_THRESHOLD

        # 后续事件（含 b 的大幅提升）不改变冻结结果
        assert ab.receive_events(conn, eid, rest) == []
        status = ab.get_status(conn, eid)
        assert status.terminated
        assert status.winning_variant == "a"
        assert status.winning_point == 3000
        assert status.settled_points == (1000, 2000, 3000)
        assert ab.get_observation(conn, eid, 4000) is None
        frozen = stats_map(ab.get_observation(conn, eid, 3000))
        assert frozen["b"].impressions == 650  # 未混入后续事件


class TestSignificanceRules:
    def test_higher_rate_without_significance_does_not_win(self, conn):
        eid = ab.create_experiment(conn, ["ctrl", "a"], "ctrl")
        events: list[ab.Event] = []
        for i in range(1, 1051):
            events.append(ab.ImpressionEvent(seq=i, variant="ctrl"))
        for i in range(1051, 2101):
            events.append(ab.ImpressionEvent(seq=i, variant="a"))
        for k, i in enumerate(range(2101, 2221)):  # ctrl 120 同意
            events.append(ab.ActionEvent(seq=i, impression_seq=1 + k, action_type="consent"))
        for k, i in enumerate(range(2221, 2342)):  # a 121 同意（略高但不显著）
            events.append(ab.ActionEvent(seq=i, impression_seq=1051 + k, action_type="consent"))
        for k, i in enumerate(range(2342, 3001)):  # 填充到 3000
            events.append(ab.ActionEvent(seq=i, impression_seq=121 + k, action_type="reject"))
        results = ab.receive_events(conn, eid, events)
        final = results[-1]
        assert final.point == 3000
        assert final.qualified_variants == ()
        assert final.winning_variant is None
        assert not ab.get_status(conn, eid).terminated

    def test_impressions_must_exceed_1000(self, conn):
        eid = ab.create_experiment(conn, ["ctrl", "a"], "ctrl")
        events: list[ab.Event] = []
        for i in range(1, 1001):  # ctrl 恰好 1000 个展示
            events.append(ab.ImpressionEvent(seq=i, variant="ctrl"))
        for i in range(1001, 2001):  # a 恰好 1000 个展示
            events.append(ab.ImpressionEvent(seq=i, variant="a"))
        for k, i in enumerate(range(2001, 2101)):
            events.append(ab.ActionEvent(seq=i, impression_seq=1 + k, action_type="consent"))
        for k, i in enumerate(range(2101, 2301)):
            events.append(ab.ActionEvent(seq=i, impression_seq=1001 + k, action_type="consent"))
        for k, i in enumerate(range(2301, 3001)):
            events.append(ab.ActionEvent(seq=i, impression_seq=101 + k, action_type="reject"))
        results = ab.receive_events(conn, eid, events)
        assert results[-1].qualified_variants == ()


class TestBasics:
    def test_no_complete_observation_point_returns_unsettled(self, conn):
        eid = ab.create_experiment(conn, ["ctrl", "a"], "ctrl")
        ab.receive_events(
            conn, eid, [ab.ImpressionEvent(seq=i, variant="ctrl") for i in range(1, 1000)]
        )
        status = ab.get_status(conn, eid)
        assert status.settled_points == ()
        assert not status.terminated
        assert status.winning_variant is None
        assert ab.get_observation(conn, eid, 1000) is None

    def test_experiment_config_validation(self, conn):
        with pytest.raises(ab.InvalidExperimentConfigError):
            ab.create_experiment(conn, ["ctrl"], "ctrl")
        with pytest.raises(ab.InvalidExperimentConfigError):
            ab.create_experiment(conn, ["a", "b", "c", "d", "e"], "a")
        with pytest.raises(ab.InvalidExperimentConfigError):
            ab.create_experiment(conn, ["a", "a"], "a")
        with pytest.raises(ab.InvalidExperimentConfigError):
            ab.create_experiment(conn, ["a", "b"], "c")

    def test_unknown_experiment(self, conn):
        with pytest.raises(ab.ExperimentNotFoundError):
            ab.get_status(conn, 999)

    def test_invalid_events(self, conn):
        eid = ab.create_experiment(conn, ["ctrl", "a"], "ctrl")
        with pytest.raises(ab.InvalidEventError):
            ab.receive_events(conn, eid, [ab.ImpressionEvent(seq=1, variant="nope")])
        with pytest.raises(ab.InvalidEventError):
            ab.receive_events(conn, eid, [ab.ActionEvent(seq=5, impression_seq=5, action_type="consent")])

    def test_chi2_known_value_and_zero_guards(self):
        value = pearson_chi2_2x2(40, 60, 30, 70)
        assert value == pytest.approx(2.197802197802198)
        assert pearson_chi2_2x2(0, 100, 0, 100) is None
        assert pearson_chi2_2x2(100, 0, 100, 0) is None
