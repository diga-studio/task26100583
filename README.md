# task26100583

同意横幅 A/B 实验判胜结算库（Python + SQLite），由上层直接调用。

## 能力

- `ab_experiment.connect(path)`：打开/创建 SQLite 数据库。
- `ab_experiment.create_experiment(conn, variants, control)`：创建实验（2-4 个变体，其一为对照；创建后不可变），返回实验 id。
- `ab_experiment.receive_events(conn, experiment_id, events)`：接收一批事件
  （`ImpressionEvent(seq, variant)` / `ActionEvent(seq, impression_seq, action_type)`），
  返回本次新结算的观察点结果列表。重复投递幂等，乱序到达不影响结果。
- `ab_experiment.get_status(conn, experiment_id)`：查询当前状态（已结算观察点、是否终止、胜者）。
- `ab_experiment.get_observation(conn, experiment_id, point)`：查询某个已结算观察点的统计与判胜结果；未结算返回 `None`。

## 规则摘要

- 每 1000 个序号一个观察点（1000、2000、3000……），仅当此前全部序号收到才结算，缺口不可越过。
- 展示数为分母，同意数为成功数，其余展示（含未收到动作的）均为失败；动作只影响其序号所在的观察点及之后。
- 非对照变体与对照展示数均超过 1000 才比较；同意率严格更高且 2x2 Pearson 卡方 >= 3.841458820694124 判为 95% 显著。
- 最早出现合格变体的观察点定胜者；同点多个合格取相对对照绝对提升最大者（未舍入比较），再平则取创建顺序靠前者；此后统计冻结。

## 测试

```sh
python3 -m pytest tests/ -q
```
