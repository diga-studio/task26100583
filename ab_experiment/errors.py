"""库异常定义。"""


class ExperimentError(Exception):
    """实验相关错误基类。"""


class ExperimentNotFoundError(ExperimentError):
    """实验不存在。"""


class InvalidExperimentConfigError(ExperimentError):
    """实验配置非法（变体数量、对照身份等）。"""


class InvalidEventError(ExperimentError):
    """事件内容非法或与已收到事件冲突。"""
