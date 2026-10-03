# 同意横幅 A/B 实验判胜结算库

Python + SQLite 实现，由上层直接调用。事件与结算结果均持久化在 SQLite 中，
支持事件重复、乱序、动作先于展示到达；同一批业务数据的结算结果不随
投递顺序或批次大小变化。

## 用法

```python
from absettle import BannerExperimentStore, Event, ActionType

store = BannerExperimentStore("experiments.db")   # 或 ":memory:"

# 1. 创建实验：2~4 个变体，其一为对照；创建后不可变
store.create_experiment("exp-1", ["control", "a", "b"], control="control")

# 2. 接收事件（seq 从 1 开始连续递增；可乱序、可重复）
store.receive_events("exp-1", [
    Event.impression(seq=1, variant="a"),
    Event.action_event(seq=2, impression_seq=1, action=ActionType.CONSENT),
    # ...
])

# 3. 查询结算结果
current = store.get_result("exp-1")                 # 当前状态
point = store.get_result("exp-1", observation_point=1000)  # 指定观察点
print(current.status, current.winner, current.stats)
```

## 判胜规则

- 每 1000 个序号一个观察点（1000、2000、3000…），此前序号全部收到才结算；
  缺口不越过，补齐后逐点补结算。
- 展示数为分母，同意数为成功数，其余展示（含未收到动作的）均为失败；
  动作只影响其序号所在的观察点及之后。
- 非对照变体与对照的展示数均 > 1000 才比较；同意率须严格更高，且
  2x2 Pearson 卡方（无连续性校正）>= 3.841458820694124（95% 显著性）。
- 最早出现合格变体的观察点定胜；同点多个合格变体取相对对照的绝对
  提升最大者（未舍入比例），并列取创建顺序靠前者。此后统计冻结。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
