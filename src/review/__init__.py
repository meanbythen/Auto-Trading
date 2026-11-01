# 收盘复盘：成绩单 + 信号质量 + 缺陷诊断 + 近几日/回测基准 + 盘面/策略对照
# 功能：给「复盘」页和测试一个入口，真正的步骤在 service.build_review。
from .service import build_review

__all__ = ["build_review"]
