"""统计检验：2x2 Pearson 卡方。"""

SIGNIFICANCE_CHI2_THRESHOLD = 3.841458820694124


def pearson_chi2_2x2(
    success_a: int, failure_a: int, success_b: int, failure_b: int
) -> float | None:
    """计算 2x2 Pearson 卡方统计量（不做连续性校正）。

    A 为处理组，B 为对照组。双方成功总数或失败总数为 0 时返回 None。
    """
    total_success = success_a + success_b
    total_failure = failure_a + failure_b
    if total_success == 0 or total_failure == 0:
        return None
    n_a = success_a + failure_a
    n_b = success_b + failure_b
    n = n_a + n_b
    numerator = n * (success_a * failure_b - failure_a * success_b) ** 2
    denominator = n_a * n_b * total_success * total_failure
    return numerator / denominator
